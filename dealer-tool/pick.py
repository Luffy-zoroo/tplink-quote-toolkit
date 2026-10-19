# -*- coding: utf-8 -*-
"""选型检索助手：在本地 dealer 备份库中按关键词/品类检索候选型号。

用法：
    pick.py 关键词1 关键词2 ...            # 全库 AND 检索（型号/名称/描述/规格全文）
    pick.py --cat 交换机 --kw 万兆         # 限品类
    pick.py --model LT-XXX                 # 精确看某型号全部规格
"""
import sys, re, json, os, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as S

NET_CATS = None


def sm(r):
    """规格 -> dict(name -> ' / '.join(values))"""
    d = {}
    for it in (r.get('spec') or []):
        d.setdefault(it['name'], []).extend(it['values'] or [])
    return {k: ' / '.join(v) for k, v in d.items()}


def blob(r):
    parts = [r.get('model') or '', r.get('name') or '', r.get('desc') or '',
             r.get('cat1') or '', r.get('cat2') or '']
    # 规格大分组名（如「图像与视频参数」）也进检索范围，2026-10-19 起 spec 才带上 group
    for it in (r.get('spec') or []):
        parts.append('%s %s=%s' % (it.get('group') or '', it.get('name') or '',
                                   ' / '.join(it.get('values') or [])))
    return '\n'.join(parts)


def load():
    st = S.load()
    return st['products']


def search(kws, cat=None, brand=None, industry=None, limit=40, kbs=None):
    ps = load()
    out = []
    for m, r in ps.items():
        if cat and cat not in ((r.get('cat1') or '') + '/' + (r.get('cat2') or '')):
            continue
        if brand and r.get('brand') != brand:
            continue
        if industry is not None and bool(r.get('industry')) != industry:
            continue
        b = blob(r)
        if kws and not all(k in b for k in kws):
            continue
        if kbs and not all(k in b for k in kbs):
            continue
        out.append(r)
    out.sort(key=lambda r: (r.get('brand') != 'TP', r.get('model') or ''))
    return out[:limit]


def brief(r, keys=None):
    s = sm(r)
    head = '%-26s %-6s %-6s %-14s %-18s %s' % (
        (r.get('model') or '')[:26], r.get('brand'), '行业' if r.get('industry') else '渠道',
        (r.get('cat1') or '')[:14], (r.get('cat2') or '')[:18], (r.get('name') or '')[:40])
    line2 = '   desc: ' + (r.get('desc') or '').replace('\n', ' / ')[:150]
    lines = [head, line2]
    if keys:
        for k in keys:
            if k == 'siblings':          # 同系列型号（detail_raw.productGroupList 提取）
                sib = [x.get('model') for x in (r.get('siblings') or [])]
                if sib:
                    lines.append('   同系列: %s' % ' / '.join(sib))
            elif k in s:
                lines.append('   %s: %s' % (k, s[k][:160]))
    return '\n'.join(lines)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('kw', nargs='*')
    ap.add_argument('--cat')
    ap.add_argument('--brand')
    ap.add_argument('--industry', type=int, default=None)
    ap.add_argument('--limit', type=int, default=40)
    ap.add_argument('--keys', default='')
    ap.add_argument('--model')
    ap.add_argument('--show', action='store_true')
    a = ap.parse_args()
    if a.model:
        ps = load()
        r = ps.get(a.model)
        if not r:
            cand = [m for m in ps if a.model.upper() in m.upper()]
            print('未精确命中，相近型号：', cand[:20])
        else:
            print(json.dumps({k: v for k, v in r.items() if k != 'spotlight'},
                             ensure_ascii=False, indent=1))
        sys.exit()
    keys = [k for k in a.keys.split(',') if k]
    res = search(a.kw, cat=a.cat, brand=a.brand, industry=a.industry, limit=a.limit)
    print('命中 %d 条（显示前 %d）\n' % (len(res), a.limit))
    for r in res:
        print(brief(r, keys))
        print()
