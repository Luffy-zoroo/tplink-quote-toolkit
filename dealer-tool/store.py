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
    """规格原文（JSON 串或 list）→ [{'group':..,'name':..,'values':[..]}]

    官网结构是三级：spe（大分组，如「图像与视频参数」）→ speItems（子分组，如「基本参数」）
    → speItemValues（「传感器类型：1/3英寸CMOS」这种 "名称：值" 行）。
    ⚠ 大分组字段名是 `spe`，不是 `speGroupName` —— 2026-10-18 前漏读，导致 group 全空、分组信息丢失。
    """
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
        gname = (grp.get('spe') or grp.get('speGroupName')
                 or grp.get('groupName') or '')
        for it in (grp.get('speItems') or []):
            vals = it.get('speItemValues') or []
            if isinstance(vals, str):
                vals = [vals]
            out.append({'group': gname, 'name': it.get('speItemName'),
                        'values': [str(v) for v in vals]})
    return out


# 平台详情里已单独映射、不再重复进 detail_raw 的键
_DETAIL_SKIP = {'productDesc', 'productGroupList'}


def make_record(row, detail=None, official=None):
    """把平台列表行 + 详情 + 官网 PRODUCT_INFO 合成一条本地记录

    「宁滥勿缺」（2026-10-19 用户要求）：平台详情的**全部非空字段**都留在 `detail_raw`，
    官网规格按三级结构完整展开（大分组 `spe` / 子分组 / 「名称：值」行）。
    目的：以后选型要用到别的字段时，不用重新登录抓一遍。
    官网那 128 条视频/常见问题列表（`DOCUMENT`，35 KB/条）**不存**——与选型无关，
    每条另存会额外撑大 37 MB，属于噪音而非信息。
    """
    detail = detail or {}
    official = official or {}
    spec = flatten_spec(official.get('specification2') or official.get('specification'))
    if not spec:
        spec = flatten_spec(detail.get('specification'))
    rec = {
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
    # 完整平台详情（去空值，省体积但一个有用字段都不丢）
    raw = {k: v for k, v in detail.items()
           if k not in _DETAIL_SKIP and v not in (None, '', [], {})}
    if raw:
        rec['detail_raw'] = raw
    # 同系列型号：选型时「这个系列还有哪几款」直接可查，不必再搜一遍
    sib = [{'model': g.get('productModel'), 'name': g.get('productName')}
           for g in (detail.get('productGroupList') or [])
           if g.get('productModel')]
    if sib:
        rec['siblings'] = sib
    return rec


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
