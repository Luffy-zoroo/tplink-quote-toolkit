"""合作伙伴平台（组织内部系统）→ 本地备份 / 离线选型 CLI。

用法示例：
  python backup.py login                        # 验证账号
  python backup.py refresh                      # ★每周增量刷新（只拉列表，约十几秒）
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
from dealer_api import Dealer, UA, dealer_base    # noqa: E402
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

# 平台**列表行**就带的字段 → 刷新这些不用拉详情（0.29s/条 那步可省）
# 本地字段名 : 平台列表字段名
LIST_FIELDS = {
    'name': 'productName',
    'cat1': 'firstCategoryName',
    'cat2': 'secondCategoryName',
    'productId': 'productId',
    'officialRelateId': 'officialRelateId',
    'brand': 'brandValue',
    'industry': 'projectProduct',      # 行业产品标记（选型优先档要用）
    'dealerProductType': 'dealerProductType',
    'price_untrusted': 'price',        # 平台价不可信，只作量级参考
    'boxNumber': 'boxNumber',
    # ⚠ 平台的 haltProduct 实测基本不更新（2850 个型号 0 条为 True），
    #   所以**不写 `halt`**，只写 `halt_platform` 留作观察。
    #   `halt` 由 enrich.py crm 按 CRM 的 lifeCycle 独占写入，别的脚本不许碰。
    'halt_platform': 'haltProduct',
}


# ---------------- 官网公开规格接口 ----------------
def product_info(official_id, brand='TPLINK'):
    url = ('https://www.tp-link.com.cn/publicservice/productinfo?'
           + urllib.parse.urlencode({'brand': brand, 'productModel': official_id}))
    try:
        ref = dealer_base() + '/'          # 内部平台地址来自配置，不能写死
    except Exception:
        ref = 'https://www.tp-link.com.cn/'  # 未配置时退回官网，够过防盗链
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': ref})
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


def cmd_refresh(d, a):
    """每周增量刷新：只拉列表（十几秒），更新存量状态 + 补新增型号详情。

    为什么不做全量重拉：详情 0.29s/条 × 2000+ 条 ≈ 11 分钟，而规格参数几乎不随时间变。
    本命令只更新「列表行就带」的字段（在售/停产/行业/类目/价格量级），
    新增型号才去拉详情；消失的型号**不删除**，只打 missing_since 标记。

    ⚠ 已知坑（本命令存在的原因）：fetch_rows 的断点续跑按「已有 desc+spec」跳过，
    已入库型号**永远不会被重新抓**，平台后来改的 haltProduct 会丢。
    走列表行更新就绕开了这个坑，不用拉详情也能把停产状态刷对。
    """
    t0 = time.time()
    st = S.load(a.store)
    old = st['products']

    bc = d.brand_category()['result']
    st['taxonomy'] = {'brands': bc.get('brandList'), 'modules': bc.get('moduleList'),
                      'categories': bc.get('categoryList'), 'specs': bc.get('specsList'),
                      'updated': time.strftime('%Y-%m-%d %H:%M:%S')}
    cats = [c for c in (bc.get('categoryList') or []) if c['categoryName'] not in SKIP_CATS]
    if a.module:
        cats = [c for c in cats if c.get('module') in a.module]
    if not cats:
        print('没有匹配的品类'); return

    # ---- 1) 拉全站列表（只要列表，不拉详情）----
    by_model, total_rows = {}, 0
    for c in cats:
        rows = paged_rows(d, cat_id=c['categoryId'], verbose=False)
        total_rows += len(rows)
        for r in rows:
            m = r.get('productModel')
            if m:
                by_model[m] = r
    print('列表拉完：%d 个品类 / %d 行 → 去重 %d 个型号  (%.1fs)'
          % (len(cats), total_rows, len(by_model), time.time() - t0))
    if not by_model:
        print('⚠ 一条列表都没拉到（网络或权限问题），本次不落盘'); return

    new_models = [m for m in by_model if m not in old]
    gone_models = [m for m in old if m not in by_model]

    # ---- 2) 存量型号：只更新列表级字段 ----
    changed = []
    for m, row in by_model.items():
        rec = old.get(m)
        if rec is None:
            continue
        for local_k, plat_k in LIST_FIELDS.items():
            v = row.get(plat_k)
            if local_k in ('industry', 'halt_platform'):
                v = bool(v)                    # 平台 None/False 一律视为「否」
            elif v in (None, '', [], {}):
                continue                       # 平台没给值就别把本地覆盖成空
            ov = rec.get(local_k)
            cmp_old = bool(ov) if local_k in ('industry', 'halt_platform') else ov
            if cmp_old != v:
                changed.append((m, local_k, ov, v))
            rec[local_k] = v
        rec.pop('missing_since', None)         # 重新出现 → 清掉消失标记
        rec['seen'] = time.strftime('%Y-%m-%d')

    # ---- 3) 抓详情 ----
    got, tried = 0, 0
    if a.deep:
        # 全量重抓：store=None 关掉断点续跑（否则已有 desc+spec 的会被整批跳过）
        tried = len(by_model)
        print('深度刷新：全量重抓 %d 个型号（平台详情 + 官网规格），预计 5~10 分钟...'
              % tried, flush=True)
        recs = fetch_rows(d, list(by_model.values()), workers=a.workers,
                          want_spec=not a.no_spec, store=None, verbose=True)
    elif new_models:
        tried = len(new_models)
        print('新增 %d 个型号，抓详情+规格...' % tried, flush=True)
        recs = fetch_rows(d, [by_model[m] for m in new_models],
                          workers=a.workers, want_spec=not a.no_spec, store=st)
    else:
        recs = []
    for rec in recs:
        if S.upsert(st, rec):
            got += 1

    # ---- 4) 消失型号：不删，打标记（防漏页误判）----
    today = time.strftime('%Y-%m-%d')
    marked = []
    skipped_missing = False
    if not a.module and gone_models:
        ratio = len(gone_models) / max(len(old), 1)
        if ratio > 0.10:
            skipped_missing = True             # 一次消失超 10%，大概率是漏页/权限变动
        else:
            for m in gone_models:
                old[m]['missing_since'] = today
                marked.append(m)

    st['_meta']['last_refresh'] = time.strftime('%Y-%m-%d %H:%M:%S')
    if a.deep:
        st['_meta']['last_deep'] = st['_meta']['last_refresh']
    if not a.no_save:
        S.save(st, a.store)

    # ---- 5) 报告 ----
    # 停产只认 CRM（平台 haltProduct 那个字段基本不更新）
    halted = [m for m, r in old.items() if r.get('halt')]
    lacking = [m for m in by_model if m in old and not (old[m].get('spec')
                                                       and old[m].get('desc'))]
    has_raw = sum(1 for r in old.values() if r.get('detail_raw'))
    has_sib = sum(1 for r in old.values() if r.get('siblings'))
    grouped = sum(1 for r in old.values() if any(i.get('group') for i in (r.get('spec') or [])))
    print('---------- 刷新报告 ----------')
    if tried:
        print('抓取详情        : %d/%d 成功（失败 %d）' % (got, tried, tried - got))
    print('新增型号        : %d %s' % (len(new_models), new_models[:10]))
    print('本次未见(下架)  : %d %s' % (len(gone_models), gone_models[:10]))
    if skipped_missing:
        print('  ⚠ 未见比例 %.0f%% > 10%%，疑似漏页/权限变动，**未打标记**，建议重跑一次'
              % (ratio * 100))
    elif marked:
        print('  已打 missing_since=%s（保留记录，选型时提示下架）' % today)
    print('字段变化        : %d 处' % len(changed))
    for m, k, ov, nv in changed[:15]:
        print('   %-30s %-16s %r → %r' % (m, k, ov, nv))
    if len(changed) > 15:
        print('   ...（其余 %d 处省略）' % (len(changed) - 15))
    print('当前停产型号    : %d 个%s' % (len(halted), ('（' + ', '.join(halted[:6]) + '）') if halted else ''))
    print('缺描述/规格     : %d 个%s' % (len(lacking), ('（建议对它们跑 models）') if lacking else ''))
    print('数据完整度      : 完整详情 %d / 同系列 %d / 规格带分组 %d' % (has_raw, has_sib, grouped))
    print('本地总条数      : %d' % len(old))
    print('上次刷新        : %s' % st['_meta']['last_refresh'])
    print('耗时            : %.1fs' % (time.time() - t0))


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
    meta = st.get('_meta', {})
    print('本地备份:', a.store)
    print('产品数  :', len(ps))
    print('含规格  :', sum(1 for r in ps.values() if r.get('spec')))
    print('含描述  :', sum(1 for r in ps.values() if r.get('desc')))
    print('已停产  :', sum(1 for r in ps.values() if r.get('halt')),
          '（CRM 口径；平台 haltProduct 为 True 的 %d 个）'
          % sum(1 for r in ps.values() if r.get('halt_platform')))
    missing = [m for m, r in ps.items() if r.get('missing_since')]
    print('平台已下架:', len(missing), ('（' + ', '.join(missing[:8]) + '）') if missing else '')
    print('渠道/行业:', Counter('行业' if r.get('industry') else '渠道' for r in ps.values()).most_common())
    print('一级品类:', Counter(r.get('cat1') for r in ps.values()).most_common(15))
    print('_meta   :', meta.get('updated'))
    print('上次刷新:', meta.get('last_refresh') or '（从未 refresh，建议跑一次）')


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
            if a.force or (not r.get('desc')) or (not r.get('spec'))]
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


def cmd_tidy(d, a):
    """就地清洗已有记录：把描述里的 HTML 标签压成可读文本。

    2026-10-30 用：`--fast fill` 全量重抓走的是官网 `spotlight`（HTML 原文），
    把 2255 条原本干净的平台描述洗成了 `<ul><li>…`。`store.plain_desc()` 修好了入库侧，
    但已经写进库的脏数据得单独洗一遍，否则要再花 5 分钟重抓一次。
    """
    st = S.load(a.store)
    ps = st['products']
    n = 0
    changed = 0
    for rec in ps.values():
        if rec.get('desc'):
            n += 1
            clean = S.plain_desc(rec['desc'])
            # 清洗后变空的（原文只有 `<p></p>` 这类空壳）不要写回，
            # 否则等于把这条描述删了 —— 2026-10-30 这样丢过 23 条
            if clean and clean != rec['desc']:
                rec['desc'] = clean
                changed += 1
        if rec.get('spotlight'):
            clean = S.plain_desc(rec['spotlight'])
            if clean and clean != rec['spotlight']:
                rec['spotlight'] = clean
    left = sum(1 for r in ps.values() if '<' in (r.get('desc') or ''))
    st['_meta']['desc_tidied'] = time.strftime('%Y-%m-%d %H:%M:%S')
    if a.no_save:
        print('（--no-save，未落盘）清洗 %d / %d 条，仍有 HTML 的 %d 条' % (changed, n, left))
        return
    S.save(st, a.store)
    print('描述清洗：检查 %d 条，改写 %d 条；仍有 HTML 的 %d 条 → %s'
          % (n, changed, left, a.store))


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
    p.add_argument('--force', action='store_true',
                   help='fill 时连已有 desc+spec 的一起重抓（全量刷新官网规格）')
    p.add_argument('--out', default=None)
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ['login', 'taxonomy', 'all', 'refresh', 'stats', 'html', 'fill', 'tidy']:
        s = sub.add_parser(name)
        if name in ('all', 'refresh'):
            s.add_argument('--module', nargs='*', default=None)
        if name == 'refresh':
            s.add_argument('--deep', action='store_true',
                           help='全量重抓详情+规格（数分钟；默认只抓新增型号）')
    for name in ['cat', 'search', 'models', 'show', 'find']:
        s = sub.add_parser(name)
        s.add_argument('keyword', nargs='+' if name in ('models', 'find') else 1)
    a = p.parse_args()

    need_net = a.cmd not in ('stats', 'html', 'show', 'find', 'tidy')
    d = Dealer(*require_creds()) if need_net else Dealer()
    {'login': cmd_login, 'taxonomy': cmd_taxonomy, 'cat': cmd_cat, 'search': cmd_search,
     'models': cmd_models, 'all': cmd_all, 'refresh': cmd_refresh, 'show': cmd_show,
     'find': cmd_find, 'stats': cmd_stats, 'html': cmd_html, 'fill': cmd_fill,
     'tidy': cmd_tidy}[a.cmd](d, a)


if __name__ == '__main__':
    try:
        main()
    except Exception as _e:
        import sys as _sys
        if type(_e).__name__ == 'NotConfigured':
            _sys.exit("配置缺失：%s" % _e)
        raise
