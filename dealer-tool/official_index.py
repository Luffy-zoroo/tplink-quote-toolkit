#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""官网产品索引 —— 用官方公开接口遍历 productId，建立「型号 → 官网页面」反查表。

为什么需要这个（2026-10-30）
---------------------------
选型库里有 528 个型号的 `officialRelateId` 是空的（平台没给），
于是官网抓不到规格，备用控标参数就是 0 条。可这些型号官网明明有页面
（TL-SC6020 太阳能套装、TL-AH400S 门禁一体机、TL-NVR8011G-20L 录像机…）。

平台那个按型号查官网页面的接口（findProductPageByModel）本账号无权限（-1002），
但官网自己有个公开接口：

    https://www.tp-link.com.cn/publicservice/productinfo?brand=TPLINK&productModel=<id>

参数名虽然叫 productModel，实际传的是**官网自增 id**。id 连续、上限约 4700，
所以能遍历一遍把全站产品扫下来，反查出型号对应的 id。

顺带能发现「官网有、平台没有」的型号（平台还没上架的新品）——这类最值得关注。

用法
----
    python official_index.py build                 # 全量建立索引（约 4 分钟）
    python official_index.py build --brand MERCURY # 只扫水星
    python official_index.py stats                 # 看索引覆盖与差异
    python official_index.py match                 # 给库里缺 officialRelateId 的型号回填
    python official_index.py match --no-save       # 只看会补上哪些，不落盘
    python official_index.py --selftest

索引落在选型库同目录的 `tplink_official_index.json`（数据文件，不进仓库）。
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backup as B          # noqa: E402
import store as S           # noqa: E402

INDEX_NAME = 'tplink_official_index.json'
INDEX_VERSION = 1

# 各品牌的 id 上界（实测 2026-10-30：TPLINK 4600 有、4700 起全空；水星 800 内有效）
ID_RANGE = {
    'TPLINK':  (1, 4700),
    'MERCURY': (1, 900),
    'FAST':    (1, 900),
}


def index_path(store_path=None):
    """索引与选型库放同一目录，跟着配置走。"""
    base = os.path.dirname(store_path or S.DEFAULT_STORE)
    return os.path.join(base, INDEX_NAME)


def load_index(path=None):
    p = path or index_path()
    if not os.path.exists(p):
        return {'_meta': {}, 'items': {}}
    try:
        d = json.load(open(p, encoding='utf-8'))
    except Exception:
        return {'_meta': {}, 'items': {}}
    d.setdefault('_meta', {})
    d.setdefault('items', {})
    return d


