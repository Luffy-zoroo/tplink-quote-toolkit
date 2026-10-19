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
    ps = st['products']
    if not ps:
        # 库是空的要说清楚原因（2026-10-19 实测：路径配错时只报"命中 0 条"，
        # 会被误读成"库里没有这个产品"）。store.load() 在文件不存在时静默返回空库，
        # 这里补一层诊断，不改变上层逻辑。
        p = S.DEFAULT_STORE
        sys.stderr.write(
            '[选型库] 读到 0 条：%s\n'
            '  只可能是这三种原因，按顺序查：\n'
            '    1) 文件不存在 → 数据包里的 数据/2-选型库.json 有没有拷到该位置\n'
            '    2) 路径配错   → ~/.workbuddy/tplink-toolkit.config.json 的 dealer_backup_json\n'
            '    3) 还没备份过 → 跑 backup.py refresh 拉一次（需合作伙伴平台账号）\n'
            '  查当前实际读的是哪个文件：python tpkit_config.py\n' % p)
    return ps


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
