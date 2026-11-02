#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dxf_tally.py —— 从 DXF 直接统计设备/点位（不需要 AutoCAD）

用途：本机没装 AutoCAD（拿不到 accoreconsole）时的替代路线。
DWG 先用转换器转成 DXF，再喂给本脚本。

    典型用法
    --------
    python dxf_tally.py 图纸.dxf                    # 概览：实体 + 块排名 + 文字排名
    python dxf_tally.py 图纸.dxf --find 交换机       # 只看含关键词的标注
    python dxf_tally.py 图纸.dxf --csv 文字明细.csv  # 导出带坐标的文字明细（做图名/行带定位）
    python dxf_tally.py 图纸.dxf --layer E-弱电     # 只统计指定图层
    python dxf_tally.py 图纸.dwg                    # 报错并给出转换指引

为什么不做 DWG：DWG 是闭源二进制格式，ezdxf 只读 DXF。
DWG→DXF 必须靠外部转换器（ODA File Converter / LibreDWG / AutoCAD 自己），
本脚本不做转换，也**不假装能转**。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict

try:
    import ezdxf
except ImportError:
    sys.exit("缺少 ezdxf。装：pip install ezdxf")

# 设备标注里的倍数写法：名*N / N*名 / 名×N / 含N个名
# sep 要单独捕获：`*` `×` 是明确的乘号，小写 `x` 会和尺寸写法（600x600 地砖）冲突
RE_MUL_AFTER = re.compile(r"^(?P<name>.*?)\s*(?P<sep>[*×xX])\s*(?P<n>\d{1,3})\s*$")
RE_MUL_BEFORE = re.compile(r"^\s*(?P<n>\d{1,3})\s*(?P<sep>[*×xX])\s*(?P<name>.+?)\s*$")
RE_MUL_HAN = re.compile(r"^含\s*(?P<n>\d{1,3})\s*个\s*(?P<name>.+?)\s*$")

# 这些"倍数"其实是线缆/户内线，不是设备台数（见 SKILL.md 统计口径）
CABLE_HINT = re.compile(
    r"cat\s*\.?\s*[56]|utp|ftp|rvv|rvvp|bvr|byj|yjv|rvs|光纤|网线|电缆|电源线|桥架|线槽|pvc|sc\d|jdg",
    re.IGNORECASE,
)


def _die(msg: str, code: int = 2):
    sys.stderr.write(msg.rstrip() + "\n")
    sys.exit(code)


