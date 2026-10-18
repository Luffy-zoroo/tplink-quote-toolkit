#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成周报草稿骨架：条目（背景来自登记表）+ 估价；**不含汇总表**。

用法:
    python draft_report.py --out "周报草稿0915.md" "清单1.xlsx" "清单2.xls" ...
    python draft_report.py --week --out "周报草稿0915.md"        # 自动取本周登记表里的清单
    python draft_report.py --date 09-15 --out ... --week

脚本只提供"文件名 + 背景（登记表里的口头说明）+ 估价"的骨架；配单内容描述与
未计入部分说明需要在此基础上补写。不修改任何原始清单文件。
"""
import argparse
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tp_estimate  # noqa: E402
import week_log  # noqa: E402

CAVEAT = (
    "> 估价说明：TP-LINK 部分按清单中 TL- 型号行的\"数量 × 单价\"估算（已填总价的直接取总价）；"
    "个别行未填数量的按 1 台/个计；未填单价的部分未计入。均为设备部分估价，不含施工辅材与其他品牌。"
)


def load_notes(manifest_path, label):
    """返回 {文件名: 背景}；登记表不存在时返回空字典。"""
    if not os.path.exists(manifest_path):
        return {}
    with open(manifest_path, encoding="utf-8") as fh:
        text = fh.read()
    return {fname: note for _d, fname, note in week_log.parse_section(text, label)}


def main(paths, out_path, date_str, notes):
    entries = []
    for path in paths:
        base = os.path.basename(path)
        if base.startswith("~$") or base.lower().endswith(".bak"):
            continue
        try:
            sheets = tp_estimate.load_sheets(path)
        except Exception as exc:  # noqa: BLE001
            entries.append({"file": base, "tp": 0.0, "sheets_meta": [], "err": str(exc)})
            continue
        file_tp = 0.0
        sheets_meta = []
        for name, _mr, _mc, grid in sheets:
            lines = []
            tp, n_rows, qty_missing, qty_zero = tp_estimate.estimate_sheet(name, grid, lines)
            if n_rows:
                sheets_meta.append((name, n_rows, tp, qty_missing, qty_zero))
                file_tp += tp
        entries.append({"file": base, "tp": file_tp, "sheets_meta": sheets_meta, "err": None})

    md = [f"# 周报（草稿 {date_str}）", "", CAVEAT, "", "## 本周配单项目", ""]
    idx = 0
    for e in entries:
        idx += 1
        note = notes.get(e["file"], "")
        bg = note if note and note != "（待补充）" else "项目背景未知"
        if e["err"]:
            md += [f"{idx}. **{e['file']}**：{bg}。打开失败：{e['err']}。", ""]
            continue
        parts = []
        for name, n_rows, tp, miss, zero in e["sheets_meta"]:
            extra = ""
            if miss:
                extra += f"，缺数量按1计{miss}行"
            if zero:
                extra += f"，数量为0不计{zero}行"
            parts.append(f"{name}（TL行{n_rows}，{tp:,.0f} 元{extra}）")
        head = f"{idx}. **{e['file']}**：{bg}。"
        if e["tp"] > 0:
            md.append(head + f"配单内容：TODO（工作表构成：{'；'.join(parts)}）。"
                             f"TP-LINK 部分估价约 **{e['tp'] / 10000:.1f} 万**（{e['tp']:,.0f} 元）。")
        else:
            md.append(head + "配单内容：TODO（清单中未取到有效的 TP-LINK 数量与单价，未给出估价，请核对口径）。")
        md.append("")
    md += ["## 其他事项（待补充）", "", "- （配置类事项、跟进中项目等请自行补充/删除）", ""]

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    print("已生成周报骨架:", out_path)
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="生成周报草稿骨架（无汇总表）")
    ap.add_argument("files", nargs="*", help="清单文件路径")
    ap.add_argument("--out", required=True)
    ap.add_argument("--date", default=None, help="草稿日期标识，如 0915")
    ap.add_argument("--week", action="store_true", help="从登记表取本周清单")
    ap.add_argument("--root", default=week_log.DEFAULT_ROOT)
    args = ap.parse_args()

    if not args.root:                       # 未配置 weekly_report_root 时给出明确指引
        args.root = week_log.cfg.path("weekly_report_root")

    date_str = args.date or datetime.date.today().strftime("%m%d")
    label = week_log.week_label()
    _list_dir, manifest_path = week_log.paths(args.root)
    notes = load_notes(manifest_path, label)

    paths = list(args.files)
    if args.week:
        for _d, fname, _n in week_log.parse_section(week_log.load_manifest(manifest_path), label):
            p = os.path.join(_list_dir, fname)
            if os.path.exists(p):
                paths.append(p)
            else:
                print("  警告：登记表里的文件不在本周清单目录中:", fname, file=sys.stderr)
    if not paths:
        print("没有可处理的清单文件", file=sys.stderr)
        sys.exit(1)
    main(paths, args.out, date_str, notes)
