#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""周报清单登记表：每天把清单登记进来（顺带复制进"本周清单"），周末据此生成周报。

目录约定（根目录由 tpkit_config 的 weekly_report_root 指定）：
    <root>\\本周清单\\       清单文件副本（每天自动复制进来）
    <root>\\项目背景.md      登记表：按周分节，每行 = 日期 | 文件名 | 口头背景
    <root>\\周报草稿MMDD.md  生成的周报草稿

用法:
    # 登记（--note 为用户的口头背景说明，可省略）
    python week_log.py add --file "D:\\...\\0909视频监视系统-某项目清单-.xlsx" --note "某集团监控，数量与图纸对不上"
    python week_log.py add --file "a.xlsx" --file "b.xls" --note "同一项目两个版本"
    # 查看本周已登记（周末生成周报时用）
    python week_log.py list
    python week_log.py list --json

不修改用户的原始清单文件；只往"本周清单"目录复制副本、往登记表追加文字。
"""
import argparse
import datetime
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tpkit_config as cfg  # noqa: E402

# 周报根目录走配置（~/.workbuddy/tplink-toolkit.config.json 的 weekly_report_root）
DEFAULT_ROOT = cfg.path("weekly_report_root", must=False)
LIST_DIRNAME = "本周清单"
MANIFEST_NAME = "项目背景.md"
HEADER = f"""# 配单清单登记表

> 每天把当天收到的清单登记到本周小节里（文件名 + 口头背景说明），清单文件放在同目录的 `{LIST_DIRNAME}\\` 下。
> 每周日 20:00 自动按本周小节生成周报草稿；新的一周请在登记表里新起一节（脚本会自动创建）。

