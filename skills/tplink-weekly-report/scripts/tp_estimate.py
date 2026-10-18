#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读估算清单中 TP-LINK（TL- 型号）设备总价。

用法:
    python tp_estimate.py "清单1.xlsx" "清单2.xls" ...
    python tp_estimate.py --quiet "清单1.xlsx" ...          # 只看每个文件合计
    python tp_estimate.py --md-out 明细.md "清单1.xlsx" ...  # 同时把逐行明细写入 Markdown

规则:
    - 只统计"型号列"以 TL- 开头的行（型号列自动定位，优先表头含"型号"的列，
      否则取全表 TL- 出现最多的列）；
    - 行金额优先取已填的 总价/合价/小计 列，否则 数量 × 单价；
    - 数量缺失时按 1 台/个计并在结果里标注；数量为 0 的行不计；
    - 处理合并单元格、跨行表头、表头出现在表格中间的分区式清单；
    - 表头识别出的价格列若整列为 0/空（如"含税单价"占位为 0，真价格在第二层
      表头的"单价/小计"列），自动回退到"有数据的数值列"重新定位列。

不修改任何原始文件。
"""
import os
import sys

import openpyxl

try:
    import xlrd
except ImportError:
    xlrd = None

sys.stdout.reconfigure(encoding="utf-8")

PRICE_KEYS = ("单价", "总价", "合价", "小计")
TOTAL_HEADERS = ("总价", "合价", "小计", "合计", "金额", "成本总价")
TOTAL_PREFIXES = ("总价", "合价", "小计", "成本总价")


def is_total_header(h):
    """总价/合价类列名，允许前缀/后缀修饰（如"总价（元）"、"概算成本总价"）。"""
    h = h.replace(" ", "")
    if h in TOTAL_HEADERS:
        return True
    if any(h.startswith(p) for p in TOTAL_PREFIXES):
        return True
    # 后缀式："概算成本总价" / "含税总价" / "成本总价"（不能以"清单/表"结尾）
    return h.endswith(("总价", "合价", "小计")) and not h.endswith(("清单", "表", "明细"))


def is_qty_header(h):
    """数量列名识别：允许【前缀/后缀】修饰，排除"xx工程量清单"这类标题句。

    实测覆盖："数量"、"数量（项目需求值）"、"图纸核实工程量"、"工程量"。
    """
    h = h.replace(" ", "")
    if h.startswith("数量") or h.startswith("工程量"):
        return True
    if "数量" in h and len(h) <= 12 and not h.endswith(("清单", "表", "明细", "汇总")):
        return True
    # 后缀式："图纸核实工程量" / "核实工程量"
    if h.endswith("工程量") and len(h) <= 14:
        return True
    return False


def heuristic_cols(grid, tl_counts):
    """按数值列分布推断列：型号列=TL-最多的列，数量=其左侧最近数值列，单价=右侧最近数值列。"""
    model_col = max(tl_counts, key=tl_counts.get)
    numeric_cols = [
        j for j in range(len(grid[0]))
        if sum(1 for r in grid if num(r[j]) is not None) >= 2
    ]
    left = [j for j in numeric_cols if j < model_col and j != 0]
    right = [j for j in numeric_cols if j > model_col]
    qty_col = left[-1] if left else None
    price_col = right[0] if right else None
    return model_col, qty_col, price_col, None


def s(v):
    return "" if v is None else str(v).strip()


def num(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        t = v.replace(",", "").replace("，", "").strip()
        try:
            return float(t)
        except ValueError:
            return None
    return None


def grid_with_merges(ws, max_row, max_col):
    """读取单元格并做合并值传播，避免合并表头读成 None。

    同时返回 cont：合并区域里"非左上角"的单元格集合（0 基 (行,列)）。
    这类格子的值是上面传播下来的，累加金额时必须跳过，否则会重复计数
    （实测：出入口清单里 型号/总价 纵向合并 7 行，被误算成 7 倍）。
    """
    grid = [[None] * max_col for _ in range(max_row)]
    for i, row in enumerate(ws.iter_rows(max_row=max_row, max_col=max_col, values_only=True)):
        for j, v in enumerate(row):
            grid[i][j] = v
    cont = set()
    for rng in ws.merged_cells.ranges:
        v = grid[rng.min_row - 1][rng.min_col - 1]
        for r in range(rng.min_row - 1, min(rng.max_row, max_row)):
            for c in range(rng.min_col - 1, min(rng.max_col, max_col)):
                grid[r][c] = v
                if (r, c) != (rng.min_row - 1, rng.min_col - 1):
                    cont.add((r, c))
    return grid, cont


def load_sheets(path):
    """返回 [(name, max_row, max_col, grid, cont)]，支持 .xlsx 与 .xls。"""
    if path.lower().endswith(".xls"):
        if xlrd is None:
            raise RuntimeError("缺少 xlrd，无法读取 .xls（pip install xlrd）")
        book = xlrd.open_workbook(path, formatting_info=False)
        out = []
        for sh in book.sheets():
            grid = [[sh.cell_value(i, j) for j in range(sh.ncols)] for i in range(sh.nrows)]
            out.append((sh.name, sh.nrows, sh.ncols, grid, set()))
        return out
    wb = openpyxl.load_workbook(path, data_only=True)
    out = []
    for ws in wb.worksheets:
        grid, cont = grid_with_merges(ws, ws.max_row, ws.max_column)
        out.append((ws.title, ws.max_row, ws.max_column, grid, cont))
    wb.close()
    return out


def _header_tokens(grid, hi):
    """把表头行及其上下各一行合并成"列名串"，用于识别跨行表头（两行表头）。"""
    idxs = [k for k in (hi - 1, hi, hi + 1) if 0 <= k < len(grid)]
    rows = [grid[k] for k in idxs]
    out = {"qty": [], "price": [], "total": [], "model": []}
    ncols = max(len(r) for r in rows)
    for j in range(ncols):
        tk = "".join(s(row[j]).replace(" ", "").replace("\n", "") for row in rows if j < len(row))
        if not tk:
            continue
        if is_qty_header(tk):
            out["qty"].append(j)
        if "单价" in tk:
            out["price"].append(j)
        if is_total_header(tk):
            out["total"].append(j)
        if tk == "型号" or (tk.endswith("型号") and "规格" not in tk and "品牌" not in tk):
            out["model"].append(j)
    return out


def _tl_rows(grid, model_col):
    return [r for r in grid if s(r[model_col]).startswith("TL-")]


def _col_data_scores(grid, model_col):
    """统计每列在 TL 行里"非零数值"的个数——用来找真正有数据的列。"""
    rows = _tl_rows(grid, model_col)
    scores = {}
    for j in range(len(grid[0])):
        n = sum(1 for r in rows if (num(r[j]) or 0) > 0)
        if n:
            scores[j] = n
    return scores


def _data_driven_cols(grid, model_col, tokens):
    """回退方案：按"哪列真的有数据"重新定位数量列与价格列。"""
    scores = _col_data_scores(grid, model_col)
    if not scores:
        return None
    qty_col = None
    for j in tokens.get("qty", []):
        if j in scores and j < model_col:
            qty_col = j
    if qty_col is None:
        left = sorted(j for j in scores if j < model_col and j != 0)
        qty_col = left[-1] if left else None
    total_col = next((j for j in tokens.get("total", []) if j in scores and j != qty_col), None)
    price_col = next((j for j in tokens.get("price", []) if j in scores and j not in (qty_col, total_col)), None)
    if total_col is None and price_col is None:
        right = sorted(j for j in scores if j not in (qty_col, model_col))
        if right:
            total_col = right[-1]
    if qty_col is None and total_col is None and price_col is None:
        return None
    return model_col, qty_col, price_col, total_col


def _accumulate(name, grid, model_col, qty_col, price_col, total_col, lines, cont=None):
    """按给定列累加金额，返回 (合计, TL行数, 缺数量行数, 数量0行数)。"""
    cont = cont or set()
    sheet_tp = 0.0
    n_rows = qty_missing = qty_zero = 0
    for ri, r in enumerate(grid):
        m = s(r[model_col])
        if not m.startswith("TL-"):
            continue
        n_rows += 1
        q = num(r[qty_col]) if qty_col is not None and qty_col < len(r) else None
        p = num(r[price_col]) if price_col is not None and price_col < len(r) else None
        t = num(r[total_col]) if total_col is not None and total_col < len(r) else None

        # 合并单元格跳过：型号列与总价列都取到"非左上角"的传播值，
        # 说明本行是上面那一行的子行（金额已并入首行）—— 不能再累加一次。
        # 实测：出入口清单 B12:J18 整块合并，序号 4~9 被误算成同一笔 17,832 共 7 遍。
        if (total_col is not None and t and (ri, total_col) in cont
                and (ri, model_col) in cont):
            label = s(r[1])[:14] or m[:24]
            lines.append(f"      [{name}] {label} | {m[:28]} | 合并单元格·金额已并入上一行，跳过")
            continue

        # 价格兜底：价格列没取到 / 为 0（占位列） / 与型号列同列时，从右往左取第一个非零数值格
        if not p or price_col == model_col:
            for j in range(len(r) - 1, -1, -1):
                if j in (qty_col, total_col, 0):
                    continue
                v = num(r[j])
                if v:
                    p = v
                    break
        # 数量兜底：从型号列向左找一个小整数（跳过序号列与价格列）
        if q is None:
            for j in range(model_col - 1, 0, -1):
                if j in (price_col, total_col):
                    continue
                v = num(r[j])
                if v is not None and float(v).is_integer() and 0 < v < 10000:
                    q = v
                    break

        label = s(r[1])[:14] or m[:24]
        if t:
            sheet_tp += t
            used = f"总价列{t:g}"
        elif q is not None and q > 0 and p:
            value = q * p
            sheet_tp += value
            used = f"{q:g}x{p:g}={value:,.0f}"
        elif q == 0:
            qty_zero += 1
            used = "数量0跳过"
        elif p:
            sheet_tp += p
            qty_missing += 1
            used = f"缺数量x1={p:g}"
        else:
            used = "无价格跳过"
        lines.append(f"      [{name}] {label} | {m[:28]} | {used}")

    return sheet_tp, n_rows, qty_missing, qty_zero


def estimate_sheet(name, grid, lines, cont=None):
    """估算单个工作表，返回 (合计, 行数, 缺数量行数, 数量0行数)。"""
    if len(grid) < 3:
        return 0.0, 0, 0, 0

    # 型号列定位：全表扫描 TL- 出现最多的列
    tl_counts = {}
    for r in grid:
        for j, c in enumerate(r):
            if s(c).startswith("TL-"):
                tl_counts[j] = tl_counts.get(j, 0) + 1

    # 表头行：前 15 行里含价格关键字的行；优先选真正带单价/总价列名的那一行
    # （标题行常含"报价明细表"之类字样，不能当作表头）
    cands = [
        i for i in range(min(15, len(grid)))
        if any(k in "".join(s(c) for c in grid[i]) for k in PRICE_KEYS)
    ]
    hi = None
    for i in cands:
        hdr0 = [s(c).replace(" ", "") for c in grid[i]]
        if any(("单价" in h) or (h in TOTAL_HEADERS) for h in hdr0):
            hi = i
            break
    if hi is None and cands:
        hi = cands[0]
    if hi is None and not tl_counts:
        return 0.0, 0, 0, 0

    tokens = _header_tokens(grid, hi) if hi is not None else {"qty": [], "price": [], "total": [], "model": []}

    if hi is None:
        model_col, qty_col, price_col, total_col = heuristic_cols(grid, tl_counts)
    else:
        hdr = [s(c).replace(" ", "") for c in grid[hi]]
        price_col = qty_col = total_col = model_col = None
        for j, h in enumerate(hdr):
            if "单价" in h:
                price_col = j
            if is_qty_header(h):
                qty_col = j
            if is_total_header(h) and total_col is None:
                total_col = j
            if h == "型号" or (h.endswith("型号") and "规格" not in h and "品牌" not in h):
                model_col = j
        # 跨行表头：第二层表头里的"型号/单价/小计"列
        if model_col is None and tokens["model"]:
            model_col = tokens["model"][0]
        if price_col is None and tokens["price"]:
            price_col = tokens["price"][0]
        if total_col is None and tokens["total"]:
            total_col = tokens["total"][0]
        # 跨行表头兜底：本行没找到数量列时，向上 3 行内再找
        if qty_col is None:
            for k in range(max(0, hi - 3), hi):
                for j, c in enumerate(grid[k]):
                    if is_qty_header(s(c)):
                        qty_col = j
                        break
                if qty_col is not None:
                    break
        # 全表扫描：分区式清单的表头可能在中间，表头之上也有数据行
        data = grid
        # 型号列兜底：数据区 TL- 最多的列
        dtl = {}
        for r in data:
            for j, c in enumerate(r):
                if s(c).startswith("TL-"):
                    dtl[j] = dtl.get(j, 0) + 1
        if (model_col is None or dtl.get(model_col, 0) == 0) and dtl:
            model_col = max(dtl, key=dtl.get)

    if model_col is None:
        return 0.0, 0, 0, 0

    # 第 1 遍：按表头识别的列
    tmp = []
    res = _accumulate(name, grid, model_col, qty_col, price_col, total_col, tmp, cont)

    # 第 2 遍（回退）：表头列取不到钱时，改用"真正有数据的数值列"
    # 典型场景：第一层表头"含税单价"占位为 0，真价格在第二层表头的"单价/小计"
    if res[0] <= 0 and res[1] > 0:
        alt = _data_driven_cols(grid, model_col, tokens)
        if alt and alt[1:] != (qty_col, price_col, total_col):
            tmp2 = []
            res2 = _accumulate(name, grid, alt[0], alt[1], alt[2], alt[3], tmp2, cont)
            if res2[0] > res[0]:
                res, tmp = res2, tmp2

    lines.extend(tmp)
    return res


def main(paths, quiet=False, md_out=None):
    md_lines = ["# TP-LINK 估价逐行明细", ""]
    summary = []
    for path in paths:
        base = os.path.basename(path)
        if base.startswith("~$"):
            continue
        print("PROCESS:", base, flush=True)
        md_lines.append(f"## {base}")
        md_lines.append("")
        try:
            sheets = load_sheets(path)
        except Exception as exc:  # noqa: BLE001
            print("  SKIP:", exc, flush=True)
            md_lines += [f"打开失败：{exc}", ""]
            continue
        file_tp = 0.0
        detail = []
        for name, _mr, _mc, grid, cont in sheets:
            lines = []
            tp, n_rows, qty_missing, qty_zero = estimate_sheet(name, grid, lines, cont)
            if n_rows:
                note = ""
                if qty_missing:
                    note += f" 缺数量按1计{qty_missing}行"
                if qty_zero:
                    note += f" 数量为0不计{qty_zero}行"
                detail.append(f"    {name}: TL行{n_rows}, 估价 {tp:,.0f} 元{note}")
                md_lines += [f"### {name}（TL行 {n_rows}，估价 {tp:,.0f} 元{note}）", ""]
                md_lines += lines
                md_lines.append("")
                file_tp += tp
        print(f"  ==> TP-LINK估价合计: {file_tp:,.0f} 元", flush=True)
        for d in detail:
            print(d, flush=True)
        summary.append((base, file_tp))
        md_lines += [f"**小计：{file_tp:,.0f} 元**", ""]
    print("=" * 80, flush=True)
    total = sum(v for _, v in summary)
    print(f"全部文件合计: {total:,.0f} 元（注意主/备、多批次可能重复计数）", flush=True)
    for base, v in summary:
        print(f"  {base}: {v:,.0f} 元", flush=True)
    if md_out:
        with open(md_out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(md_lines))
        print("逐行明细已写入:", md_out)


if __name__ == "__main__":
    argv = sys.argv[1:]
    quiet = "--quiet" in argv
    md_out = None
    if "--md-out" in argv:
        idx = argv.index("--md-out")
        md_out = argv[idx + 1]
        del argv[idx:idx + 2]
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)
    main(args, quiet=quiet, md_out=md_out)
