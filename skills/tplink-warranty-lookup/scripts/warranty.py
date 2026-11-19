#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TP-LINK 保修期查询（数据源：service.tp-link.com.cn/static/js/consultData.js）

用法:
  python warranty.py TL-NS650-48GP4F
  python warranty.py TL-A TL-B --json
  python warranty.py --like NS650
  python warranty.py --refresh TL-SG1008D
  python warranty.py --xlsx 清单.xlsx --sheet 清单二 --model-col G --warranty-col M
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

SRC_URL = "https://service.tp-link.com.cn/static/js/consultData.js"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "..", "data", "warranty_cache.json")
CACHE_TTL = 7 * 86400  # 7 天


def norm(s):
    """清洗型号：去数量后缀 *3、全角空格、首尾空白、统一大写"""
    if s is None:
        return ""
    s = str(s).replace("\u3000", " ").strip()
    s = re.sub(r"\s*\*\s*\d+$", "", s)          # TL-SG1008D*3 -> TL-SG1008D
    s = re.sub(r"\s+", "", s)
    return s.upper()


def flatten(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, (list, tuple)):
        for i in x:
            for y in flatten(i):
                yield y


def build_map(raw):
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("consultData.js 格式异常：找不到 JSON 主体")
    data = json.loads(raw[start:end + 1])
    m = {}
    for cat in data.get("products", []):
        cat_name = cat.get("name", "")
        for sub in cat.get("detail", []):
            times = sub.get("time") or []
            warranty = times[0] if times else ""
            for model in flatten(sub.get("model")):
                key = norm(model)
                if key:
                    m.setdefault(key, {"warranty": warranty, "cat": cat_name,
                                       "sub": sub.get("type", ""), "model": model})
    return {"updatetime": data.get("updatetime", ""), "map": m}


