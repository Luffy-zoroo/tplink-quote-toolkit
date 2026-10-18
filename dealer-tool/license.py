# -*- coding: utf-8 -*-
"""多合一特征库（License 授权）SKU 查询工具。

背景（2026-10-13 用户确认 + 实测）：
  行为管理器（TL-NASG 系列）和防火墙（TL-FW / TL-NFW / YLFW）**共用同一个「多合一」特征库**，
  但具体是哪个 SKU、多少钱一年，**取决于该产品自己的规格**：
      TL-FW-LIS-ALL     (200 元/年) —— 老平台，内存普遍 ≤ 2GB DDR4
      TL-FW-LIS-ALL-E   (800 元/年) —— 新平台，内存普遍 ≥ 4GB DDR4
  权威来源 = 官网产品页规格表里那组「License授权（<SKU>，多合一）」的分组名，
  即 https://www.tp-link.com.cn 产品详情的「规格参数」→「License授权…」分组标题。

  所以**不要按产品线猜、也不要按价格档猜**，一律按型号查官网规格里的 SKU 原文。

用法：
  python license.py TL-NASG6003 TL-FW5800        # 查指定型号（离线缓存优先，缺失才联网）
  python license.py --all                        # 全量刷新安全类（防火墙/行为管理/网关）映射表
  python license.py --refresh TL-FW5900          # 强制联网重查
  python license.py --list                       # 只看本地缓存映射表
  python license.py --price                      # 附上两个 SKU 的年费（读 price_cache 备份/联网）
"""
import argparse
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tpkit_config as cfg  # noqa: E402

# License 映射表走配置；未配置时落到 ~/.workbuddy/tplink-toolkit/data/ 下
CACHE = cfg.data_path('license_map_json')

SKU_LOW = 'TL-FW-LIS-ALL'        # 多合一（老平台）200 元/年
SKU_HIGH = 'TL-FW-LIS-ALL-E'     # 多合一（新平台）800 元/年

# 安全类二级品类的判定（用于 --all 扫描）
SEC_CATS = ('防火墙', '行为管理器', '安全网关', '工业防火墙', '网络安全')


def clean(x):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', html.unescape(x or ''))).strip()


def load_cache():
    if os.path.exists(CACHE):
        try:
            return json.load(open(CACHE, encoding='utf-8'))
        except Exception:
            pass
    return {'_meta': {'source': 'www.tp-link.com.cn 产品规格 → License授权分组',
                      'sku_price': {SKU_LOW: 200, SKU_HIGH: 800}}, 'models': {}}


