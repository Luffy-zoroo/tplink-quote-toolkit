"""合作伙伴平台（组织内部系统）→ 本地备份 / 离线选型 CLI。

用法示例：
  python backup.py login                        # 验证账号
  python backup.py taxonomy                     # 备份品类树与规格筛选项
  python backup.py cat 交换机                   # 抓某品类全部产品（含描述+规格）入库
  python backup.py search NS650                 # 按关键词搜（默认入库）
  python backup.py models TL-NS650-24G4F        # 按型号抓（入库）
  python backup.py all                          # 整站备份（跳过“特价/清仓”）
  python backup.py show TL-NS650-24G4F          # 离线查看已备份型号
  python backup.py find 万兆 堆叠               # 离线按关键词搜本地备份
  python backup.py stats                        # 本地备份统计
  python backup.py html                         # 生成单文件离线查看器
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dealer_api import Dealer, BASE, UA            # noqa: E402
import store as S                                  # noqa: E402

# 凭据不写死在代码里（2026-10-18 安全整改）：
# 优先级：环境变量 TP_DEALER_PHONE / TP_DEALER_PASSWORD
#       → 配置文件 ~/.workbuddy/tplink-toolkit.config.json 的 dealer 段
try:
    from tpkit_config import dealer_creds as _dealer_creds
    _CFG_PHONE, _CFG_PASSWORD = _dealer_creds()
except Exception:                                   # 配置模块缺失也不能崩
    _CFG_PHONE, _CFG_PASSWORD = "", ""

PHONE = os.environ.get("TP_DEALER_PHONE", "").strip() or _CFG_PHONE
PASSWORD = os.environ.get("TP_DEALER_PASSWORD", "").strip() or _CFG_PASSWORD


def require_creds():
    """拿不到凭据时给明确指引，而不是抛 401。"""
    if not PHONE or not PASSWORD:
        raise SystemExit(
            "未配置合作伙伴平台账号。请任选一种方式：\n"
            "  1) 在 ~/.workbuddy/tplink-toolkit.config.json 的 dealer 段填 phone / password\n"
            "  2) 设环境变量 TP_DEALER_PHONE / TP_DEALER_PASSWORD\n"
            "字段说明见 config.example.json。")
    return PHONE, PASSWORD
BRAND_MAP = {'TP': 'TPLINK', 'MERCURY': 'MERCURY', 'FAST': 'FAST'}
SKIP_CATS = {'特价/清仓'}


# ---------------- 官网公开规格接口 ----------------
def product_info(official_id, brand='TPLINK'):
    url = ('https://www.tp-link.com.cn/publicservice/productinfo?'
           + urllib.parse.urlencode({'brand': brand, 'productModel': official_id}))
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': BASE + '/'})
    txt = urllib.request.urlopen(req, timeout=40).read().decode('utf-8', 'replace')
    if not txt.startswith('var PRODUCT_INFO='):
        return {}
    return json.loads(txt[len('var PRODUCT_INFO='):].rstrip().rstrip(';'))


def rows_of(res):
    out = []
    r = (res or {}).get('result') or {}
    for g in (r.get('results') or []):
        out.extend(g if isinstance(g, list) else [g])
    return out, r


# ---------------- 抓取 ----------------
def fetch_one(d, row, want_spec=True, retry=2):
    """抓一个产品的详情 + 官网规格，返回本地记录"""
    pid = row.get('productId')
    for i in range(retry + 1):
        try:
            det = (d.detail(pid) or {}).get('result') or {}
            break
        except Exception as e:
            if i == retry:
                det = {}
    off = {}
    oid = det.get('officialRelateId') or row.get('officialRelateId')
    if want_spec and oid:
        brand = BRAND_MAP.get(det.get('brandValue') or row.get('brandValue'), 'TPLINK')
        for i in range(retry + 1):
            try:
                off = product_info(oid, brand)
                break
            except Exception:
                if i == retry:
                    off = {}
    return S.make_record(row, det, off)


def fetch_rows(d, rows, workers=5, want_spec=True, verbose=True, store=None):
    if store is not None:                      # 断点续跑：已抓过 desc+spec 的跳过
        have = store.get('products', {})
        rows = [r for r in rows
                if not (have.get(r.get('productModel'), {}).get('desc')
                        and have.get(r.get('productModel'), {}).get('spec'))]
    recs, done = [], [0]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one, d, r, want_spec): r for r in rows}
        for f in futs:
            pass
        for f in futs:
            try:
                rec = f.result()
            except Exception:
                continue
            if rec:
                recs.append(rec)
            done[0] += 1
            if verbose and done[0] % 25 == 0:
                print('   ... %d/%d' % (done[0], len(rows)), flush=True)
    return recs


def paged_rows(d, cat_id=None, search=None, brand_id=None, page_size=100, cap=None, verbose=True):
    out, start = [], 0
    while True:
        res = d.find_products(start=start, limit=page_size, firstCategoryId=cat_id,
                              searchValue=search or '', brandId=brand_id)
        rows, r = rows_of(res)
        if not rows:
            break
        out.extend(rows)
        total = r.get('totalCounts') or 0
        start += page_size
        if verbose:
            print('   拉取 %d/%d' % (len(out), total), flush=True)
        if start >= total or (cap and len(out) >= cap):
            break
    return out


# ---------------- 命令 ----------------
def cmd_login(d, a):
    r = d.login(*require_creds())
    print(json.dumps(r, ensure_ascii=False))
    print('checkSession:', json.dumps(d.check_session(), ensure_ascii=False)[:200])


def cmd_taxonomy(d, a):
    st = S.load(a.store)
    r = d.brand_category()['result']
    st['taxonomy'] = {'brands': r.get('brandList'),
                      'modules': r.get('moduleList'),
                      'categories': r.get('categoryList'),
                      'specs': r.get('specsList'),
                      'updated': time.strftime('%Y-%m-%d %H:%M:%S')}
    S.save(st, a.store)
    n1 = len(r.get('categoryList') or [])
    n2 = sum(len(c.get('children') or []) for c in r.get('categoryList') or [])
    print('品类树已备份：一级 %d 个 / 二级 %d 个 / 规格筛选项 %d 个 → %s'
          % (n1, n2, len(r.get('specsList') or []), a.store))


def cmd_cat(d, a):
    key = a.keyword[0]
    st = S.load(a.store)
    bc = d.brand_category()['result']
    cats = bc.get('categoryList') or []
    tgt = None
    for c in cats:
        if str(c['categoryId']) == str(key) or key in (c['categoryName'] or ''):
            tgt = c
            break
    if not tgt:
        print('没找到品类：%s' % key)
        print('可选：', ', '.join(c['categoryName'] for c in cats))
        return
    print('品类：%s(%s)' % (tgt['categoryName'], tgt['categoryId']))
    rows = paged_rows(d, cat_id=tgt['categoryId'], cap=a.limit)
    print('共 %d 个产品，开始抓详情+规格...' % len(rows))
    recs = fetch_rows(d, rows, workers=a.workers, want_spec=not a.no_spec, store=st)
    for rec in recs:
        S.upsert(st, rec)
    S.save(st, a.store)
    print('入库 %d 条（含规格 %d 条）→ %s'
          % (len(recs), sum(1 for r in recs if r.get('spec')), a.store))


def cmd_search(d, a):
    key = a.keyword[0]
    st = S.load(a.store)
    rows = paged_rows(d, search=key, cap=a.limit)
    print('搜索命中 %d 条' % len(rows))
    for x in rows[:30]:
        print('  %-28s %-34s %s' % (x.get('productModel'), x.get('productName'),
                                    '行业' if x.get('projectProduct') else '渠道'))
    if a.no_save or not rows:
        return
    recs = fetch_rows(d, rows, workers=a.workers, want_spec=not a.no_spec, store=st)
    for rec in recs:
        S.upsert(st, rec)
    S.save(st, a.store)
    print('入库 %d 条 → %s' % (len(recs), a.store))


def cmd_models(d, a):
    st = S.load(a.store)
    rows = []
    for m in a.keyword:
        rows_found, _ = rows_of(d.find_products(searchValue=m, limit=50))
        hit = [x for x in rows_found if x.get('productModel') == m] or rows_found[:1]
        rows.extend(hit)
    seen, uniq = set(), []
    for x in rows:
        if x['productId'] not in seen:
            seen.add(x['productId'])
            uniq.append(x)
    print('匹配 %d 条：%s' % (len(uniq), [x.get('productModel') for x in uniq]))
    recs = fetch_rows(d, uniq, workers=a.workers, want_spec=not a.no_spec, store=st)
    for rec in recs:
        S.upsert(st, rec)
    S.save(st, a.store)
    print('入库 %d 条 → %s' % (len(recs), a.store))


def cmd_all(d, a):
    st = S.load(a.store)
    bc = d.brand_category()['result']
    st['taxonomy'] = {'brands': bc.get('brandList'), 'modules': bc.get('moduleList'),
                      'categories': bc.get('categoryList'), 'specs': bc.get('specsList')}
    cats = [c for c in (bc.get('categoryList') or [])
            if c['categoryName'] not in SKIP_CATS]
    if a.module:
        cats = [c for c in cats if c.get('module') in a.module]
    print('待备份品类 %d 个' % len(cats))
    grand = 0
    for c in cats:
        rows = paged_rows(d, cat_id=c['categoryId'], verbose=False)
        if not rows:
            continue
        recs = fetch_rows(d, rows, workers=a.workers, want_spec=not a.no_spec, verbose=False, store=st)
        for rec in recs:
            S.upsert(st, rec)
        grand += len(recs)
        print('%-16s %3d 个 → 累计 %d' % (c['categoryName'], len(recs), grand), flush=True)
        S.save(st, a.store)          # 每个品类落一次盘，中断可续
    S.save(st, a.store)
    print('完成：共入库 %d 条 → %s' % (grand, a.store))


# ---------------- 离线 ----------------
def cmd_show(d, a):
    st = S.load(a.store)
    rec = st['products'].get(a.keyword[0])
    if not rec:
        print('本地没有 %s（先跑 search/models 抓一次）' % a.keyword[0])
        return
    print('型号   :', rec['model'])
    print('名称   :', rec['name'])
    print('分类   :', rec.get('cat1'), '/', rec.get('cat2'), '| 品牌', rec.get('brand'))
    print('渠道/行业:', '行业产品' if rec.get('industry') else '渠道产品',
          '| 平台价(不可信):', rec.get('price_untrusted'))
    print('官网   :', rec.get('official_url'))
    print('--- 产品描述 ---')
    print(rec.get('desc') or '(无)')
    print('--- 规格参数 ---')
    for it in (rec.get('spec') or []):
        print('  %-14s %s' % (it['name'], ' / '.join(it['values'])))
    if not rec.get('spec'):
        print('  (无规格数据)')


def cmd_find(d, a):
    st = S.load(a.store)
    kws = [w.lower() for w in a.keyword]
    hit = []
    for m, rec in st['products'].items():
        hay = ' '.join([str(rec.get('model', '')), str(rec.get('name', '')),
                        str(rec.get('cat1', '')), str(rec.get('cat2', '')),
                        str(rec.get('desc', '') or '')]
                       + ['%s %s' % (i['name'], ' '.join(i['values']))
                          for i in (rec.get('spec') or [])]).lower()
        if all(k in hay for k in kws):
            hit.append(rec)
    print('本地命中 %d 条：' % len(hit))
    for rec in hit[:40]:
        print('  %-28s %-32s %s' % (rec['model'], rec['name'],
                                    '行业' if rec.get('industry') else '渠道'))


def cmd_stats(d, a):
    st = S.load(a.store)
    ps = st['products']
    from collections import Counter
    print('本地备份:', a.store)
    print('产品数  :', len(ps))
    print('含规格  :', sum(1 for r in ps.values() if r.get('spec')))
    print('含描述  :', sum(1 for r in ps.values() if r.get('desc')))
    print('渠道/行业:', Counter('行业' if r.get('industry') else '渠道' for r in ps.values()).most_common())
    print('一级品类:', Counter(r.get('cat1') for r in ps.values()).most_common(15))
    print('_meta   :', st.get('_meta', {}).get('updated'))


def fetch_one_fast(row, retry=2):
    """只要官网那一次请求：拿 spotlight（描述）+ specification（规格）。
    库内已经有型号/名称/品类等元数据，不必再打平台详情。"""
    oid = row.get('officialRelateId')
    if not oid:
        return None
    brand = BRAND_MAP.get(row.get('brandValue'), 'TPLINK')
    off = {}
    for i in range(retry + 1):
        try:
            off = product_info(oid, brand)
            break
        except Exception:
            if i == retry:
                return None
    return S.make_record(row, {}, off)


def fetch_rows_fast(rows, workers=8, verbose=True):
    recs, done = [], [0]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_one_fast, r) for r in rows]
        for f in futs:
            try:
                rec = f.result()
            except Exception:
                rec = None
            if rec:
                recs.append(rec)
            done[0] += 1
            if verbose and done[0] % 100 == 0:
                print('   ... %d/%d' % (done[0], len(rows)), flush=True)
    return recs


def cmd_fill(d, a):
    """补齐缺失字段：库内已有但 desc/spec 为空的型号，重新抓一遍"""
    st = S.load(a.store)
    lack = [r for r in st['products'].values()
            if (not r.get('desc')) or (not r.get('spec'))]
    if a.limit:
        lack = lack[:a.limit]
    rows = [{'productId': r['productId'], 'productModel': r['model'],
             'productName': r.get('name'), 'brandValue': r.get('brand'),
             'firstCategoryName': r.get('cat1'), 'secondCategoryName': r.get('cat2'),
             'projectProduct': r.get('industry'),
             'officialRelateId': r.get('officialRelateId')}
            for r in lack if r.get('productId')]
    print('待补齐 %d 条（fast=%s），开始...' % (len(rows), a.fast), flush=True)
    recs = (fetch_rows_fast(rows, workers=a.workers) if a.fast
            else fetch_rows(d, rows, workers=a.workers, want_spec=not a.no_spec))
    n = 0
    for rec in recs:
        old = st['products'].get(rec['model'])
        if old and not (rec.get('desc') or rec.get('spec')):
            continue                      # 抓回来还是空，就别覆盖
        S.upsert(st, rec)
        n += 1
        if n % 200 == 0:
            S.save(st, a.store)
            print('   已补齐 %d' % n, flush=True)
    S.save(st, a.store)
    print('补齐 %d 条 → %s' % (n, a.store))


def cmd_html(d, a):
    st = S.load(a.store)
    tmpl = os.path.join(HERE, 'viewer_template.html')
    data = json.dumps({'products': st['products'],
                       'taxonomy': st.get('taxonomy', {}),
                       'meta': st.get('_meta', {})}, ensure_ascii=False)
    if not os.path.exists(tmpl):
        print('缺少 viewer_template.html'); return
    with open(tmpl, encoding='utf-8') as f:
        html = f.read()
    out = a.out or os.path.join(os.path.dirname(a.store), 'tplink选型备份.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html.replace('/*__DATA__*/', 'window.__DATA__=' + data + ';'))
    print('已生成离线查看器:', out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--store', default=S.DEFAULT_STORE)
    p.add_argument('--workers', type=int, default=5)
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--no-spec', action='store_true', help='不抓官网规格（快）')
    p.add_argument('--no-save', action='store_true')
    p.add_argument('--fast', action='store_true', help='fill 时只打官网接口（快一倍）')
    p.add_argument('--out', default=None)
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ['login', 'taxonomy', 'all', 'stats', 'html', 'fill']:
        s = sub.add_parser(name)
        if name == 'all':
            s.add_argument('--module', nargs='*', default=None)
    for name in ['cat', 'search', 'models', 'show', 'find']:
        s = sub.add_parser(name)
        s.add_argument('keyword', nargs='+' if name in ('models', 'find') else 1)
    a = p.parse_args()

    need_net = a.cmd not in ('stats', 'html', 'show', 'find')
    d = Dealer(*require_creds()) if need_net else Dealer()
    {'login': cmd_login, 'taxonomy': cmd_taxonomy, 'cat': cmd_cat, 'search': cmd_search,
     'models': cmd_models, 'all': cmd_all, 'show': cmd_show, 'find': cmd_find,
     'stats': cmd_stats, 'html': cmd_html, 'fill': cmd_fill}[a.cmd](d, a)


if __name__ == '__main__':
    try:
        main()
    except Exception as _e:
        import sys as _sys
        if type(_e).__name__ == 'NotConfigured':
            _sys.exit("配置缺失：%s" % _e)
        raise
