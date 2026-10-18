#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读浏览配单清单：输出每个文件的工作表、行数、表头样例、TL- 型号清单。

用法:
    python peek_lists.py "清单1.xlsx" "清单2.xls" ...
不修改任何原始文件。
"""
import os
import re
import sys

import openpyxl

try:
    import xlrd
except ImportError:  # .xls 支持可选
    xlrd = None

sys.stdout.reconfigure(encoding="utf-8")


def cs(v, limit=36):
    if v is None:
        return ""
    return str(v).strip().replace("\n", " ")[:limit]


def read_sheets(path):
    """返回 [(sheet_name, rows)]，rows 为二维列表。"""
    if path.lower().endswith(".xls"):
        if xlrd is None:
            raise RuntimeError("缺少 xlrd，无法读取 .xls（pip install xlrd）")
        book = xlrd.open_workbook(path)
        out = []
        for sh in book.sheets():
            rows = [[sh.cell_value(i, j) for j in range(sh.ncols)] for i in range(sh.nrows)]
            out.append((sh.name, rows))
        return out
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out = []
    for ws in wb.worksheets:
        out.append((ws.title, [list(r) for r in ws.iter_rows(values_only=True)]))
    wb.close()
    return out


def main(files):
    for path in files:
        print("=" * 90, flush=True)
        print("FILE:", os.path.basename(path), flush=True)
        if os.path.basename(path).startswith("~$"):
            print("  (Excel 临时文件，跳过)", flush=True)
            continue
        try:
            sheets = read_sheets(path)
        except Exception as exc:  # noqa: BLE001
            print("  打开失败:", exc, flush=True)
            continue
        for title, rows in sheets:
            rows = [r for r in rows if any(c is not None and str(c).strip() for c in r)]
            print(f"  [sheet] {title}  非空行数: {len(rows)}", flush=True)
            for r in rows[:6]:
                line = " | ".join(cs(c) for c in r if c is not None and str(c).strip())
                if line:
                    print("    ", line[:210], flush=True)
            text = " ".join(cs(c) for r in rows for c in r)
            models = sorted(set(re.findall(r"TL-[A-Za-z0-9\-]+", text)))
            if models:
                print(f"    型号({len(models)}): {', '.join(models[:10])}", flush=True)
    print("=" * 90)
    print("DONE (read-only)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)
    main(args)
