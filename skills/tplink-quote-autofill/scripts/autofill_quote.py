# -*- coding: utf-8 -*-
"""
TP-LINK 商务标/工程清单：按清单型号自动填【控标参数】+【价格】

流程：
  1. 读清单 Excel，定位 型号 / 参数 / 价格 三列
  2. 参数：本地控标库（优先，含 ★▲ 加分项）→ 查不到转官网「产品特性」
  3. 参数脱品牌：清洗商云APP / TUMS / 算法商城 / TP-SRC 等品牌性字眼
  4. 价格：CRM 取标准价(price) + businessDiscount + seriesValue
           **折扣口径由用户当次通过 --rate-spec 指定**（本脚本不内置任何折扣规则）
           未命中打折条件 → 直接取标准价
           折后价向上取整到个位 0/2/5（不出现小数）
  5. 只填空单元格，用户已填内容一律不动

⚠️ 折扣是商务决策，每次都要问用户。本脚本**不预设、不记住**任何"该给几折"的规则。

用法示例见 SKILL.md / README.md。

  python autofill_quote.py --xlsx "D:\\...\\清单.xlsx" \
      --sheet "智能化专业清单（国产一档）" \
      --model-col I --param-col J --price-col K \
      --header-row 2 \
      --rate-spec '{"mode":"by_field","field":"seriesValue","values":["项目机型"],"rate":0.34}'
"""

import argparse
import json
import math
import os
import re
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import openpyxl
from openpyxl.styles import Alignment

# 交付门禁（同目录 deliver_gate.py）：每段交付后必须问用户、确认后才许进下一段。
# 拿不到模块时**降级为只打提示**，不能因为门禁缺失就让填表跑不了。
try:
    import deliver_gate as gate
except Exception:
    gate = None

# 同目录脚本
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sanitize_params import sanitize_multiline, scan_text          # noqa: E402

# ------------------------------------------------------------- 路径 / 凭据配置
# 不写死本机路径：统一从 tpkit_config 读（用户级 ~/.workbuddy/tplink-toolkit.config.json）。
import tpkit_config as cfg                                        # noqa: E402
DEFAULT_KB = cfg.path('kongbiao_json', must=False)                # 可用 --kb 覆盖


def crm_api():
    """CRM API 根地址（含 /api）。内部系统地址不写死在代码里，从配置项 crm_base 读；
    未配置时抛出 cfg.NotConfigured —— 主流程会捕获并提示「跳过取价」。"""
    return cfg.require_base("crm_base", "CRM") + "/api"

# --------------------------------------------------- 折扣换算方法（长期有效）
# CRM 里每个型号带一个「业务折扣率」businessDiscount，由它可换算另外两档：
#     业务折扣率  →  片区折扣率 / 总部折扣率
# 这是**换算方法**（怎么算），可以长期复用；
# 而"某一单到底给几折"是商务决策，由用户当次通过 rate_spec 指定，不在此预设。
#
# ⚠️ 下表是**换算对照表**，供构造 rate_spec 时使用，不是自动套用的默认值。
BIZDISC_TO_PQ = {'0.45': 0.43, '0.43': 0.40, '0.39': 0.34, '0.32': 0.27}  # → 片区
BIZDISC_TO_HQ = {'0.45': 0.40, '0.43': 0.40, '0.39': 0.32, '0.32': 0.25}  # → 总部

# 「项目机型」是唯一带业务折扣的系列值（型号属性层面的通用规律）
DISCOUNT_SERIES = '项目机型'

# 监控类走【片区折扣】的型号前缀
MONITOR_PREFIXES = (
    'TL-IPC', 'TL-NIPC', 'TL-AIPC', 'TL-NAIPC',      # 网络摄像机
    'TL-NVR', 'TL-NVS',                               # 录像机 / 存储服务器
    'TL-NVD', 'TL-NVDU', 'TL-VS',                     # 解码器 / 解码矩阵
    'TL-SDC',                                         # 拼接显示控制器
    'TL-TUMS',                                        # 综合管理一体机
)
# 走【总部折扣】的型号前缀（门禁梯控道闸 + 两个易搞反的）
NON_MONITOR_PREFIXES = (
    'TL-KBG', 'TL-EC', 'TL-EML', 'TL-EBL', 'TL-AH', 'TL-AS', 'TL-CR',
    'TL-CW', 'TL-DEB', 'TL-CARD',                     # 门禁 / 梯控 / 道闸 / 考勤
    'TL-VE',                                          # 拼接屏（属监控系统但走总部）
)


def is_monitor_row(model):
    """该型号属监控类（走片区折扣）还是其他类（走总部折扣）。

    仅用于**辅助构造 rate_spec**（如给用户建议用哪档），
    **不参与自动算价** —— 具体给哪档仍由用户当次指定。
    """
    mu = str(model or '').upper()
    for p in NON_MONITOR_PREFIXES:
        if mu.startswith(p.upper()):
            return False
    for p in MONITOR_PREFIXES:
        if mu.startswith(p.upper()):
            return True
    return False


def make_by_class_spec(scope='项目机型'):
    """构造「项目机型按类别分档」的 rate_spec（片区/总部由型号前缀自动判）。

    ⚠️ 只是**便捷构造器**，等于替用户把对照表和分类写进 spec；
    用不用、给几折仍取决于用户当次的指示。
    返回的 spec 带 'by_class': True，由 resolve_rate 处理。
    """
    return {'mode': 'by_class', 'field': 'seriesValue',
            'values': [scope],
            'biz_to_pq': dict(BIZDISC_TO_PQ),
            'biz_to_hq': dict(BIZDISC_TO_HQ)}


# ----------------------------------------------------------- 折扣口径 rate_spec
# 折扣口径每次由用户当场指定，通过 rate_spec 传入。支持的写法：
#
#   1) 全部不打折，取标准价：
#        {'mode': 'none'}
#   2) 统一一个折扣率：
#        {'mode': 'flat', 'rate': 0.34}
#   3) 按 CRM 字段判定哪些型号打折（直接给折扣率）：
#        {'mode': 'by_field', 'field': 'seriesValue', 'values': ['项目机型'],
#         'rate': 0.34}
#   4) 按字段判定 + 用"业务底折率→折扣率"对照表换算：
#        {'mode': 'by_discount_table', 'field': 'seriesValue',
#         'values': ['项目机型'], 'table': {'0.32': 0.27, '0.39': 0.34}}
#   5) 项目机型按监控/非监控自动分档（片区/总部），表用内置换算表：
#        make_by_class_spec()   ← 便捷构造器，等价于把上面两张表写进 spec
#   6) 以该型号自己的业务折扣率为基底，再加减 N 个点（每型号折扣率不同，不能用固定值）：
#        {'mode': 'by_field', 'field': 'seriesValue', 'values': ['项目机型'],
#         'rate': {'from': 'business', 'offset': -0.02}}
#        {'mode': 'from_business', 'offset': -0.02}          ← 不限系列，所有带 bd 的都算
#
#   上面第 3/4/5/6 条的 rate 都可以写成「表达式」：
#        0.34                                          固定折扣率
#        {'from': 'business', 'offset': -0.02}         业务折扣率 − 2 个点
#        {'from': 'pq',       'offset': -0.02}         查片区表 − 2 个点
#        {'from': 'hq',       'offset': -0.02}         查总部表 − 2 个点
#        {'from': 'auto',     'offset': -0.02}         按型号自动分片区/总部 − 2 个点
#
# 未命中打折条件的型号 → 直接取标准价（面价）。
NO_DISCOUNT = {'mode': 'none'}


class PricingError(ValueError):
    """口径能解析、但落到这一行算不出合法折扣率（如减点后 ≤ 0）。

    与「型号没查到」一样处理：该行价格留空并说明，不按面价凑数。
    """


def _lookup_by_class(row, pq, hq, forced=None):
    """按型号判断走片区表还是总部表，返回 (业务折扣率, 命中折扣率, 说明) 。"""
    bd = str(row.get('businessDiscount') or '')
    if not bd:
        return None, None, '该型号无业务折扣率'
    if forced == 'pq':
        tbl, label = pq, '片区'
    elif forced == 'hq':
        tbl, label = hq, '总部'
    else:
        model = row.get('productModel')
        tbl, label = (pq, '片区') if is_monitor_row(model) else (hq, '总部')
    if bd in tbl:
        return bd, tbl[bd], '%s %s→%s' % (label, bd, tbl[bd])
    return bd, None, '业务折扣率 %s 不在%s表内' % (bd, label)