def load(force=False):
    cache = os.path.abspath(CACHE)
    if not force and os.path.exists(cache):
        age = time.time() - os.path.getmtime(cache)
        if age < CACHE_TTL:
            try:
                with open(cache, encoding="utf-8") as f:
                    return json.load(f), False
            except Exception:
                pass
    req = urllib.request.Request(SRC_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8", "replace")
    obj = build_map(raw)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return obj, True


def query(db, model):
    key = norm(model)
    hit = db["map"].get(key)
    if hit:
        return {"input": model, "model": hit.get("model", key), "found": True,
                "warranty": hit["warranty"], "cat": hit["cat"], "sub": hit["sub"]}
    # 后缀/颜色词差异：允许唯一前缀命中
    cands = [v for k, v in db["map"].items() if k.startswith(key)]
    if len(cands) == 1:
        v = cands[0]
        return {"input": model, "model": v.get("model", key), "found": True,
                "warranty": v["warranty"], "cat": v["cat"], "sub": v["sub"],
                "note": "按前缀唯一命中"}
    return {"input": model, "found": False, "warranty": "", "cat": "", "sub": "", "model": key}


def cmd_query(db, models, as_json, refreshed):
    rows = [query(db, m) for m in models]
    if as_json:
        print(json.dumps({"updatetime": db["updatetime"], "refreshed": refreshed,
                          "results": rows}, ensure_ascii=False, indent=2))
        return
    print("数据更新日期: %s%s" % (db["updatetime"], "（本次已联网刷新）" if refreshed else "（缓存）"))
    print("-" * 78)
    for r in rows:
        if r["found"]:
            extra = "  [%s]" % r["note"] if r.get("note") else ""
            print("%-26s %-8s   %s / %s%s" % (r["input"], r["warranty"], r["cat"], r["sub"], extra))
        else:
            print("%-26s %-8s   (未收录，用 --like 查同系列)" % (r["input"], "查不到"))
    missing = [r["input"] for r in rows if not r["found"]]
    if len(rows) > 1:
        print("-" * 78)
        print("共 %d 个型号，命中 %d，未收录 %d" % (len(rows), len(rows) - len(missing), len(missing)))
        if missing:
            print("未收录: " + ", ".join(missing))


def cmd_like(db, kw):
    k = norm(kw)
    hits = [(v.get("model") or m, v) for m, v in db["map"].items() if k in m]
    hits.sort()
    print("数据更新日期: %s，包含 '%s' 的型号 %d 个" % (db["updatetime"], kw, len(hits)))
    for model, v in hits:
        print("  %-30s %-8s %s / %s" % (model, v["warranty"], v["cat"], v["sub"]))


HEADER_HINTS = ("型号", "名称", "产品", "设备", "材料", "品名", "规格")


def is_header(cell):
    t = str(cell)
    return any(h in t for h in HEADER_HINTS)


def split_models(cell):
    txt = str(cell)
    parts = re.split(r"[\n,，、/;；]+", txt)
    return [p for p in (norm(x) for x in parts) if p]


def cmd_xlsx(db, path, sheet, mcol, wcol, dry, header_row=0):
    try:
        import openpyxl
    except ImportError:
        venv = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries",
                            "python", "envs", "default", "Scripts", "python.exe")
        sys.exit("当前 Python 缺 openpyxl。批量填表请换解释器或装一下：\n"
                 "  pip install openpyxl\n"
                 "  或用 WorkBuddy 自带环境：%s" % venv)
    if not os.path.exists(path):
        sys.exit("文件不存在: %s" % path)
    if not dry:
        bak = path + ".bak"
        if not os.path.exists(bak):
            import shutil
            shutil.copy2(path, bak)
            print("已备份: %s" % bak)
    wb = openpyxl.load_workbook(path)
    ws = wb[sheet] if sheet else wb.active
    mcol = str(mcol).upper()
    wcol = str(wcol).upper()
    filled = skip = 0
    for row in range(1, ws.max_row + 1):
        cell_m = ws["%s%d" % (mcol, row)].value
        if not cell_m or not str(cell_m).strip():
            continue
        if row == header_row or (header_row == 0 and is_header(cell_m)):
            print("  第%d行 跳过表头: %s" % (row, str(cell_m)[:32]))
            continue
        models = split_models(cell_m)
        vals, unknown = [], []
        for mm in models:
            r = query(db, mm)
            if r["found"]:
                if r["warranty"] not in vals:
                    vals.append(r["warranty"])
            else:
                unknown.append(mm)
        if vals and not unknown:
            val = vals[0] if len(vals) == 1 else "/".join(vals)
        elif vals and unknown:
            val = "%s（%s 未收录）" % ("/".join(vals), ",".join(unknown))
        else:
            val = "未收录"
            skip += 1
        print("  第%d行 %-32s -> %s" % (row, str(cell_m)[:32], val))
        if not dry:
            ws["%s%d" % (wcol, row)].value = val
        filled += 1
    if not dry:
        wb.save(path)
        print("已写入 %s（%d 行，未收录 %d 行）" % (path, filled, skip))
    else:
        print("[dry-run] 未写入，共 %d 行" % filled)


def main():
    ap = argparse.ArgumentParser(description="TP-LINK 保修期查询")
    ap.add_argument("models", nargs="*", help="型号，可多个")
    ap.add_argument("--like", metavar="KW", help="按关键词列出全部型号变体")
    ap.add_argument("--refresh", action="store_true", help="强制联网刷新数据")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--xlsx", help="批量填表：xlsx 路径")
    ap.add_argument("--sheet", default="", help="工作表名，默认活动表")
    ap.add_argument("--model-col", default="G", help="型号列，默认 G")
    ap.add_argument("--warranty-col", default="M", help="保修期列，默认 M")
    ap.add_argument("--dry-run", action="store_true", help="只预览不写盘")
    ap.add_argument("--header-row", type=int, default=0,
                    help="表头所在行号，默认 0=自动识别（含 型号/名称/产品 等字样即跳过）")
    a = ap.parse_args()

    db, refreshed = load(force=a.refresh)

    if a.xlsx:
        cmd_xlsx(db, a.xlsx, a.sheet, a.model_col, a.warranty_col, a.dry_run, a.header_row)
    elif a.like:
        cmd_like(db, a.like)
    elif a.models:
        cmd_query(db, a.models, a.json, refreshed)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
