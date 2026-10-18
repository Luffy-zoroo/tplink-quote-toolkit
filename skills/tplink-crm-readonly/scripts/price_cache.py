#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TP-LINK CRM 价格「查得到就落盘」本地备份（2026-10-13 用户新规）。

规则（用户原话）：
    价格每次都要去 CRM 查，但查到之后本地都要写一份 json 备份对应的产品和价格，
    方便无法访问 CRM 时查询。不用考虑安全问题。

用法:
    python price_cache.py get TL-A TL-B ...      # 默认：先查 CRM → 落盘；CRM 不通则读备份
    python price_cache.py refresh TL-A ...       # 强制走 CRM 刷新（失败即报错，不降级）
    python price_cache.py --offline TL-A TL-B    # 强制只读本地备份，绝不联网
    python price_cache.py save TL-A TL-B         # 只刷新落盘，不打印明细
    python price_cache.py stats                  # 备份概况
    python price_cache.py --path                 # 打印备份文件路径

备份文件：
    由 tpkit_config 的 price_backup_json 指定；
    未配置时默认落到 ~/.workbuddy/tplink-toolkit/data/tplink_crm_price_backup.json

每个型号存: 标准价 price / 目录价 catalogPrice / 批发价 wholesalePrice /
            业务折扣率 businessDiscount / 系列 seriesValue / 在产 lifeCycle /
            单位 unit / 阶梯价 ladderPrice / lineInfo / valid / 取数时间 fetchedAt

注意:
    - CRM `productModel` 是模糊匹配，本脚本按精确型号再筛一遍（与 crm_query.py 一致）。
    - 同一型号可能返回多条（不同 OU / 阶梯档），**全部保留**，由调用方按
      `valid is True` 过滤后再裁决，不要自己挑一条。
    - 备份是**只增不删**的：已存在且本次查不到的型号，保留旧记录并标 stale，
      不覆盖成空（避免 CRM 抽风把历史价格抹掉）。
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crm_query as Q  # noqa: E402
import tpkit_config as cfg  # noqa: E402

# 备份文件走配置；未配置时自动落到 ~/.workbuddy/tplink-toolkit/data/ 下
STORE = cfg.data_path('price_backup_json')

# 只留价格相关的稳定字段，避免把整行塞进备份
KEEP = ("productModel", "productName", "brandValue", "seriesValue",
        "price", "catalogPrice", "wholesalePrice", "businessDiscount",
        "ladderPrice", "lineInfo", "lifeCycle", "unit", "valid",
        "startQty", "endQty", "lineCode")


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_store(path=STORE):
    if not os.path.exists(path):
        return {"_meta": {"schema": 1,
                          "desc": "TP-LINK CRM 价格本地备份（配单取价用；CRM 不可达时兜底）",
                          "source": "CRM /api/line/findByPage",
                          "updated": None},
                "prices": {}}
    with open(path, encoding="utf-8") as fh:
        st = json.load(fh)
    st.setdefault("_meta", {})
    st.setdefault("prices", {})
    return st


def save_store(st, path=STORE):
    st["_meta"]["updated"] = _now()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)          # 原子替换，避免写坏


def slim(rows):
    return [{k: r.get(k) for k in KEEP if k in r} for r in rows]


def fetch_from_crm(models, limit=300, verbose=True):
    """走 CRM 查一批型号 → {型号: [记录...]}。返回 (结果, 登录信息)。"""
    email, pwd = Q.resolve_creds(sys.argv[1:])
    op = Q.make_opener()
    who, office = Q.login(op, email, pwd)
    if verbose:
        print(f"[登录] {who} / {office}")
    out = {}
    for m in models:
        res = Q.find_lines(op, m, limit=limit).get("results", [])
        hit = [x for x in res
               if str(x.get("productModel") or "").strip().upper() == m.strip().upper()]
        # 精确命中优先；只有精确查不到时才给模糊候选（标 fuzzy，让人工判断）
        out[m] = {"exact": slim(hit), "fuzzy": [] if hit else slim(res)[:10]}
        if verbose:
            n = len(hit)
            print(f"  {m}: 精确命中 {n} 条" + ("" if n else f"，模糊候选 {len(out[m]['fuzzy'])} 条"))
    return out, f"{who} / {office}"


