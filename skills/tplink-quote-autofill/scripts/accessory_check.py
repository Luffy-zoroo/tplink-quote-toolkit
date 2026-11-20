# -*- coding: utf-8 -*-
"""配单配件漏项检查：电源 / 风扇 / 业务板 / 主控 是否随机标配

背景（2026-11-20 用户要求）：
  数据中心级交换机、带冗余电源的中高端机型，**电源模块和风扇模块常常不随机**，
  官网 desc 末尾会写「XX 需单独购买」，不看就会漏报、投标价直接亏。
  典型案例：TL-CS6260-48S6CQ 电源 TL-PSM550-AC(F)、风扇 TL-FAN040A3-CF 均需另购，
  一台漏配 = 少报 4190 元（2 电源 + 4 风扇）。

用法：
  python scripts/accessory_check.py TL-CS6260-48S6CQ TL-NAC1000S
  python scripts/accessory_check.py --file models.txt
  python scripts/accessory_check.py --file models.txt --json out.json

退出码：0 全部通过；1 发现需补配件（提醒不是错误）；2 用法/数据错误
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---- 关键词 ----
# desc 里出现这些模式 → 该配件不随机
RE_NEED_BUY = re.compile(r'(?P<item>[^/；;。]{0,40}?)(?:需|须|需另行|需单独)(?:单独)?(?:购买|选配|订购)')
RE_NO_INCLUDE = re.compile(r'(?P<item>[^/；;。]{0,30}?)(?:不含|不随机|不标配|另购)')
# 电源字段里的"只有单电源"类描述
RE_SINGLE_PSU = re.compile(r'(出厂只有单电源|出厂仅配单电源|默认单电源|标配单电源|单电源出厂)')

# 配件大类（用于归类输出）
CATS = [
    ('电源', ['电源', 'PSU', 'PSM']),
    ('风扇', ['风扇', 'FAN']),
    ('主控板', ['主控', 'MPU']),
    ('业务板', ['业务板', '板卡', '接口板', '业务模块']),
    ('光模块', ['光模块', 'SFP', 'QSFP']),
    ('堆叠线', ['堆叠线', '堆叠电缆', 'SFP+电缆']),
    ('硬盘', ['硬盘', 'HDD']),
]

def cat_of(text):
    for name, kws in CATS:
        if any(k.upper() in text.upper() for k in kws):
            return name
    return '其他'

def load_products():
    """读选型库（dealer_backup_json）。未配置 / 配置了但读不到，分别报错，不静默降级。"""
    try:
        import tpkit_config as cfg
    except ImportError:
        print('ERROR 缺少 tpkit_config.py（应与本脚本同级）', file=sys.stderr)
        sys.exit(2)
    path = cfg.path('dealer_backup_json', must=False)
    if not path:
        print('ERROR 未配置 dealer_backup_json（选型库路径）。\n'
              '  在配置文件的顶层加一行：\n'
              '    "dealer_backup_json": "D:/你的路径/tplink_dealer_backup.json"\n'
              '  配置文件位置见 tpkit_config.py 或仓库 config.example.json。',
              file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(path):
        print('ERROR 配置的选型库不存在：%s\n'
              '  路径没错但文件不在 → 先备份一次（backup.py），或改配置指到现有文件。'
              % path, file=sys.stderr)
        sys.exit(2)
    return json.load(open(path, encoding='utf-8'))['products']

def check_one(model, prods):
    """返回 dict: {model, name, halt, need:[{cat,item,suggest,evidence}], warn:[...]}"""
    r = prods.get(model)
    if not r:
        return dict(model=model, name=None, missing=True, need=[], warn=['选型库里没有这个型号'])

    desc = (r.get('desc') or '') + '\n' + (r.get('spotlight') or '')
    specs = r.get('spec') or []
    need, warn = [], []

    # 1) desc / spotlight 里的"需单独购买 / 不含 / 另购"
    seen = set()
    for m in RE_NEED_BUY.finditer(desc):
        seg = m.group(0).strip()
        if seg in seen:
            continue
        seen.add(seg)
        need.append(dict(cat=cat_of(seg), item=seg, suggest=None, evidence='desc：' + seg))
    for m in RE_NO_INCLUDE.finditer(desc):
        seg = m.group(0).strip()
        if seg in seen or len(seg) < 4:
            continue
        seen.add(seg)
        need.append(dict(cat=cat_of(seg), item=seg, suggest=None, evidence='desc：' + seg))

    # 2) spec 电源字段里的"出厂只有单电源"
    for g in specs:
        name = str(g.get('name') or '')
        val = ';'.join(str(v) for v in (g.get('values') or []))
        if '电源' in name and RE_SINGLE_PSU.search(val):
            need.append(dict(cat='电源', item='出厂仅单电源，冗余需补配',
                             suggest='按 1+1 冗余补 1 个电源模块',
                             evidence='spec[%s]：%s' % (name, val)))

    # 3) 框式机型提示（有业务槽但没写端口 → 必须配板卡）
    txt = model + ' ' + (r.get('name') or '')
    if re.search(r'(插卡式|框式|多业务智能路由)', txt):
        warn.append('框式/插卡式机型：需另配主控板 + 业务板 + 电源 + 风扇，逐项确认')

    # 4) 无电源字段的存储/服务器类 → 提示核实是否标配双电源
    has_psu = any('电源' in str(g.get('name') or '') for g in specs)
    if not has_psu and any(w in txt for w in ('存储', '服务器', '磁盘阵列')):
        warn.append('未查到电源字段：需核实是否标配双电源（招标常要求 1+1 冗余）')

    return dict(model=model, name=r.get('name'), halt=bool(r.get('halt')),
                missing=False, need=need, warn=warn)

def main():
    ap = argparse.ArgumentParser(description='配单配件漏项检查（电源/风扇/板卡）')
    ap.add_argument('models', nargs='*', help='型号，可多个')
    ap.add_argument('--file', help='型号清单文件（每行一个）')
    ap.add_argument('--json', help='结果写入 JSON 文件')
    a = ap.parse_args()

    models = list(a.models)
    if a.file:
        with open(a.file, encoding='utf-8') as f:
            models += [ln.strip() for ln in f if ln.strip() and not ln.startswith('#')]
    if not models:
        ap.print_help()
        sys.exit(2)

    prods = load_products()
    results = [check_one(m, prods) for m in models]

    bad = [x for x in results if x['need'] or x['warn'] or x['missing']]
    for x in results:
        if not x['need'] and not x['warn'] and not x['missing']:
            continue
        title = x['model'] + ('　【' + x['name'] + '】' if x['name'] else '')
        if x['missing']:
            print('✗ %s　选型库无此型号' % title)
            continue
        print('! %s%s' % (title, '　（已停产）' if x['halt'] else ''))
        for n in x['need']:
            sug = ('　→ ' + n['suggest']) if n['suggest'] else ''
            print('    [%s] %s%s' % (n['cat'], n['item'], sug))
        for w in x['warn']:
            print('    [提示] %s' % w)
        print('')

    print('—— 共查 %d 个型号，%d 个需补配件/待核实 ——' % (len(results), len(bad)))
    if not bad:
        print('全部型号电源、风扇随机标配，无需补配。')

    if a.json:
        json.dump(results, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('JSON →', a.json)

    sys.exit(1 if bad else 0)

if __name__ == '__main__':
    main()
