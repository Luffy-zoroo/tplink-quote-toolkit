#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""选型库增强：CRM 在产状态 + 标准价/业务折扣 + 备用控标参数。

为什么要单独一个脚本（而不是塞进 backup.py）：
  backup.py 负责「平台详情 + 官网规格」，全量深抓 20~35 分钟；
  这里负责「CRM 侧信息」和「派生数据」，秒级增量。
  两边解耦，任何一侧失败都不用重跑另一侧。

产出的三个字段都是**参考值，不是配单口径**（用户 2026-10-30 明确要求）：
  rec['crm']          面价 / 批发价 / 现价 / 业务折扣 —— 只用来粗判“这机器贵不贵”，
                      配单时必须实时查 CRM，折扣口径每次现问。
  rec['param_backup'] 官网规格自动拼出来的**备用**控标参数 —— 未经人工提炼，
                      控标库没有这个型号时才参考，有的话以控标库为准。
  rec['crm_lifecycle'] 在产状态（CRM 权威），会同步修正 rec['halt']。

价格字段口径（2026-10-30 拿全量 8397 条价格手册实测，不是猜的）：
  catalogPrice         面价（标准目录价）。**通常**最高，但 2797 条三档齐全的记录里，
                       有 584 条（20.9%）批发价反而高于面价 → 别当铁律
  wholesalePrice       批发价
  price                价格手册现价。配单只看这个；实测 ≈ 面价 × catalogPriceRatio，
                       但有 39 条现价略高于面价 → 别用面价乘系数反推
  businessDiscount     业务折扣，**独立字段**。值域 0.1~1，8397 条里只有 1048 条有值。
                       ⚠ 它 ≠ price/面价（实测中位差 0.05，1032 条里只有 79 条相等），
                       不能拿它反算现价。真实口径（是折扣下限？项目申报折扣？）需向商务确认，
                       所以这里原样存字段值，不替它编语义。

用法：
  python enrich.py crm        # 拉 CRM 价格手册全量 → 补在产状态 + 价格 + 折扣
  python enrich.py params     # 用库内 官网描述+规格 生成备用控标参数
  python enrich.py stats      # 覆盖度报告
  python enrich.py all        # 两步一起
  python enrich.py --selftest
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store as S           # noqa: E402
import tpkit_config as cfg  # noqa: E402

CRM_NOTE = ('价格取自 CRM 价格手册快照，仅供粗判档次，非配单口径。'
            '配单时必须实时查 CRM，折扣口径每次现问。')
PARAM_NOTE = ('备用参数：由官网规格自动拼接，未经人工提炼，非配单首选。'
              '控标库有该型号时以控标库为准。')

LC_CN = {
    'PRODUCTION': '在产(量产)',
    'HALT': '停产',
    'HALT_WARNING': '停产预警(在产中)',
}

# productLabel 是多值逗号串（如 'PROJECT_PRODUCT,CAN_CUSTOM'），
# 比 seriesValue 更权威：实测有 3 条 productLabel 标了项目机型但 seriesValue 没标。
LABEL_CN = {
    'PROJECT_PRODUCT': '项目机型',
    'CUSTOMER_CUSTOM': '客户定制',
    'CAN_CUSTOM': '可定制',
    'DEMAND_PRODUCTION': '按需生产',
    'BARGAIN_CLEARANCE': '特价清仓',
    'VALUE_ADD': '增值产品',
    'ECOMMERCE_VERSION': '电商版',
}

# 官网规格里这些分组对控标没用，拼进去只是噪音。
# ⚠ 必须用 startswith 而不是 in：实测官网分组叫「图像与视频参数」，
#   用 `'视频' in g` 会把摄像机的核心参数整段丢掉（2026-10-30 踩到，
#   TL-IPC435F-W4 的 11 条规格只剩 4 条）。
_SKIP_GROUP = ('文档', '订购', '包装', '相关', '下载', '认证', '售后',
               '服务政策', '常见问题', '视频', '说明书')


def _nz(s):
    """型号归一：去空格/全角空格/连字符/下划线，转大写。"""
    return re.sub(r'[\s\u3000\-_]', '', str(s or '')).upper()


