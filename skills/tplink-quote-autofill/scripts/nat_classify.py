# -*- coding: utf-8 -*-
"""弱电智能化清单：设备/材料统计 + 网关 NAT 分类

从工程清单（xlsx）里枚举全部设备与材料条目，逐条判定"是否需要走网关 NAT"。

判定口径
  需要走网关 NAT  = 设备自身有 IP，且会主动发起跨网段/外网通信
                    （云平台纳管、远程查看、跨 VLAN 访问服务器、与上级平台对接）
  不需要走网关 NAT = 无独立 IP；或仅在同二层域内转发/受控/供电，不主动发起跨网段访问
  视选型而定      = 是否带网络接口取决于具体型号（网管型 UPS、网络打印机、音频处理器…）
  不适用          = 型号列写的是备注（"没有防爆球机""相关产品已停产"）

用法
  python nat_classify.py --xlsx "清单.xlsx" [--sheet "表名"] \
      --out-json items_nat.json --out-html nat_report.html
  python nat_classify.py --selftest          # 不联网，验证判定规则

⚠️ 规则维护提示：新增 rule 时注意**顺序**。「*支架 / *立杆」这类含"球机/枪机"字样的
   结构件必须排在摄像机规则之前，否则会被当成 IP 终端（实测踩过）。
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, OrderedDict

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

SEC = re.compile(r'^[一二三四五六七八九十]+、')
SUB = re.compile(r'^\([一二三四五六七八九十]+\)')

# ------------------------------------------------------------------ 判定规则
# 顺序有意义：越靠前越优先。
RULES = [
    # ---- 先排除会被摄像机关键词污染的安装件/管线/线缆 ----
    ('N', r'支架|立杆|室外防水箱|楼层安装箱|采集器箱|门禁控制箱|^岗亭$|^控制台（每联）$'
          r'|网络机柜|服务器机柜|电池柜|^PDU |机柜、操作台|^槽体恢复$|凿槽刨沟|支架制作安装',
     '安装件/机柜结构件，无网络功能'),
    ('N', r'线槽|线管|桥架|接线盒|金属软管|踢脚线|天花|地板|墙面|地面|微孔铝合金|彩钢板'
          r'|隔断|玻璃门|乳胶漆|防尘漆|保温层|铝箔|轻钢龙骨架|不锈钢|^LED灯$|照明开关|五孔插座',
     '装修/管线材料'),
    ('N', r'双绞线|大对数线缆|信号线|电线电缆|音频线|音箱线|音视频跳线|控制线缆|数据跳线'
          r'|语音跳线|单口面板|双口面板|地插面板',
     '线缆/跳线/面板，无源'),
    ('N', r'光模块|光纤|分纤箱|分光器|终端盒|光纤配线架|尾纤|跳线（LC-LC）|配线架|理线器|弱电箱',
     '无源光/铜配线器材，无 IP'),

    # ---- 需要走网关 NAT ----
    # 交换机必须算 Y：网管型交换机自身持有管理 IP，且本类清单型号均支持商用云平台/
    # 商云 APP 远程管理与告警上报 —— 这些流量必须经网关 NAT 出公网。做带机量核算时，
    # 交换机是一台独立的 IP 主机（本项由用户 2026-09-22 明确纠正，勿再判 N）。
    ('Y', r'交换机', '网管型交换机：自身有管理 IP，云管理/远程运维须经网关'),
    ('Y', r'摄像机|球机|枪机|半球|鹰眼|相机', '网络摄像机，有 IP，需云平台/远程查看'),
    ('Y', r'录像机', 'NVR 有 IP，需远程访问与平台纳管'),
    ('Y', r'解码器$|解码器\b', '解码器有 IP，需从 NVR/平台取流'),
    ('Y', r'监控授权管理一体机', '综合管理一体机，有 IP，需外网/远程访问'),
    ('Y', r'监控控制键盘', '网络控制键盘，有 IP'),
    ('Y', r'无线AP$|大功率AP$|面板型AP$', 'AP 有 IP，需 AC 跨 VLAN 纳管 + 云平台'),
    # ⚠️ 早期只写「无线控制器（AC）」，清单里写成「无线控制器」就命中不了（2026-09-24 实测踩到）。
    ('Y', r'无线控制器|无线控制设备|^AC$|（AC）$', 'AC 有 IP，需管理 AP 与云平台'),
    # 出口设备：上网行为管理同样是 NAT 路径上的设备（早期漏写，2026-09-24 实测清单未命中）
    ('Y', r'^防火墙$|^路由器$|^网关$|网络安全服务器|无线路由器|上网行为管理', '网络出口/边界设备，本身承担 NAT'),
    ('Y', r'门禁控制器|人脸识别一体机|访客机|考勤机|发卡器|梯控控制器'
          r'|闸机用人脸识别装置',
     '网络型门禁/梯控设备，需平台下发权限'),
    ('Y', r'报警主机|报警控制键盘|报警联动模块|双防区脉冲主机', '报警主机带网络/485 上行至平台'),
    ('Y', r'网络引擎', 'BA 网络引擎，有 IP，上行至自控平台'),
    ('Y', r'DDC控制箱|PLC控制箱', '控制器有 IP/网关上行，需接入自控平台'),
    ('Y', r'智能照明网关|风机盘管网关', '网关设备，需与上层系统对接'),
    ('Y', r'IP网络桌面寻呼站|IP网络有源监听音箱|机柜式IP功放|广播主机|自办节目播放器'
          r'|^4路编码器|^8路编码器',
     'IP 广播/编码设备，有 IP'),
    ('Y', r'IPTV机顶盒', 'IPTV 机顶盒，需经网关访问 IPTV 平台'),
    ('Y', r'多媒体控制器|查询机|信息发布一体机', '信息发布终端/控制器，有 IP 需下发内容'),
    ('Y', r'车位引导控制器|视频车位探测器|反向寻车终端|出入口显示屏|智能一体道闸|信号转换器'
          r'|落地式剩余车位显示屏|LED车位引导屏',
     '停车/车位引导网络设备，有 IP'),
    ('Y', r'会议一体机$', '会议一体机内置网络模块，需联网'),
    ('Y', r'高清混合矩阵', '矩阵带网络控制口，需远程控制'),
    ('Y', r'管理工作站|^服务器$', '工作站/服务器，需跨网段访问各子系统'),
    ('Y', r'区域采集器', '能源采集器有 IP/上行通道'),
    ('Y', r'UPS主机', '机房 UPS 配 SNMP/网络监控卡，需经网关接入动环监控'),
    # 软件不是主机：部署在服务器上，不单独占 IP、不单独算带机量（2026-09-22 更正）。
    ('N', r'软件$|软件（|接口集成开发', '软件/接口开发服务，部署于服务器，不单独占 IP'),

    # ---- 不需要走网关 NAT ----
    ('N', r'光纤收发器', '非网管收发器，二层透明桥接，无独立对外 IP 访问'),
    ('N', r'^硬盘|硬盘$|硬盘 ', '存储介质（裸盘），由 NVR/阵列统一寻址'),
    ('N', r'^单门电磁锁|^双门电磁锁|电插锁|开门按钮|破玻按钮|^IC卡$|^CPU卡$'
          r'|门禁读卡器|电梯读卡器',
     '受控末端（锁具/按钮/卡）与韦根26/34读卡器——挂门禁控制器下，无独立 IP'),
    ('N', r'继电器梯控联动模块', '联动模块挂控制器总线，无独立 IP'),
    ('N', r'^智能门锁$', '智能门锁经蓝牙/Zigbee 网关接入，锁体不直接持有 IP'),
    ('N', r'防区模块|红外探测器|玻璃破碎探测器|红外对射|电子围栏|脚挑开关|报警按钮'
          r'|紧急按钮|声光报警器|防区避雷器|同轴避雷器|防雷器',
     '报警探测/执行末端，总线或开关量，无 IP'),
    ('N', r'中继台|合路器|分路器|双工器|干线放大器|定向耦合器|全向天线|室外天线|定向天线|天线$'
          r'|馈线接头|射频线缆|馈线|对讲机|光纤直放站',
     '无线对讲/射频器件（天线、馈线、耦合器），不走 IP 网络'),
    ('N', r'巡更棒|巡更点|通讯座', '离线巡更，通讯座经 USB 接主机'),
    ('N', r'温度传感器|温湿度传感器|压力传感器|风速传感器|风向传感器|焓值传感器|风阀驱动器'
          r'|液位|漏水检测绳|温控器|压差开关|防冻开关|水流开关|CO浓度探测器|CO2浓度传感器'
          r'|PM2.5传感器',
     '传感器/执行器，挂 DDC 总线，无独立 IP'),
    ('N', r'开关模块|智能控制面板|红外感应器|照度感应器|^电源模块$', '照明总线模块/面板，由网关统一纳管'),
    ('N', r'天花喇叭|号角扬声器|草坪音箱|吸顶扬声器|^音箱$|返听音箱|^功放$|^音频处理器$'
          r'|鹅颈发言话筒|无线手持话筒|广播话筒|CD播放器|数字调谐器|节目定时器|电源时序器'
          r'|多媒体插座|HDMI输入卡|HDMI输出卡',
     '模拟音视频设备/板卡，无 IP'),
    ('N', r'^显示器$|液晶显示屏$|拼接屏|P1\.0 LED|P1\.25 LED|P1\.5 LED|P2\.5 LED|P3\.75 LED'
          r'|投影机|^打印机$',
     '纯显示/用电设备（无网络口，由控制器驱动）'),
    ('N', r'车位占用灯|超声波探测器', '车位探测器走总线，无 IP'),
    ('N', r'报警电源|电源管理器|开关电源|配电箱|等电位箱|紫铜排|绝缘端子|电池 |^电池柜$|电动吊架'
          r'|电动投影屏幕|入口台阶|入口斜坡|POE供电模块|PoE供电模块|POE模块|供电模块',
     '供电/接地材料、土建装饰件'),
]

# 精确名覆盖（优先于关键字规则）
OVERRIDE = {
    # 磁盘阵列带千兆 RJ45（如 TL-NVS7242-256 有 2×10/100/1000M 自适应口），可作存储服务器
    # → 占 IP、需经网关；早期误判为 N，2026-09-22 更正。
    '24盘位磁盘整列': ('Y', '磁盘阵列带千兆 RJ45，可作存储服务器，占 IP'),
    '36盘位磁盘整列': ('Y', '磁盘阵列带千兆 RJ45，可作存储服务器，占 IP'),
    '48盘位磁盘整列': ('Y', '磁盘阵列带千兆 RJ45，可作存储服务器，占 IP'),
    '无线网桥': ('Y', '网桥有管理 IP，需纳入网管'),
    '管理工作站': ('Y', '工作站需访问各子系统与平台，跨网段经网关'),
    '服务器': ('Y', '服务器承载各管理软件，需跨网段/外网访问'),
    '显示器': ('N', '显示器为工作站外设，无网络接口'),
    '打印机': ('Y', '集采清单里的打印机按网络型计，占 IP（若为 USB 本地打印则为 N）'),
    '门禁控制箱': ('N', '机箱箱体，控制器另行计列'),
    '发卡器': ('C', '网络发卡器需 NAT；USB 发卡器不需要，视选型'),
    '音频处理器': ('C', '带网络控制口的需 NAT；纯模拟/RS232 的不需要'),
    '投影机': ('C', '带网络控制口的需 NAT；纯 HDMI 输入的不需要'),
    '广播主机': ('Y', 'IP 广播主机有 IP，需平台/外网访问'),
    'CD播放器': ('N', '音源设备，模拟/音频接口接入广播主机'),
    '数字调谐器': ('N', '音源设备，接入广播主机'),
    '节目定时器': ('N', '广播控制设备，接入广播主机'),
    '报警主机': ('Y', '联网报警主机，需上传至接警平台'),
    '报警联动模块': ('C', '网络型联动模块需 NAT；挂主机的开关量模块不需要'),
    '报警控制键盘': ('N', '报警键盘挂报警主机总线，无独立 IP'),
    '报警电源': ('N', '报警系统供电单元'),
    '声光报警器': ('N', '声光报警末端，开关量'),
    '中继台': ('N', '无线对讲中继，射频链路，不走 IP'),
    '对讲机': ('N', '手持对讲终端，射频链路，不走 IP'),
    '双防区脉冲主机': ('N', '脉冲电子围栏主机，报警总线/开关量输出至报警主机'),
    '电子围栏（4线制）': ('N', '电子围栏前端（合金线/立柱等），射频总线'),
    '电子围栏（6线制）': ('N', '电子围栏前端（合金线/立柱等），射频总线'),
    '自办节目播放器': ('Y', '自办节目服务器/播放器，有 IP'),
    'IPTV机顶盒': ('Y', 'IPTV 机顶盒需经网关访问 IPTV 平台'),
    '8路编码器': ('Y', 'IP 编码器，需接入 IPTV 网络'),
    '4路编码器': ('Y', 'IP 编码器，需接入 IPTV 网络'),
    '网络引擎': ('Y', 'BA 网络引擎（上层管理站），有 IP'),
}

# 同名不同系统 → 判定不同
SYS_OVERRIDE = {
    ('计算机网络系统', '网关'): ('Y', '本系统网关即 NAT 出口设备（承担全网地址转换）'),
    ('楼宇设备自控系统', '网关'): ('Y', 'BA 网关，需与上层自控平台对接，有 IP'),
}

NOTE_MODELS = ('没有防爆球机', '没有65寸', '相关产品已停产')

LABEL = {'Y': '需走网关 NAT', 'N': '不需走网关', 'C': '视选型而定', '-': '不适用'}


def classify(name, sysname=''):
    for (sk, nm), val in SYS_OVERRIDE.items():
        if nm == name and sk in (sysname or ''):
            return val
    if name in OVERRIDE:
        return OVERRIDE[name]
    for verdict, pat, why in RULES:
        if re.search(pat, name):
            return verdict, why
    return 'C', '未命中规则，需人工确认'


def extract(xlsx, sheet=None, model_col=9, param_col=10, price_col=11,
            name_col=3, unit_col=7, qty_col=8, seq_col=1):
    import openpyxl
    wb = openpyxl.load_workbook(xlsx, data_only=False)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    # 跳过表头行（表头本身会被当成一条"项目名称"，实测漏进过来）
    start = 1
    for r in range(1, min(12, ws.max_row) + 1):
        v = ws.cell(r, name_col).value
        if v and ('项目名称' in str(v) or str(v).strip() in ('名称', '设备名称', '材料名称')):
            start = r + 1
            break
    items, cur = [], None
    for r in range(start, ws.max_row + 1):
        a = ws.cell(r, seq_col).value
        n = ws.cell(r, name_col).value
        if a is not None and n:
            s = str(a).strip()
            if SEC.match(s):
                cur = str(n).strip()
                continue
            if SUB.match(s):
                cur = (cur + ' / ' + str(n).strip()) if cur else str(n).strip()
                continue
        if n is None or str(n).strip() == '':
            continue
        # 合计/总计行不是设备条目 —— 实测清单末尾「合计」行会被当成一条设备捞进来
        if str(n).strip() in ('合计', '合 计', '总计', '小计', '总额', '合计金额', '总计金额'):
            continue
        mv = ws.cell(r, model_col).value
        items.append({
            'row': r, 'seq': a, 'sys': cur, 'name': str(n).strip(),
            'unit': str(ws.cell(r, unit_col).value or '').strip(),
            'qty': ws.cell(r, qty_col).value,
            'model': str(mv).strip() if mv else '',
            'has_param': bool(ws.cell(r, param_col).value),
            'has_price': bool(ws.cell(r, price_col).value),
        })
    for it in items:
        if it['model'] in NOTE_MODELS:
            it['nat'], it['why'] = '-', '备注行：清单未列该产品，不参与统计'
            continue
        v, w = classify(it['name'], it.get('sys'))
        it['nat'], it['why'] = v, w
    return items


def selftest():
    """判定规则自测（含顺序陷阱）。"""
    bad = 0
    cases = [
        ('球机壁装支架', 'N', '含"球机"字样的支架必须是 N（顺序陷阱）'),
        ('枪机吊装伸缩支架', 'N', '同上'),
        ('200W枪机', 'Y', '摄像机本体是 Y'),
        ('机柜式IP功放', 'Y', '"机柜式"不得被机柜规则吃掉'),
        ('网络机柜（42U）600*600', 'N', '机柜是 N'),
        ('门禁控制箱', 'N', '箱体是 N'),
        ('DDC控制箱', 'Y', '自控控制器是 Y'),
        ('室外立杆抱箱（防水）', 'N', '立杆抱箱是 N'),
        ('24口接入交换机', 'Y', '交换机有管理IP，云管理须经网关（2026-09-22 口径纠正）'),
        ('TL-EML100', 'C', '未知名称留 C 提醒人工确认'),
        ('单门电磁锁', 'N', '锁具末端无 IP'),
        ('门禁读卡器', 'N', '韦根26/34读卡器挂控制器下，不占 IP'),
        ('24盘位磁盘整列', 'Y', '磁盘阵列带千兆 RJ45，占 IP'),
        ('无线网桥', 'Y', '网桥有管理 IP'),
        ('光纤收发器', 'N', '二层桥接'),
        ('16KVA UPS主机', 'Y', '机房 UPS 带网管卡'),
        ('32寸液晶显示屏', 'N', '纯显示'),
        ('32寸信息发布一体机', 'Y', '内置网络播放器'),
        ('超声波探测器', 'N', '总线探测器'),
        ('视频车位探测器', 'Y', '网络型车位相机'),
        ('光电一体配线架', 'N', '无源配线'),
    ]
    print('=== 网关 NAT 判定自测 ===')
    for nm, want, why in cases:
        v, _ = classify(nm)
        ok = (v == want)
        if not ok:
            bad += 1
        print('  %-24s → %-3s %s  %s' % (nm, v, 'OK' if ok else '!! FAIL(期望%s)' % want, why))
    print()
    print('自测结果：%s' % ('全部通过' if bad == 0 else '%d 项失败' % bad))
    return bad


def main():
    ap = argparse.ArgumentParser(description='弱电清单设备统计 + 网关 NAT 分类')
    ap.add_argument('--xlsx', help='清单 Excel 路径')
    ap.add_argument('--sheet', default=None, help='工作表名（默认第一个）')
    ap.add_argument('--out-json', default='items_nat.json')
    ap.add_argument('--out-html', default=None, help='输出 HTML 报告（可选）')
    # 列位（1 基）。清单模板各异，列位几乎每次不同 —— 默认值对应常见模板，
    # 对不上时用这些参数覆盖，别硬套默认值（实测 0914 清单 name/model/qty/unit 四列全错位）。
    ap.add_argument('--model-col', type=int, default=9, help='型号列，默认 I')
    ap.add_argument('--param-col', type=int, default=10, help='参数列，默认 J')
    ap.add_argument('--price-col', type=int, default=11, help='价格/小计列，默认 K')
    ap.add_argument('--name-col', type=int, default=3, help='设备名称列，默认 C')
    ap.add_argument('--unit-col', type=int, default=7, help='单位列，默认 G')
    ap.add_argument('--qty-col', type=int, default=8, help='数量列，默认 H')
    ap.add_argument('--seq-col', type=int, default=1, help='序号列，默认 A')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest or not a.xlsx:
        sys.exit(1 if selftest() else 0)

    items = extract(a.xlsx, a.sheet,
                    model_col=a.model_col, param_col=a.param_col, price_col=a.price_col,
                    name_col=a.name_col, unit_col=a.unit_col, qty_col=a.qty_col, seq_col=a.seq_col)
    json.dump(items, open(a.out_json, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    c = Counter(i['nat'] for i in items)
    print('条目合计 %d：需走网关 NAT %d / 不需走 %d / 视选型 %d / 不适用 %d'
          % (len(items), c['Y'], c['N'], c['C'], c['-']))
    for i in items:
        if i['nat'] == 'C':
            print('  待确认 行%-4d %-24s %s' % (i['row'], i['name'], i['why']))
    print('已写出:', a.out_json)
    if a.out_html:
        _html(items, a.out_html)
        print('已写出:', a.out_html)


def _html(items, path):
    agg = OrderedDict()
    for i in items:
        agg.setdefault(i['sys'], []).append(i)
    H = ['''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>清单设备统计 · 网关 NAT 分类</title><style>
body{margin:0;background:#f6f7f9;color:#1f2429;
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1400px;margin:0 auto;padding:24px 18px 70px}
h1{font-size:20px}.tbl{max-height:820px;overflow:auto;border:1px solid #e3e6ea;border-radius:10px;background:#fff}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{border:1px solid #e3e6ea;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f0f2f5;position:sticky;top:0}
tr.sys td{background:#e8f1fc;font-weight:700}
.num{text-align:right;white-space:nowrap}
.tag{padding:1px 7px;border-radius:20px;font-size:11.5px;font-weight:600;white-space:nowrap}
.tY{background:#fdeceb;color:#b3261e}.tN{background:#e6f5ec;color:#137a4b}
.tC{background:#fdf3e0;color:#9a6400}.tx{background:#f0f2f5;color:#8a95a1}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12.3px}.muted{color:#5b6672}
</style></head><body><div class="wrap">''']
    c = Counter(i['nat'] for i in items)
    H.append('<h1>清单设备统计 · 网关 NAT 分类</h1>')
    H.append('<p class="muted">条目合计 %d：需走网关 NAT %d / 不需走 %d / 视选型 %d / 不适用 %d</p>'
             % (len(items), c['Y'], c['N'], c['C'], c['-']))
    H.append('<div class="tbl"><table><thead><tr><th>行号</th><th>序号</th><th>设备/材料</th>'
             '<th>单位</th><th class="num">数量</th><th>型号</th><th>是否走网关NAT</th><th>依据</th>'
             '</tr></thead><tbody>')
    for sysname, its in agg.items():
        H.append('<tr class="sys"><td colspan="8">%s（%d 条）</td></tr>'
                 % (sysname, len(its)))
        for i in its:
            H.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class="num">%s</td>'
                     '<td class="mono">%s</td><td><span class="tag t%s">%s</span></td><td class="muted">%s</td></tr>'
                     % (i['row'], i['seq'], i['name'], i['unit'], i['qty'], i['model'],
                        i['nat'] if i['nat'] != '-' else 'x', LABEL.get(i['nat'], ''), i['why']))
    H.append('</tbody></table></div></div></body></html>')
    open(path, 'w', encoding='utf-8').write('\n'.join(H))


if __name__ == '__main__':
    main()
