#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TP-LINK CRM 库存查询（全国分仓 / 本办仓 / 返修仓分离）。

用法:
    python inv_check.py TL-KSG400 TL-AS8230            # 查指定型号
    python inv_check.py --from 清单.xlsx               # 整张清单批量查，标缺口
    python inv_check.py --from 清单.xlsx --json out.json
    python inv_check.py TL-KSG400 --include-refurb     # 把返修仓也计入可用量（一般不要）

⚠️ 口径（必读，2026-11-25 用户明确）:
    1. **`HLJ` 前缀的仓是返修仓**。返修品不算可发库存，默认**不计入可用量**，
       只在「返修」列里显示数量。把返修当现货报出去，等于答应客户交不出的货。
       （前缀可用 `--refurb-prefix` 改，判断的是仓号不是办事处。）
    2. `qty` 是**账面库存**，接口没给「已被订单占用多少」的可信字段（`orderQty`
       大多数为 null），所以可用量 = 账面量，不等于「现在下单一定能发」。
    3. 返回的是**全国各仓**。「本办仓」按当前登录账号动态取（`findInvPage` 只返
       本办那几个仓），**不假定任何办事处**——南昌、上海、北京都是同一套逻辑；
       取不到时本办列显示 `-`，不猜。

只读，不修改任何数据。接口路径不写域名，`crm_base` 从配置读（tpkit_config）。
输出：默认是给人看的简表；要接程序就加 `--json <文件>` 或 `--json -`（打到 stdout）。
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import crm_query as Q        # noqa: E402  复用登录/凭据/opener
import tpkit_config as _cfg  # noqa: E402

REFURB_PREFIX = ("HLJ",)          # 返修仓（用户 2026-11-25 明确；--refurb-prefix 可改）
UA = {"User-Agent": "Mozilla/5.0"}


# ---------------------------------------------------------------- 接口
def _get(op, path, params, timeout=60):
    url = Q.crm_api() + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    return json.loads(op.open(req, timeout=timeout).read().decode("utf-8"))


def inv_rows(op, model):
    """返回该型号的全国分仓行；型号必填（不传接口直接 400）。"""
    j = _get(op, "/inv/findInvQtyTab", {"productModel": model})
    rows = j.get("list")
    if rows is None:
        rows = j.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("list") or rows.get("rows") or []
    return rows or []


def office_invs(op):
    """当前账号所属办事处的仓号集合；失败返回空集（不报错，降级用前缀判断）。"""
    try:
        j = _get(op, "/inv/findInvPage", {"start": 0, "limit": 500})
        return {r.get("invName") for r in (j.get("results") or []) if r.get("invName")}
    except Exception:
        return set()


# ---------------------------------------------------------------- 归并
def norm(m):
    return str(m or "").replace("\u3000", " ").strip().upper()


def pick_exact(rows, model):
    """接口是前缀模糊匹配，必须按型号精确筛。返回 (精确行, 近似型号列表)。"""
    want = norm(model)
    exact = [r for r in rows if norm(r.get("model")) == want]
    near = sorted({r.get("model") for r in rows if norm(r.get("model")) != want})
    return exact, near


def kind(inv_name, local_set, refurb_prefix=REFURB_PREFIX):
    n = str(inv_name or "")
    if n.startswith(tuple(refurb_prefix)):
        return "refurb"
    if local_set:
        return "local" if n in local_set else "away"
    return "away"          # 不知道本办是哪些仓时，不猜，只保证返修不算可用


def aggregate(rows, need, local_set, refurb_prefix=REFURB_PREFIX):
    """把分仓行归并成可用量。返修仓一律不进 available。"""
    out = {"need": need, "available": 0, "local": 0, "refurb": 0,
           "intransit": 0, "by_inv": {}, "models": set(),
           "local_known": bool(local_set)}
    for r in rows:
        q = r.get("qty") or 0
        inv = r.get("invName")
        k = kind(inv, local_set, refurb_prefix)
        if k == "refurb":
            out["refurb"] += q
        else:
            out["available"] += q
            out["intransit"] += (r.get("orderIntransitNum") or 0)
            if q:
                out["by_inv"][inv] = out["by_inv"].get(inv, 0) + q
        if k == "local":
            out["local"] += q
        out["models"].add(r.get("model"))
    return out