def save_index(idx, path=None):
    p = path or index_path()
    tmp = p + '.tmp'
    json.dump(idx, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
    os.replace(tmp, p)


# ---------------- 抓一个 id ----------------
def fetch_id(pid, brand):
    """返回 (key, 记录) 或 (key, None)。key = 'brand:id'，便于增量续跑。"""
    key = '%s:%d' % (brand, pid)
    try:
        d = B.product_info(str(pid), brand)
    except Exception:
        return key, None
    if not isinstance(d, dict) or not d.get('productModel'):
        return key, None
    # ⚠ 水星/迅捷把规格放在 specification2，TP 两者都有；specification 可能是 HTML 表格
    spec = d.get('specification2') or d.get('specification') or []
    if isinstance(spec, str):
        t = spec.strip()
        try:
            spec = json.loads(t) if t.startswith('[') else []
        except Exception:
            spec = []
    groups = [str(g.get('spe') or '') for g in (spec or []) if isinstance(g, dict)]
    return key, {
        'id': pid,
        'brand': brand,
        'model': str(d.get('productModel')).strip(),
        'name': str(d.get('productName') or '').strip(),
        'groups': len(groups),
        'named_groups': sum(1 for g in groups if g),
        'items': sum(len(g.get('speItems') or []) for g in (spec or [])
                     if isinstance(g, dict)),
        'spotlight': len(str(d.get('spotlight') or '')),
    }


def cmd_build(a):
    idx = load_index(a.index)
    items = idx['items']
    brands = [a.brand] if a.brand else list(ID_RANGE)
    t0 = time.time()
    for brand in brands:
        lo, hi = ID_RANGE.get(brand, (1, 900))
        if a.frm:
            lo = a.frm
        if a.to:
            hi = a.to
        # 待抓：没抓过 / 记过空 / 抓过但没规格（规格字段找错了要能自我修正）
        todo = []
        for i in range(lo, hi + 1):
            k = '%s:%d' % (brand, i)
            v = items.get(k)
            if v is None and k in items:
                continue                     # 确认空 id，不再抓
            if v and v.get('groups'):
                continue                     # 已有且有规格
            todo.append(i)
        print('%s：范围 %d~%d，已有 %d，本次待抓 %d'
              % (brand, lo, hi, (hi - lo + 1) - len(todo), len(todo)), flush=True)
        if not todo:
            continue
        done = [0]
        hit = [0]
        lock = threading.Lock()      # ⚠ 多线程同时改 items / 序列化会
                                    #   RuntimeError: dictionary changed size during iteration

        def work(pid):
            k, v = fetch_id(pid, brand)
            with lock:
                if v:
                    items[k] = v
                    hit[0] += 1
                else:
                    items[k] = None          # 记空，下次不重复抓
                done[0] += 1
                if done[0] % 500 == 0:
                    print('   %d/%d 命中 %d  (%.0fs)'
                          % (done[0], len(todo), hit[0], time.time() - t0), flush=True)
                    save_index(idx, a.index)
            return v

        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            list(ex.map(work, todo))
        save_index(idx, a.index)

    real = {k: v for k, v in items.items() if v}
    idx['_meta'].update({
        'version': INDEX_VERSION,
        'built_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'total_ids': len(real),
        'brands': sorted({v['brand'] for v in real.values()}),
    })
    save_index(idx, a.index)
    print()
    print('---------- 索引完成 ----------')
    print('有效产品 : %d 个（扫描 %d 个 id）' % (len(real), len(items)))
    for b in sorted({v['brand'] for v in real.values()}):
        n = sum(1 for v in real.values() if v['brand'] == b)
        ns = sum(1 for v in real.values() if v['brand'] == b and v['groups'])
        print('  %-8s %4d 个，其中带规格表 %d' % (b, n, ns))
    print('耗时     : %.0f 秒' % (time.time() - t0))
    print('索引     : %s' % (a.index or index_path()))


def _by_model(idx):
    out = {}
    for v in idx['items'].values():
        if not v:
            continue
        out.setdefault(_nz(v['model']), []).append(v)
    return out


def _nz(s):
    """型号归一：大写、去空格、全角括号转半角。"""
    t = str(s or '').upper().strip()
    for a, b in (('（', '('), ('）', ')'), ('　', ' '), (' ', '')):
        t = t.replace(a, b)
    return t


# 平台型号常带这些销售包装后缀，官网型号没有（如 `MS05CP 件装` → `MS05CP`）
_SUFFIX = re.compile(r'(件装|套装|\(\d+[只个]装\))\s*$')


def _match_model(model, by):
    """型号 → 索引候选列表（可能多条，同型号多个 id）。匹配不上返回 None。"""
    k = _nz(model)
    if k in by:
        return by[k]
    for t in (_SUFFIX.sub('', k).strip(),
              re.sub(r'(件装|套装)', '', k).strip()):
        if t and t in by:
            return by[t]
    return None


# 官网 id 空间里混着大量作废/测试条目，不能当新品推荐
_JUNK = ('作废', 'TEST', '测试', '删除', '临时', '废弃')


def _is_junk(model, name=''):
    t = (str(model or '') + str(name or '')).upper()
    return any(k in t for k in _JUNK)


def cmd_stats(a):
    idx = load_index(a.index)
    st = S.load(a.store or S.DEFAULT_STORE)
    ps = st['products']
    real = {k: v for k, v in idx['items'].items() if v}
    m = idx.get('_meta', {})
    print('索引文件 : %s' % (a.index or index_path()))
    print('建立时间 : %s' % (m.get('built_at') or '未建'))
    print('官网产品 : %d 个' % len(real))
    print()
    by = _by_model(idx)
    miss = [r for r in ps.values() if not r.get('officialRelateId')]
    hit = [r for r in miss if _match_model(r['model'], by)]
    print('选型库缺官网ID : %d' % len(miss))
    print('  索引能反查到  : %d' % len(hit))
    for r in hit[:15]:
        v = _match_model(r['model'], by)[0]
        print('    %-30s → id=%-5d 规格分组 %-2d 条 %d' %
              (r['model'][:30], v['id'], v['groups'], v['items']))
    if len(hit) > 15:
        print('    …（其余 %d 个省略）' % (len(hit) - 15))
    print()
    # 官网有、平台没有（多半是老型号/海外型号/未上架，只列有规格的以免噪音）
    have = {_nz(r['model']) for r in ps.values()}
    extra = [c[0] for k, c in by.items() if k not in have]
    with_spec = [v for v in extra
                 if v.get('groups') and not _is_junk(v['model'], v.get('name'))]
    junk = sum(1 for v in extra if _is_junk(v['model'], v.get('name')))
    print('官网有、平台没有 : %d 个（其中带规格表 %d，作废/测试 %d）'
          % (len(extra), len(with_spec), junk))
    for v in sorted(with_spec, key=lambda x: -x['items'])[:10]:
        print('    %-32s id=%-5d %-2d 组 %-3d 条 %s'
              % (v['model'][:32], v['id'], v['groups'], v['items'], v['name'][:24]))


def cmd_match(a):
    idx = load_index(a.index)
    st = S.load(a.store or S.DEFAULT_STORE)
    ps = st['products']
    by = _by_model(idx)
    miss = [r for r in ps.values() if not r.get('officialRelateId')]
    pairs = []
    for r in miss:
        cand = _match_model(r['model'], by)
        if not cand:
            continue
        if len(cand) > 1:
            # 同型号多个 id：挑规格最全的
            cand = sorted(cand, key=lambda v: (-v['groups'], -v['items']))
        pairs.append((r, cand[0]))
    print('缺官网ID %d 个，索引能补 %d 个' % (len(miss), len(pairs)))
    if not pairs:
        return
    for r, v in pairs[:20]:
        print('  %-32s → id=%-5d 分组 %-2d 条 %-3d %s'
              % (r['model'][:32], v['id'], v['groups'], v['items'], v['name'][:22]))
    if a.no_save:
        print()
        print('（--no-save，未回填）')
        return

    # 回填 ID，然后用正常流程抓一次详情+规格
    for r, v in pairs:
        r['officialRelateId'] = v['id']
        r['official_url'] = 'https://www.tp-link.com.cn/product_%d.html' % v['id']
        r['id_from'] = 'official_index'

    print()
    print('回填 %d 个官网 ID，按官网 id 直接抓规格（不需要平台连接）...' % len(pairs))
    n_spec = n_desc = n_ok = 0

    def work(pair):
        r, v = pair
        brand = B.BRAND_MAP.get(r.get('brand'), 'TPLINK')
        try:
            off = B.product_info(str(v['id']), brand)
        except Exception:
            return None
        if not off:
            return None
        # ⚠ make_record 里 industry 是 bool(...)，没有详情会被算成 False 覆盖掉原值，
        #   所以必须把原记录的行业标记带上
        rec = S.make_record({'productModel': r['model'],
                             'brandValue': r.get('brand'),
                             'officialRelateId': v['id'],
                             'projectProduct': r.get('industry')}, {}, off)
        return rec

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        recs = list(ex.map(work, pairs))
    for rec in recs:
        if not rec:
            continue
        if rec.get('spec'):
            n_spec += 1
        if rec.get('desc'):
            n_desc += 1
        if S.upsert(st, rec):
            n_ok += 1
    S.save(st, a.store or S.DEFAULT_STORE)
    print('写入记录     : %d / %d' % (n_ok, len(pairs)))
    print('  其中有规格 : %d' % n_spec)
    print('  其中有描述 : %d' % n_desc)
    print('落盘：%s' % (a.store or S.DEFAULT_STORE))
    print()
    print('下一步：python enrich.py params   # 用新抓到的规格重新生成备用参数')


# ---------------- 自检 ----------------
def selftest():
    ok = fail = 0

    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print('  OK   %s' % name)
        else:
            fail += 1
            print('  FAIL %s' % name)

    print('--- 型号归一 ---')
    chk('去空格', _nz(' TL-SC6020 ') == 'TL-SC6020')
    chk('大写', _nz('tl-sc6020') == 'TL-SC6020')
    chk('全角括号', _nz('TL-IPC445FP-W4（2.8mm）') == 'TL-IPC445FP-W4(2.8MM)')
    chk('None 安全', _nz(None) == '')
    chk('大小写不同视为同一型号',
        _nz('TL-SC6020') == _nz('tl-sc6020'))

    print('--- 索引读写 ---')
    import tempfile
    tmpd = tempfile.mkdtemp()
    p = os.path.join(tmpd, INDEX_NAME)
    chk('不存在的索引返回空结构', load_index(p)['items'] == {})
    save_index({'_meta': {'built_at': 'x'}, 'items': {'TPLINK:1': {'model': 'A'}}}, p)
    chk('存了能读回', load_index(p)['items']['TPLINK:1']['model'] == 'A')
    chk('meta 保留', load_index(p)['_meta']['built_at'] == 'x')

    print('--- 索引路径跟着选型库走 ---')
    chk('与选型库同目录',
        os.path.dirname(index_path('/a/b/tplink_dealer_backup.json'))
        == os.path.dirname('/a/b/tplink_dealer_backup.json'))
    chk('文件名固定', os.path.basename(index_path('/a/b/x.json')) == INDEX_NAME)

    print('--- 按型号索引 ---')
    idx = {'items': {'a': {'model': 'TL-SC6020'}, 'b': {'model': 'tl-sc6020'},
                     'c': None}}
    m = _by_model(idx)
    chk('同型号归并到一起', len(m[_nz('TL-SC6020')]) == 2)
    chk('空记录不进索引', len(m) == 1)

    print()
    print('自检：OK %d / FAIL %d' % (ok, fail))
    return 0 if fail == 0 else 1


def main():
    ap = argparse.ArgumentParser(description='官网产品索引（型号 → 官网页面反查）')
    ap.add_argument('cmd', nargs='?', choices=['build', 'stats', 'match'],
                    help='build=遍历建索引  stats=看覆盖  match=回填缺ID的型号')
    ap.add_argument('--brand', help='只处理某个品牌（TPLINK/MERCURY/FAST）')
    ap.add_argument('--from', dest='frm', type=int, help='起始 id')
    ap.add_argument('--to', type=int, help='结束 id')
    ap.add_argument('--workers', type=int, default=10)
    ap.add_argument('--index', help='索引文件路径（默认与选型库同目录）')
    ap.add_argument('--store', default=None, help='选型库路径')
    ap.add_argument('--no-save', action='store_true')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()

    if a.selftest:
        sys.exit(selftest())
    if not a.cmd:
        ap.print_help()
        sys.exit(1)

    {'build': cmd_build, 'stats': cmd_stats, 'match': cmd_match}[a.cmd](a)


if __name__ == '__main__':
    main()
