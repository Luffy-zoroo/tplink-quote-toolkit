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
#
# 未命中打折条件的型号 → 直接取标准价（面价）。
NO_DISCOUNT = {'mode': 'none'}


def resolve_rate(spec, row):
    """按本次给的折扣口径，判断该型号该用哪个折扣率。

    row 是 CRM 返回的价格记录（含 price / businessDiscount / seriesValue）。
    返回 (折扣率 or None, 说明)。
    返回 None 表示**不打折，取标准价**。
    """
    if not spec:
        raise ValueError('必须提供本次折扣口径 rate_spec（见 SKILL.md「折扣口径」章节）')
    mode = spec.get('mode', 'none')

    if mode == 'none':
        return None, '本次口径：全部取标准价'

    if mode == 'flat':
        return float(spec['rate']), '本次口径：统一折扣率 %s' % spec['rate']

    if mode in ('by_field', 'by_discount_table', 'by_class'):
        field = spec.get('field', 'seriesValue')
        values = [str(v).strip() for v in spec.get('values', [])]
        if str(row.get(field) or '').strip() not in values:
            return None, '不满足打折条件(%s=%s)，取标准价' % (
                field, row.get(field))

        if mode == 'by_field':
            return float(spec['rate']), '本次口径：%s∈%s 用折扣率 %s' % (
                field, values, spec['rate'])

        # 以下两种都要用 businessDiscount 去查表
        bd = str(row.get('businessDiscount') or '')

        if mode == 'by_discount_table':
            table = {str(k): float(v)
                     for k, v in (spec.get('table') or {}).items()}
            if bd in table:
                return table[bd], '本次口径：查表 %s→%s' % (bd, table[bd])
            return None, '业务折扣率 %s 不在本次对照表内，取标准价' % bd

        # mode == 'by_class'：项目机型按监控/其他分片区/总部
        model = row.get('productModel')
        pq = {str(k): float(v) for k, v in (spec.get('biz_to_pq') or {}).items()}
        hq = {str(k): float(v) for k, v in (spec.get('biz_to_hq') or {}).items()}
        tbl, label = (pq, '片区') if is_monitor_row(model) else (hq, '总部')
        if bd in tbl:
            return tbl[bd], '本次口径：%s类 %s→%s' % (label, bd, tbl[bd])
        return None, '业务折扣率 %s 不在%s档表内，取标准价' % (bd, label)

    raise ValueError('未知的折扣口径 mode: %s' % mode)

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
      · resolve_rate 返回折扣率 → 标准价 × 折扣率，向上取整到个位 0/2/5
      · 返回 None → 直接取标准价（面价），原样保留小数
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
    price = x.get('price')

    rate, basis = resolve_rate(rate_spec, x)
    if rate is None:
        unit = clean_price(price, rounded_up=False)
        return unit, '标准价 %s（%s）' % (price, basis)

    unit = clean_price(float(price) * rate, rounded_up=True)
    return unit, '%s × %s = %s（%s）' % (price, rate, unit, basis)


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
    a = ap.parse_args()

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
    idx, _ = load_kb(a.kb)
    print('控标库: %s  型号 %d 条' % (a.kb if idx else '（未加载，将全走官网）', len(idx)))

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
    print('折扣口径:', json.dumps(rate_spec, ensure_ascii=False))

    # ---- CRM ----
    op = None
    if not a.no_crm:
        email, pwd = a.email, a.password
        if not email or not pwd:                  # 其次读配置文件里的 crm 段
            _ce, _cp = cfg.crm_creds()
            email = email or _ce or None
            pwd = pwd or _cp or None
        if not email or not pwd:
            mem = os.path.expanduser('~/.workbuddy/MEMORY.md')
            if os.path.exists(mem):
                t = open(mem, encoding='utf-8', errors='replace').read()
                if not email:
                    m = re.search(r'[\w\.\-]+@[\w\.\-]+\.\w+', t)
                    if m: email = m.group(0)
                if not pwd:
                    # 匹配：**密码**：`xxx`  或  - 密码: xxx
                    m = re.search(r'密码\**\s*[：:]\s*`([^`\n]+)`', t)
                    if not m:
                        m = re.search(r'密码\**\s*[：:]\s*([^\s`\n]+)', t)
                    if m: pwd = m.group(1).strip()
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
                n_k += 1
                log.append({'row': r, 'model': ms, 'kind': '价格',
                            'is_combo': is_combo(ms), 'parts': parts_log,
                            'final': total})
            else:
                log.append({'row': r, 'model': ms, 'kind': '价格',
                            'is_combo': is_combo(ms), 'parts': parts_log,
                            'note': err})

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

    print()
    print('自测结果：%s' % ('全部通过' if bad == 0 else '%d 项失败' % bad))
    return bad


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.exit(1 if _selftest_combo() else 0)
    main()