def eval_rate(expr, row, spec=None):
    """把口径表达式求值成 (折扣率 or None, 说明)。

    expr 可以是：
        None                          不打折、取基准价原样
        数字                          固定折扣率
        {'from':'business','offset':x} 该型号业务折扣率 + x
        {'from':'pq'|'hq'|'auto','offset':x}
                                      查片区/总部/自动分档表 + x
    返回 None 表示不打折；算出的率越界则抛 PricingError。
    """
    if expr is None:
        return None, '取基准价原样'
    if isinstance(expr, bool):                      # 防 True/False 被当数字
        raise ValueError('口径 rate 不能是布尔值')
    if isinstance(expr, (int, float)):
        return float(expr), '固定折扣率 %s' % expr
    if not isinstance(expr, dict):
        raise ValueError('口径 rate 写法不认识：%r' % (expr,))

    src = str(expr.get('from') or '').strip().lower()
    off = float(expr.get('offset') or 0)
    spec = spec or {}

    if src == 'business':
        bd = row.get('businessDiscount')
        if bd in (None, ''):
            return None, '该型号无业务折扣率，取基准价原样'
        base, label = float(bd), '业务折扣 %s' % bd
    elif src in ('pq', 'hq', 'auto'):
        pq = {str(k): float(v) for k, v in (spec.get('biz_to_pq') or BIZDISC_TO_PQ).items()}
        hq = {str(k): float(v) for k, v in (spec.get('biz_to_hq') or BIZDISC_TO_HQ).items()}
        bd, hit, label = _lookup_by_class(row, pq, hq, None if src == 'auto' else src)
        if hit is None:
            return None, label + '，取基准价原样'
        base = float(hit)
    else:
        raise ValueError('口径 from 只支持 business / pq / hq / auto，收到 %r' % src)

    rate = round(base + off, 6)
    if off:
        label = '%s %s %g个点 = %s' % (label, '+' if off > 0 else '-', abs(off) * 100, rate)
    if rate <= 0:
        raise PricingError('%s，折扣率 ≤ 0（口径写太低或该型号折扣率异常）' % label)
    if rate > 3:
        raise PricingError('%s，折扣率 > 3（口径写太高）' % label)
    return rate, label


def resolve_rate(spec, row):
    """按本次给的折扣口径，判断该型号该用哪个折扣率。

    row 是 CRM 返回的价格记录（含 price / businessDiscount / seriesValue）。
    返回 (折扣率 or None, 说明)。None 表示**不打折，取基准价原样**。

    可以算不出来的情形（该型号无业务折扣率、折扣率越界）也返回 None 并说明，
    但**越界会抛 PricingError** —— 那种情况宁可让这行价格留空，也不按面价凑数。
    """
    if not spec:
        raise ValueError('必须提供本次折扣口径 rate_spec（见 SKILL.md「折扣口径」章节）')
    mode = spec.get('mode', 'none')

    if mode == 'none':
        return None, '本次口径：全部取标准价'

    if mode == 'flat':
        return eval_rate(spec.get('rate'), row, spec)

    if mode == 'dynamic':
        # 全表一套动态口径（不分系列）：折扣率按每行自己的字段现算。
        # 没有 businessDiscount 的行 → eval_rate 返回 None → 取基准价原样，不猜折扣。
        return eval_rate(spec.get('rate'), row, spec)

    if mode == 'from_business':
        # 不限系列：凡是带 businessDiscount 的型号都按「自己的折扣率 ± offset」算。
        # 渠道机没有这个字段 → 该行取基准价原样（不猜一个折扣给它）。
        return eval_rate({'from': 'business', 'offset': spec.get('offset') or 0},
                         row, spec)

    if mode in ('by_field', 'by_discount_table', 'by_class'):
        field = spec.get('field', 'seriesValue')
        values = [str(v).strip() for v in spec.get('values', [])]
        if str(row.get(field) or '').strip() not in values:
            # 另一档口径（如「项目机型 vs 其余=渠道机」各给一套）：给了 else_rate 就用它，
            # 没给则维持老行为 —— 不打折、取标准价。
            other = spec.get('else_rate')
            if other is not None:
                return eval_rate(other, row, spec)
            return None, '不满足打折条件(%s=%s)，取标准价' % (
                field, row.get(field))

        if mode == 'by_field':
            r = spec.get('rate')
            if r is None:                       # 该档不打折：基准价原样
                return None, '本次口径：%s∈%s 取基准价原样' % (field, values)
            return eval_rate(r, row, spec)

        off = float(spec.get('offset') or 0)

        if mode == 'by_discount_table':
            table = {str(k): float(v)
                     for k, v in (spec.get('table') or {}).items()}
            bd = str(row.get('businessDiscount') or '')
            if bd in table:
                if not off:
                    return table[bd], '本次口径：查表 %s→%s' % (bd, table[bd])
                return _shift(table[bd], off, '查表 %s→%s' % (bd, table[bd]))
            return None, '业务折扣率 %s 不在本次对照表内，取标准价' % bd

        # mode == 'by_class'：项目机型按监控/其他分片区/总部（可用 class 强制指定）
        pq = {str(k): float(v) for k, v in (spec.get('biz_to_pq') or {}).items()}
        hq = {str(k): float(v) for k, v in (spec.get('biz_to_hq') or {}).items()}
        bd, hit, label = _lookup_by_class(row, pq, hq, spec.get('class'))
        if hit is None:
            return None, label + '，取标准价'
        if not off:
            return hit, '本次口径：' + label
        return _shift(hit, off, label)

    raise ValueError('未知的折扣口径 mode: %s' % mode)


def _shift(base_rate, off, label):
    """在已算出的折扣率上加减点，并做范围检查。"""
    rate = round(float(base_rate) + float(off), 6)
    shown = '%s %s %g个点 = %s' % (label, '+' if off > 0 else '-', abs(off) * 100, rate)
    if rate <= 0:
        raise PricingError('%s，折扣率 ≤ 0（口径写太低或该型号折扣率异常）' % shown)
    if rate > 3:
        raise PricingError('%s，折扣率 > 3（口径写太高）' % shown)
    return rate, shown


# CRM 一条价格记录里同时有三个价，渠道机和项目机的基准不是同一个，
# 所以「折扣率」和「基准价」必须分开指定。
BASE_FIELDS = {
    'price':     ('price',          '标准价'),
    'catalog':   ('catalogPrice',   '面价'),
    'wholesale': ('wholesalePrice', '批发价'),
}


def resolve_base(spec, row):
    """按本次口径选「用哪个价做基准」。返回 (字段名, 中文名, 说明)。

    spec 里可以整体指定 base，也可以按分流结果给两套：
        {'mode':'by_field','field':'seriesValue','values':['项目机型'],
         'rate':0.36,'else_rate':1.05,
         'hit':{'base':'price'},'else':{'base':'catalog'}}
    没写 base → 用 price（标准价），与历史行为一致。
    """
    mode = spec.get('mode', 'none')
    key = None
    if mode in ('by_field', 'by_discount_table', 'by_class'):
        field = spec.get('field', 'seriesValue')
        values = [str(v).strip() for v in spec.get('values', [])]
        hit = str(row.get(field) or '').strip() in values
        node = spec.get('hit' if hit else 'else') or {}
        key = node.get('base')
        if key is None and hit:
            key = spec.get('base')            # 老写法：顶层 base 只作用于命中档
        if key is None and not hit:
            key = spec.get('else_base')
    if key is None:
        key = spec.get('base') or 'price'

    fld, label = BASE_FIELDS.get(str(key).strip().lower(), BASE_FIELDS['price'])
    return fld, label, '基准价=%s' % label


def describe_rate_expr(expr):
    """把一个口径表达式翻成人话（不带基准价）。"""
    if expr is None:
        return '原样'
    if isinstance(expr, (int, float)):
        return '×%g' % float(expr)
    if not isinstance(expr, dict):
        return str(expr)
    src = str(expr.get('from') or '').strip().lower()
    off = float(expr.get('offset') or 0)
    tag = {'business': '各自业务折扣率',
           'pq': '片区表折扣率',
           'hq': '总部表折扣率',
           'auto': '按型号分片区/总部后的折扣率'}.get(src, src or '?')
    if not off:
        return tag
    return '%s %s%g个点' % (tag, '+' if off > 0 else '-', abs(off) * 100)


