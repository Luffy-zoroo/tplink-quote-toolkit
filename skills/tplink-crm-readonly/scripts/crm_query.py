#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TP-LINK 销售 CRM 只读价格查询（价格手册 / 标准价 / 业务折扣）。

用法:
    python crm_query.py TL-NFW6430 TL-TUMS1500 ...     # 精确型号查询
    python crm_query.py --like 停车 --like 授权          # 模糊搜索（找 SKU 用）
    python crm_query.py --fields                         # 只打印 CRM 字段清单

凭据来源（按优先级）:
    1. 命令行 --email / --password
    2. 环境变量 TP_CRM_EMAIL / TP_CRM_PASSWORD
    3. 本机 ~/.workbuddy/MEMORY.md 里 "- **账号**：`xxx`" / "- **密码**：`xxx`"（默认）

只读，不修改任何数据。用法注意：
    - `productModel` 过滤是【模糊匹配】，本脚本会按精确型号再筛一遍（--like 时不做筛选）。
    - 同一型号可能返回多条：`valid=True` 的才是当前有效档；带 `lineInfo` 的可能是阶梯价。
    - `seriesValue='项目机型'` 才有 `businessDiscount`（业务折扣率）。
"""
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
import http.cookiejar
from urllib.request import getproxies

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tpkit_config as _cfg  # noqa: E402

MEMORY = os.path.join(os.path.expanduser("~"), ".workbuddy", "MEMORY.md")


def crm_api():
    """CRM API 根地址（含 /api）。内部系统地址不写死在代码里，从 crm_base 读；
    未配置时抛出 _cfg.NotConfigured，由上层决定报错还是降级到本地备份。"""
    return _cfg.require_base("crm_base", "CRM") + "/api"


def _from_memory():
    try:
        with open(MEMORY, encoding="utf-8") as fh:
            txt = fh.read()
    except OSError:
        return None, None
    email = re.search(r"账号[^\n`]*`([^`]+)`", txt)
    pwd = re.search(r"密码[^\n`]*`([^`]+)`", txt)
    return (email.group(1) if email else None), (pwd.group(1) if pwd else None)


def resolve_creds(argv):
    email = pwd = None
    for i, a in enumerate(argv):
        if a == "--email" and i + 1 < len(argv):
            email = argv[i + 1]
        if a == "--password" and i + 1 < len(argv):
            pwd = argv[i + 1]
    email = email or os.environ.get("TP_CRM_EMAIL")
    pwd = pwd or os.environ.get("TP_CRM_PASSWORD")
    if not email or not pwd:
        m_email, m_pwd = _from_memory()
        email = email or m_email
        pwd = pwd or m_pwd
    if not email or not pwd:
        sys.exit("缺少凭据：请用 --email/--password、环境变量 TP_CRM_*，"
                 f"或写入 {MEMORY}")
    return email, pwd


def make_opener():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(getproxies()),
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=ctx),
    )


def login(op, email, pwd):
    data = urllib.parse.urlencode({"email": email, "password": pwd}).encode()
    req = urllib.request.Request(
        crm_api() + "/login", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "Mozilla/5.0"})
    j = json.loads(op.open(req, timeout=40).read().decode("utf-8"))
    s = j.get("sessionInfo", {})
    return s.get("chineseName"), s.get("officeName")


def find_lines(op, kw, limit=300):
    qs = urllib.parse.urlencode({"start": 0, "limit": limit, "productModel": kw})
    req = urllib.request.Request(crm_api() + "/line/findByPage?" + qs,
                                 headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(op.open(req, timeout=60).read().decode("utf-8"))


def show(rows, header=None):
    if header:
        print(f"\n=== {header}  (命中 {len(rows)} 条)")
    if not rows:
        print("  无记录")
        return
    for x in rows:
        print(f"  {x.get('productModel')} | {x.get('productName')} | "
              f"price={x.get('price')} | bd={x.get('businessDiscount')} | "
              f"series={x.get('seriesValue')} | valid={x.get('valid')} | "
              f"ladder={x.get('ladderPrice')} | line={x.get('lineInfo')}")


def main(argv):
    exact, like = [], []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--like" and i + 1 < len(argv):
            like.append(argv[i + 1]); i += 2; continue
        if a in ("--email", "--password") and i + 1 < len(argv):
            i += 2; continue
        if not a.startswith("--"):
            exact.append(a)
        i += 1

    email, pwd = resolve_creds(argv)
    op = make_opener()
    who, office = login(op, email, pwd)
    print(f"[登录] {who} / {office}")

    if "--fields" in argv:
        j = find_lines(op, exact[0] if exact else "TL-")
        rows = j.get("results", [])
        if rows:
            print("\n字段:", ", ".join(sorted(rows[0].keys())))
        return

    for kw in like:
        show(find_lines(op, kw).get("results", []), f"[模糊] {kw}")
    for kw in exact:
        res = find_lines(op, kw).get("results", [])
        hit = [x for x in res
               if str(x.get("productModel") or "").strip().upper() == kw.strip().upper()]
        show(hit or res, kw)

    if not exact and not like:
        print(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except _cfg.NotConfigured as e:
        sys.exit("配置缺失：%s" % e)