def verdict(agg):
    need = agg["need"]
    av = agg["available"]
    if not need:
        return "查量"
    if av >= need:
        if agg.get("local_known") and agg["local"] >= need:
            return "本办够"
        return "够(需调拨)"
    if av == 0 and agg["refurb"] > 0:
        return "仅返修"
    return "缺 %d" % (need - av)


def top_invs(agg, n=3):
    items = sorted(agg["by_inv"].items(), key=lambda kv: -kv[1])[:n]
    return ", ".join("%s=%d" % (k, v) for k, v in items) or "-"


# ---------------------------------------------------------------- 清单读取
def detect_columns(ws, scan=20):
    """按表头找「型号」「数量」列，返回 (model_idx, qty_idx, header_row) 或 None。

    不硬编码列位 —— 清单模板几乎每份都不同，读错列会静默算出错误的缺口。
    表头常见写法：`产品型号` / `数量(PCS)` / `数量（台）`，所以先把括号单位去掉再比。
    """
    keys_model = ("型号", "规格型号", "设备型号", "产品型号")
    keys_qty = ("数量", "数目", "采购数量")
    for r in range(1, min(scan, ws.max_row) + 1):
        mi = qi = None
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            if not isinstance(v, str):
                continue
            s = re.sub(r"[（(][^）)]*[）)]", "", v).strip()   # 去掉 (PCS) / （台）
            if not s:
                continue
            if mi is None and any(s == k or s.endswith(k) for k in keys_model):
                mi = c
            elif qi is None and any(s == k or s.endswith(k) for k in keys_qty):
                qi = c
        if mi and qi:
            return mi, qi, r
    return None


def load_xlsx(path, sheet=None, model_col=None, qty_col=None):
    try:
        import openpyxl
    except ImportError:
        sys.exit("缺 openpyxl：pip install openpyxl（读 .xlsx 用）")
    if path.lower().endswith((".xls", ".xlsb")):
        sys.exit("只支持 .xlsx；老 .xls 请先用 Excel 另存为 .xlsx")
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    det = detect_columns(ws)
    if det:
        mi, qi, hdr = det
        if model_col:
            mi = model_col
        if qty_col:
            qi = qty_col
        print("[列位] 表头在第 %d 行：型号=%s 列，数量=%s 列"
              % (hdr, openpyxl.utils.get_column_letter(mi),
                 openpyxl.utils.get_column_letter(qi)), file=sys.stderr)
        start = hdr + 1
    else:
        if not (model_col and qty_col):
            sys.exit("没识别出「型号 / 数量」表头：请用 --model-col / --qty-col "
                     "显式指定列号（A=1），或 --sheet 换工作表")
        mi, qi = model_col, qty_col
        start = 1
        print("[列位] 未识别表头，按指定列号读：型号=%d 数量=%d" % (mi, qi), file=sys.stderr)
    out = []
    for r in range(start, ws.max_row + 1):
        m = ws.cell(row=r, column=mi).value
        if not isinstance(m, str) or not m.strip():
            continue
        if norm(m) in ("型号", "合计", "小计"):
            continue
        q = ws.cell(row=r, column=qi).value
        try:
            q = int(float(q))
        except (TypeError, ValueError):
            q = 0
        out.append((m.strip(), q))
    return out