def describe_spec(spec):
    """把 rate_spec 翻成人话 —— 日志和结果页都会打，JSON 只有写代码的人看得懂。"""
    _cn = {'price': '标准价', 'catalog': '目录价', 'wholesale': '批发价'}

    def b(k):
        return _cn.get(str(k or 'price').strip().lower(), '标准价')

    def one(r, base):
        return '%s × %s' % (base, r) if isinstance(r, (int, float)) \
            else ('%s 原样' % base if r is None else '%s × %s' % (base, describe_rate_expr(r)))

    mode = spec.get('mode', 'none')
    if mode == 'none':
        return '全部取标准价，不打折'
    if mode == 'flat':
        return '全表统一：' + one(spec.get('rate'), b(spec.get('base') or spec.get('else_base')))
    if mode == 'dynamic':
        return '全表统一：%s × %s（无业务折扣率的型号 → 原样）' % (
            b(spec.get('base')), describe_rate_expr(spec.get('rate')))
    if mode == 'from_business':
        return '所有带业务折扣率的型号：基准价 × %s（渠道机无业务折扣率 → 原样）' % \
            describe_rate_expr({'from': 'business', 'offset': spec.get('offset') or 0})
    if mode == 'by_field':
        hit, alt = spec.get('hit') or {}, spec.get('else') or {}
        return '按 %s 分两档：%s → %s；其余 → %s' % (
            spec.get('field', 'seriesValue'),
            '/'.join(spec.get('values') or []) or '(未指定)',
            one(spec.get('rate'), b(hit.get('base') or spec.get('base'))),
            one(spec.get('else_rate'), b(alt.get('base') or spec.get('else_base'))))
    if mode in ('by_discount_table', 'by_class'):
        force = spec.get('class')
        what = {'pq': '片区表', 'hq': '总部表'}.get(force, '片区/总部表按型号自动分')
        off = float(spec.get('offset') or 0)
        tail = '' if not off else '，再 %s%g个点' % ('+' if off > 0 else '-', abs(off) * 100)
        return '按 %s 判定 %s 后查%s%s' % (
            spec.get('field', 'seriesValue'),
            '/'.join(spec.get('values') or []) or '(未指定)', what, tail)
    return '未知口径 mode=%s' % mode

# 表格里出现的"非型号备注"，遇到就跳过不动
SKIP_NOTES = ('没有防爆球机', '没有65寸', '相关产品已停产', '无', '-', '/', '停产')


def clean_price(v, rounded_up=True):
    """价格取整。

    - rounded_up=True（折扣算出来的价）：整数**原样保留**；有小数则向上取整到个位 0/2/5。
      例：212.85→215、331.10→332；1326 本身是整数 → 保持 1326。
    - rounded_up=False（直接取 CRM 面价/标准价）：原样保留（面价本身可能带 .5，如 137.5）。
    """
    f = float(v)
    if not rounded_up:
        return int(f) if abs(f - round(f)) < 1e-9 else f
    if abs(f - round(f)) < 1e-9:          # 已是整数 → 保持
        return int(round(f))
    n = int(math.ceil(f))
    while n % 10 not in (0, 2, 5):
        n += 1
    return n


# ------------------------------------------------------------------ 组合型号
# 型号列经常写成组合形式，语义是「一套 = 若干配件」：
#   TL-VS7500+TL-NVDU7606S*4      → 1 台解码矩阵 + 4 块业务板卡
#   TL-EML100+TL-EML100-LZ        → 1 把锁 + 1 个支架
#   TL-FC311A-3/TL-FC311B-3       → A/B 一对
#   TL-EC100-M18*2                → 单个型号 ×2
# 处理口径（用户 2026-09-18 明确）：
#   · 参数：只填**第一个型号**（主型号）的参数
#   · 价格：**所有配件单价加总**（配件单价各自按自身折扣率算，再乘数量）
# 型号 token：字母/数字/连字符，允许尾随中文版本后缀（如「易展版」），
# 否则 TL-AP1206GC-PoE/DC易展版 会被截成 TL-AP1206GC-PoE-DC 而查不到。
# ⚠️ 型号尾部可能带**半角括号短后缀**：`TL-PSM550-AC(F)`、`TL-GP110(UN)` 等，
#    括号也是型号本身的一部分。早期正则不认识 `(`，会把 token 截成 `TL-PSM550-AC`，
#    更糟的是紧跟其后的数量后缀也跟着失效——`TL-PSM550-AC(F)*2` 被当成 **×1**
#    （2026-09-30 智慧医院清单实测：少算 1 个电源，静默错价 969 元）。
#    故在型号主体后允许一个 1~6 位的半角括号组（只认字母数字，中文注解如
#    `TL-VS7500（含业务板卡）` 是控标库里的全角写法，不会被吃掉）。
_MODEL_TOKEN = re.compile(
    r'TL-[A-Za-z0-9\-\x02]+(?:\([A-Za-z0-9]{1,6}\))?(?:易展版|易展)?', re.I)
# ⚠️ 数量后缀可以是**小数**：实测 2026-09-24 抚州S2地块清单里有 `TL-SA3045*0.5`
#    （摆闸单机芯只要半副摆臂），早期只认 \d+ 会把它当成 ×1，静默多算一倍。
_QTY_AFTER = re.compile(r'^\s*[*×xX]\s*(\d*\.?\d+)')

# ⚠️ CRM 官方型号名本身可能**内嵌中文**（套装类物料）：
#    TL-E201固定端&TL-E201轿厢端套装 / TL-E501固定端&TL-E501轿厢端套装 / TL-E521套装
#    全文扫描的正则在中文处断句 → 前者被切成 (TL-E201, 2)，而 CRM 里没有裸的
#    TL-E201 → 静默取价失败（2026-09-30 智慧医院监控实测）。
#    判据：整串无 '+'/'、' 等组合分隔符 + 含非 ASCII + 去重后只有一个 TL- 型号
#    且它是整串前缀 → 整串就是一个型号名，只另算尾部 *N。
#    纯 ASCII 串（含 TL-FC311A-3/TL-FC311B-3 真双型号）不走此路；
#    含中文但内嵌了**另一个**型号的（TL-VS7500（含TL-NVDU7606S业务板卡））也不走。
_QTY_TAIL = re.compile(r'\s*[*×]\s*(\d*\.?\d+)\s*$')
_COMBO_SEP = re.compile(r'[+＋、;；,，\n\r]')
_NON_ASCII = re.compile(r'[^\x00-\x7f]')

# ⚠️ 型号里合法出现的 "/"，不是"两个型号"的分隔符。实测踩坑：
#    TL-AP1206GC-PoE/DC易展版  → /DC 是电源规格后缀，被 split('/') 误拆成
#    TL-AP1206GC-PoE + (空)，导致查不到 CRM 价。
#
# 处理方式：解析时把 / 后的后缀**接到前一个 token 上**（不插入连字符，
# 因为 CRM 里的型号名就是原样带 / 的），再用**原始串子串**去查 CRM。
_MODEL_SUFFIX_AFTER_SLASH = re.compile(
    r'/(DC|AC|POE|PoE)(易展版|易展|)(?=$|[\s,，、;；*×xX])', re.I)


def parse_combo(models_str):
    """把型号单元格拆成 [(型号, 数量), ...]。

    无法识别出 TL- 型号时，返回 [(原串, 1)]，保持向后兼容。
    """
    s = str(models_str or '').strip()
    if not s:
        return []
    merged = _MODEL_SUFFIX_AFTER_SLASH.sub(
        lambda m: '\x02' + m.group(0)[1:], s)      # \x02 占位替代 '/'，让后缀并入前一 token
    # ⓿ 整串即一个「内嵌中文」的官方型号名（如 TL-E201固定端&TL-E201轿厢端套装）。
    #    全文扫描会把中文当断点，切出 TL-E201 ×2 且 CRM 查不到 → 静默取价失败。
    if not _COMBO_SEP.search(s) and _NON_ASCII.search(s):
        toks = [t.replace('\x02', '/').replace('\x01', '') for t in _MODEL_TOKEN.findall(merged)]
        uniq = {t.upper() for t in toks}
        if len(uniq) == 1 and s.upper().startswith(next(iter(uniq))):
            qm = _QTY_TAIL.search(s)
            if qm:
                name, qty = s[:qm.start()].strip(), float(qm.group(1))
            else:
                name, qty = s, 1
            if qty == int(qty):
                qty = int(qty)
            return [(name, qty)]
    out = []
    for m in _MODEL_TOKEN.findall(merged):
        m = m.replace('\x02', '/').replace('\x01', '')   # 还原为原始写法
        idx = s.upper().find(m.upper())                  # 回原文定位，取真实子串
        real = s[idx:idx + len(m)] if idx >= 0 else m
        qm = _QTY_AFTER.match(s[idx + len(m):]) if idx >= 0 else None
        qty = float(qm.group(1)) if qm else 1
        if qty == int(qty):
            qty = int(qty)
        for i, (em, eq) in enumerate(out):
            if em.upper() == real.upper():
                out[i] = (em, eq + qty)
                break
        else:
            out.append((real, qty))
    return out or [(s, 1)]