def open_dxf(path: str):
    """打开 DXF；DWG 直接给转换指引后退出（不猜、不静默）。"""
    if not os.path.exists(path):
        _die("文件不存在：%s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dwg":
        _die(
            "这是 DWG（闭源二进制），ezdxf 读不了，必须先转 DXF。任选一种：\n"
            "\n"
            "  ① ODA File Converter（免费，保真度最好，推荐）\n"
            "     https://www.opendesign.com/guestfiles/oda_file_converter\n"
            "     装完命令行批量转（注意三个目录参数都要给）：\n"
            '       ODAFileConverter "D:/图纸" "D:/图纸/out" ACAD2018 DXF 0 1 "*.dwg"\n'
            "\n"
            "  ② 有 AutoCAD 的机器上直接 DXFOUT（等价于路线 A 的机器顺手转一下）\n"
            '     accoreconsole /i "图纸.dwg" /s dxfout.scr\n'
            "     dxfout.scr 内容（两行）：\n"
            '       (setvar "FILEDIA" 0)\n'
            '       (command "_.DXFOUT" "D:/图纸/out.dxf" "16")\n'
            "\n"
            "  ③ LibreDWG 的 dwg2dxf（开源 GPL，冷门块/动态块偶有丢失，最后再选）\n"
            "     dwg2dxf -o 图纸.dxf 图纸.dwg\n"
            "\n"
            "转完把 .dxf 喂给本脚本；转换器保真度由转换器负责，脚本只负责读。",
            code=3,
        )
    if ext not in (".dxf", ""):
        _die("只支持 .dxf（当前：%s）。DWG 请先转换，见上。" % ext)
    try:
        return ezdxf.readfile(path)
    except Exception as e:  # noqa: BLE001
        _die("DXF 读取失败：%s: %s\n（文件损坏 / 版本过新 / 不是 ASCII 或二进制 DXF）"
             % (type(e).__name__, e))


def iter_spaces(doc, want: str):
    """按需遍历 模型空间 / 图纸空间。want ∈ model|paper|all"""
    if want in ("model", "all"):
        yield "model", doc.modelspace()
    if want in ("paper", "all"):
        for lay in doc.layouts:
            if lay.name.lower() == "model":
                continue
            yield lay.name, lay


def text_of(e):
    """取实体的可读文字。返回 (text, kind) 或 (None, None)。"""
    t = e.dxftype()
    try:
        if t == "TEXT":
            return e.dxf.text, "TEXT"
        if t == "MTEXT":
            # plain_text() 会去掉 \P 换行与格式码，比 dxf.text 干净
            try:
                return e.plain_text(), "MTEXT"
            except Exception:  # noqa: BLE001
                return e.dxf.text, "MTEXT"
        if t == "ATTRIB":
            return e.dxf.text, "ATTRIB"
    except Exception:  # noqa: BLE001
        return None, None
    return None, None


def table_texts(e):
    """ACAD_TABLE 里的单元格文字（设计院设备表常用）。尽力而为，失败返回空。"""
    out = []
    try:
        for v in e.virtual_entities():
            s, _ = text_of(v)
            if s:
                out.append(s)
    except Exception:  # noqa: BLE001
        pass
    return out


def collect(doc, spaces="model", layers=None):
    """收集文字与块参照。返回 (texts, inserts, ent_counter)"""
    texts = []      # dict(text, kind, layer, x, y, space)
    inserts = []    # dict(name, layer, x, y, space)
    ent = Counter()
    lay_filter = {s.strip().lower() for s in layers} if layers else None

    def ok(e):
        if not lay_filter:
            return True
        try:
            return (e.dxf.layer or "").lower() in lay_filter
        except Exception:  # noqa: BLE001
            return True

    for space_name, space in iter_spaces(doc, spaces):
        for e in space:
            ent[e.dxftype()] += 1
            if not ok(e):
                continue
            t = e.dxftype()
            if t == "INSERT":
                try:
                    p = e.dxf.insert
                    inserts.append(dict(name=e.dxf.name, layer=e.dxf.layer,
                                        x=round(p.x, 2), y=round(p.y, 2), space=space_name))
                    # 块属性里常写设备名/编号
                    for a in e.attribs:
                        if a.dxf.text.strip():
                            ap = a.dxf.insert
                            texts.append(dict(text=a.dxf.text, kind="ATTRIB", layer=a.dxf.layer,
                                              x=round(ap.x, 2), y=round(ap.y, 2), space=space_name))
                except Exception:  # noqa: BLE001
                    pass
                continue
            if t == "ACAD_TABLE":
                for s in table_texts(e):
                    texts.append(dict(text=s, kind="TABLE", layer=getattr(e.dxf, "layer", ""),
                                      x=None, y=None, space=space_name))
                continue
            s, kind = text_of(e)
            if s and s.strip():
                try:
                    p = e.dxf.insert if t != "MTEXT" else e.dxf.insert
                    x, y = round(p.x, 2), round(p.y, 2)
                except Exception:  # noqa: BLE001
                    x = y = None
                texts.append(dict(text=s, kind=kind, layer=getattr(e.dxf, "layer", ""),
                                  x=x, y=y, space=space_name))
    return texts, inserts, ent


def split_multiplier(text: str):
    """解析 '名*N' 这类倍数写法。返回 (name, n) 或 (None, 0)。
    线缆类（Cat6/RVV/PVC…）返回 None，避免把线缆对数当设备台数。
    尺寸类（600x600 / @300x300）返回 None，避免把小写 x 当乘号。"""
    s = " ".join(text.split())
    if not s or len(s) > 80:
        return None, 0
    for rx, swap in ((RE_MUL_AFTER, 0), (RE_MUL_BEFORE, 1), (RE_MUL_HAN, 1)):
        m = rx.match(s)
        if not m:
            continue
        n = int(m.group("n"))
        name = m.group("name").strip(" /·-")
        sep = m.groupdict().get("sep") or ""      # RE_MUL_HAN 没有 sep 组
        if not name or n <= 0:
            continue
        # 设备名里一定至少有一个字母或汉字；纯数字/符号的是尺寸或坐标（如 @300*150、600x600）
        if not re.search(r"[A-Za-z\u4e00-\u9fff]", name):
            continue
        # 小写/大写 x 作乘号时，两端只要有一端是数字，就当成尺寸（300x300、600X900）
        if sep in "xX" and (re.search(r"\d$", name) or re.match(r"\d", name)):
            continue
        if swap:  # 数字在前时，名字里若还带 *N 说明是 "2*某线缆*3" 之类，跳过
            if re.search(r"[*×]", name):
                continue
        if CABLE_HINT.search(name) or CABLE_HINT.search(s):
            return None, 0
        return name, n
    return None, 0


def build_report(texts, inserts, ent, top=25):
    text_hist = Counter(t["text"].strip() for t in texts)
    dev_sum = Counter()       # 倍数式标注求和
    for t in texts:
        name, n = split_multiplier(t["text"])
        if name:
            dev_sum[name] += n
    block_hist = Counter(i["name"] for i in inserts)
    layer_text = Counter(t["layer"] for t in texts)
    layer_block = Counter(i["layer"] for i in inserts)
    return dict(
        entity_types=dict(ent.most_common()),
        text_total=len(texts),
        block_total=len(inserts),
        text_hist=text_hist,
        dev_sum=dev_sum,
        block_hist=block_hist,
        layer_text=layer_text,
        layer_block=layer_block,
        space_text=Counter(t["space"] for t in texts),
        space_block=Counter(i["space"] for i in inserts),
    )


def print_human(rep, args):
    print("=" * 68)
    print("实体类型：", ", ".join("%s=%d" % (k, v) for k, v in
                              list(rep["entity_types"].items())[:10]))
    print("可读文字 %d 条 / 块参照 %d 个" % (rep["text_total"], rep["block_total"]))
    sp = []
    for k, v in rep["space_text"].most_common(4):
        sp.append("%s: 文字%d/块%d" % (k, v, rep["space_block"].get(k, 0)))
    if sp:
        print("空间分布：" + "   ".join(sp))
        if args.space != "all" and (rep["space_text"].get("paper") or any(
                k not in ("model", "paper") and v for k, v in rep["space_block"].items())):
            print("  ↑ 布局里还有内容，单看模型空间会漏。去掉 --space model 重跑（默认 all）")
    print("=" * 68)

    if rep["dev_sum"]:
        print("\n【倍数式标注 → 设备台数】  名*N / N*名 / 含N个名")
        print("-" * 68)
        for name, n in rep["dev_sum"].most_common(args.top):
            print("  %-46s %4d 台" % (name[:46], n))
        print("  合计 %d 台（%d 种标注）" % (sum(rep["dev_sum"].values()), len(rep["dev_sum"])))
    else:
        print("\n【倍数式标注】未发现 名*N / N*名 写法"
              "（该图纸可能用逐台点名，见下面文字排行）")

    print("\n【块参照排行】(图块名 → 次数)  共 %d 个块参照" % rep["block_total"])
    print("-" * 68)
    for name, n in rep["block_hist"].most_common(args.top):
        print("  %-46s %4d" % (name[:46], n))

    print("\n【文字排行】(完全相同的文字归一类)")
    print("-" * 68)
    for s, n in rep["text_hist"].most_common(args.top):
        print("  %-46s %4d" % (s.replace("\n", " ")[:46], n))

    print("\n【图层分布】(文字条数 / 块参照数)")
    print("-" * 68)
    merged = Counter(rep["layer_text"])
    for k, v in rep["layer_text"].most_common(args.top):
        print("  %-30s 文字 %5d   块 %5d" % (k[:30], v, rep["layer_block"].get(k, 0)))
    extra = [(k, v) for k, v in rep["layer_block"].items() if k not in merged]
    for k, v in sorted(extra, key=lambda x: -x[1])[:5]:
        print("  %-30s 文字 %5d   块 %5d" % (k[:30], 0, v))


def main():
    ap = argparse.ArgumentParser(
        description="DXF 设备/点位统计（不需要 AutoCAD）。DWG 请先转 DXF。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dxf", help="DXF 文件路径（.dwg 会报错并给转换指引）")
    ap.add_argument("--layer", action="append", default=[],
                    help="只统计这些图层，可重复或逗号分隔")
    ap.add_argument("--space", default="all", choices=["model", "paper", "all"],
                    help="统计范围：all(默认，含模型+布局，与 accoreconsole 的 ssget \"_X\" 口径一致) / model / paper")
    ap.add_argument("--top", type=int, default=25, help="各排行显示条数，默认 25")
    ap.add_argument("--find", default="", help="只显示含该关键词的文字（不区分大小写）")
    ap.add_argument("--csv", default="", help="把文字明细（含坐标）导出为 CSV")
    ap.add_argument("--json", default="", help="把完整结果导出为 JSON")
    ap.add_argument("--encoding", action="store_true", help="只显示 DXF 版本与编码")
    args = ap.parse_args()

    layers = []
    for s in args.layer:
        layers += [x for x in re.split(r"[,，]", s) if x.strip()]

    doc = open_dxf(args.dxf)
    print("DXF 版本 %s   编码 %s   块定义 %d 个"
          % (doc.dxfversion, getattr(doc, "encoding", "?"), len(doc.blocks)))
    if args.encoding:
        return

    texts, inserts, ent = collect(doc, args.space, layers or None)

    warns = []
    if args.space == "model" and not texts:
        warns.append("模型空间没有文字。试试 --space all（图纸内容可能在布局里）。")
    if not texts and not inserts:
        warns.append("一个字都没读到。可能：① 文字被炸成曲线 ② 图层被 --layer 过滤掉了 "
                     "③ 内容在块定义里未被实例化。先去掉 --layer 再跑一次。")

    if args.find:
        kw = args.find.lower()
        hit = [t for t in texts if kw in t["text"].lower()]
        print("\n含「%s」的文字 %d 条：" % (args.find, len(hit)))
        for t in hit[:args.top]:
            print("  [%s] %-30s (%s, %s) %s"
                  % (t["kind"], (t["layer"] or "")[:30], t["x"], t["y"],
                     t["text"].replace("\n", " ")[:50]))
        texts = hit

    rep = build_report(texts, inserts, ent, args.top)
    print_human(rep, args)

    if args.csv:
        with io.open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["文字", "类型", "图层", "X", "Y", "空间", "倍数解析台数"])
            for t in texts:
                name, n = split_multiplier(t["text"])
                w.writerow([t["text"], t["kind"], t["layer"], t["x"], t["y"],
                            t["space"], ("%s=%d" % (name, n)) if name else ""])
        print("\n文字明细已导出：%s（%d 行）" % (args.csv, len(texts)))

    if args.json:
        payload = dict(
            file=os.path.abspath(args.dxf),
            dxfversion=doc.dxfversion,
            entity_types=rep["entity_types"],
            text_total=rep["text_total"],
            block_total=rep["block_total"],
            dev_sum=dict(rep["dev_sum"]),
            block_hist=dict(rep["block_hist"]),
            text_hist=dict(rep["text_hist"]),
            layer_text=dict(rep["layer_text"]),
            layer_block=dict(rep["layer_block"]),
            space_text=dict(rep["space_text"]),
            space_block=dict(rep["space_block"]),
            texts=texts,
            inserts=inserts,
        )
        with io.open(args.json, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2))
        print("完整结果已导出：%s" % args.json)

    for w in warns:
        sys.stderr.write("\n⚠ " + w + "\n")
    if warns:
        sys.exit(1)


if __name__ == "__main__":
    main()