# ---------------------------------------------------------------- 输出
def print_report(results, show_refurb=True):
    known = any(r["agg"].get("local_known") for r in results)
    head = "%-24s %4s %8s %8s %8s %8s  %s" % (
        "型号", "需", "可用", "本办", "返修", "在途", "结论")
    print(head)
    print("-" * 78)
    bad = 0
    for r in results:
        a = r["agg"]
        flag = verdict(a)
        if a["need"] and a["available"] < a["need"]:
            bad += 1
        local_cell = "%8d" % a["local"] if a.get("local_known") else "       -"
        line = "%-24s %4d %8d %s %8d %8d  %s" % (
            r["model"][:24], a["need"], a["available"], local_cell,
            a["refurb"] if show_refurb else 0, a["intransit"], flag)
        print(line)
        if r["note"]:
            print("    ⚠ %s" % r["note"])
        if a["by_inv"]:
            print("    仓: %s" % top_invs(a))
    print()
    print("合计 %d 个型号，其中 %d 个不满足（可用量已剔除返修仓）" % (len(results), bad))
    if not known:
        print("注：本办仓字典没取到，本办列显示 -；可用量是全国口径（不含返修）。")
    return bad


def main(argv):
    ap = argparse.ArgumentParser(
        description="查 TP-LINK CRM 全国库存（返修仓默认不计入可用量）")
    ap.add_argument("models", nargs="*", help="型号，可多个")
    ap.add_argument("--from", dest="xlsx", help="从清单 .xlsx 批量查")
    ap.add_argument("--sheet", help="工作表名（默认第一个）")
    ap.add_argument("--model-col", type=int, help="型号列号（A=1），识别不出表头时用")
    ap.add_argument("--qty-col", type=int, help="数量列号（A=1）")
    ap.add_argument("--include-refurb", action="store_true",
                    help="把返修仓计入可用量（默认不计，慎用）")
    ap.add_argument("--json", dest="json_out",
                    help="把结果写成 JSON；写 - 则打到 stdout（给程序/agent 用）")
    ap.add_argument("--refurb-prefix", default="HLJ",
                    help="返修仓号前缀，多个用逗号分隔（默认 HLJ）")
    ap.add_argument("--email", help="CRM 账号（默认取环境变量/记忆）")
    ap.add_argument("--password", help="CRM 密码")
    ap.add_argument("--selftest", action="store_true", help="离线自测（不联网）")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    refurb = tuple(x.strip().upper() for x in a.refurb_prefix.split(",") if x.strip())

    items = [(m, 0) for m in a.models]
    if a.xlsx:
        items += load_xlsx(a.xlsx, a.sheet, a.model_col, a.qty_col)
    if not items:
        ap.print_help()
        return 2

    _cfg.require_base("crm_base", "CRM")   # 先确认配置，再找凭据：报错更早也更准
    op = Q.make_opener()
    email, pwd = Q.resolve_creds(argv)
    who, office = Q.login(op, email, pwd)
    print("[登录] %s / %s" % (who, office))
    local_set = office_invs(op)
    if local_set:
        print("[本办仓] %s" % ", ".join(sorted(local_set)))
    else:
        print("[本办仓] 未取到本办仓字典 → 本办列显示 -，只给全国口径（返修仍剔除）")

    results = []
    for model, need in items:
        rows = inv_rows(op, model)
        exact, near = pick_exact(rows, model)
        note = ""
        if not exact and near:
            note = "无精确型号，接口返回近似：%s" % ", ".join(near[:4])
        agg = aggregate(exact, need, local_set, refurb)
        if a.include_refurb:
            agg["available"] += agg["refurb"]
        results.append({"model": model, "need": need, "agg": agg,
                        "note": note, "rows": exact})

    print()
    bad = print_report(results, show_refurb=not a.include_refurb)

    if a.json_out:
        dump = {}
        for r in results:
            ag = r["agg"]
            dump[r["model"]] = {
                "need": ag["need"], "available": ag["available"],
                "local": ag["local"] if ag["local_known"] else None,
                "refurb": ag["refurb"], "intransit": ag["intransit"],
                "verdict": verdict(ag), "by_inv": ag["by_inv"],
                "note": r["note"],
            }
        text = json.dumps(dump, ensure_ascii=False, indent=1)
        if a.json_out == "-":
            print(text)
        else:
            with open(a.json_out, "w", encoding="utf-8") as f:
                f.write(text)
            print("已写：%s" % a.json_out)

    return 1 if bad else 0