def parse_combo_exact_first(models_str):
    """组合里只取第一个型号的**原始写法**（用于整串查 CRM，如带 /DC 后缀的）。"""
    c = parse_combo(models_str)
    return c[0][0] if c else str(models_str or '').strip()


def parse_combo_first(models_str):
    """组合里只取第一个（主）型号，用于填参数。"""
    c = parse_combo(models_str)
    return c[0][0] if c else str(models_str or '').strip()


def is_combo(models_str):
    """是否为组合型号（含 2 个及以上不同型号，或带 *N 数量后缀）。

    组合行必须走「参数取首个 + 价格全部加总」的口径，不能只算第一个型号。
    """
    c = parse_combo(models_str)
    if len(c) > 1:
        return True
    return bool(c) and c[0][1] > 1


def combo_unit_price(op, model, kb_idx=None, rate_spec=None):
    """算单个配件的对外单价。

    返回 (单价, 依据说明)。无 CRM 记录时返回 (None, 原因)。

    **折扣口径由调用方通过 rate_spec 传入**（用户本次指定），本函数不预设任何折扣规则：
      · resolve_rate 返回折扣率 → 基准价 × 折扣率，向上取整到个位 0/2/5
      · 返回 None → 直接取基准价，原样保留小数
    基准价由 resolve_base 决定（标准价 / 面价 / 批发价），渠道机和项目机可以各用各的。
    """
    ex = crm_price(op, model)
    if not ex:
        return None, 'CRM 未查到该型号'
    # 同一型号返回多条，通常是该物料**历年阶梯价（ladderPrice）的历史档**，
    # 只有 valid=True 的那条是现行生效档（实测 2026-09-22：TL-EC5e-305B 6 条、
    # TL-SG1008D 3 条，valid 各只有 1 条；不过滤会误报"多条目需人工确认"，
    # 或更糟——按顺序取到 2020 年的过期价）。
    val = [r for r in ex if r.get('valid') is True]
    if len(val) == 1:
        ex = val
    elif len(ex) > 1:
        return None, 'CRM 命中 %d 条同型号记录(valid=%d)，需人工确认取哪条' % (len(ex), len(val))
    x = ex[0]

    base_fld, base_label, _ = resolve_base(rate_spec, x)
    price = x.get(base_fld)
    if price in (None, ''):
        # 该条记录没有这个价（部分型号无批发价/面价）→ 回退标准价，并在说明里点出来
        if base_fld != 'price' and x.get('price') not in (None, ''):
            price = x.get('price')
            base_label = '标准价（该型号无%s）' % base_label
        else:
            return None, 'CRM 记录缺%s（%s），无法算价' % (base_label, base_fld)

    try:
        rate, basis = resolve_rate(rate_spec, x)
    except PricingError as e:
        # 口径本身没写错，但落到这行算不出合法折扣率（如业务折扣 0.32 减 40 个点）。
        # 宁可这行价格留空并说明，也不按面价凑一个数出来。
        return None, '价格留空：%s' % e
    if rate is None:
        unit = clean_price(price, rounded_up=False)
        return unit, '%s %s（%s）' % (base_label, price, basis)

    unit = clean_price(float(price) * rate, rounded_up=True)
    return unit, '%s %s × %s = %s（%s）' % (base_label, price, rate, unit, basis)


def combo_total_price(op, models_str, kb_idx=None, rate_spec=None):
    """组合行价格 = 所有配件单价 × 各自数量，全部加总。

    返回 (总价 or None, [配件明细], 错误说明 or None)。
    约定：任一配件查不到价 → 总价返回 None（不填半截价）。
    """
    total = 0
    parts = []
    for pm, pqty in parse_combo(models_str):
        unit, note = combo_unit_price(op, pm, kb_idx, rate_spec)
        if unit is None:
            parts.append({'model': pm, 'qty': pqty, 'error': note})
            return None, parts, '%s %s' % (pm, note)
        total += unit * pqty
        parts.append({'model': pm, 'qty': pqty, 'unit': unit, 'basis': note})
    return total, parts, None


# ------------------------------------------------------------------ CRM
def resolve_crm_creds(email=None, password=None):
    """找 CRM 凭据：命令行参数 → 配置文件 crm 段 → ~/.workbuddy/MEMORY.md。

    返回 (email, pwd)，可能都是 None（三处都没有）。
    抽成函数是因为 one_query.py 也要用同一套规则 —— 凭据来源只留一份，
    别让两个脚本各记一套"配置里没有就去哪找"。
    """
    email = (email or '').strip() or None
    pwd = (password or '').strip() or None
    # 环境变量优先于配置文件：网页版按"每个人自己的 CRM 账号"跑，
    # 用环境变量传，密码就不会出现在进程命令行里（argv 全机可见）。
    if not email:
        email = (os.environ.get('TPKIT_CRM_EMAIL') or '').strip() or None
    if not pwd:
        pwd = (os.environ.get('TPKIT_CRM_PASSWORD') or '').strip() or None
    if not email or not pwd:
        try:
            _ce, _cp = cfg.crm_creds()
        except Exception:
            _ce = _cp = ''
        email = email or (_ce or None)
        pwd = pwd or (_cp or None)
    if not email or not pwd:
        mem = os.path.expanduser('~/.workbuddy/MEMORY.md')
        if os.path.exists(mem):
            t = open(mem, encoding='utf-8', errors='replace').read()
            if not email:
                m = re.search(r'[\w\.\-]+@[\w\.\-]+\.\w+', t)
                if m:
                    email = m.group(0)
            if not pwd:
                # 匹配：**密码**：`xxx`  或  - 密码: xxx
                m = re.search(r'密码\**\s*[：:]\s*`([^`\n]+)`', t)
                if not m:
                    m = re.search(r'密码\**\s*[：:]\s*([^\s`\n]+)', t)
                if m:
                    pwd = m.group(1).strip()
    return email, pwd


