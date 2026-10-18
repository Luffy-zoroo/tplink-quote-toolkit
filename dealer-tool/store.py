"""普联合作伙伴平台 · 本地备份存储。

设计要点（2026-10-13 建立）：
- **缓存即备份**：凡是看过「产品描述 / 规格参数」的型号，都写进本地 JSON，下次免登录直接用。
- 键 = 型号；写盘用临时文件 + os.replace，避免写坏。
- 价格字段照抄但**标记为不可信**（用户明确：平台价格不真实）。
- 与控标库同目录，路径稳定，跨会话可查。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tpkit_config as cfg  # noqa: E402

# 选型备份库走配置；未配置时落到 ~/.workbuddy/tplink-toolkit/data/ 下
DEFAULT_STORE = cfg.data_path('dealer_backup_json')
STORE_VERSION = 1


def _empty():
    return {
        '_meta': {'version': STORE_VERSION, 'source': 'dealer platform',
                  'created': time.strftime('%Y-%m-%d %H:%M:%S'),
                  'note': '平台价格不真实，price 仅作渠道/行业与量级参考；规格参数取自官网公开接口'},
        'taxonomy': {},
        'products': {},
    }


def load(path=DEFAULT_STORE):
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return _empty()


def save(store, path=DEFAULT_STORE):
    store['_meta']['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    store['_meta']['count'] = len(store.get('products', {}))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def flatten_spec(spec_raw):
    """规格原文（JSON 串或 list）→ [{'group':..,'name':..,'values':[..]}]"""
    if not spec_raw:
        return []
    data = spec_raw
    if isinstance(spec_raw, str):
        try:
            data = json.loads(spec_raw)
        except Exception:
            return []
    out = []
    for grp in data or []:
        gname = grp.get('speGroupName') or grp.get('groupName') or ''
        for it in (grp.get('speItems') or []):
            vals = it.get('speItemValues') or []
            if isinstance(vals, str):
                vals = [vals]
            out.append({'group': gname, 'name': it.get('speItemName'),
                        'values': [str(v) for v in vals]})
    return out


def make_record(row, detail=None, official=None):
    """把平台列表行 + 详情 + 官网 PRODUCT_INFO 合成一条本地记录"""
    detail = detail or {}
    official = official or {}
    spec = flatten_spec(official.get('specification2') or official.get('specification'))
    if not spec:
        spec = flatten_spec(detail.get('specification'))
    return {
        'model': detail.get('productModel') or row.get('productModel'),
        'name': detail.get('productName') or row.get('productName'),
        'productId': detail.get('productId') or row.get('productId'),
        'officialRelateId': detail.get('officialRelateId', row.get('officialRelateId')),
        'brand': detail.get('brandValue') or row.get('brandValue'),
        'cat1': detail.get('firstCategoryName') or row.get('firstCategoryName'),
        'cat2': detail.get('secondCategoryName') or row.get('secondCategoryName'),
        'industry': bool(detail.get('projectProduct', row.get('projectProduct'))),
        'dealerProductType': detail.get('dealerProductType', row.get('dealerProductType')),
        'price_untrusted': detail.get('price') if detail.get('price') is not None else row.get('price'),
        'boxNumber': detail.get('boxNumber', row.get('boxNumber')),
        'halt': detail.get('haltProduct'),
        'desc': ((detail.get('productDesc') or '').strip()
                 or (official.get('spotlight') or '').strip() or None),
        'desc_from': ('platform' if (detail.get('productDesc') or '').strip()
                      else ('official' if (official.get('spotlight') or '').strip() else None)),
        'spotlight': (official.get('spotlight') or '').strip() or None,
        'spec': spec,
        'official_url': ('https://www.tp-link.com.cn/product_%s.html' % official['productId']
                         if official.get('productId') else None),
        'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
    }


def upsert(store, rec):
    if not rec or not rec.get('model'):
        return False
    old = store['products'].get(rec['model']) or {}
    # 合并：新值覆盖旧值，但不要把已有的 desc/spec 覆盖成空
    for k, v in rec.items():
        if v in (None, '', [], {}):
            if k in old:
                continue
        old[k] = v
    store['products'][rec['model']] = old
    return True