def save_cache(c):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(c, open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def official_id(model):
    """从本地备份库拿官网产品 ID（officialRelateId）"""
    import store as S
    st = S.load()
    r = (st.get('products') or {}).get(model)
    return (r or {}).get('officialRelateId'), (r or {}).get('cat2')


def parse_official(pi):
    """从官网 productinfo 抽 License 授权信息"""
    try:
        groups = json.loads(pi.get('specification') or '[]')
    except Exception:
        groups = []
    lic_groups, libs = [], []
    for g in groups:
        gname = g.get('spe') or ''
        if 'License' in gname or '授权' in gname:
            lic_groups.append(gname)
            if '多合一' in gname:
                for it in g.get('speItems', []):
                    n = clean(it.get('speItemName'))
                    # 过滤「*」「*参数说明」这类备注行
                    if n and not n.startswith('*') and '说明' not in n:
                        libs.append(n)
    blob = ' '.join(lic_groups)
    if SKU_HIGH in blob:
        sku = SKU_HIGH
    elif SKU_LOW in blob:
        sku = SKU_LOW
    else:
        sku = None                      # 例如 TL-NR4914 系列只写「License授权（多合一）」，没写 SKU
    spot = pi.get('spotlight') or ''
    mem = re.findall(r'\d+\s*GB\s*DDR[IV0-9]{0,4}', spot)
    cpu = re.findall(r'(?:Intel|AMD)?\s*(?:i3|i5|i7|Xeon|ARM|X86|多核|双核)[^，,。<]{0,28}处理器', spot)
    mem_s = mem[0].replace(' ', '') if mem else ''
    # 官网分组名没写 SKU 时，按内存档位推测（2026-10-13 实测规律，需人工确认）
    guess, reason = None, ''
    mm = re.match(r'(\d+)\s*GB', mem_s)
    if mm:
        gb = int(mm.group(1))
        guess = SKU_HIGH if gb >= 4 else SKU_LOW
        reason = '按内存 %s 推测（≥4GB→-E / ≤2GB→无后缀）' % mem_s
    return {
        'sku': sku,
        'sku_guess': guess,
        'sku_guess_reason': reason,
        'license_groups': lic_groups,
        'libs': libs,                   # 多合一涵盖的库清单
        'memory': mem_s,
        'cpu': clean(cpu[0]) if cpu else '',
        'product_name': pi.get('productName') or '',
    }


def lookup(model, force=False, cache=None):
    c = cache if cache is not None else load_cache()
    if not force:
        hit = c['models'].get(model)
        if hit and (hit.get('sku') or hit.get('sku_guess')):
            return hit
    oid, cat2 = official_id(model)
    if not oid:
        rec = {'model': model, 'cat2': cat2, 'sku': None,
               'note': '本地库无官网产品ID，需先在选型平台按型号搜一次入库'}
    else:
        import backup as B
        try:
            pi = B.product_info(oid) or {}
        except Exception as e:
            return {'model': model, 'cat2': cat2, 'sku': None, 'note': '官网抓取失败: %s' % e}
        info = parse_official(pi)
        rec = {'model': model, 'cat2': cat2, 'official_id': oid, **info}
        if not info['sku']:
            if info.get('sku_guess'):
                rec['note'] = '官网分组名未写 SKU → %s' % info['sku_guess_reason']
            else:
                rec['note'] = '官网规格未写明 SKU 且无内存信息 → 需人工确认'
    c['models'][model] = rec
    save_cache(c)
    return rec


def scan_all(force=False):
    import store as S
    st = S.load()
    ps = st.get('products') or {}
    todo = []
    for m, r in sorted(ps.items()):
        c1, c2 = r.get('cat1') or '', r.get('cat2') or ''
        if c2 in ('防火墙', '行为管理器', '安全网关', '工业防火墙') or '防火墙' in c2 or '行为管理' in c2 or '安全网关' in c2:
            todo.append(m)
    c = load_cache()
    print('安全类产品 %d 个，开始核对官网规格…' % len(todo))
    for m in todo:
        rec = lookup(m, force=force, cache=c)
        sku = rec.get('sku') or (('%s*推测' % rec['sku_guess']) if rec.get('sku_guess') else None)
        print('  %-18s %-8s %s' % (m, rec.get('cat2') or '', sku or ('? ' + (rec.get('note') or ''))))
    return c


def sku_price(cache):
    """读 price_cache 本地备份拿两个 SKU 的年费（不联网）"""
    p = cfg.data_path('price_backup_json')
    out = {}
    if not os.path.exists(p):
        return out
    try:
        d = json.load(open(p, encoding='utf-8'))
        prices = d.get('prices') or d.get('products') or d
        for s in (SKU_LOW, SKU_HIGH):
            r = prices.get(s) or {}
            recs = r.get('records') or []
            out[s] = recs[0].get('price') if recs else r.get('price')
    except Exception:
        pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='多合一特征库 SKU 查询')
    ap.add_argument('model', nargs='*', help='型号，可多个')
    ap.add_argument('--all', action='store_true', help='全量扫描安全类产品并刷新映射表')
    ap.add_argument('--refresh', action='store_true', help='忽略缓存强制联网重查')
    ap.add_argument('--list', action='store_true', help='只列本地缓存的映射表')
    ap.add_argument('--price', action='store_true', help='附上 SKU 年费')
    a = ap.parse_args(argv)

    if a.all:
        scan_all(force=a.refresh)
        print('\n映射表已写入 %s' % CACHE)
        return

    c = load_cache()
    models = a.model or (sorted(c['models']) if a.list else [])

    if a.list and not a.model:
        print('%-18s %-10s %-20s %-12s %s' % ('型号', '品类', '多合一SKU', '内存', '涵盖库'))
        for m in models:
            r = c['models'].get(m) or {}
            sku = r.get('sku') or (('%s*推测' % r['sku_guess']) if r.get('sku_guess') else '-')
            print('%-18s %-10s %-20s %-12s %s' % (m, (r.get('cat2') or '')[:10],
                  sku, r.get('memory') or '-', '/'.join(r.get('libs') or [])[:40]))
        return

    if not models:
        ap.print_help()
        return

    pr = sku_price(c) if a.price else {}
    print('%-18s %-10s %-18s %-10s %-7s %s' % ('型号', '品类', '多合一SKU', '内存', '年费', '涵盖库'))
    for m in models:
        r = lookup(m, force=a.refresh, cache=c)
        sku = r.get('sku')
        if not sku and r.get('sku_guess'):
            sku = '%s*推测' % r['sku_guess']          # 带 * 的一律要人工确认
        price = pr.get(r.get('sku') or '', None) if a.price else None
        print('%-18s %-10s %-18s %-10s %-7s %s' % (
            m, (r.get('cat2') or '')[:10], sku or ('? ' + (r.get('note') or '')[:20]),
            r.get('memory') or '-', price if price is not None else '-',
            '/'.join(r.get('libs') or [])[:42]))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