def crm_login(email, pwd):
    import urllib.request, urllib.parse, http.cookiejar
    from urllib.request import getproxies
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler(getproxies()),
        urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [('User-Agent', 'Mozilla/5.0'), ('Accept', 'application/json')]
    data = urllib.parse.urlencode({'email': email, 'password': pwd}).encode()
    req = urllib.request.Request(
        crm_api() + '/login', data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'})
    j = json.loads(op.open(req, timeout=60).read().decode('utf-8'))
    return op, j


def crm_price(op, model):
    """按型号查价格手册，返回精确匹配条目列表。

    实测踩坑（2026-09-22）：清单写 TL-XAP1502GI-PoE，CRM 里真实物料名是
    「TL-XAP1502GI-PoE 薄款（方）易展版」——精确匹配为空，整行价格填不出来。
    故加一层**唯一前缀回退**：精确无果时，若只有一个物料的型号以查询串开头
    （且后继字符非字母数字，避免 TL-SG2210 撞上 TL-SG2210P），返回该条；
    命中多条则仍返回空，交由上层提示"需人工确认"，绝不自己挑一条。
    """
    import urllib.request, urllib.parse
    p = urllib.parse.urlencode({'productModel': model, 'start': 0, 'limit': 200})
    url = crm_api() + '/line/findByPage?' + p
    try:
        j = json.loads(op.open(url, timeout=60).read().decode('utf-8'))
    except Exception:
        return []
    # 注意：返回体是 {"results":[...], "totalCounts":N}
    rows = j.get('results') or j.get('data') or []
    mu = model.upper()
    exact = [x for x in rows
             if str(x.get('productModel', '')).strip().upper() == mu]
    if exact:
        return exact
    near = [x for x in rows
            if str(x.get('productModel', '')).strip().upper().startswith(mu)
            and _boundary_ok(str(x.get('productModel', '')).strip().upper(), mu)]
    return near if len(near) == 1 else []


# ------------------------------------------------------------------ 控标库
def load_kb(path):
    if not path or not os.path.exists(path):
        return {}, {}
    kb = json.load(open(path, encoding='utf-8'))
    idx = {}
    for x in kb:
        idx.setdefault(str(x.get('model', '')).strip().upper(), []).append(x)
    return idx, kb


def _boundary_ok(k, c):
    """判断模糊匹配的边界是否"干净"。

    实测踩坑（2026-09-22）：控标库里 TL-SG2210 没有条目，但 TL-SG2210P 有，
    前缀匹配就把 TL-SG2210P 的参数填给了「8口接入交换机」；
    TL-SH5428 同理被 TL-SH5428F-Combo 顶替。两者**是完全不同的产品**，
    却静默填错参数——比查不到更危险。

    判据：候选型号之后紧跟的字符必须**不是 ASCII 字母或数字**（是 （ 、- 、空格、
    中文等），才算"同一个型号名带了后缀/注解"，
    例如 TL-VS7500 → TL-VS7500（含TL-NVDU7606S业务板卡）。

    ⚠️ 实测踩坑（2026-09-30 智慧医院监控单）：CRM 里物料名写成 `TL-E521套装`，
    而 _MODEL_TOKEN 只吃到 `TL-E521` 就断了（正则不含中文），回退时 rest='套装'，
    旧写法 `rest[0].isalnum()` 对中文返回 True → 判定"边界不干净" → 回退被拒
    → **静默查不到价**。中文后缀（套装/固定端/轿厢端…）属正常型号后缀，不该阻断。
    故改为只对 **ASCII** 字母数字设防：`TL-SG2210P` vs `TL-SG2210` 的保护不受影响。
    """
    rest = k[len(c):]
    if not rest:
        return True
    ch = rest[0]
    return not (ch.isascii() and ch.isalnum())


def kb_lookup(idx, cands):
    """精确 → 前缀/包含模糊（模糊匹配须通过词边界校验）。

    控标库里型号名可能带括号注解（如 TL-VS7500（含…）），所以需要模糊兜底；
    但模糊必须严格，否则会把同系列不同型号的参数填错（见 _boundary_ok 注释）。
    """
    for c in cands:
        if c.upper() in idx:
            return idx[c.upper()][0]
    for c in cands:
        cu = c.upper()
        if len(cu) < 5:
            continue
        for k, ents in idx.items():
            if k.startswith(cu) and _boundary_ok(k, cu):
                return ents[0]
        for k, ents in idx.items():
            i = k.find(cu)
            if i > 0 and _boundary_ok(k[i:], cu) and not k[i - 1].isalnum():
                return ents[0]
    return None


def kb_params(ent):
    """控标库参数 → 文本（★ 表示加分项）。"""
    lines = []
    for p in ent['param_items']:
        t = str(p['text']).strip().replace('★★', '★')
        lines.append(t)
    return '\n'.join(lines)


# ------------------------------------------------------------------ 官网
def web_feature(model, sleep=0.3):
    """官网搜型号 → 产品页 #smbproductFeature 下的 li。返回 (items, url) 或 (None, None)。"""
    import urllib.request, urllib.parse, time
    from urllib.request import getproxies
    UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/120 Safari/537.36')
    op = urllib.request.build_opener(urllib.request.ProxyHandler(getproxies()))
    op.addheaders = [('User-Agent', UA), ('Accept-Language', 'zh-CN,zh;q=0.9')]

    def get(u):
        return op.open(urllib.request.Request(u), timeout=45).read().decode('utf-8', 'replace')

    try:
        h = get('https://www.tp-link.com.cn/search.html?keywords=' + urllib.parse.quote(model))
    except Exception:
        return None, None

    pids, seen = [], set()
    for m in re.finditer(r'href="/product_(\d+)\.html"', h):
        if m.group(1) not in seen:
            seen.add(m.group(1)); pids.append(m.group(1))
    if not pids:
        return None, None

    # 优先选标题里含该型号的页
    pick = pids[0]
    for pid in pids[:6]:
        try:
            hh = get('https://www.tp-link.com.cn/product_%s.html' % pid)
        except Exception:
            continue
        tm = re.search(r'<title>(.*?)</title>', hh, re.S)
        title = tm.group(1) if tm else ''
        mu = re.sub(r'[\s\-]', '', model).upper()
        if mu in re.sub(r'[\s\-]', '', title).upper():
            pick = pid
            break
    time.sleep(sleep)

    url = 'https://www.tp-link.com.cn/product_%s.html' % pick
    try:
        h = get(url)
    except Exception:
        return None, None
    m = re.search(r'<div id="smbproductFeature">(.*?)(?:</div>\s*</div>|</section>)', h, re.S)
    if not m:
        return None, None
    items = []
    for li in re.findall(r'<li[^>]*>(.*?)</li>', m.group(1), re.S):
        s = re.sub(r'<[^>]+>', ' ', li)
        s = (s.replace('&nbsp;', ' ').replace('&amp;', '&')
              .replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"'))
        s = re.sub(r'\s+', ' ', s).strip()
        if s:
            items.append(s)
    return (items or None), url


# ------------------------------------------------------------------ 主流程
# 价格口径词：出现在报价表里就是泄露
# （用户 2026-10-22 反馈：清单里老是被写进业务折扣、价格计算方法；
#   实际发生位置 = 1021 替换清单的 A32「备注：单价为业务折扣后单价…」）
#
# ⚠️ "片区"/"总部" 不能单独作词——SD-WAN 参数里正常就有「总部分部互联互通」，
#    实测会误报，必须要求后面跟"折"。
_LEAK = re.compile(
    r'折扣|折率|底价|成本价|进价|成本合计|毛利率|利润率|'
    r'(?:片区|总部|业务|区域)\s*折|'
    r'[×✕xX＊*]\s*0\.\d|打\s*[0-9.]+\s*折|按\s*[0-9.]+\s*折|'
    r'businessDiscount|seriesValue')


def _leak_text(cell):
    """这个格子里有没有价格口径词；公式和空值不算。返回片段或 None。

    ⚠️ 必须跳过公式：`=125*0.03` 里的 `*0.03` 会被 _LEAK 当成「×0.03」折扣，
    实测把整张带税率/系数的客户表卡死（2026-10-27 一张 10 行清单报了 I26/I27）。
    """
    if getattr(cell, 'data_type', None) == 'f':
        return None
    v = cell.value
    if not isinstance(v, str) or not v.strip():
        return None
    return ' '.join(v.split())[:90] if _LEAK.search(v) else None


def scan_price_leak(ws, header_row=1, only_written=None):
    """找价格计算口径残留，返回 [(行, 列字母, 片段)]。

    only_written：给一组 (row, col_idx) 就只查这些格子。脚本自己只写参数列和
    价格列，原表原有的公式、汇总区、客户自己的备注不该由我们背锅 —— 只查写入
    位置，才不会因为「表里本来就有折扣字样」把正常清单卡死（2026-10-27 实测）。

    不给 only_written 就全表扫（旧行为，留给外部调用）。
    表头行跳过 —— "折扣率"当列名是清单原有结构，不算泄露。
    """
    hits = []
    if only_written is not None:
        for r, c in sorted(set(only_written)):
            cell = ws.cell(r, c)
            t = _leak_text(cell)
            if t:
                hits.append((cell.row, cell.column_letter, t))
        return hits
    for row in ws.iter_rows(min_row=header_row + 1):
        for c in row:
            t = _leak_text(c)
            if t:
                hits.append((c.row, c.column_letter, t))
    return hits


def main():
    ap = argparse.ArgumentParser(description='按清单型号自动填控标参数 + 价格')
    ap.add_argument('--xlsx', required=True, help='清单 Excel 路径')
    ap.add_argument('--sheet', default=None, help='工作表名（默认第一个含型号列的）')
    ap.add_argument('--header-row', type=int, default=2, help='表头所在行（默认2）')
    ap.add_argument('--model-col', default='I', help='型号列（默认 I）')
    ap.add_argument('--param-col', default='J', help='参数列（默认 J）')
    ap.add_argument('--price-col', default='K', help='含税单价列（默认 K）')
    ap.add_argument('--kb', default=DEFAULT_KB, help='控标库 JSON 路径')
    ap.add_argument('--email', default=None, help='CRM 账号（不填则读 ~/.workbuddy/MEMORY.md）')
    ap.add_argument('--password', default=None, help='CRM 密码')
    ap.add_argument('--no-crm', action='store_true', help='跳过 CRM，只填参数不填价格')
    ap.add_argument('--no-sanitize', action='store_true', help='不做品牌词清洗（不推荐）')
    ap.add_argument('--out', default=None, help='另存为新文件（默认就地写回，自动备份 .bak）')
    ap.add_argument('--report', default=None, help='输出改动的 JSON 台账')
    ap.add_argument('--rate-spec', default=None,
                    help='本次折扣口径（JSON）。不填则交互式询问，不用默认值。'
                         '例：{"mode":"flat","rate":0.34} 或 '
                         '{"mode":"by_field","field":"seriesValue",'
                         '"values":["项目机型"],"rate":0.34} 或 '
                         '{"mode":"none"} 表示全部取标准价')
    ap.add_argument('--no-gate', action='store_true',
                    help='跳过交付门禁（仅批量/调试用；正常配单不要加，'
                         '加了就不会强制向用户要反馈）')
    ap.add_argument('--allow-price-note', action='store_true',
                    help='允许表里带价格口径词（仅用于内部成本分析表；'
                         '对外报价表**不要加**，会把折扣亮给甲方）')
    a = ap.parse_args()

    # ---- 交付门禁：上一段没确认，不许开工 ----
    # stage：只填参数(--no-crm) 记为 params；连价格一起填记为 autofill。
    # 门禁只看「同项目的上一段有没有 clear」，与业务逻辑无关，读表之前先挡。
    stage = 'params' if a.no_crm else 'autofill'
    if gate and not a.no_gate:
        gate.require_clear(stage, a.xlsx)

    # ⚠️ 安全闸：默认列位是 I/J/K，但清单的实际列位经常不同（如参数在 G、数量在 I）。
    #    若用户没显式指定列位，且默认列完全对不上，就在写文件**之前**停下来问清楚，
    #    避免把参数/价格写到错误的列里（实测踩过：把参数写进了 I 列）。
    if not (a.model_col != 'I' or a.param_col != 'J' or a.price_col != 'K'):
        pass  # 用户可能就是要用默认列，继续

    col = lambda s: openpyxl.utils.column_index_from_string(s)

    # ---- 读表 ----
    if not os.path.exists(a.xlsx):
        print('找不到文件:', a.xlsx); sys.exit(1)
    # rich_text=True：清单里常有手工标色/富文本单元格（实测 2026-09-22 视频监控清单全表都是），
    # 不开此开关，openpyxl 加载即丢弃富文本，保存后用户手工格式静默丢失。
    wb = openpyxl.load_workbook(a.xlsx, rich_text=True)
    ws = wb[a.sheet] if a.sheet else wb.worksheets[0]
    cM, cP, cK = col(a.model_col), col(a.param_col), col(a.price_col)
    print('工作表: %s  型号列=%s 参数列=%s 价格列=%s  表头行=%d'
          % (ws.title, a.model_col, a.param_col, a.price_col, a.header_row))

    # ---- 列位自检 ----
    # 目标：在**写任何文件之前**发现"列位填错了"。
    # 实测踩过的灾难：默认列是 I/J/K，但清单型号其实在 D 列、参数在 G 列。
    # 直接跑会把参数/价格写进 I/J 列，把原有数据搅乱。
    #
    # 判据（任一不满足即中止）：
    #   ① 型号列里必须能认出"型号样"的值（TL- 开头，或带数字字母的型号串），
    #      不能只是数字（数字往往是数量列）；
    #   ② 参数列的表头应含"参数/规格/配置"等字样（或为空——有些表头没写）。
    hdr = lambda c: str(ws.cell(a.header_row, c).value or '').strip()

    def looks_like_model(v):
        s = str(v or '').strip()
        if not s or len(s) < 3:
            return False
        if re.match(r'^[\d\.,]+$', s):          # 纯数字 → 是数量，不是型号
            return False
        if s.upper().startswith('TL-'):
            return True
        return bool(re.search(r'[A-Za-z]', s)) and bool(re.search(r'\d', s))

    n_models = 0
    for r in range(a.header_row + 1, ws.max_row + 1):
        v = ws.cell(r, cM).value
        if looks_like_model(v):
            n_models += 1

    print('列位自检: 型号列 %s 表头=%r  疑似型号行数=%d' % (a.model_col, hdr(cM), n_models))
    print('          参数列 %s 表头=%r   价格列 %s 表头=%r'
          % (a.param_col, hdr(cP), a.price_col, hdr(cK)))

    if n_models == 0:
        print()
        print('!! 型号列（%s）里没找到任何像"型号"的值 —— 列位几乎肯定填错了。' % a.model_col)
        print('   请先 dump 表头确认列位，再用 --model-col/--param-col/--price-col 指定。')
        print('   已中止，未改动任何文件。')
        sys.exit(2)

    # ---- 控标库 ----
    # 区分三种情况，不能一律静默降级（2026-10-19 干净环境实测发现）：
    #   有内容        → 正常
    #   配了但读不到  → 路径写错了，必须喊出来，否则用户以为"库里没这个型号"
    #   根本没配      → 第一次用，走官网兜底是预期行为
    idx, _ = load_kb(a.kb)
    if idx:
        print('控标库: %s  型号 %d 条' % (a.kb, len(idx)))
    elif a.kb:
        print('⚠ 控标库路径已配置，但文件不存在：%s' % a.kb, file=sys.stderr)
        print('  → 本次全部走官网兜底，参数**不带 ★ 加分项**。'
              '请检查该路径，或重跑数据包里的 生成配置.py', file=sys.stderr)
    else:
        print('控标库: （未配置，将全走官网）  型号 0 条')

    # ---- 折扣口径（本次由用户指定，不预设）----
    rate_spec = NO_DISCOUNT
    if a.rate_spec:
        try:
            rate_spec = json.loads(a.rate_spec)
        except Exception as e:
            print('--rate-spec 不是合法 JSON:', e); sys.exit(1)
    else:
        print()
        print('⚠ 未指定本次折扣口径（--rate-spec）。')
        print('  折扣是商务决策，本脚本不预设。请按下面之一重跑：')
        print('    全部取标准价        --rate-spec \'{"mode":"none"}\'')
        print('    全部统一折扣        --rate-spec \'{"mode":"flat","rate":0.34}\'')
        print('    按字段判打折        --rate-spec \'{"mode":"by_field",'
              '"field":"seriesValue","values":["项目机型"],"rate":0.34}\'')
        print('    按对照表折算        --rate-spec \'{"mode":"by_discount_table",'
              '"field":"seriesValue","values":["项目机型"],'
              '"table":{"0.32":0.27,"0.39":0.34}}\'')
        print('    项目机型分片区/总部 --rate-spec \'{"mode":"by_class",'
              '"field":"seriesValue","values":["项目机型"],'
              '"biz_to_pq":{"0.32":0.27,"0.39":0.34},'
              '"biz_to_hq":{"0.32":0.25,"0.39":0.32}}\'')
        print('  本次先按「全部取标准价」继续；如需折扣请带 --rate-spec 重跑。')
        print()
    print('折扣口径:', describe_spec(rate_spec))
    print('            ', json.dumps(rate_spec, ensure_ascii=False))

    # ---- CRM ----
    op = None
    if not a.no_crm:
        email, pwd = resolve_crm_creds(a.email, a.password)
        if email and pwd:
            try:
                op, info = crm_login(email, pwd)
                si = info.get('sessionInfo') or {}
                print('CRM 登录成功:', si.get('chineseName'), si.get('officeName'))
            except Exception as e:
                print('CRM 登录失败（将跳过价格）:', e)
                op = None
        else:
            print('未提供 CRM 凭证，跳过价格')
            print('  提示：可用 --email/--password 传入，或在 ~/.workbuddy/MEMORY.md 按「**密码**：`xxx`」格式记录')

    # ---- 备份 ----
    # 就地写回时，备份名带时间戳，避免覆盖掉上一次的干净备份
    # （实测踩过：连续跑两次，第二次的 .bak 覆盖了原件备份，害得没得还原）
    if not a.out:
        import time as _t
        bak = '%s.%s.bak' % (a.xlsx, _t.strftime('%Y%m%d-%H%M%S'))
        shutil.copy2(a.xlsx, bak)
        print('备份 ->', bak)

    # ---- 逐行处理 ----
    log, rows = [], []
    n_p = n_k = 0
    written = []            # 本次自动写入的格子 (row, col_idx)，只查这些有没有口径泄露
    for r in range(1, ws.max_row + 1):
        mv = ws.cell(r, cM).value
        if not mv:
            continue
        ms = str(mv).strip()
        if ms in SKIP_NOTES:
            continue

        # 型号候选（拆 A/B、A+B、*N 数量）
        cands = []
        for t in (ms.split('/') if '/' in ms else []) + (ms.split('+') if '+' in ms else []):
            cands.append(t.strip())
        cands.append(re.split(r'[*×]', ms)[0].strip())
        cands.append(ms)
        seen = set(); cands = [c for c in cands if c and not (c in seen or seen.add(c))]

        # ---------- 参数 ----------
        if ws.cell(r, cP).value in (None, ''):
            raw = None; src = None
            # 组合型号只取主型号（第一个）的参数
            main_model = parse_combo_first(ms)
            ent = kb_lookup(idx, [main_model] + cands)
            if ent and ent.get('param_items'):
                raw = kb_params(ent)
                src = '控标库:%s(%s)' % (ent.get('category'), ent.get('model'))
            else:
                for c in [main_model] + cands:
                    items, url = web_feature(c)
                    if items:
                        raw = '\n'.join(
                            (t if re.match(r'^[★▲]?\d+[、.\s]', t) else '%d. %s' % (i, t))
                            for i, t in enumerate(items, 1))
                        src = '官网:' + (url or '')
                        break
            if raw:
                clean = raw if a.no_sanitize else sanitize_multiline(raw)
                cell = ws.cell(r, cP)
                cell.value = clean
                cell.alignment = Alignment(wrap_text=True, vertical='top')
                written.append((r, cP))
                n_p += 1
                hits = [] if a.no_sanitize else scan_text(raw)
                log.append({'row': r, 'model': ms, 'kind': '参数', 'source': src,
                            'brand_words_removed': sorted({h[0] for h in hits}),
                            'len': len(clean)})
                rows.append((r, ms, '参数', src))
            else:
                log.append({'row': r, 'model': ms, 'kind': '参数', 'source': None,
                            'note': '控标库与官网均未找到'})

        # ---------- 价格 ----------
        # 组合型号（A+B*4 / A*2）：价格 = 全部配件单价 × 数量 加总（见 combo_total_price）
        if op is not None and ws.cell(r, cK).value in (None, ''):
            total, parts_log, err = combo_total_price(op, ms, idx, rate_spec)
            if err is None:
                ws.cell(r, cK).value = total
                written.append((r, cK))
                n_k += 1
                log.append({'row': r, 'model': ms, 'kind': '价格',
                            'is_combo': is_combo(ms), 'parts': parts_log,
                            'final': total})
            else:
                log.append({'row': r, 'model': ms, 'kind': '价格',
                            'is_combo': is_combo(ms), 'parts': parts_log,
                            'note': err})

    # ---- 价格口径泄露闸门（保存前，不通过就不存）----
    # 折扣率、片区/总部口径、"×0.39" 这类东西只能写在回复里告诉用户，
    # 一旦落进表里、表又发给甲方，等于把底牌亮出去。宁可中断让人改。
    leaks = scan_price_leak(ws, a.header_row, written)
    if leaks and not a.allow_price_note:
        bar = '━' * 64
        msg = ['', bar,
               '🛑 中止保存：本次填入的内容里有价格计算口径（%d 处）' % len(leaks),
               bar,
               '这类内容只能出现在对话回复里，不能写进报价表：']
        for r, c, t in leaks[:15]:
            msg.append('  %s%-4d %s' % (c, r, t))
        if len(leaks) > 15:
            msg.append('  … 共 %d 处' % len(leaks))
        msg += [
            '',
            '怎么处理：',
            '  · 上面这几处是刚填进去的内容，把折扣/成本说明删掉，只留单价、小计',
            '  · 计算过程（折扣档、取整规则）写在回复里告诉用户',
            '  · 只查自动写入的格子 —— 你原表自带的公式、汇总区、客户备注不会被拦',
            '  · 确实要给内部看的成本分析表，才加 --allow-price-note',
            bar, '']
        print('\n'.join(msg), file=sys.stderr)
        sys.exit(4)

    # ---- 保存 ----
    target = a.out or a.xlsx
    wb.save(target)
    print('\n已保存: %s' % target)
    print('填参数 %d 行，填价格 %d 行' % (n_p, n_k))

    if a.report:
        json.dump(log, open(a.report, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('台账:', a.report)

    # ---- 品牌词复核提示 ----
    warn = [x for x in log if x.get('brand_words_removed')]
    if warn:
        print('\n⚠ 以下行做了品牌词中性化，建议人工复核：')
        for x in warn[:30]:
            print('  行%-4s %-28s 去掉: %s' % (x['row'], x['model'][:26], '、'.join(x['brand_words_removed'])))
        if len(warn) > 30:
            print('  … 共 %d 行' % len(warn))

    # ---- 交付回执（最后一块，必须在所有提示之后） ----
    # 这不是"建议"，是流程的强制断点：不打这一段，用户就不知道要反馈什么；
    # 不 clear，下一段脚本会退出码 3 拒绝执行。
    if gate and not a.no_gate:
        gate.deliver(stage, a.xlsx, rows=n_p + n_k,
                     note='参数 %d 行 / 价格 %d 行' % (n_p, n_k))


def _selftest_combo():
    """组合型号解析自测（不联网）。跑法：python autofill_quote.py --selftest"""
    cases = [
        ('TL-VS7500+TL-NVDU7606S*4', [('TL-VS7500', 1), ('TL-NVDU7606S', 4)], 'TL-VS7500'),
        ('TL-VS7500+TL-NVDU7606S*3', [('TL-VS7500', 1), ('TL-NVDU7606S', 3)], 'TL-VS7500'),
        ('TL-EML100+TL-EML100-LZ', [('TL-EML100', 1), ('TL-EML100-LZ', 1)], 'TL-EML100'),
        ('TL-EBL100+TL-EBL100-U', [('TL-EBL100', 1), ('TL-EBL100-U', 1)], 'TL-EBL100'),
        ('TL-FC311A-3/TL-FC311B-3', [('TL-FC311A-3', 1), ('TL-FC311B-3', 1)], 'TL-FC311A-3'),
        ('TL-EC100-M18*2', [('TL-EC100-M18', 2)], 'TL-EC100-M18'),
        # ⚠️ 小数数量后缀：2026-09-24 抚州S2地块清单里的 TL-SA3045*0.5
        #    （摆闸单机芯只要半副摆臂）——早期只认 \d+ 会静默当成 ×1 多算一倍
        ('TL-SA3045*0.5', [('TL-SA3045', 0.5)], 'TL-SA3045'),
        ('TL-KBG300-L+TL-SA3045*0.5+TL-AZJ320',
         [('TL-KBG300-L', 1), ('TL-SA3045', 0.5), ('TL-AZJ320', 1)], 'TL-KBG300-L'),
        ('TL-A+TL-B*2+TL-C*3', [('TL-A', 1), ('TL-B', 2), ('TL-C', 3)], 'TL-A'),
        ('TL-SG3226', [('TL-SG3226', 1)], 'TL-SG3226'),
        # 型号里合法出现的 / 后缀，不能被当成"两个型号"（/ 保留，整串查 CRM）
        ('TL-AP1206GC-PoE/DC易展版', [('TL-AP1206GC-PoE/DC易展版', 1)],
         'TL-AP1206GC-PoE/DC易展版'),
        # ⚠️ CRM 官方型号名**内嵌中文**（套装类）：全文扫描会在中文处断句，切成
        #    TL-E201 ×2 且 CRM 无裸型号 → 静默取价失败（2026-09-30 智慧医院监控实测）
        ('TL-E201固定端&TL-E201轿厢端套装',
         [('TL-E201固定端&TL-E201轿厢端套装', 1)], 'TL-E201固定端&TL-E201轿厢端套装'),
        ('TL-E521套装', [('TL-E521套装', 1)], 'TL-E521套装'),
        # 含中文但内嵌了**另一个**型号 → 仍按组合拆（不能被 ⓿ 分支吞掉）
        ('TL-VS7500（含TL-NVDU7606S业务板卡）',
         [('TL-VS7500', 1), ('TL-NVDU7606S', 1)], 'TL-VS7500'),
        # ⚠️ 型号尾部带半角括号后缀 + 数量：2026-09-30 智慧医院清单实测
        #    TL-PSM550-AC(F)*2 早期被截成 TL-PSM550-AC 且数量退化成 ×1（静默少算一个电源）
        ('TL-CS6260-48S6CQ+TL-PSM550-AC(F)*2+TL-FAN040A3-CF*2',
         [('TL-CS6260-48S6CQ', 1), ('TL-PSM550-AC(F)', 2), ('TL-FAN040A3-CF', 2)],
         'TL-CS6260-48S6CQ'),
        ('TL-PSM550-AC(F)*2', [('TL-PSM550-AC(F)', 2)], 'TL-PSM550-AC(F)'),
        ('TL-PSM550-AC(F)', [('TL-PSM550-AC(F)', 1)], 'TL-PSM550-AC(F)'),
    ]
    bad = 0
    print('=== 组合型号解析自测 ===')
    for s, exp, exp_first in cases:
        got = parse_combo(s)
        gf = parse_combo_first(s)
        okc = (got == exp)
        okf = (gf == exp_first)
        flag = 'OK' if (okc and okf) else '!! FAIL'
        if not (okc and okf):
            bad += 1
        print('  %-30s %-46s 首个=%-24s %s' % (s, got, gf, flag))

    # 关键断言：组合行的价格必须是加总，不能等于第一个配件单价
    print()
    print('=== 组合加总断言（模拟算价，不联网）===')
    # 这里只是验证「加总」这个算术，rate 用个示意值，不代表任何商务规则
    t = clean_price(36000 * 0.27, True) + clean_price(9200 * 0.27, True) * 4
    print('  TL-VS7500+TL-NVDU7606S*4  公式加总 = %s（预期 19656）' % t)
    if t != 19656:
        print('  !! FAIL 加总公式不对'); bad += 1
    else:
        print('  OK')

    # 关键断言：折扣口径必须是外部传入的，脚本内不得内置任何折扣规则
    print()
    print('=== 折扣口径外部化断言（不联网）===')
    SPECS = [
        ({'mode': 'none'}, None, '无折扣'),
        ({'mode': 'flat', 'rate': 0.34}, 0.34, '统一折扣'),
    ]
    row = {'price': 1000, 'businessDiscount': '0.32', 'seriesValue': '项目机型'}
    for spec, want_rate, label in SPECS:
        got_rate, _ = resolve_rate(spec, row)
        ok = (got_rate == want_rate)
        print('  %-22s rate=%-6s %s' % (label, got_rate, 'OK' if ok else '!! FAIL'))
        if not ok:
            bad += 1

    # 按字段判定：命中才打折、不命中取标准价
    spec = {'mode': 'by_field', 'field': 'seriesValue',
            'values': ['项目机型'], 'rate': 0.34}
    r1, _ = resolve_rate(spec, {'seriesValue': '项目机型'})
    r2, _ = resolve_rate(spec, {'seriesValue': '域联产品'})
    ok = (r1 == 0.34 and r2 is None)
    print('  %-22s 命中=%-6s 未命中=%-6s %s'
          % ('按字段判折扣', r1, r2, 'OK' if ok else '!! FAIL'))
    if not ok:
        bad += 1

    # 按对照表折算：业务折扣率 0.32 → 0.27
    spec = {'mode': 'by_discount_table', 'field': 'seriesValue',
            'values': ['项目机型'], 'table': {'0.32': 0.27, '0.39': 0.34}}
    r3, _ = resolve_rate(spec, {'seriesValue': '项目机型', 'businessDiscount': '0.32'})
    r4, _ = resolve_rate(spec, {'seriesValue': '项目机型', 'businessDiscount': '0.99'})
    ok = (r3 == 0.27 and r4 is None)
    print('  %-22s 0.32→%-5s 表外→%-5s %s'
          % ('按对照表折算', r3, r4, 'OK' if ok else '!! FAIL'))
    if not ok:
        bad += 1

    # 按类别分档：交换机走总部、摄像机走片区
    spec = make_by_class_spec()
    r5, n5 = resolve_rate(spec, {'seriesValue': '项目机型',
                                 'businessDiscount': '0.32',
                                 'productModel': 'TL-NS650-48G6T'})
    r6, n6 = resolve_rate(spec, {'seriesValue': '项目机型',
                                 'businessDiscount': '0.32',
                                 'productModel': 'TL-IPC544EP-A'})
    ok = (r5 == 0.25 and r6 == 0.27)
    print('  %-22s 交换机→%-5s 摄像机→%-5s %s'
          % ('按类别分档', r5, r6, 'OK' if ok else '!! FAIL'))
    if not ok:
        bad += 1

    # 换算表本身要正确
    ok = (BIZDISC_TO_PQ == {'0.45': 0.43, '0.43': 0.40, '0.39': 0.34, '0.32': 0.27}
          and BIZDISC_TO_HQ == {'0.45': 0.40, '0.43': 0.40, '0.39': 0.32, '0.32': 0.25})
    print('  %-22s %s' % ('换算对照表', 'OK' if ok else '!! FAIL 表被改动'))
    if not ok:
        bad += 1

    # 缺 rate_spec 必须报错，不能静默给默认折扣
    try:
        resolve_rate(None, row)
        print('  %-22s !! FAIL 缺口径竟未报错' % '缺口径应报错')
        bad += 1
    except ValueError:
        print('  %-22s OK（强制用户指定）' % '缺口径应报错')

    # 控标库模糊匹配必须过词边界：同系列不同型号不得互相顶替
    print()
    print('=== 控标库模糊匹配边界断言（不联网）===')
    fake = {'TL-SG2210P': [{'model': 'TL-SG2210P'}],
            'TL-SH5428F-COMBO': [{'model': 'TL-SH5428F-Combo'}],
            'TL-VS7500（含TL-NVDU7606S业务板卡）': [{'model': 'TL-VS7500（含业务板卡）'}]}
    cases = [('TL-SG2210', None, '不得被 TL-SG2210P 顶替'),
             ('TL-SH5428', None, '不得被 TL-SH5428F-Combo 顶替'),
             ('TL-VS7500', 'TL-VS7500（含业务板卡）', '带括号注解的应能模糊命中')]
    for m, want, why in cases:
        e = kb_lookup(fake, [m])
        got = e.get('model') if e else None
        ok = (got == want)
        print('  %-14s → %-30s %s（%s）' % (m, got, 'OK' if ok else '!! FAIL', why))
        if not ok:
            bad += 1

    # 型号后缀边界：中文后缀（套装/固定端…）不得阻断 CRM 前缀回退，ASCII 后缀仍须拦截
    print()
    print('=== 型号后缀边界断言（不联网）===')
    boc = [('TL-E521套装', 'TL-E521', True,
            '中文后缀「套装」应视为合法后缀，否则静默查不到价（2026-09-30 实测）'),
           ('TL-SG2210P', 'TL-SG2210', False,
            'ASCII 字母后缀仍须拦截，防撞同系列不同型号')]
    for k, c, exp, why in boc:
        got = _boundary_ok(k.upper(), c.upper())
        ok = (got == exp)
        print('  %-22s vs %-12s → %-5s %s（%s）'
              % (k, c, got, 'OK' if ok else '!! FAIL', why))
        if not ok:
            bad += 1

    # 价格口径闸门：公式不得被当成折扣说明，真文本仍须拦
    # （2026-10-27 实测：=125*0.03 里的 *0.03 被当成「×0.03」，把一张 10 行清单卡死）
    print()
    print('=== 价格口径闸门（公式 vs 文本）===')
    try:
        from openpyxl import Workbook as _WB
        _wb = _WB(); _ws = _wb.active
        _ws['A1'] = '序号'
        _ws['I26'] = '=125*0.03'                     # ① 原表公式
        _ws['J26'] = '单价按业务折扣后单价，折扣率0.34'  # ② 真·口径说明
        leak_cases = [
            ('只查公式格 I26', [(26, 9)], 0,
             '公式是计算不是说明，误判会把带税率/系数的客户表卡死'),
            ('只查文本格 J26', [(26, 10)], 1,
             '真·折扣说明必须拦，否则报价表把底牌给甲方'),
            ('同时查 I26+J26', [(26, 9), (26, 10)], 1,
             '同一行里公式放行、文本照拦'),
        ]
        for name, cells, exp, why in leak_cases:
            got = len(scan_price_leak(_ws, 1, cells))
            ok = (got == exp)
            print('  %-18s → 报 %d 处 %s（%s）'
                  % (name, got, 'OK' if ok else '!! FAIL', why))
            if not ok:
                bad += 1
    except ImportError:
        print('  跳过：没装 openpyxl')

    print()
    print('自测结果：%s' % ('全部通过' if bad == 0 else '%d 项失败' % bad))
    return bad


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.exit(1 if _selftest_combo() else 0)
    main()
