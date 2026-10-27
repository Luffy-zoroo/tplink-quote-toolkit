# -*- coding: utf-8 -*-
"""单型号查询：控标参数 + 四个价 —— 给网页版「查型号」页用。

**只读**：不生成表格、不改任何文件，结果以 JSON 打到 stdout，由网页侧渲染。

参数来源（和 autofill_quote.py 同一套，不另立逻辑）：
  1. 本地控标库 `tplink_kongbiao_data.json`（优先，带 ★ 加分项）
     → src = 「控标库 · <类目>」
  2. 官网产品页 `#smbproductFeature` 的产品特性条目（**不带 ★**）
     → src = 「官网 · <url>」
  3. 两边都没有 → params 为空 + notes 写明「没查到」，**不编、不猜**

价格只报 CRM 里真实存在的四个数，**本脚本不做任何折扣计算**：
  标准价 price、业务折扣率 businessDiscount、
  片区折扣率（BIZDISC_TO_PQ 查表）、总部折扣率（BIZDISC_TO_HQ 查表）。
渠道机（非「项目机型」）没有 businessDiscount → 三个折扣率都是 null。

用法：
  python one_query.py TL-NS650-24G4F
  python one_query.py TL-NS650-24G4F TL-SG1008D --no-price
  python one_query.py TL-NS650-24G4F --kb "D:/.../tplink_kongbiao_data.json"

输出（stdout，UTF-8 JSON）：
  {"ok": true, "crm": "刘泽义 / 南昌办", "crm_err": null,
   "items": [{"model": ..., "product_model": ..., "name": ..., "series": ...,
              "src": ..., "params": [...], "price": 2300, "bd": "0.39",
              "pq": 0.34, "hq": 0.32, "cands": 1, "notes": [...]}]}
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autofill_quote as A                                      # noqa: E402


def pick_price(rows):
    """从 CRM 记录里选一条单台价，返回 (记录 or None, 候选条数)。

    同一型号可能有多条 `valid=True` 的记录，其中一条是**整箱价**
    （`boxNumber=1`，价 = 单台 × 箱数），其余是单台价（如 boxNumber=32）。
    实测 TL-SG1008D：3168（box=1）和 99（box=32）两条都 valid，
    取第一条会把价格放大 32 倍，所以多条时取**最低价**，
    并把候选条数报上去，让网页提示「有多条记录，人工核一下」。
    """
    vs = [r for r in rows if r.get('valid')] or list(rows)
    vs = [r for r in vs if r.get('price') not in (None, '')]
    if not vs:
        return None, 0
    vs.sort(key=lambda r: float(r.get('price') or 0))
    return vs[0], len(vs)


def query_one(model, idx, op, why=''):
    """查一个型号。op 为 None 表示不查价格；why 说明为什么没查（有就写明）。"""
    model = model.strip()
    out = {'model': model, 'product_model': None, 'name': '', 'series': '',
           'src': None, 'src_kind': None, 'url': None, 'params': [],
           'price': None, 'bd': None,
           'pq': None, 'hq': None, 'cands': 0, 'notes': []}

    # ---------------- 参数 ----------------
    ent = A.kb_lookup(idx, [model]) if idx else None
    if ent and ent.get('param_items'):
        raw = A.kb_params(ent)
        out['src'] = '控标库 · %s' % (ent.get('category') or ent.get('model') or '有记录')
        out['src_kind'] = 'kb'
        out['product_model'] = ent.get('model')
        out['params'] = [x for x in A.sanitize_multiline(A.kb_params(ent)).split('\n') if x.strip()]
    else:
        items, url = A.web_feature(model)
        if items:
            out['src'] = '控标库未命中，采用官网描述'
            out['src_kind'] = 'web'
            out['url'] = url
            out['params'] = items
            out['notes'].append('这条不是控标参数：控标库里没有这个型号，'
                                '参数取自官网产品页「产品特性」，**不带 ★ 加分项**，'
                                '正式投标前要人工过一遍')
        else:
            out['src'] = '控标库未命中，官网也没抓到'
            out['src_kind'] = 'none'
            out['notes'].append('参数留空 —— 控标库和官网都没有这个型号，不编造')

    # ---------------- 价格 ----------------
    if op is None:
        # 有 why = 本来要查、但没查成（登录失败等）→ 必须写明原因，
        # 不能笼统说"没连 CRM"，否则用户会以为是自己没勾。
        out['notes'].append(('价格没查成：%s' % why) if why
                            else '这次没勾「算价格」，只出了参数')
        return out

    rows = A.crm_price(op, model)
    rec, n = pick_price(rows)
    out['cands'] = n
    if not rec:
        out['notes'].append('CRM 里没查到这个型号的价格（可能已下架或型号写法不同）')
        return out

    out['product_model'] = rec.get('productModel') or model
    out['name'] = rec.get('productName') or ''
    out['series'] = rec.get('seriesValue') or ''
    out['price'] = rec.get('price')
    out['bd'] = rec.get('businessDiscount')

    if out['bd'] not in (None, ''):
        k = str(out['bd']).strip()
        out['pq'] = A.BIZDISC_TO_PQ.get(k)
        out['hq'] = A.BIZDISC_TO_HQ.get(k)
        if out['pq'] is None or out['hq'] is None:
            out['notes'].append('业务折扣率 %s 不在换算表里（表里只有 0.45 / 0.43 / 0.39 / 0.32），'
                                '片区/总部折扣率算不出来' % k)
    else:
        out['notes'].append('这个型号没有业务折扣率（不属于「项目机型」），'
                            '片区/总部折扣率也无从换算')
    if out['product_model'].strip().upper() != model.upper():
        out['notes'].append('型号是按前缀匹配到的：%s' % out['product_model'])
    if n > 1:
        out['notes'].append('CRM 里这个型号有 %d 条有效价格记录（可能含整箱价），'
                            '这里取的是最低那条，重要单子请人工核一下' % n)
    return out


def main(argv):
    ap = argparse.ArgumentParser(description='单型号查询：控标参数 + 四个价（只读，输出 JSON）')
    ap.add_argument('models', nargs='+', help='型号，可给多个')
    ap.add_argument('--kb', default=A.DEFAULT_KB, help='控标库 JSON 路径')
    ap.add_argument('--no-price', action='store_true', help='不连 CRM，只查参数')
    ap.add_argument('--email', default='', help='CRM 账号（默认读配置）')
    ap.add_argument('--password', default='', help='CRM 密码（默认读配置）')
    a = ap.parse_args(argv)

    idx = A.load_kb(a.kb)[0] if a.kb else {}

    op, crm_name, crm_err = None, None, None
    if not a.no_price:
        email, pwd = A.resolve_crm_creds(a.email, a.password)
        if not email or not pwd:
            crm_err = '没配置 CRM 账号密码，价格查不了（参数不受影响）'
        else:
            try:
                op, info = A.crm_login(email, pwd)
                si = (info or {}).get('sessionInfo') or {}
                crm_name = '%s / %s' % (si.get('chineseName') or '', si.get('officeName') or '')
                crm_name = crm_name.strip(' /')
            except Exception as e:
                crm_err = 'CRM 登录失败：%s' % e
                op = None

    items = [query_one(m, idx, op, crm_err or '') for m in a.models]
    json.dump({'ok': True, 'crm': crm_name, 'crm_err': crm_err,
               'kb_count': len(idx), 'items': items},
              sys.stdout, ensure_ascii=False)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
