# -*- coding: utf-8 -*-
"""带机量核算：清单「需经网关的 IP 主机数」 vs 出口设备「最大带机量」。

为什么要把交换机算进来（2026-09-22 用户口径）：
    网管型交换机自身持有管理 IP，且支持商用云平台/商云 APP 远程管理与告警上报，
    这些流量必须经网关 NAT 出公网。做带机量核算时它是一台独立 IP 主机。

用法：
    python capacity_check.py --xlsx 清单.xlsx --sheet 表名 \
        --exit 151:TL-ER8920T --exit 152:TL-NR4914-S [--info-points 1800]

    --exit  可重复；格式「行号:型号」或「型号」。带机量自动从控标参数库解析。
    --info-points  工位信息点终端数（清单未列时人工给，用于估算总量）
    --scope all|net  统计口径。默认 all（全量 IP 主机）；
                     net = 只计网络基础设施（交换机 + 无线 AP + 无线控制器 AC），
                     剔除摄像机、门禁、广播、BA 等业务 IP 设备。

控标库路径默认取用户级 MEMORY.md 里记录的位置，可用 --kb 覆盖。
"""
import argparse, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nat_classify import extract, classify  # noqa: E402
import tpkit_config as cfg                  # noqa: E402

# 路径统一走配置（~/.workbuddy/tplink-toolkit.config.json），不再写死本机路径
KB_DEFAULT = cfg.path('kongbiao_json', must=False)
FW_SPEC_DEFAULT = cfg.path('fw_spec_json', must=False)

# 网络基础设施（--scope net 的口径）
NET_SCOPE = re.compile(r'交换机|无线AP$|大功率AP$|面板型AP$|无线控制器')