"""


def week_label(day=None):
    """返回本周（周一~周日）标签，如 2026-09-14 ~ 2026-09-20。"""
    day = day or datetime.date.today()
    mon = day - datetime.timedelta(days=day.weekday())
    sun = mon + datetime.timedelta(days=6)
    return f"{mon.isoformat()} ~ {sun.isoformat()}"


def paths(root):
    return (
        os.path.join(root, LIST_DIRNAME),
        os.path.join(root, MANIFEST_NAME),
    )


def load_manifest(manifest_path):
    if not os.path.exists(manifest_path):
        return HEADER
    with open(manifest_path, encoding="utf-8") as fh:
        return fh.read()


def section_span(text, label):
    """返回本周小节 (start, end)；start 指向 '## label' 行首，end 指向下一节行首或文末。"""
    marker = f"## {label}"
    start = text.find(marker)
    if start < 0:
        return None
    nxt = text.find("\n## ", start + len(marker))
    end = len(text) if nxt < 0 else nxt + 1
    return start, end


def parse_section(text, label):
    """解析某周小节，返回 [(日期, 文件名, 背景)]。"""
    span = section_span(text, label)
    if not span:
        return []
    rows = []
    for line in text[span[0]:span[1]].splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if cells[0] in ("登记日期", "日期") or set(cells[0]) <= set("-: "):
            continue
        rows.append((cells[0], cells[1], cells[2] if len(cells) > 2 else ""))
    return rows


def append_section(text, label):
    """在本周小节不存在时创建；返回新文本。"""
    if section_span(text, label):
        return text
    block = (
        f"\n## {label}\n\n"
        "| 登记日期 | 清单文件 | 项目背景（口头补充） |\n"
        "|---|---|---|\n"
    )
    return text.rstrip("\n") + "\n" + block


def add_rows(text, label, day_str, entries):
    """把 (文件名, 背景) 写入本周小节；同名文件已存在则更新背景。"""
    text = append_section(text, label)
    start, end = section_span(text, label)
    body = text[start:end]
    existing = {}
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("|") and not s.startswith("|---"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) >= 2 and cells[1] != "清单文件":
                existing[cells[1]] = s
    for fname, note in entries:
        row = f"| {day_str} | {fname} | {note} |"
        if fname in existing:
            if note and note != "（待补充）":
                body = body.replace(existing[fname], row)
                existing[fname] = row
        else:
            if not body.endswith("\n"):
                body += "\n"
            body += row + "\n"
            existing[fname] = row
    return text[:start] + body + text[end:]


def cmd_add(args):
    root = args.root or cfg.path("weekly_report_root")
    list_dir, manifest_path = paths(root)
    os.makedirs(list_dir, exist_ok=True)
    day = datetime.date.today()
    if args.date:
        mm, dd = args.date.split("-")
        day = datetime.date(day.year, int(mm), int(dd))
    day_str = day.strftime("%m-%d")
    label = args.week or week_label(day if args.date else None)

    raw = load_manifest(manifest_path)
    added, skipped = [], []
    entries = []
    for src in args.file:
        src = os.path.abspath(src)
        base = os.path.basename(src)
        if not os.path.exists(src):
            skipped.append((base, "源文件不存在"))
            continue
        if base.startswith("~$") or base.lower().endswith(".bak"):
            skipped.append((base, "备份/临时文件，跳过"))
            continue
        dest = os.path.join(list_dir, base)
        if os.path.exists(dest):
            if os.path.getsize(dest) == os.path.getsize(src):
                pass  # 已登记过同一份
            else:
                stem, ext = os.path.splitext(base)
                k = 2
                while os.path.exists(os.path.join(list_dir, f"{stem}_{k}{ext}")):
                    k += 1
                dest = os.path.join(list_dir, f"{stem}_{k}{ext}")
                shutil.copy2(src, dest)
                added.append((os.path.basename(dest), "同名不同内容，另存"))
        else:
            shutil.copy2(src, dest)
            added.append((base, "已复制进本周清单"))
        entries.append((base, args.note or "（待补充）"))

    text = add_rows(raw, label, day_str, entries)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        fh.write(text)

    print(f"登记周次: {label}")
    print(f"登记表  : {manifest_path}")
    print(f"清单目录: {list_dir}")
    for fname, why in added:
        print(f"  + {fname}（{why}）")
    for fname, why in skipped:
        print(f"  - {fname}（{why}）")
    print(f"小计: 本次登记 {len(entries)} 个文件")
    return 0


def cmd_list(args):
    root = args.root or cfg.path("weekly_report_root")
    list_dir, manifest_path = paths(root)
    label = args.week or week_label()
    text = load_manifest(manifest_path)
    rows = parse_section(text, label)
    out = []
    for date_s, fname, note in rows:
        p = os.path.join(list_dir, fname)
        if not os.path.exists(p):
            alt = os.path.join(list_dir, fname)
            p = alt if os.path.exists(alt) else ""
        out.append({"date": date_s, "file": fname, "path": p, "note": note})
    if args.json:
        print(json.dumps({"week": label, "items": out}, ensure_ascii=False, indent=2))
        return 0
    print(f"本周（{label}）已登记 {len(out)} 个清单：")
    for i, it in enumerate(out, 1):
        flag = "" if it["path"] else "  [文件缺失]"
        print(f"  {i}. {it['file']}{flag}")
        if it["note"] and it["note"] != "（待补充）":
            print(f"     背景: {it['note']}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="周报清单登记表")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="登记清单")
    a.add_argument("--file", action="append", required=True, help="清单文件路径，可重复")
    a.add_argument("--note", default="", help="口头背景说明")
    a.add_argument("--root", default=DEFAULT_ROOT)
    a.add_argument("--date", default=None, help="补登记日期，形如 09-15")
    a.add_argument("--week", default=None, help="指定周次标签（默认本周）")
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="列出本周登记的清单")
    l.add_argument("--root", default=DEFAULT_ROOT)
    l.add_argument("--week", default=None)
    l.add_argument("--json", action="store_true")
    l.set_defaults(func=cmd_list)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