# ---------------------------------------------------------------- CRM 侧
def load_crm_query():
    """复用 crm-readonly 技能的登录/请求逻辑，不在这里复制一份凭据处理。"""
    cands = [
        os.path.join(HERE, '..', 'skills', 'tplink-crm-readonly', 'scripts', 'crm_query.py'),
        os.path.expanduser('~/.workbuddy/skills/tplink-crm-readonly/scripts/crm_query.py'),
    ]
    for p in cands:
        p = os.path.abspath(p)
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location('_crm_query', p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise SystemExit('找不到 crm_query.py（需要 tplink-crm-readonly 技能）')


def pull_all_lines(Q, op, page=500, verbose=True):
    """全量拉价格手册。

    `productModel` 传空串 = 不过滤（2026-10-30 实测可用），一次拿全量 8400 条，
    比逐个型号查（2841 次请求）快两个数量级。
    """
    out, start, total = [], 0, None
    while True:
        qs = urllib.parse.urlencode({'start': start, 'limit': page, 'productModel': ''})
        req = urllib.request.Request(Q.crm_api() + '/line/findByPage?' + qs,
                                     headers={'User-Agent': 'Mozilla/5.0'})
        try:
            j = json.loads(op.open(req, timeout=90).read().decode('utf-8'))
        except Exception as e:
            print('   第 %d 页拉取失败：%s' % (start // page + 1, e), flush=True)
            break
        rows = j.get('results') or []
        total = j.get('totalCounts') or total
        out.extend(rows)
        if verbose:
            print('   已拉 %d/%s' % (len(out), total), flush=True)
        if not rows or len(rows) < page:
            break
        if total and len(out) >= total:
            break
        start += page
    return out, total


def pick_rep(lines):
    """同一型号常有多条（不同 OU / 区域 / 价格档），选一个代表值。

    优先级：valid=True > 有标准价 > 有业务折扣。
    全部原始行仍留在 `variants` 计数里，需要明细可回 CRM 查。
    """
    def score(r):
        bd = r.get('businessDiscount')
        return (bool(r.get('valid')),
                r.get('price') is not None,
                1 if bd not in (None, '', 1, '1', 1.0) else 0)
    return sorted(lines, key=score, reverse=True)[0]


def _labels(lines):
    """productLabel 拆成集合（多值逗号串）。"""
    s = set()
    for r in lines:
        for x in str(r.get('productLabel') or '').split(','):
            x = x.strip().upper()
            if x:
                s.add(x)
    return sorted(s)


def build_crm_block(lines, fetched_at):
    rep = pick_rep(lines)
    lc = str(rep.get('lifeCycle') or '').strip().upper()
    bd = next((x.get('businessDiscount') for x in lines
               if x.get('businessDiscount') not in (None, '')), None)
    labels = _labels(lines)
    is_proj = ('PROJECT_PRODUCT' in labels
               or '项目机型' in str(rep.get('seriesValue') or ''))
    return {
        'hit': True,
        'lifecycle': lc,
        'lifecycle_cn': LC_CN.get(lc) or (rep.get('lifeCycleMeaning') or ''),
        # 数据来源：CRM 价格手册 /line/findByPage 全量分页
        'tax_name': rep.get('taxName'),
        'series': rep.get('seriesValue'),
        'labels': labels,
        'label_cn': [LABEL_CN.get(x, x) for x in labels],
        'is_project_model': is_proj,
        'is_custom': bool({'CUSTOMER_CUSTOM', 'CAN_CUSTOM'} & set(labels)),
        'is_bargain': 'BARGAIN_CLEARANCE' in labels,
        # 价格三档，原样存 CRM 字段名对应值，不换算、不合并
        'catalog_price': rep.get('catalogPrice'),
        'catalog_ratio': rep.get('catalogPriceRatio'),
        'wholesale_price': rep.get('wholesalePrice'),
        'wholesale_ratio': rep.get('wholesalePriceRatio'),
        'price': rep.get('price'),
        'ladder_price': rep.get('ladderPrice'),
        'line_info': rep.get('lineInfo'),
        # 业务折扣：独立字段，不可用来反算现价（见模块 docstring）
        'business_discount': bd,
        'unit': rep.get('unit'),
        'box_number': rep.get('boxNumber'),
        'order_types': rep.get('orderTypesName'),
        'line_valid': bool(rep.get('valid')),
        'product_valid': rep.get('productValid'),
        'variants': len(lines),
        'fetched_at': fetched_at,
        'note': CRM_NOTE,
    }


def cmd_crm(a):
    Q = load_crm_query()
    op = Q.make_opener()
    email, pwd = Q.resolve_creds([])
    who, office = Q.login(op, email, pwd)
    print('[登录] %s / %s' % (who, office))

    t0 = time.time()
    rows, total = pull_all_lines(Q, op, page=a.page, verbose=not a.quiet)
    print('价格手册全量：%d 条（接口报 %s），耗时 %.1fs' % (len(rows), total, time.time() - t0))
    if not rows:
        raise SystemExit('一条都没拉到，先确认 CRM 网络（代理/内网）')

    by_exact, by_norm = {}, {}
    for r in rows:
        m = str(r.get('productModel') or '').strip()
        if not m:
            continue
        by_exact.setdefault(m, []).append(r)
        by_norm.setdefault(_nz(m), []).append(r)

    st = S.load(a.store)
    ps = st['products']
    ts = time.strftime('%Y-%m-%d %H:%M:%S')

    hit = miss = 0
    nulled, unhalted = [], []
    lc_count = {}
    plat_halt = 0          # 平台 detail_raw.haltProduct 说停产的条数
    for model, rec in ps.items():
        if (rec.get('detail_raw') or {}).get('haltProduct'):
            plat_halt += 1
        lines = by_exact.get(model) or by_norm.get(_nz(model))
        if not lines:
            miss += 1
            rec['crm'] = {'hit': False, 'note': 'CRM 价格手册无此型号（可能未入册或为套装/定制名）',
                          'fetched_at': ts}
            continue
        hit += 1
        blk = build_crm_block(lines, ts)
        rec['crm'] = blk
        rec['crm_lifecycle'] = blk['lifecycle'] or None
        lc_count[blk['lifecycle_cn'] or '未知'] = lc_count.get(blk['lifecycle_cn'] or '未知', 0) + 1
        # CRM 是权威口径，而且**独占** `halt` 这个字段：
        # 不管之前是什么来源，这里一律按 lifecycle 重写一遍。
        # 2026-10-30 踩过：`backup.py` 的平台字段恒为 False，抓一次详情就把
        # 30 个 CRM 判停产的型号的 halt 刷回 False，页面还照常显示「在产」。
        want_halt = blk['lifecycle'] == 'HALT'
        if want_halt and not rec.get('halt'):
            nulled.append(model)
        elif not want_halt and rec.get('halt'):
            unhalted.append(model)
        rec['halt'] = want_halt
        rec['halt_from'] = 'crm'

    st['_meta']['crm_enriched'] = ts
    if a.no_save:
        print('（--no-save，未落盘）')
    else:
        S.save(st, a.store)

    print('---------- CRM 增强结果 ----------')
    print('选型库型号      : %d' % len(ps))
    print('CRM 命中        : %d（%.1f%%）' % (hit, 100.0 * hit / max(len(ps), 1)))
    print('CRM 未命中      : %d' % miss)
    for k, v in sorted(lc_count.items(), key=lambda x: -x[1]):
        print('   %-16s %d' % (k, v))
    print('平台说在产→CRM 说停产 : %d %s' % (len(nulled), nulled[:8]))
    print('平台说停产→CRM 说在产 : %d %s' % (len(unhalted), unhalted[:8]))
    print('⚠ 平台 haltProduct 说停产的只有 %d 条 —— 这个字段基本不更新，'
          '停产判断只能以 CRM 为准' % plat_halt)
    print('落盘：%s' % a.store)


# ---------------------------------------------------------------- 备用参数
def load_kb_index():
    """控标库型号索引（归一型号 → 条目）。控标库有 = 配单首选，不用备用参数。"""
    try:
        p = cfg.data_path('kongbiao_json')
    except Exception as e:
        print('⚠ 控标库路径取不到（%s），全部按“无控标参数”处理' % e)
        return {}
    if not p or not os.path.exists(p):
        print('⚠ 控标库不存在：%s，全部按“无控标参数”处理' % p)
        return {}
    with open(p, encoding='utf-8') as f:
        data = json.load(f)
    idx = {}
    for e in (data if isinstance(data, list) else data.get('products') or []):
        m = e.get('model') or e.get('product_model')
        if m:
            idx[_nz(m)] = e
    return idx


def gen_backup_params(rec):
    """官网规格（三级：group / name / values）→ 控标参数条目。

    控标库的格式是 `[{'text': '1、...', 'is_bonus': False}]`，这里对齐它，
    但 **is_bonus 一律 False** —— 官网不知道哪个是加分项，不能瞎标。
    """
    spec = rec.get('spec') or []
    if not spec:
        return None
    items = []
    for it in spec:
        g = str(it.get('group') or '').strip()
        name = str(it.get('name') or '').strip()
        vals = [str(v).strip() for v in (it.get('values') or []) if str(v).strip()]
        if not name or not vals:
            continue
        if g and any(g.startswith(k) for k in _SKIP_GROUP):
            continue
        txt = '%s：%s' % (name, ' / '.join(vals))
        if len(txt) < 4:
            continue
        items.append({'text': '%d、%s' % (len(items) + 1, txt),
                      'is_bonus': False, 'group': g or None})
    if not items:
        return None
    return {
        'items': items,
        'count': len(items),
        'source': 'official_spec',
        'official_url': rec.get('official_url'),
        'from_kongbiao': False,
        'note': PARAM_NOTE,
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }


def cmd_params(a):
    kb = load_kb_index()
    st = S.load(a.store)
    ps = st['products']
    n_kb = n_gen = n_none = 0
    for model, rec in ps.items():
        ent = kb.get(_nz(model))
        if ent:
            # 控标库那份就是这个型号的「首选参数」。**不在这里复制 items**：
            # 复制一份等于同一段文字存两处，控标库改了这边不动 → 两份口径打架。
            # 只记条数，要用时回控标库读。
            pitems = ent.get('param_items') or []
            rec['param_backup'] = {
                'items': [],
                'count': len(pitems),
                'source': 'kongbiao',
                'from_kongbiao': True,
                'note': '控标库已有此型号 —— 配单首选控标库那份参数，'
                        '这里不存副本（避免两份口径打架），要用请查控标库。',
                'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
            n_kb += 1
            continue
        blk = gen_backup_params(rec)
        if blk:
            rec['param_backup'] = blk
            n_gen += 1
        else:
            rec['param_backup'] = {'items': [], 'count': 0, 'source': None,
                                   'from_kongbiao': False,
                                   'note': '官网也没有可用规格，需人工补参数'}
            n_none += 1

    st['_meta']['param_enriched'] = time.strftime('%Y-%m-%d %H:%M:%S')
    if not a.no_save:
        S.save(st, a.store)

    print('---------- 备用控标参数 ----------')
    print('选型库型号        : %d' % len(ps))
    print('控标库已有（首选）: %d' % n_kb)
    print('官网生成（备用）  : %d' % n_gen)
    print('两边都没有        : %d %s' % (n_none, '（需人工补）' if n_none else ''))
    print('落盘：%s' % a.store)


# ---------------------------------------------------------------- 统计
def cmd_stats(a):
    st = S.load(a.store)
    ps = st['products']
    n = len(ps)
    from collections import Counter
    lc = Counter()
    has_price = has_bd = proj = has_cat = 0
    pb_kb = pb_gen = pb_none = 0
    plat_halt = 0
    for rec in ps.values():
        if (rec.get('detail_raw') or {}).get('haltProduct'):
            plat_halt += 1
        c = rec.get('crm') or {}
        if c.get('hit'):
            lc[c.get('lifecycle_cn') or '未知'] += 1
            if c.get('price') is not None:
                has_price += 1
            if c.get('catalog_price') is not None:
                has_cat += 1
            if c.get('business_discount') not in (None, ''):
                has_bd += 1
            if c.get('is_project_model'):
                proj += 1
        else:
            lc['CRM 无此型号'] += 1
        p = rec.get('param_backup') or {}
        if p.get('from_kongbiao'):
            pb_kb += 1
        elif p.get('count'):
            pb_gen += 1
        elif p:
            pb_none += 1

    print('本地备份  : %s' % a.store)
    print('型号总数  : %d' % n)
    print('含规格/描述: %d / %d' % (sum(1 for r in ps.values() if r.get('spec')),
                                   sum(1 for r in ps.values() if r.get('desc'))))
    print()
    print('--- CRM 在产状态（权威口径）---')
    for k, v in lc.most_common():
        print('  %-18s %d' % (k, v))
    print('  平台 haltProduct 说停产: %d  ⚠ 该字段基本不更新，判停产只能看 CRM' % plat_halt)
    # 一致性自查：`halt` 必须和 CRM 的 lifecycle 完全一致。
    # 对不上说明有别的脚本写过 `halt`（平台抓取曾干过这事，见模块 docstring）。
    mism = [r['model'] for r in ps.values()
            if (r.get('crm') or {}).get('hit')
            and bool(r.get('halt')) != ((r['crm'].get('lifecycle') == 'HALT'))]
    if mism:
        print('  ⚠ %d 个型号 halt 与 CRM 不一致（跑一遍 enrich.py crm 即可修正）：%s'
              % (len(mism), mism[:6]))
    else:
        print('  halt 与 CRM lifecycle 一致 ✅')
    print()
    print('--- 价格（备选参考，非配单口径）---')
    print('  有面价          : %d' % has_cat)
    print('  有现价          : %d' % has_price)
    print('  有业务折扣      : %d' % has_bd)
    print('  项目机型        : %d' % proj)
    print()
    print('--- 备用控标参数 ---')
    print('  控标库首选      : %d（只记条数，不存副本）' % pb_kb)
    print('  官网生成(备用)  : %d' % pb_gen)
    print('  无              : %d' % pb_none)
    meta = st.get('_meta', {})
    print()
    print('platform 深抓   : %s' % (meta.get('last_deep') or '-'))
    print('平台列表刷新    : %s' % (meta.get('last_refresh') or meta.get('last_deep') or '-'))
    print('CRM 增强        : %s' % (meta.get('crm_enriched') or '未跑'))
    print('备用参数        : %s' % (meta.get('param_enriched') or '未跑'))


def cmd_all(a):
    cmd_crm(a)
    print()
    cmd_params(a)
    print()
    cmd_stats(a)


# ---------------------------------------------------------------- 自检
def _selftest():
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
    chk('去连字符', _nz('TL-IPC544EP-W4') == 'TLIPC544EPW4')
    chk('空格与全角', _nz(' TL IPC544EP-W4 ') == 'TLIPC544EPW4')
    chk('大小写', _nz('tl-ipc544ep-w4') == 'TLIPC544EPW4')
    chk('None 安全', _nz(None) == '')

    print('--- 代表值选取 ---')
    lines = [{'valid': False, 'price': 100, 'businessDiscount': None, 'lifeCycle': 'HALT',
              'catalogPrice': 999, 'productLabel': 'BARGAIN_CLEARANCE'},
             {'valid': True, 'price': 200, 'businessDiscount': '0.39',
              'lifeCycle': 'PRODUCTION', 'catalogPrice': 500,
              'productLabel': 'PROJECT_PRODUCT,CAN_CUSTOM', 'seriesValue': '项目机型'}]
    chk('valid=True 优先', pick_rep(lines)['price'] == 200)
    chk('停产标记取到', pick_rep(lines)['lifeCycle'] == 'PRODUCTION')

    print('--- CRM 块 ---')
    b = build_crm_block(lines, 'now')
    chk('lifecycle 归一', b['lifecycle'] == 'PRODUCTION')
    chk('中文映射', b['lifecycle_cn'] == '在产(量产)')
    chk('折扣取到', b['business_discount'] == '0.39')
    chk('变体计数', b['variants'] == 2)
    chk('带备选说明', '非配单口径' in b['note'])
    chk('面价与现价分开存', b['catalog_price'] == 500 and b['price'] == 200)
    chk('productLabel 拆多值', b['labels'] == ['BARGAIN_CLEARANCE', 'CAN_CUSTOM',
                                               'PROJECT_PRODUCT'])
    chk('项目机型按 label 判', b['is_project_model'] is True)
    chk('可定制也认', b['is_custom'] is True)
    chk('中文标签', '项目机型' in b['label_cn'])
    chk('清仓标记（取全部行）', b['is_bargain'] is True)

    print('--- 停产判定 ---')
    halt_lines = [{'valid': True, 'price': 1, 'lifeCycle': 'HALT', 'productLabel': ''}]
    hb = build_crm_block(halt_lines, 'now')
    chk('HALT 中文', hb['lifecycle_cn'] == '停产')
    chk('HALT 不是项目机', hb['is_project_model'] is False)
    warn = build_crm_block([{'valid': True, 'price': 1, 'lifeCycle': 'HALT_WARNING',
                             'productLabel': None}], 'now')
    chk('停产预警单独一档', warn['lifecycle_cn'] == '停产预警(在产中)')
    chk('label 为空不报错', warn['labels'] == [])

    print('--- 备用参数生成 ---')
    rec = {'spec': [{'group': '基本参数', 'name': '端口', 'values': ['5个千兆']},
                    {'group': '图像与视频参数', 'name': '补光', 'values': ['2颗红外+2颗白光']},
                    {'group': '订购信息', 'name': '包装', 'values': ['1台']},
                    {'group': '文档下载', 'name': '说明书', 'values': ['1份']},
                    {'group': '', 'name': '空值', 'values': []}],
           'official_url': 'https://example/x.html'}
    blk = gen_backup_params(rec)
    # 「图像与视频参数」是摄像机核心参数，绝不能因为名字里带「视频」就被跳过
    chk('跳过噪音分组', blk and blk['count'] == 2)
    chk('「图像与视频参数」不被误杀',
        blk and any('补光' in i['text'] for i in blk['items']))
    chk('编号带顿号', blk and blk['items'][0]['text'].startswith('1、'))
    chk('is_bonus 一律 False', blk and blk['items'][0]['is_bonus'] is False)
    chk('标为备用', blk and blk['from_kongbiao'] is False)
    chk('无规格返回 None', gen_backup_params({'spec': []}) is None)

    print('--- 描述清洗（store.plain_desc）---')
    chk('HTML 列表转分隔', S.plain_desc('<ul><li>A</li><li>B</li></ul>') == 'A / B')
    chk('斜杠不被误伤', S.plain_desc('1/2.8英寸 DC/PoE') == '1/2.8英寸 DC/PoE')
    chk('纯文本原样返回', S.plain_desc('已清洗 / 不动') == '已清洗 / 不动')
    chk('实体反转义', S.plain_desc('<p>a&nbsp;&amp;b</p>') == 'a &b')
    chk('孤立的 < 去掉', S.plain_desc('IPv6< / 支持Web') == 'IPv6 / 支持Web')
    chk('小于号是真值要留', S.plain_desc('用时<2min') == '用时<2min')
    chk('空值安全', S.plain_desc(None) == '' and S.plain_desc('') == '')

    print()
    print('自检：OK %d / FAIL %d' % (ok, fail))
    return 0 if fail == 0 else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--store', default=S.DEFAULT_STORE)
    p.add_argument('--page', type=int, default=500, help='CRM 分页大小')
    p.add_argument('--quiet', action='store_true')
    p.add_argument('--no-save', action='store_true')
    p.add_argument('--selftest', action='store_true')
    sub = p.add_subparsers(dest='cmd')
    for name in ('crm', 'params', 'stats', 'all'):
        sub.add_parser(name)
    a = p.parse_args()

    if a.selftest:
        sys.exit(_selftest())
    if not a.cmd:
        p.print_help()
        return
    {'crm': cmd_crm, 'params': cmd_params, 'stats': cmd_stats, 'all': cmd_all}[a.cmd](a)


if __name__ == '__main__':
    try:
        main()
    except Exception as _e:
        if type(_e).__name__ == 'NotConfigured':
            sys.exit('配置缺失：%s' % _e)
        raise