def merge_into_store(st, res, mode_note="crm"):
    ts = _now()
    for m, pack in res.items():
        recs = pack["exact"] if pack.get("exact") else pack.get("fuzzy", [])
        if not recs:
            old = st["prices"].get(m)
            if old:
                old["stale"] = True
                old["lastTriedAt"] = ts
            continue
        st["prices"][m] = {
            "model": m,
            "fetchedAt": ts,
            "match": "exact" if pack.get("exact") else "fuzzy",
            "source": mode_note,
            "stale": False,
            "records": recs,
        }
    return st


def print_records(m, rec):
    if not rec:
        print(f"  {m}: ✗ 无记录")
        return
    tag = "" if not rec.get("stale") else "  ⚠stale(本次查不到，沿用历史)"
    fuzzy = "  ⚠fuzzy(非精确命中，需人工确认)" if rec.get("match") == "fuzzy" else ""
    print(f"  {m}: {len(rec['records'])} 条  [取数 {rec['fetchedAt']}]{fuzzy}{tag}")
    for r in rec["records"]:
        print(f"      price={r.get('price')} | bd={r.get('businessDiscount')} | "
              f"series={r.get('seriesValue')} | lifeCycle={r.get('lifeCycle')} | "
              f"valid={r.get('valid')} | catalog={r.get('catalogPrice')} | "
              f"ladder={r.get('ladderPrice')} | {r.get('productName')}")


def cmd_get(models, offline=False, refresh=False, quiet=False):
    if not models:
        print(__doc__)
        return
    st = load_store()
    used = "cache"
    if not offline:
        try:
            res, _who = fetch_from_crm(models, verbose=not quiet)
            merge_into_store(st, res, "crm")
            save_store(st)
            used = "crm"
        except Exception as e:                      # noqa: BLE001
            if refresh:
                raise
            print(f"⚠ CRM 不可达（{type(e).__name__}: {e}）→ 降级读本地备份")
            used = "cache"
    if used == "cache":
        miss = [m for m in models if m not in st["prices"]]
        if miss:
            print(f"⚠ 备份里也没有这些型号: {', '.join(miss)}")
    if used == "cache":
        print(f"[本地备份] {STORE}  (更新于 {st['_meta'].get('updated')})")
    if not quiet:
        for m in models:
            print_records(m, st["prices"].get(m))
    return st


def cmd_stats():
    st = load_store()
    ps = st["prices"]
    print(f"备份文件: {STORE}")
    print(f"最后更新: {st['_meta'].get('updated')}")
    print(f"收录型号: {len(ps)}")
    stale = [m for m, r in ps.items() if r.get("stale")]
    fuzzy = [m for m, r in ps.items() if r.get("match") == "fuzzy"]
    print(f"stale(历史沿用): {len(stale)}" + (f"  {stale[:5]}" if stale else ""))
    print(f"fuzzy(模糊命中): {len(fuzzy)}" + (f"  {fuzzy[:5]}" if fuzzy else ""))
    cats = {}
    for r in ps.values():
        for x in r["records"]:
            s = x.get("seriesValue") or "(空)"
            cats[s] = cats.get(s, 0) + 1
    print("系列分布:", dict(sorted(cats.items(), key=lambda kv: -kv[1])[:8]))


def main(argv):
    offline = "--offline" in argv
    quiet = "--quiet" in argv
    args = [a for a in argv if not a.startswith("--")]
    if args and args[0] in ("get", "refresh", "save", "stats"):
        cmd, args = args[0], args[1:]
    else:
        cmd = "get"
    if "--path" in argv:
        print(STORE)
        return
    if cmd == "stats":
        cmd_stats()
        return
    if cmd == "refresh":
        cmd_get(args, offline=False, refresh=True, quiet=quiet)
        return
    if cmd == "save":
        cmd_get(args, offline=False, quiet=True)
        return
    cmd_get(args, offline=offline, quiet=quiet)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except cfg.NotConfigured as e:
        sys.exit("配置缺失：%s" % e)