def parse_fw_spec(path, model):
    """从官方《防火墙与行为管理控制器产品性能规格》JSON 兜底取带机量。

    控标参数库**没有「最大带机量」这一项**（只有并发/吞吐），防火墙与行为管理
    控制器的官方带机量只能从这份规格表拿。字段名：带机量 / 最大并发连接数。
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        d = json.load(open(path, encoding='utf-8'))
    except Exception:
        return {}
    raw_halt = d.get('停产(License表标注或CRM HALT)')
    # 该键结构可能是 list / dict，统一用正则从序列化文本里抠型号，避免猜结构
    halt = {m.upper() for m in re.findall(r'TL-[A-Z0-9][A-Z0-9\-]*',
                                         json.dumps(raw_halt, ensure_ascii=False))}
    for m in d.get('models', []):
        # ⚠ 规格表解析出的 model 带版本号后缀，形如 "TL-FW5900  1.0"（两个空格+版本），
        #   必须按首个空白切分后再比对，否则裸型号永远匹配不上。
        raw = str(m.get('model') or '')
        base = raw.split()[0].upper() if raw.strip() else ''
        if base != model.upper():
            continue
        out = {'src': '官方规格表 20251120'}
        cap = str(m.get('带机量') or '')
        if cap:
            out['capacity_raw'] = cap
            nums = re.findall(r'\d+', cap.replace(',', ''))
            if nums:
                out['capacity'] = int(nums[-1]) if '~' in cap else int(nums[0])
        conn = str(m.get('最大并发连接数') or '')
        mm = re.search(r'([\d.]+)\s*([WK万]?)', conn)
        if mm:
            out['conn'] = int(float(mm.group(1)) * {'W': 10000, 'K': 1000, '万': 10000, '': 1}[mm.group(2)])
        if model.upper() in halt:
            out['halt'] = True
        return out
    return {}


def apply_scope(items, scope):
    """按统计口径过滤条目。

    scope='net'：只保留网络基础设施自身（交换机 / AP / AC）需占 IP 的条目，
                 剔除监控、门禁、广播、BA 等业务 IP 设备。
                 出口设备（nat=='E'）始终保留——它们是判定对象，不是被带机方。
    """
    if scope != 'net':
        return items
    return [i for i in items if i['nat'] != 'Y' or NET_SCOPE.search(i['name'])]

# 没有「最大带机量」指标的设备，改用并发连接数 / 吞吐做等效判断
FALLBACK_METRIC = re.compile(r'最大并发连接数不?少于?\s*([\d.]+)\s*([WK万]?)条?|纯NAT吞吐量不?少于?\s*([\d.]+)Mbps')


def parse_capacity(kb_path, model):
    """从控标库解析某型号的 最大带机量 / 并发 / 吞吐。"""
    if not os.path.exists(kb_path):
        return {}
    d = json.load(open(kb_path, encoding='utf-8'))
    for x in d:
        if (x.get('model') or '').strip().upper() != model.upper():
            continue
        txt = '\n'.join(p['text'] for p in x.get('param_items', []))
        out = {'name': x.get('product_name') or '', 'category': x.get('category') or ''}
        m = re.search(r'最大带机量\D{0,6}(\d+)', txt)
        if m:
            out['capacity'] = int(m.group(1))
        m = re.search(r'最大并发连接数\D{0,8}([\d.]+)\s*([WK万]?)', txt)
        if m:
            v = float(m.group(1)); u = m.group(2)
            out['conn'] = int(v * {'W': 10000, 'K': 1000, '万': 10000, '': 1}[u])
        m = re.search(r'纯NAT吞吐量\D{0,8}([\d.]+)\s*Mbps', txt)
        if m:
            out['nat'] = float(m.group(1))
        m = re.search(r'包转发率\D{0,8}([\d.]+)\s*Mpps', txt)
        if m:
            out['pps'] = float(m.group(1))
        return out
    return {}


def selftest():
    """口径过滤自测（不依赖清单文件）。"""
    bad = 0

    def chk(desc, got, want):
        nonlocal bad
        if got != want:
            bad += 1
            print('  [FAIL] %s  期望 %r 实得 %r' % (desc, want, got))
        else:
            print('  [ OK ] %s' % desc)

    def it(name, nat, qty=1):
        return {'name': name, 'nat': nat, 'qty': qty, 'sys': '计算机网络系统'}

    sample = [
        it('24口接入交换机', 'Y', 100),
        it('核心层交换机（6U以下）', 'Y', 6),
        it('室内无线AP', 'Y', 500),
        it('室外大功率AP', 'Y', 20),
        it('面板型AP', 'Y', 150),
        it('无线控制器（AC）', 'Y', 2),
        it('网络摄像机', 'Y', 300),
        it('门禁控制器', 'Y', 50),
        it('IP网络广播终端', 'Y', 53),
        it('DDC控制器', 'Y', 20),
        it('防火墙', 'E', 2),
        it('路由器', 'E', 2),
        it('千兆单模光模块', 'N', 300),
    ]
    kept = apply_scope(sample, 'net')
    names = [i['name'] for i in kept]

    chk('net 口径保留交换机', '24口接入交换机' in names, True)
    chk('net 口径保留 AP', '室内无线AP' in names, True)
    chk('net 口径保留 AC', '无线控制器（AC）' in names, True)
    chk('net 口径剔除摄像机', '网络摄像机' in names, False)
    chk('net 口径剔除门禁控制器', '门禁控制器' in names, False)
    chk('net 口径剔除 IP 广播', 'IP网络广播终端' in names, False)
    chk('net 口径剔除 BA/DDC', 'DDC控制器' in names, False)
    chk('net 口径保留出口设备供核对',
        sorted(i['name'] for i in kept if i['nat'] == 'E'), ['路由器', '防火墙'])
    chk('net 口径 Y 台数只含网络设备',
        int(sum(i['qty'] for i in kept if i['nat'] == 'Y')), 100 + 6 + 500 + 20 + 150 + 2)
    chk('all 口径不做过滤', len(apply_scope(sample, 'all')), len(sample))

    print()
    print('自测结果：%s' % ('全部通过' if bad == 0 else '%d 项失败' % bad))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description='清单带机量核算')
    ap.add_argument('--xlsx', default=None, help='清单文件（--selftest 时可省）')
    ap.add_argument('--sheet', default=None)
    ap.add_argument('--exit', action='append', default=[], help='格式 行号:型号 或 型号')
    ap.add_argument('--info-points', type=int, default=0, help='工位信息点终端数（可选）')
    ap.add_argument('--scope', choices=['all', 'net'], default='all',
                    help='统计口径：all=全量 IP 主机（默认）；net=只计交换机+AP+AC')
    ap.add_argument('--kb', default=KB_DEFAULT)
    ap.add_argument('--fw-spec', default=FW_SPEC_DEFAULT,
                    help='官方《防火墙与行为管理控制器产品性能规格》JSON（带机量兜底）')
    ap.add_argument('--out-json', default=None)
    ap.add_argument('--selftest', action='store_true', help='跑口径过滤自测')
    # 列位（1 基），与 nat_classify 保持一致；清单模板各异，列位对不上时用这些覆盖
    ap.add_argument('--model-col', type=int, default=9, help='型号列，默认 I')
    ap.add_argument('--param-col', type=int, default=10, help='参数列，默认 J')
    ap.add_argument('--price-col', type=int, default=11, help='价格/小计列，默认 K')
    ap.add_argument('--name-col', type=int, default=3, help='设备名称列，默认 C')
    ap.add_argument('--unit-col', type=int, default=7, help='单位列，默认 G')
    ap.add_argument('--qty-col', type=int, default=8, help='数量列，默认 H')
    ap.add_argument('--seq-col', type=int, default=1, help='序号列，默认 A')
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if not a.xlsx:
        ap.error('需要 --xlsx（或使用 --selftest）')

    items = extract(a.xlsx, a.sheet,
                    model_col=a.model_col, param_col=a.param_col, price_col=a.price_col,
                    name_col=a.name_col, unit_col=a.unit_col, qty_col=a.qty_col, seq_col=a.seq_col)
    # 备注行（清单标注"没有该产品"）原本不参与统计，但带机量核算必须计入真实需求：
    # 例如"防爆球机 10 台"清单注明暂无型号，设备仍然要装、要占 IP。
    for i in items:
        if i['nat'] == '-':
            v, w = classify(i['name'], i.get('sys'))
            i['nat'] = v
            i['why'] = ('备注行：清单未列型号，但设备需求真实存在，计入带机量'
                        if v in ('Y', 'E') else w)
    # 出口设备（防火墙/路由器/网关/安全网关/网络安全服务器）自身就是 NAT 路径上的
    # 转发设备，不"被带机"——从带机量需求中扣除，单列展示。
    EXIT_PAT = re.compile(r'^防火墙$|^路由器$|^网关$|网络安全服务器|上网行为管理|多业务安全网关')
    for i in items:
        # 限「计算机网络系统」内，避免把楼宇自控系统的"网关"也当成出口设备
        if (i['nat'] == 'Y' and EXIT_PAT.search(i['name'])
                and '计算机网络' in (i.get('sys') or '')):
            i['nat'] = 'E'
    items = apply_scope(items, a.scope)
    Y = [i for i in items if i['nat'] == 'Y']
    E = [i for i in items if i['nat'] == 'E']
    ip_tot = sum(float(i['qty'] or 0) for i in Y)
    sw = sum(float(i['qty'] or 0) for i in items if i['nat'] == 'Y' and '交换机' in i['name'])
    ap_qty = sum(float(i['qty'] or 0) for i in items
                 if i['nat'] == 'Y' and re.search(r'无线AP$|大功率AP$|面板型AP$', i['name']))
    ac_qty = sum(float(i['qty'] or 0) for i in items
                 if i['nat'] == 'Y' and '无线控制器' in i['name'])
    total = ip_tot + a.info_points

    print('=' * 76)
    print('清单：%s' % os.path.basename(a.xlsx))
    print('口径：%s' % ('只计网络设备（交换机 + 无线 AP + 无线控制器 AC）'
                      if a.scope == 'net' else '全量 IP 主机（含监控/门禁/广播/BA 等）'))
    print('=' * 76)
    print('  设备条目总数                  : %d 条' % len(items))
    print('  需经网关的 IP 主机            : %d 台（%d 条）' % (ip_tot, len(Y)))
    print('    其中 交换机                 : %d 台' % sw)
    print('    其中 无线 AP                : %d 台' % ap_qty)
    if a.scope == 'net':
        print('    其中 无线控制器 AC           : %d 台' % ac_qty)
    print('  出口设备（单列，不计入带机量）  : %d 条' % len(E))
    if a.info_points:
        print('  工位信息点终端                : %d 点' % a.info_points)
        print('  → 合计需承载                  : %d 台' % total)

    print()
    print('-' * 76)
    print('出口设备带机量核对')
    print('-' * 76)
    verdicts = []
    for spec in a.exit:
        row, model = spec.split(':', 1) if ':' in spec else ('', spec)
        info = parse_capacity(a.kb, model)
        src = '控标参数库'
        # 控标库不含「最大带机量」，用官方规格表兜底
        if not info.get('capacity'):
            fs = parse_fw_spec(a.fw_spec, model)
            if fs:
                src = fs.get('src', '官方规格表')
                for k in ('capacity', 'capacity_raw', 'conn', 'halt'):
                    if fs.get(k) is not None:
                        info[k] = fs[k]
        need = total if a.info_points else ip_tot
        cap = info.get('capacity')
        tag = ('第 %s 行 ' % row) if row else ''
        print('%s%s  (%s)' % (tag, model, info.get('name') or info.get('category') or '—'))
        bits = []
        if cap:
            bits.append('最大带机量 %s 台' % (info.get('capacity_raw') or cap))
        if info.get('conn'):
            bits.append('并发 %d 条' % info['conn'])
        if info.get('nat'):
            bits.append('纯NAT吞吐 %.0f Mbps' % info['nat'])
        print('    指标：%s   〔来源：%s〕' % ('；'.join(bits) or '（无带机量指标）', src))
        if info.get('halt'):
            print('    ⚠ 该型号已停产（lifeCycle=HALT），不建议进投标清单')
        if not cap:
            print('    判定：控标库与官方规格表都无带机量 —— 需向产品线索取规格')
            verdicts.append((model, None, '无指标'))
            continue
        ok = cap >= need
        hist = cap >= need * 1.5
        tight = ok and not hist
        print('    需求 %d 台 → 判定：%s（余量 %s）' % (
            need, '足够' if ok else '不足', '%d 台' % (cap - need) if ok else '缺口 %d 台' % (need - cap)))
        if ok:
            print('    负载率：%.1f%%　余量评级：%s' % (
                need / cap * 100, '充裕（负载≤70%）' if hist else '偏紧（建议不超过额定值 70%）'))
        else:
            print('    负载率：%.1f%%　余量评级：超载' % (need / cap * 100))
        verdicts.append((model, cap, '足够' if ok else '不足'))
    print()
    print('说明：带机量按「单台设备」口径核算。清单中同型号 ×2 若为双机热备，')
    print('      仍按单台带机量衡量；若为负载分担，可将容量 ×2 后再比对。')

    if a.out_json:
        json.dump({'items': items, 'ip_total': ip_tot, 'switch': sw,
                   'info_points': a.info_points, 'total': total,
                   'verdicts': verdicts}, open(a.out_json, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        print('明细已写入 %s' % a.out_json)

    return 0


if __name__ == '__main__':
    sys.exit(main())