# ---------------------------------------------------------------- 自测
def selftest():
    ok = 0
    fails = []

    def chk(name, cond):
        nonlocal ok
        if cond:
            ok += 1
        else:
            fails.append(name)

    local = {"NC01", "NC02", "NC11", "HNC01", "HNC02", "HNC11"}
    rows = [
        {"model": "TL-X", "invName": "NC01", "qty": 10},
        {"model": "TL-X", "invName": "TP102", "qty": 5},
        {"model": "TL-X", "invName": "HLJ44", "qty": 300},
    ]
    ag = aggregate(rows, 12, local)
    chk("返修仓不计入可用", ag["available"] == 15)
    chk("返修量单独统计", ag["refurb"] == 300)
    chk("本办仓单独统计", ag["local"] == 10)
    chk("够 12 台判够", verdict(ag) == "够(需调拨)")
    chk("本办够 10 台判本办够", verdict(aggregate(rows, 8, local)) == "本办够")

    only_ref = [{"model": "TL-Y", "invName": "HLJ52", "qty": 2}]
    chk("仅返修时判仅返修", verdict(aggregate(only_ref, 2, local)) == "仅返修")
    chk("仅返修可用量为 0", aggregate(only_ref, 2, local)["available"] == 0)

    inc = aggregate(only_ref, 2, local)
    inc["available"] += inc["refurb"]
    chk("--include-refurb 才把返修算进去", inc["available"] == 2)

    chk("型号归一化忽略大小写空格", norm(" tl-x ") == "TL-X")
    ex, near = pick_exact([{"model": "TL-XPQ", "qty": 1},
                           {"model": "tl-x", "qty": 2}], "TL-X")
    chk("精确匹配只留同型号", len(ex) == 1 and ex[0]["qty"] == 2)
    chk("近似型号单独返回", near == ["TL-XPQ"])

    chk("在途只算非返修", aggregate(
        [{"model": "TL-Z", "invName": "TP144", "qty": 1, "orderIntransitNum": 7},
         {"model": "TL-Z", "invName": "HLJ44", "qty": 1, "orderIntransitNum": 99}],
        1, local)["intransit"] == 7)

    # 不假定办事处：拿不到本办仓字典时只给全国口径，且不误判「本办够」
    unk = aggregate(rows, 8, set())
    chk("无本办字典时 local_known=False", unk["local_known"] is False)
    chk("无本办字典时本办量记 0", unk["local"] == 0)
    chk("无本办字典不判本办够", verdict(unk) == "够(需调拨)")
    chk("返修前缀可改", aggregate(
        [{"model": "TL-R", "invName": "RP01", "qty": 5}], 1, local, ("RP",))["refurb"] == 5)

    # 表头识别（不联网，造假 sheet）
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["某项目设备清单"])
        ws.append(["序号", "设备名称", "品牌", "规格型号", "单位", "数量"])
        ws.append([1, "交换机", "TP-LINK", "TL-SG1024D", "台", 3])
        det = detect_columns(ws)
        chk("表头识别：型号列=D", det and det[0] == 4)
        chk("表头识别：数量列=F", det and det[1] == 6)
        chk("表头识别：表头行=2", det and det[2] == 2)
        chk("非字母列位不会误判", detect_columns(ws) is not None)
    except ImportError:
        print("(跳过表头识别测试：未装 openpyxl)")

    print("自测：%d 通过 / %d 失败" % (ok, len(fails)))
    for f in fails:
        print("  FAIL %s" % f)
    print("结果：" + ("全部通过" if not fails else "有失败"))
    return 0 if not fails else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except _cfg.NotConfigured as e:          # crm_base 没配：给指引，不吐 traceback
        print("FATAL %s" % e, file=sys.stderr)
        sys.exit(2)
