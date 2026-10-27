#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""弱电清单报价 · 内网网页版（手机 / 异地组网都能访问）

一个文件、零额外依赖（除 openpyxl），用 Python 标准库起 HTTP 服务。
同事手机连公司 WiFi，或通过异地组网（蒲公英 / ZeroTier / Tailscale 等），
浏览器打开 http://<这台机的 IP>:8000 就能用，什么都不用装。

用法：
    python webapp.py                    # 监听 0.0.0.0:8000，首次自动生成口令并记住
    python webapp.py --port 8080        # 换端口
    python webapp.py --pass 口令         # 指定口令（会覆盖记住的那个）
    python webapp.py --no-pass          # 不要口令（只在自己电脑上调试时用）
    python webapp.py --host 127.0.0.1   # 只允许本机

长期跑在分部机器上：双击 启动.bat 即可（崩溃自动重启），
开机自启和口令管理见 部署-内网.md。

流程分两段（和命令行版一致）：
    ① 上传清单 → 服务自动探测列位 → 你确认探测对不对
    ② 确认后才真填，填完给下载链接

安全边界：
    · 文件只落在本机临时目录，处理完（或超时）自动删
    · 不联网、不上传任何数据到外部；只有"算价格"会连公司 CRM
    · 只给组网内的人用，不要做端口映射到公网
"""

import os
import re
import io
import sys
import json
import time
import uuid
import shutil
import random
import socket
import tempfile
import threading
import subprocess
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

try:
    import openpyxl
    from openpyxl.utils import get_column_letter, column_index_from_string
except ImportError:
    sys.stderr.write('缺少 openpyxl：pip install openpyxl\n')
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import webapp_users as WU          # 多用户 + 使用统计（同目录，不进技能包）


# ---------------------------------------------------------------- 配置

MAX_UPLOAD = 20 * 1024 * 1024      # 单文件上限 20 MB
SESSION_TTL = 3 * 3600             # 会话 3 小时
WORK_DIR = os.path.join(tempfile.gettempdir(), 'tpkit_web')
ACCESS_PASS = ''                   # 由 --pass 设置，未设则自动生成并落盘
_sessions = {}                     # sid -> dict
_lock = threading.Lock()

# 口令 / 日志放在用户目录，不在仓库里，也不随技能重装丢
STATE_DIR = os.path.join(os.path.expanduser('~'), '.workbuddy', 'tplink-toolkit')
PASS_FILE = os.path.join(STATE_DIR, 'web_pass.txt')
LOG_FILE = None                    # 由 --log 设置


def load_or_make_pass(explicit):
    """口令持久化：第一次随机生成，之后每次启动都读同一个。

    为什么要持久化：分部那台机器会重启，如果每次口令都变，
    同事存的书签全部失效，等于每次都要重新发链接。
    """
    if explicit:
        return explicit
    try:
        if os.path.exists(PASS_FILE):
            with io.open(PASS_FILE, encoding='utf-8') as f:
                v = f.read().strip()
            if v:
                return v
    except Exception:
        pass
    v = ''.join(random.choice('abcdefghjkmnpqrstuvwxyz23456789') for _ in range(6))
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with io.open(PASS_FILE, 'w', encoding='utf-8', newline='\n') as f:
            f.write(v + '\n')
    except Exception as e:
        sys.stderr.write('口令存不下来（%s），本次用临时口令\n' % e)
    return v


_TLS = threading.local()            # 当前请求是谁（每个请求线程各存一份）


def qk():
    """URL 上带当前身份的查询串。

    **必须按"当前请求的人"取 token，不能用全局口令** —— 每人一条链接、
    各自用自己的 CRM 账号，如果这里返回维护者的口令，别人点一下链接
    就会被当成管理员（提权）。所以用 thread-local 存当前 token。
    """
    tok = getattr(_TLS, 'token', '')
    if tok:
        return '?k=' + tok
    return ('?k=' + ACCESS_PASS) if ACCESS_PASS else ''


def _write_log(line):
    """访问日志追加到文件，超 2 MB 自己滚一份。长期跑在分部机器上，出问题要看得到。"""
    if not LOG_FILE:
        return
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2 * 1024 * 1024:
            bak = LOG_FILE + '.1'
            try:
                os.remove(bak)
            except Exception:
                pass
            os.rename(LOG_FILE, bak)
        with io.open(LOG_FILE, 'a', encoding='utf-8', newline='\n') as f:
            f.write(line)
    except Exception:
        pass


def find_autofill():
    """找 autofill_quote.py：优先仓库内，其次技能安装目录。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(here, 'skills', 'tplink-quote-autofill', 'scripts', 'autofill_quote.py'),
        os.path.join(os.path.expanduser('~'), '.workbuddy', 'skills',
                     'tplink-quote-autofill', 'scripts', 'autofill_quote.py'),
    ]
    for p in cands:
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------- 单价口径

RATE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*折')
POINT_RE = re.compile(r'([+\-＋－])\s*(\d+(?:\.\d+)?)\s*(?:个)?点')
# 动态基底：折扣率不写死，按每个型号自己的字段现算
DYN_BASES = (
    (('业务折扣', '业务折'), 'business', '业务折扣'),
    (('片区折扣', '片区'), 'pq', '片区表'),
    (('总部折扣', '总部'), 'hq', '总部表'),
    (('自动分档', '自动'), 'auto', '自动分档'),
)
PRICING_HELP = (
    '可写：面价 / 面价+5点 / 3.4折 / 3.4折-2点；'
    '也可写动态基底：业务折扣 / 业务折扣-2点 / 片区折扣-2点 / 总部折扣 / 自动分档-1点')


def parse_pricing(text):
    """把口径写法解析成 (rate, 人话说明)。

    rate 有三种形态：
        None            → 基准价原样（不取整）
        float           → 固定折扣率
        (src, offset)   → 动态，按该型号自己的字段现算（src: business/pq/hq/auto）
    有折扣率的一律交给脚本算：算出带小数的向上进到个位 0/2/5，整数原样
    （面价原样，不取整）。
    """
    t = (text or '').strip().replace(' ', '').replace('\u3000', '')
    if not t or t in ('面价', '面价不变', '原价', '标准价', '无'):
        return None, '面价原样，不取整'

    pm = POINT_RE.search(t)
    add = 0.0
    if pm:
        add = float(pm.group(2)) / 100.0
        if pm.group(1) in '-－':
            add = -add
    sign = '+' if add >= 0 else '-'
    pts = ' %s %g个点' % (sign, abs(add) * 100) if pm else ''

    # 1) 动态基底：折扣率由每个型号自己的字段决定，不能写成固定值
    for kws, src, cn in DYN_BASES:
        if any(t.startswith(k) for k in kws):
            off = round(add, 6)
            if src == 'business':
                note = '每型号按自己的业务折扣率%s' % pts
            elif src == 'auto':
                note = '按型号自动分片区/总部两张表%s' % pts
            else:
                note = '查%s%s' % (cn, pts)
            return (src, off), note

    # 2) 固定 3.4 折这类
    m = RATE_RE.search(t)
    if m:
        zhe = float(m.group(1))
        rate = round(zhe / 10.0 + add, 6)
        if rate <= 0:
            raise ValueError('算出来折扣率 ≤ 0，口径写错了：%s' % text)
        return rate, '%g折%s = ×%s' % (
            zhe, pts, ('%.4f' % rate).rstrip('0').rstrip('.'))

    # 3) 面价加减点
    if pm:
        rate = round(1 + add, 6)
        return rate, '面价%s = ×%s' % (pts, ('%.4f' % rate).rstrip('0').rstrip('.'))

    m2 = re.match(r'^(\d+(?:\.\d+)?)%$', t)
    if m2:
        return round(float(m2.group(1)) / 100.0, 6), '%s%% = 按比例' % m2.group(1)
    if re.match(r'^0?\.\d+$', t):
        return float(t), '×%s' % t
    raise ValueError('看不懂这个口径「%s」。%s' % (text, PRICING_HELP))


def rate_expr(rate):
    """把 parse_pricing 的结果转成脚本认识的口径表达式。"""
    if rate is None:
        return None
    if isinstance(rate, tuple):
        src, off = rate
        return {'from': src, 'offset': float(off or 0)}
    return float(rate)


# ---------------------------------------------------------------- 列位探测

MODEL_HINT = ('型号', '规格型号', '设备型号', '产品型号')
PARAM_HINT = ('参数', '技术参数', '规格', '配置', '性能参数')
PRICE_HINT = ('含税单价', '单价', '价格', '报价')


def looks_like_model(v):
    """和 autofill_quote.py 里的判据保持一致，避免两边结论打架。"""
    s = str(v or '').strip()
    if not s or len(s) < 3:
        return False
    if re.match(r'^[\d\.,]+$', s):        # 纯数字是数量，不是型号
        return False
    if s.upper().startswith('TL-'):
        return True
    return bool(re.search(r'[A-Za-z]', s)) and bool(re.search(r'\d', s))


def detect_columns(ws):
    """返回 (header_row, model_col, param_col, price_col, note, heads)。

    heads 是 {列号: 表头文字}，确认页的下拉要用它显示「C — 产品型号」，
    光给字母用户根本不知道哪列是哪列。

    策略：先按表头文字找，找不到再按内容找。都找不到给默认值并说明。
    """
    max_scan = min(ws.max_row or 1, 12)
    header_row, best_hit = 2, -1
    for r in range(1, max_scan + 1):
        hit = 0
        for c in range(1, min(ws.max_column or 1, 40) + 1):
            t = str(ws.cell(r, c).value or '')
            if any(h in t for h in MODEL_HINT):
                hit += 3
            if any(h in t for h in PARAM_HINT):
                hit += 2
            if any(h in t for h in PRICE_HINT):
                hit += 1
        if hit > best_hit:
            best_hit, header_row = hit, r
    if best_hit <= 0:
        header_row = 2

    maxc = min(ws.max_column or 1, 40)
    heads = {c: str(ws.cell(header_row, c).value or '').strip() for c in range(1, maxc + 1)}

    def pick(hints):
        for hint in hints:                       # 按提示词优先级
            for c in range(1, maxc + 1):
                if heads.get(c, '') == hint:
                    return c
        for hint in hints:
            for c in range(1, maxc + 1):
                if hint in heads.get(c, ''):
                    return c
        return None

    cm = pick(MODEL_HINT)
    cp = pick(PARAM_HINT)
    ck = pick(PRICE_HINT)
    notes = []

    # 表头没写型号 → 按内容猜：统计每列"型号样"的值个数
    if not cm:
        best, best_n = None, 0
        for c in range(1, maxc + 1):
            n = sum(1 for r in range(header_row + 1, min(ws.max_row or 1, header_row + 60) + 1)
                    if looks_like_model(ws.cell(r, c).value))
            if n > best_n:
                best, best_n = c, n
        if best and best_n >= 2:
            cm = best
            notes.append('型号列按内容识别为 %s 列（表头没写"型号"，识别到 %d 个型号）'
                         % (get_column_letter(cm), best_n))

    if not cp:
        notes.append('参数列没认出来，默认放在型号列右边一格，请核对后再填')
    if not ck:
        notes.append('价格列没认出来，默认放在参数列右边一格，不需要价格可不管')

    cm = cm or 9
    cp = cp or (cm + 1)
    ck = ck or (cp + 1)
    return header_row, cm, cp, ck, notes, heads


def preview_rows(ws, header_row, cm, n=8):
    """取前 n 行做预览：行号 + 型号 + 该行其他非空内容摘要。"""
    out = []
    r = header_row + 1
    while r <= (ws.max_row or 1) and len(out) < n:
        v = ws.cell(r, cm).value
        if looks_like_model(v):
            cells = []
            for c in range(1, min(ws.max_column or 1, 20) + 1):
                if c == cm:
                    continue
                t = str(ws.cell(r, c).value or '').strip()
                if t and len(t) <= 30:
                    cells.append(t)
                if len(cells) >= 3:
                    break
            out.append({'row': r, 'model': str(v).strip(), 'extra': ' · '.join(cells)})
        r += 1
    return out


def count_rows(ws, header_row, cm):
    return sum(1 for r in range(header_row + 1, (ws.max_row or 1) + 1)
               if looks_like_model(ws.cell(r, cm).value))


def param_filled(ws, header_row, cm, cp):
    """参数列里已经有多少行有内容（脚本不会覆盖非空单元格）。

    返回 (有内容的行数, 型号总行数)。
    """
    filled = total = 0
    for r in range(header_row + 1, (ws.max_row or 1) + 1):
        if not looks_like_model(ws.cell(r, cm).value):
            continue
        total += 1
        if str(ws.cell(r, cp).value or '').strip():
            filled += 1
    return filled, total


# ---------------------------------------------------------------- 会话

def new_session(**kw):
    sid = uuid.uuid4().hex[:16]
    d = os.path.join(WORK_DIR, sid)
    os.makedirs(d, exist_ok=True)
    with _lock:
        _sessions[sid] = dict(sid=sid, dir=d, ts=time.time(), **kw)
    return sid


def get_session(sid):
    with _lock:
        s = _sessions.get(sid)
    if not s:
        return None
    if time.time() - s['ts'] > SESSION_TTL:
        drop_session(sid)
        return None
    s['ts'] = time.time()
    return s


def drop_session(sid):
    with _lock:
        s = _sessions.pop(sid, None)
    if s:
        shutil.rmtree(s['dir'], ignore_errors=True)


def janitor():
    while True:
        time.sleep(600)
        now = time.time()
        for sid, s in list(_sessions.items()):
            if now - s['ts'] > SESSION_TTL:
                drop_session(sid)


# ---------------------------------------------------------------- multipart

def parse_multipart(body, content_type):
    """极简 multipart/form-data 解析（Python 3.13 已移除 cgi 模块）。

    返回 {字段名: (文件名或None, bytes)}。够用就好，不追求 RFC 完备。
    """
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or '')
    if not m:
        return {}
    boundary = (m.group(1) or m.group(2)).strip().encode()
    sep = b'--' + boundary
    fields = {}
    for chunk in body.split(sep):
        if not chunk or chunk in (b'--\r\n', b'--', b'\r\n'):
            continue
        chunk = chunk.lstrip(b'\r\n')
        if chunk.startswith(b'--'):
            break
        pos = chunk.find(b'\r\n\r\n')
        if pos < 0:
            continue
        raw_head, data = chunk[:pos], chunk[pos + 4:]
        if data.endswith(b'\r\n'):
            data = data[:-2]
        text = raw_head.decode('utf-8', 'replace')
        nm = re.search(r'name="([^"]*)"', text)
        if not nm:
            continue
        name = nm.group(1)
        fn = re.search(r'filename="([^"]*)"', text)
        fname = None
        if fn:
            fname = fn.group(1)
            star = re.search(r"filename\*=UTF-8''([^;\r\n]+)", text)
            if star:
                from urllib.parse import unquote as _uq
                fname = _uq(star.group(1))
        fields[name] = (fname, data)
    return fields


# ---------------------------------------------------------------- 页面

def esc(s, n=None):
    """HTML 转义（表头文字来自用户的 Excel，可能带 < > &）。"""
    t = str(s if s is not None else '')
    t = t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    t = re.sub(r'\s+', ' ', t).strip()
    if n and len(t) > n:
        t = t[:n - 1] + '…'
    return t


def col_opts(cur, heads, maxc):
    """列下拉：显示成「C — 产品型号」，只给字母用户没法选。"""
    out = []
    for i in range(1, maxc + 1):
        L = get_column_letter(i)
        h = (heads or {}).get(i, '')
        txt = ('%s — %s' % (L, h)) if h else ('%s（空列）' % L)
        out.append('<option value="%s"%s>%s</option>'
                   % (L, ' selected' if i == cur else '', esc(txt, 18)))
    return ''.join(out)


CSS = """
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{overflow-x:hidden}                      /* 窄屏兜底，任何东西都不许把页面撑宽 */
body{margin:0;padding:0;background:#f5f6f8;color:#1a1a1a;
 font:16px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:640px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:20px;margin:8px 0 4px}
.sub{color:#6b7280;font-size:14px;margin-bottom:20px}
.card{background:#fff;border-radius:12px;padding:18px;margin-bottom:14px;
 box-shadow:0 1px 3px rgba(0,0,0,.06);min-width:0}
label{display:block;font-size:14px;color:#374151;margin:12px 0 6px;font-weight:600}
label:first-child{margin-top:0}

/* 文件选择：整块可点的大按钮，原生 input 藏起来（手机上原生控件太小） */
.drop{display:block;border:2px dashed #c7cdd8;border-radius:12px;background:#fafbfc;
 padding:24px 16px;text-align:center;cursor:pointer;transition:.15s}
.drop:active{background:#eef2fa;border-color:#1a56db}
.drop .ico{font-size:28px;line-height:1;opacity:.75}
.drop .t{font-size:16px;font-weight:600;color:#1a56db;margin-top:8px}
.drop .h{font-size:13px;color:#6b7280;margin-top:4px;line-height:1.5}
.drop.picked{border-style:solid;border-color:#16a34a;background:#f0fdf4}
.drop.picked .t{color:#15803d}
.visually-hidden{position:absolute;width:1px;height:1px;margin:-1px;padding:0;
 border:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

input[type=text],input[type=password],input[type=email],select{width:100%;max-width:100%;min-width:0;padding:12px;
 border:1px solid #d0d5dd;border-radius:8px;font-size:16px;background:#fff;
 text-overflow:ellipsis}
select{appearance:none;-webkit-appearance:none;
 background-image:linear-gradient(45deg,transparent 50%,#6b7280 50%),
                  linear-gradient(135deg,#6b7280 50%,transparent 50%);
 background-position:calc(100% - 16px) 52%,calc(100% - 11px) 52%;
 background-size:5px 5px,5px 5px;background-repeat:no-repeat;padding-right:34px}
.btn{display:block;width:100%;padding:15px;border:0;border-radius:10px;
 background:#1a56db;color:#fff;font-size:16px;font-weight:600;margin-top:18px;
 cursor:pointer;text-align:center;text-decoration:none}
.btn:active{background:#1543ad}
.btn.gray{background:#eef1f5;color:#374151}
.btn:disabled{background:#9aa5b4;cursor:default}
.tip{background:#eff6ff;border-left:3px solid #1a56db;padding:10px 12px;
 border-radius:6px;font-size:13px;color:#1e40af;margin:10px 0}
.warn{background:#fffbeb;border-left:3px solid #f59e0b;padding:10px 12px;
 border-radius:6px;font-size:13px;color:#92400e;margin:10px 0}
.ok{background:#f0fdf4;border-left:3px solid #16a34a;padding:10px 12px;
 border-radius:6px;font-size:13px;color:#166534;margin:10px 0}
.err{background:#fef2f2;border-left:3px solid #ef4444;padding:10px 12px;
 border-radius:6px;font-size:13px;color:#991b1b;margin:10px 0}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px;table-layout:fixed}
th{background:#f3f4f6;text-align:left;padding:8px 6px;font-weight:600;color:#374151}
td{padding:8px 6px;border-top:1px solid #eef0f3;overflow-wrap:anywhere}
.mono{font-family:ui-monospace,Consolas,monospace}
.row{display:flex;gap:10px}
.row>div{flex:1 1 0;min-width:0}                    /* min-width:0 是关键，否则 select 会撑破 */
.opt{display:flex;align-items:center;gap:8px;margin:14px 0;font-size:15px}
.opt input{width:20px;height:20px;margin:0;flex:0 0 auto}
.opt label{margin:0;font-weight:400;font-size:15px}
.opt label b{font-weight:600}
pre{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;font-size:12px;
 overflow:auto;max-height:340px;white-space:pre-wrap;word-break:break-all}
.kv{display:flex;justify-content:space-between;gap:12px;padding:10px 0;
 border-bottom:1px solid #eef0f3;font-size:15px}
.kv:last-child{border:0}
.kv span{color:#6b7280}
.kv b{font-variant-numeric:tabular-nums;text-align:right}
.hint{font-size:12px;color:#6b7280;margin-top:6px;line-height:1.5}
.who{display:flex;flex-wrap:wrap;gap:10px;align-items:center;font-size:12.5px;
     color:#6b7280;margin:-2px 0 10px}
.who a{color:#1a56db;text-decoration:none;font-weight:600}
.who .sep{color:#d1d5db}
.who .tag{background:#eef2ff;color:#3730a3;border-radius:999px;padding:1px 8px;font-size:11px}
.tb{width:100%;border-collapse:collapse;font-size:13px;table-layout:auto}
.tb th,.tb td{padding:7px 8px;border-bottom:1px solid #f1f2f4;text-align:left;
              overflow-wrap:normal;vertical-align:middle}
.tb th{color:#6b7280;font-weight:600;font-size:12px;white-space:nowrap}
.tb td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tb td.nw{white-space:nowrap}
.tb td.lk{white-space:normal;overflow-wrap:anywhere;font-size:11.5px;line-height:1.45}
/* 用户块：链接太长，用宽表会把「次数/状态/按钮」挤掉，改成一列卡片 */
.ub{border:1px solid #eef0f3;border-radius:10px;padding:10px 12px;margin-top:10px;background:#fcfcfd}
.ub h4{margin:0 0 4px;font-size:14px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.ub h4 .st{margin-left:auto;font-size:11.5px;color:#15803d;background:#f0fdf4;
           border-radius:999px;padding:1px 8px}
.ub h4 .st.off{color:#9ca3af;background:#f3f4f6}
.ub .lk{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;
        color:#1a56db;word-break:break-all;line-height:1.5}
.ub .meta{font-size:12.5px;color:#6b7280;margin-top:4px;line-height:1.6}
.tb tr:last-child td{border-bottom:0}
.tb button{padding:5px 10px;font-size:12px;border-radius:8px;box-shadow:none}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.pg{border:1px solid #e5e7eb;border-radius:10px;padding:12px;margin:10px 0;background:#fcfcfd}
.pgh{font-size:14px;font-weight:700;color:#111827;margin-bottom:8px}
.pgh span{display:block;font-weight:400;font-size:12px;color:#6b7280;margin-top:2px;line-height:1.5}
.pgr{display:flex;align-items:center;gap:8px;margin-top:8px}
.pgr>span{font-size:13px;color:#6b7280;white-space:nowrap}
.pgr select{flex:1;min-width:0;padding:9px 8px;font-size:14px}
.pgr select.narrow{flex:0 0 62px;text-align:center;padding-left:2px;padding-right:2px}
.pgr input[type=number]{flex:0 0 78px;width:78px;padding:9px 6px;font-size:14px;
 text-align:center;font-family:inherit;color:#111827;background:#fff;
 border:1px solid #d1d5db;border-radius:8px;-moz-appearance:textfield}
.pgr input[type=number]::-webkit-outer-spin-button,
.pgr input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.pgr .fix{flex:0 0 auto;padding:9px 12px;border:1px solid #e5e7eb;border-radius:8px;
 background:#f3f4f6;font-size:14px;color:#374151}
.pgn{font-size:12px;color:#6b7280;margin-top:8px;line-height:1.7}
.pgn b{color:#1a56db}
/* 顶部切换（上传清单 / 查型号） */
.tabs{display:flex;gap:8px;margin:0 0 14px}
.tabs a{flex:1;text-align:center;padding:11px 6px;border-radius:10px;font-size:14px;
 font-weight:600;text-decoration:none;border:1px solid #e5e7eb;background:#fff;color:#374151}
.tabs a.on{background:#1a56db;border-color:#1a56db;color:#fff}
/* 单型号查询 */
textarea{width:100%;box-sizing:border-box;padding:12px;font-size:15px;font-family:inherit;
 border:1px solid #d1d5db;border-radius:10px;resize:vertical;color:#111827;background:#fff;
 line-height:1.7}
textarea:focus,input:focus,select:focus{outline:2px solid #bfd3ff;outline-offset:1px}
.pbar{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0 14px}
.pi{border:1px solid #e5e7eb;border-radius:10px;padding:9px 11px;background:#f9fafb}
.pi span{display:block;font-size:12px;color:#6b7280;margin-bottom:2px}
.pi b{font-size:18px;color:#111827;font-variant-numeric:tabular-nums}
.pi.mut b{color:#9ca3af;font-size:15px}
@media (max-width:440px){.pbar{grid-template-columns:repeat(2,1fr)}}
.plist{margin:8px 0 0;padding:0;list-style:none;font-size:13px;line-height:1.85;color:#374151}
.plist li{padding:2px 0}
.psrc{font-size:12.5px;color:#6b7280;margin:10px 0 2px}
.psrc b{color:#1a56db}
.psrc.web b{color:#b45309}
.psrc a{color:#1a56db}
.mhd{font-size:16px;font-weight:700;color:#111827}
.mhd em{display:block;font-style:normal;font-weight:400;font-size:12.5px;
 color:#6b7280;margin-top:3px;line-height:1.5}
.mnote{font-size:12.5px;color:#92400e;background:#fffbeb;border:1px solid #fde68a;
 border-radius:8px;padding:8px 10px;margin:8px 0 0;line-height:1.65}
details{margin-top:10px}
summary{font-size:14px;color:#1a56db;cursor:pointer;font-weight:600}
/* 在产状态徽章：停产必须一眼看见，不能藏在备注里 */
.lc{display:inline-block;font-size:11.5px;font-weight:700;padding:2px 8px;
 border-radius:999px;margin-left:8px;vertical-align:2px;white-space:nowrap}
.lc.halt{background:#fee2e2;color:#991b1b;border:1px solid #fca5a5}
.lc.warn{background:#fef3c7;color:#92400e;border:1px solid #fcd34d}
.lc.live{background:#dcfce7;color:#166534;border:1px solid #86efac}
.lc.na{background:#f3f4f6;color:#6b7280;border:1px solid #d1d5db}
.warn.red{background:#fef2f2;border-left-color:#dc2626;color:#991b1b}
.warn.red b{color:#7f1d1d}
.hl{margin:6px 0 0;padding-left:18px}
.hl li{margin:5px 0;line-height:1.65}
.card.halted{border-color:#fca5a5;background:#fffafa}
.card.halted .pi{border-color:#fecaca;background:#fef2f2}
.alts{font-size:13px;color:#991b1b;background:#fef2f2;border:1px solid #fecaca;
 border-radius:8px;padding:9px 11px;margin:8px 0 0;line-height:1.7}
.alts b{color:#7f1d1d}
.alts code{background:#fff;border:1px solid #fecaca;border-radius:4px;
 padding:1px 5px;font-size:12.5px}
@media (max-width:440px){                          /* 手机窄屏：并排的控制改成上下 */
 .row{flex-direction:column;gap:0}
 .card{padding:16px}
}
"""


# ---------------------------------------------------------------- 定价口径（项目机 / 渠道机 分档）
# 渠道机和项目机不是一套定价逻辑：CRM 里只有 seriesValue='项目机型' 那批带业务折扣
# （businessDiscount），其余型号走渠道。所以两边必须各给一套口径 + 各选基准价。

# 基准价固定为「标准价（面价）」= CRM 的 price，就是平时报价用的那个。
# 目录价 / 批发价不给选：它们是 price 按 ratio 倒推出来的换算值
# （2300÷0.41≈5609、2300÷0.52≈4425），真正能成交的口径只有 price，
# 给多选只会让同一个人两次填出两个价。
BASE_LABEL = '标准价（面价）'
PROJECT_VALUES = ['项目机型']          # CRM 的权威判定，不用用户手工标

# 项目机可选三种动态基底；渠道机只有「面价」一条路
BASIS_ITEMS = (
    ('business', '业务折扣'),
    ('pq',       '片区折扣'),
    ('hq',       '总部折扣'),
)
BASIS_LABELS = dict(BASIS_ITEMS)


def _num(v, default=0.0):
    try:
        return float(str(v).strip() or default)
    except (TypeError, ValueError):
        return default


def compose_pricing(basis, sign, pts, channel=False):
    """下拉 + 符号 + 点数 → 口径文本；0 个点不带后缀。

    项目机：业务折扣 / 片区折扣 / 总部折扣  ±N点
    渠道机：面价                          ±N点
    """
    v = abs(_num(pts))
    s = '-' if str(sign or '+').strip() in ('-', '－', '−') else '+'
    tail = ('%s%g点' % (s, v)) if v else ''
    if channel:
        return '面价' + tail
    return (BASIS_LABELS.get(str(basis or 'business').strip()) or '业务折扣') + tail


def read_pricing(get):
    """读出两档口径 → 要写进会话的字段。

    get(k, default)：取一个原始值（表单优先、会话兜底）。
    基准价两边都固定 'price'，不再让用户选。
    """
    p_basis = get('p_basis', 'business')
    if p_basis not in BASIS_LABELS:
        p_basis = 'business'
    p_sign, p_pts = get('p_sign', '+'), get('p_pts', '0')
    c_sign, c_pts = get('c_sign', '+'), get('c_pts', '0')
    p_text = compose_pricing(p_basis, p_sign, p_pts)
    c_text = compose_pricing(None, c_sign, c_pts, channel=True)
    out = dict(p_basis=p_basis, p_sign=p_sign, p_pts=_num(p_pts),
               c_sign=c_sign, c_pts=_num(c_pts),
               pricing_p_text=p_text, pricing_c_text=c_text,
               p_base='price', c_base='price',
               pricing_p_rate=None, pricing_c_rate=None,
               pricing_p_note='', pricing_c_note='', pricing_err='')
    try:
        out['pricing_p_rate'], out['pricing_p_note'] = parse_pricing(p_text)
        out['pricing_c_rate'], out['pricing_c_note'] = parse_pricing(c_text)
    except ValueError as e:
        out['pricing_err'] = str(e)
        out['pricing_p_rate'] = out['pricing_c_rate'] = None
    return out


SIGN_ITEMS = (('+', '+'), ('-', '−'))


def _opts(items, cur):
    return ''.join('<option value="%s"%s>%s</option>'
                   % (k, ' selected' if k == cur else '', cn) for k, cn in items)


def pricing_block(p_basis='business', p_sign='+', p_pts='0',
                  c_sign='+', c_pts='0', pmsg=''):
    """两档定价口径的输入区（上传页和确认页共用，保证两处长得一样）。

    项目机：业务折扣 / 片区折扣 / 总部折扣 ＋ ±N 个点
    渠道机：面价                          ＋ ±N 个点
    基准价两边都固定「标准价（面价）」，不给选。
    """
    p_pts = '%g' % _num(p_pts)
    c_pts = '%g' % _num(c_pts)
    return f"""
  <label>定价口径（项目机和渠道机分开设）</label>
  <div class="pg">
    <div class="pgh">项目机型<span>CRM 里 seriesValue=项目机型 的，带业务折扣那批</span></div>
    <div class="pgr">
      <select name="p_basis">{_opts(BASIS_ITEMS, p_basis)}</select>
      <select name="p_sign" class="narrow">{_opts(SIGN_ITEMS, p_sign)}</select>
      <input type="number" name="p_pts" value="{p_pts}" min="0" max="99" step="0.5"
             inputmode="decimal">
      <span>个点</span>
    </div>
    <div class="pgn">基准价固定用 <b>标准价（面价）</b>。例：<b>业务折扣 − 2 个点</b>
      ＝ 该型号业务折扣率 0.39 减到 0.37 再乘标准价。</div>
  </div>
  <div class="pg">
    <div class="pgh">渠道机<span>其余型号，走渠道价的那批（没有业务折扣率）</span></div>
    <div class="pgr">
      <div class="fix">面价</div>
      <select name="c_sign" class="narrow">{_opts(SIGN_ITEMS, c_sign)}</select>
      <input type="number" name="c_pts" value="{c_pts}" min="0" max="99" step="0.5"
             inputmode="decimal">
      <span>个点</span>
    </div>
    <div class="pgn">按面价加减，默认 <b>+0 个点</b>＝面价原样、不取整。</div>
  </div>
  <div class="hint">
    · 每行按 <b>CRM 返回的 seriesValue</b> 自动归到其中一档，<b>不用手工标</b><br>
    · 点数是<b>折扣率</b>的百分点，不是价格：0.39 加 2 个点 = 0.41，减 2 个点 = 0.37<br>
    · 除了「面价 + 0 个点」，别的算法算出来的单价都要取整 ——
      带小数的向上进到个位 <b>0/2/5</b>，本来就是整数的原样保留<br>
    · 「片区折扣」「总部折扣」= 先拿业务折扣率查对照表换成片区/总部折扣率，再加减点
  </div>
  {pmsg}
"""


def page(title, body, sid='', refresh=None):
    meta = '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    if refresh:
        meta += '<meta http-equiv="refresh" content="%d">' % refresh
    return ("""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>%s</title>%s<style>%s</style></head><body><div class="wrap">%s</div></body></html>"""
            % (title, meta, CSS, body)).encode('utf-8')


MODEL_MAX = 20          # 一次最多查这么多个型号（超了要等太久）

COPY_JS = """
<script>
function cp(b){
  var c=b.closest('.card'), l=c&&c.querySelector('.plist');
  if(!l){ b.textContent='没有参数可复制'; return; }
  var t=Array.prototype.map.call(l.querySelectorAll('li'),
        function(x){return x.textContent.trim()}).join('\\n');
  var ta=document.createElement('textarea');
  ta.value=t; ta.style.position='fixed'; ta.style.top='0'; ta.style.opacity='0';
  document.body.appendChild(ta); ta.focus(); ta.select();
  var ok=false;
  try{ ok=document.execCommand('copy'); }catch(e){}
  document.body.removeChild(ta);
  b.textContent = ok ? '已复制' : '复制失败，长按参数自己选';
  setTimeout(function(){ b.textContent='复制参数'; }, 1800);
}
function busy(f){
  var b=f.querySelector('button[type=submit]');
  if(b){ b.textContent='查询中… 一个型号约 1~2 秒'; b.disabled=true; }
}
</script>
"""


def tabs(cur, q=''):
    """顶部切换：上传清单 / 查单个型号。q 是 ?k=口令。"""
    items = (('up', '/', '上传清单'), ('one', '/one', '查单个型号'))
    return '<div class="tabs">%s</div>' % ''.join(
        '<a href="%s%s" class="%s">%s</a>'
        % (u, q, 'on' if k == cur else '', t) for k, u, t in items)


def bar(u, q=''):
    """身份条：谁在用、CRM 账号绑没绑、管理员多一个「使用情况」。"""
    if not u:
        return ''
    bits = ['<span class="tag">%s</span>' % esc(u.get('name') or '')]
    if u.get('email'):
        bits.append('<a href="/me%s">CRM：%s</a>' % (q, esc(u.get('email'))))
    else:
        bits.append('<a href="/me%s">没绑 CRM 账号，点这里填</a>' % q)
    if u.get('role') == 'admin':
        bits.append('<a href="/admin%s">使用情况</a>' % q)
    return '<div class="who">%s</div>' % '<span class="sep">・</span>'.join(bits)


def me_body(u, q='', msg=''):
    """「我的」：绑自己的 CRM 账号。绑一次，之后就不用再输。"""
    email, has_pwd = (u.get('email') or ''), bool(u.get('pwd'))
    host = getattr(_TLS, 'host', '') or '这台机的IP:8000'
    link = ('http://%s/%s' % (host, q)) if q else ('http://%s/' % host)
    pwd_note = ('<div class="ok">已记住密码（加密存在这台机器上），下面留空就是不改。</div>'
                if has_pwd else
                '<div class="hint">还没记住密码 —— 每次算价格都要重新输。</div>')
    weak = ''
    if has_pwd and WU.mem_level() == 'weak':
        weak = ('<div class="warn">这台机用不了 Windows 加密接口，密码是<b>弱加密</b>存的。'
                '能登录这台电脑的人可以解出来，别把服务开到公网。</div>')
    return """
%s<h1>我的</h1>
<div class="sub">这是你自己的 CRM 账号，只用来替你算价</div>
%s
<div class="card">
  <label>我的 CRM 账号（用你自己的，别用别人的）</label>
  <form method="post" action="/me/save%s">
    <input type="text" name="email" value="%s" placeholder="你的 CRM 登录邮箱" autocomplete="username">
    <label>密码</label>
    <input type="password" name="pwd" value="" placeholder="%s" autocomplete="current-password">
    <label style="display:flex;gap:8px;align-items:center;margin-top:10px">
      <input type="checkbox" name="remember" value="1" %s style="width:18px;height:18px;flex:0 0 auto">
      <span>记住密码，下次不用再输</span>
    </label>
    <label style="display:flex;gap:8px;align-items:center;margin-top:6px">
      <input type="checkbox" name="forget" value="1" style="width:18px;height:18px;flex:0 0 auto">
      <span>删掉已记住的密码（下次要重新输）</span>
    </label>
    <div class="hint">密码用 Windows 加密接口存在<b>维护者这台机器</b>上，只用来替你登录 CRM，
      换台机器或换 Windows 账户就解不开。不想留了，勾下面的「删掉已记住的密码」再保存。</div>
    <div class="warn">要如实说一句：密码是加密存的，但<b>维护者（发你链接的人）能解出来看</b>，
      因为服务就是在他这台机上跑。介意的话就别勾「记住密码」，每次自己输。</div>
    <button class="btn" type="submit">保存</button>
  </form>
</div>
<div class="card">
  <div class="kv"><span>我的访问链接</span><b class="mono">%s</b></div>
  <div class="kv"><span>用过的次数</span><b>%s 次</b></div>
  <div class="kv"><span>最近一次</span><b>%s</b></div>
</div>
""" % (bar(u, q), msg + pwd_note + weak, q, esc(email),
       '已记住，留空不改' if has_pwd else '输入密码',
       'checked' if has_pwd else 'checked',
       esc(link, 120), int(u.get('runs') or 0), esc(u.get('last') or '还没用过'))


def cred_body(u, q, info, msg=''):
    """管理员看某人的 CRM 凭据。敏感页：不缓存，且每次访问都进审计。

    密码明文就摊在这里 —— 页面本身靠 /admin 的权限把关，不做二次弹窗，
    因为维护者一定要能解开（服务得拿它去登录 CRM），遮一层只是自欺。
    """
    lvl = info.get('level') or ''
    lvl = {'dpapi': 'Windows DPAPI（绑这台电脑的 Windows 账户）',
           'weak': '弱加密（这台机调不到 DPAPI，只做了混淆）'}.get(lvl, lvl or '未知')
    if info.get('has_pwd'):
        pwd_html = ('<div class="mono" style="font-size:20px;letter-spacing:1px;'
                    'padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;'
                    'background:#fafafa;word-break:break-all">%s</div>' % esc(info['pwd']))
    elif info.get('stored'):
        pwd_html = ('<div class="err">库里有一条密码，但解不开（换过 Windows 账户或换过机器）。'
                    '让他去「我的」重新填一次。</div>')
    else:
        pwd_html = ('<div class="hint">没存密码 —— 他只绑了邮箱，每次算价时自己输。</div>')
    return """
%s<h1>%s 的 CRM 凭据</h1>
<div class="sub">只有维护者能看，每次打开都会记一条「查看凭据」</div>
%s
<div class="card">
  <div class="kv"><span>姓名</span><b>%s</b></div>
  <div class="kv"><span>CRM 邮箱</span><b class="mono">%s</b></div>
  <div class="kv"><span>最近使用</span><b>%s</b></div>
</div>
<div class="card">
  <label>密码</label>
  %s
  <div class="hint">加密方式：%s</div>
</div>
<div class="card">
  <div style="display:flex;gap:10px;flex-wrap:wrap">
  <a class="btn gray" style="width:auto;padding:9px 16px;font-size:13px" href="/admin%s">← 回使用情况</a>
  <a class="btn gray" style="width:auto;padding:9px 16px;font-size:13px"
     href="/admin/cred%s&amp;uid=%s&amp;forget=1"
     onclick="return confirm('清掉他的密码？他下次算价要重新输。')">清掉这条密码</a>
  </div>
</div>
""" % (bar(u, q), esc(info.get('name') or '(未命名)'), msg,
       esc(info.get('name') or '—'), esc(info.get('email') or '未绑'),
       esc(info.get('last') or '—'), pwd_html, esc(lvl), q, q, esc(info.get('id')))


def admin_body(u, q, users, stats, recs, msg='', host=''):
    """管理员：发链接、看谁在用、导 CSV。"""
    base = 'http://%s/' % (host or '这台机的IP:8000')
    blocks = []
    for x in users:
        tok = x.get('token') or ''
        link = base + '?k=' + tok
        on = x.get('active', True)
        blocks.append(
            '<div class="ub">'
            '<h4>%s%s<span class="st%s">%s</span></h4>'
            '<div class="lk">%s</div>'
            '<div class="meta">CRM：%s ・ 最近：%s ・ 次数：%s</div>'
            '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">'
            '<a class="btn gray" style="width:auto;padding:8px 14px;font-size:13px" '
            'href="/admin/cred%s&amp;uid=%s">查看 CRM 凭据</a>'
            '%s</div>'
            '</div>' % (
                esc(x.get('name') or ''),
                ' <span class="tag">管理员</span>' if x.get('role') == 'admin' else '',
                '' if on else ' off', '在用' if on else '已停用',
                esc(link, 120), esc(x.get('email') or '未绑'),
                esc(x.get('last') or '—'), int(x.get('runs') or 0),
                q, esc(x.get('id') or ''),
                '' if x.get('id') == (u or {}).get('id') else
                ('<form method="post" action="/admin/toggle%s" style="display:inline;margin:0">'
                 '<input type="hidden" name="uid" value="%s">'
                 '<button class="btn gray" type="submit" '
                 'style="width:auto;padding:8px 14px;font-size:13px">%s</button></form>'
                 % (q, esc(x.get('id') or ''), '停用' if on else '启用'))))
    prow = []
    for p in stats.get('people') or []:
        prow.append('<tr><td class="nw">%s</td><td class="num">%s</td><td class="num">%s</td>'
                    '<td class="num">%s</td><td class="num">%s</td><td class="nw">%s</td></tr>' % (
                        esc(p['name']), p['n'], p['rows'], p['models'],
                        p['secs'], esc(p['last'])))
    rrow = []
    for r in recs:
        rrow.append('<tr><td class="mono nw">%s</td><td class="nw">%s</td><td class="nw">%s</td>'
                    '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                    '<td>%s</td></tr>' % (
                        esc(r['ts']), esc(r['name'] or ''), WU.kind_cn(r['kind']),
                        r['rows'] or 0, r['models'] or 0,
                        ('%.1f' % (r['secs'] or 0)), esc(r.get('note') or '')))
    t = stats.get('total') or {}
    return """
%s<h1>使用情况</h1>
<div class="sub">链接发给谁，谁的点算谁的；这里看得到用量</div>
%s
<div class="card">
  <label>加一个人（自动生成专属链接，发给他就行）</label>
  <form method="post" action="/admin/new%s">
    <input type="text" name="name" placeholder="姓名，比如 张三" required>
    <button class="btn" type="submit">生成链接</button>
  </form>
</div>
<div class="card">
  <label>用户（%d 人）</label>
  %s
  <div class="hint">链接 = 身份。谁丢了链接就找你要，别互相转发别人的链接。</div>
</div>
<div class="card">
  <label>汇总：共 %s 次操作 ・ %s 行清单 ・ %s 个型号</label>
  <div style="overflow-x:auto">
  <table class="tb">
    <tr><th>用户</th><th class="num">次数</th><th class="num">清单行数</th>
        <th class="num">型号数</th><th class="num">平均耗时</th><th>最近</th></tr>
    %s
    %s
  </table>
  </div>
  <a class="btn gray" href="/admin/usage.csv%s">导出全部记录 CSV</a>
</div>
<div class="card">
  <label>最近 40 条</label>
  <div style="overflow-x:auto">
  <table class="tb">
    <tr><th>时间</th><th>用户</th><th>动作</th><th class="num">行数</th>
        <th class="num">型号</th><th class="num">耗时s</th><th>备注</th></tr>
    %s
  </table>
  </div>
</div>
""" % (bar(u, q), msg, q, len(users),
       ''.join(blocks) or '<div class="hint">还没有人</div>',
       t.get('n', 0), t.get('rows', 0), t.get('models', 0), ''.join(prow),
       '' if not prow else '', q, ''.join(rrow) or
       '<tr><td colspan="7">还没有记录</td></tr>')


def find_one_query():
    """one_query.py 与 autofill_quote.py 同目录。"""
    p = find_autofill()
    if not p:
        return None
    f = os.path.join(os.path.dirname(p), 'one_query.py')
    return f if os.path.exists(f) else None


def run_one_query(models, with_price, creds=('', '')):
    """调 one_query.py 子进程，返回 (dict, err)。

    凭据用**环境变量**传（TPKIT_CRM_EMAIL / TPKIT_CRM_PASSWORD），
    不走命令行参数 —— argv 在这台机上谁都看得到。
    """
    script = find_one_query()
    if not script:
        return None, ('没找到 one_query.py。技能要 update 到含这个脚本的版本：'
                      '`git pull` → `python install.py` → 重启 WorkBuddy。')
    cmd = [sys.executable, '-X', 'utf8', script] + list(models)
    if not with_price:
        cmd.append('--no-price')
    env = dict(os.environ)
    if with_price and creds and creds[0]:
        env['TPKIT_CRM_EMAIL'] = creds[0]
        env['TPKIT_CRM_PASSWORD'] = creds[1] or ''
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=300,
                           cwd=os.path.dirname(script), env=env)
    except subprocess.TimeoutExpired:
        return None, '查询超时（5 分钟）。型号少给几个再试。'
    out = (p.stdout or b'').decode('utf-8', 'replace').strip()
    if not out:
        err = (p.stderr or b'').decode('utf-8', 'replace').strip()
        return None, '查询失败：%s' % (err[-600:] or ('退出码 %d' % p.returncode))
    try:
        return json.loads(out.splitlines()[-1]), None
    except Exception:
        return None, '返回的不是 JSON：%s' % out[:300]


def _num_txt(v):
    if v in (None, ''):
        return None
    try:
        return '%g' % float(v)
    except (TypeError, ValueError):
        return str(v)


def _md_bold(t):
    """提示文字里的 **加粗** → <b>（脚本那边按 Markdown 写的，网页要转一下）。"""
    return re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', esc(t))


def one_card(it):
    def cell(label, v, prefix=''):
        t = _num_txt(v)
        if t is None:
            return '<div class="pi mut"><span>%s</span><b>—</b></div>' % label
        return '<div class="pi"><span>%s</span><b>%s%s</b></div>' % (label, prefix, t)

    head = esc(it.get('product_model') or it.get('model') or '')
    bits = [x for x in (esc(it.get('name') or ''), esc(it.get('series') or '')) if x]
    em = ' · '.join(bits)

    # 在产状态（2026-10-27）：停产型号不能进报价表，所以这一条必须比价格更显眼。
    # 找不到 life 字段 = 没连 CRM 或旧版脚本，标"未核"而不是默认当成在产。
    life = str(it.get('life') or '').strip().upper()
    _lc = {'HALT': ('halt', '⛔ 已停产'),
           'HALT_WARNING': ('warn', '⚠ 停产预警'),
           'PRODUCTION': ('live', '在产')}
    _c, _t = _lc.get(life, ('na', '在产状态未核'))
    badge = '<span class="lc %s">%s</span>' % (_c, _t)
    card_cls = 'card halted' if life == 'HALT' else 'card'

    params = it.get('params') or []
    lis = ''.join('<li>%s</li>' % esc(p) for p in params)
    src = it.get('src')
    kind = it.get('src_kind') or ''
    url = it.get('url')
    if params:
        extra = ''
        if url:
            extra = ' ・ <a href="%s" target="_blank" rel="noopener">官网产品页</a>' % esc(url)
        src_line = ('<div class="psrc %s">参数来源：<b>%s</b> ・ 共 %d 条%s</div>'
                    % ('web' if kind == 'web' else '', esc(src or '未知'), len(params), extra))
        plist = '<ul class="plist">%s</ul>' % lis
        copybtn = '<button class="btn gray" type="button" onclick="cp(this)">复制参数</button>'
    else:
        src_line = ('<div class="psrc web">参数来源：<b>%s</b></div>'
                    % esc(src or '没有'))
        plist = ''
        copybtn = ''

    notes = ''.join('<div class="mnote">%s</div>' % _md_bold(n) for n in (it.get('notes') or []))

    # 替代款单独一块（不用 mnote）：这是"下一步做什么"，不是"注意事项"
    alts = it.get('alts') or []
    alts_html = ''
    if life == 'HALT':
        if alts:
            alts_html = ('<div class="alts"><b>同系列在产候选</b> —— 只是线索，'
                         '参数/档次/价格都要重新核：<br>%s</div>'
                         % '  '.join('<code>%s</code>' % esc(a) for a in alts))
        else:
            # 没有候选必须说清楚，否则用户以为"页面没显示"而不是"确实没有"
            alts_html = ('<div class="alts">同系列<b>没有找到在产型号</b> → '
                         '需要你指定替代款（问销售、或查最新选型表）。'
                         '脚本不替你挑，挑错了参数价格全得重来。</div>')

    return """
<div class="%s">
  <div class="mhd">%s%s<em>%s</em></div>
  <div class="pbar">%s%s%s%s</div>
  %s
  %s
  %s
  %s
  %s
</div>""" % (card_cls, head, badge, em,
             cell('标准价', it.get('price'), '¥'),
             cell('业务折扣', it.get('bd')),
             cell('片区折扣', it.get('pq')),
             cell('总部折扣', it.get('hq')),
             src_line, plist, notes, alts_html, copybtn)


def one_result_body(d, q='', models='', with_price=True):
    sub = '控标库 %d 条' % (d.get('kb_count') or 0)
    if d.get('crm'):
        sub += ' ・ CRM：' + esc(d['crm'])
    elif d.get('crm_err'):
        sub += ' ・ <span style="color:#92400e">%s</span>' % esc(d['crm_err'])
    cards = ''.join(one_card(it) for it in (d.get('items') or []))
    # 价格没查成要顶在最上面：小字藏在副标题里没人看，会以为"这型号没价"
    pwarn = ''
    if with_price and d.get('crm_err'):
        pwarn = ('<div class="warn"><b>价格没查成，参数正常</b><br>%s<br>'
                 '账号密码对不对、CRM 账号有没有停用，去「我的」里核对一次。'
                 '</div>' % esc(d['crm_err']))
    miss = sum(1 for it in (d.get('items') or []) if not (it.get('params') or []))
    tip = ''
    if miss:
        tip = ('<div class="card" style="font-size:13px;color:#92400e;line-height:1.7">'
               '有 %d 个型号没查到参数。控标库里没有、官网也没抓到时，'
               '这里会如实留空 —— 不编内容。这类型号要么手动补，'
               '要么在提示词里让 AI 补（那样会消耗 token）。</div>' % miss)
    return (tabs('one', q) + '<h1>查询结果</h1><div class="sub">%s</div>%s%s%s%s%s'
            % (sub, pwarn, tip, cards,
               '<a class="btn gray" href="/one%s">再查一批</a>' % q, COPY_JS))


def one_body(msg='', q='', models='', with_price=True):
    return tabs('one', q) + """
<h1>查单个型号</h1>
<div class="sub">填型号，出控标参数和四个价</div>
%s
<form method="post" action="/one%s" onsubmit="busy(this)">
<div class="card">
  <label>型号（一行一个，也可用逗号/空格隔开，最多 %d 个）</label>
  <textarea name="models" rows="4" placeholder="TL-NS650-24G4F&#10;TL-SG1008D">%s</textarea>
  <div class="opt" style="margin-top:12px">
    <input type="checkbox" name="price" value="1" %s id="p2">
    <label for="p2">同时查价格（连 CRM）</label></div>
  <div class="hint" style="margin-top:-4px">不勾就只查参数。价格要连 CRM，
    一个型号 1~2 秒；参数只能走官网的型号再多等几秒。</div>
  <button class="btn" type="submit">查询</button>
</div>
</form>
<div class="card" style="font-size:13px;color:#6b7280;line-height:1.9">
  <b style="color:#374151">参数从哪来（网页版没有 AI 参与，全是查表 + 抓页面）</b><br>
  · <b>① 本地控标库</b>（带 ★ 加分项，条数见结果页）—— 查到就直接用它<br>
  · <b>② 控标库没有 → 抓官网</b>产品页「产品特性」条目 —— 有内容，但<b>不带 ★</b>，
    正式投标前必须人工过一遍<br>
  · <b>③ 两边都没有 → 参数留空</b>，页面明写「没查到」。不编、不猜、不拿同系列顶替<br>
  · <b>四个价</b>：标准价（面价）+ 业务 / 片区 / 总部折扣率。
    折扣率只报 CRM 里真实有的数，网页<b>不替你算折后价</b>
</div>
""" % (msg, q, MODEL_MAX, esc(models), 'checked' if with_price else (
        '' if models else 'checked'))


def upload_body(msg='', q=''):
    """上传页。q 是 URL 上要带的 ?k=口令。"""
    return f"""
{tabs('up', q)}
<h1>清单填参数</h1>
<div class="sub">上传清单，自动补控标参数和价格</div>
{msg}
<form method="post" action="/upload{q}" enctype="multipart/form-data">
<div class="card">
  <label class="drop" id="drop" for="f">
    <div class="ico">📄</div>
    <div class="t" id="ft">点这里选清单</div>
    <div class="h" id="fh">.xlsx / .xlsm，最大 20 MB</div>
  </label>
  <input class="visually-hidden" type="file" id="f" name="file"
         accept=".xlsx,.xlsm" required onchange="pk(this)">

{pricing_block()}

  <div class="opt"><input type="checkbox" name="price" value="1" checked id="p">
  <label for="p">同时算价格（连 CRM 取价）</label></div>
  <div class="hint" style="margin-top:-4px">不勾就只填参数、价格列不动。CRM 连不上时先别勾。<br>
    <span style="color:#92400e">不算价格时无法知道型号是项目机还是渠道机（那信息在 CRM 里），
    两档口径都不会生效。</span></div>

  <button class="btn" type="submit">上传并检查</button>
</div>
</form>
<div class="card">
  <details>
    <summary>手机上怎么把清单弄进来（微信里收到的文件）</summary>
    <div class="hint" style="font-size:13px;line-height:1.8;margin-top:10px">
      <b>iPhone：</b>微信里点开文件 → 右上角「…」→ 用其他应用打开 → 存储到「文件」，
      再回到浏览器点上面的方块 → 浏览 → 选刚存的那个。<br>
      <b>安卓：</b>微信里点开文件 → 右上角「…」→ 用其他应用打开 → 选「浏览器」或 WPS 存一份，
      再回到本页点方块 → 文件里找到它。<br>
      <b>最省事：</b>在电脑上打开这个网址上传，手机只负责看结果、下载。<br>
      <span style="color:#92400e">为什么不能直接从微信聊天记录选？<br>
      微信个人号没有开放「选聊天文件」的接口，第三方插件会被封号，不做。
      企业微信有（JS-SDK），但要管理员建自建应用 + 配可信域名，等拿到权限再接。</span>
    </div>
  </details>
</div>
<div class="card" style="font-size:13px;color:#6b7280">
  · 上传后会先告诉你「型号列、参数列、价格列认成了哪几列」，<b>认错可以当场改</b>，改完才真填<br>
  · 文件只存在这台电脑的临时目录，处理完几小时后自动删除<br>
  · 手机拍的、扫描的表格不行，只支持 Excel 文件
</div>
<script>
function pk(i){{
  var d=document.getElementById('drop'),t=document.getElementById('ft'),h=document.getElementById('fh');
  if(i.files&&i.files.length){{
    d.className='drop picked';
    t.textContent=i.files[0].name;
    h.textContent=Math.round(i.files[0].size/1024)+' KB ・ 点一下可以换一个';
  }}
}}
</script>
"""


def confirm_body(s):
    heads = s.get('heads') or {}
    maxc = max(max(20, s.get('maxc') or 20), s.get('cm') or 1, s.get('cp') or 1, s.get('ck') or 1)
    if maxc > 40:
        maxc = 40

    trs = ''.join(
        '<tr><td class="mono" style="color:#9ca3af">%d</td><td class="mono">%s</td>'
        '<td style="color:#6b7280">%s</td></tr>'
        % (r['row'], esc(r['model'], 24), esc(r['extra'] or '—', 26)) for r in s['preview'])

    notes = ''.join('<div class="warn">%s</div>' % n for n in s['notes'])

    hr_opts = ''.join('<option value="%d"%s>第 %d 行</option>'
                      % (i, ' selected' if i == s['header_row'] else '', i)
                      for i in range(1, 11))

    if s.get('pricing_err'):
        pmsg = '<div class="err">口径没看懂：%s</div>' % esc(s['pricing_err'])
    elif s.get('with_price'):
        pmsg = ('<div class="ok"><b>这次怎么算</b><br>'
                '<b>项目机型</b>（标准价）：%s<br>'
                '<b>渠道机</b>（标准价）：%s<br>'
                '打折/加减点算出来<b>带小数的向个位 0/2/5 进位，整数原样</b>；+0 个点的面价原样不取整</div>'
                % (esc(s.get('pricing_p_note') or '面价原样'),
                   esc(s.get('pricing_c_note') or '面价原样')))
    else:
        pmsg = '<div class="tip">没勾「同时算价格」，口径不影响这次结果。</div>'

    return f"""
<h1>确认列位</h1>
<div class="sub">{esc(s['fname'], 40)} ・ 共 {s['nrows']} 行数据 ・ 工作表 {esc(s['sheet'])}</div>
{notes}
<div class="card">
  <div class="kv"><span>认出多少行型号</span>
    <b>{s['nrows']} 行{'，可以' if s['nrows'] > 0 else '，⚠ 没认出来'}</b></div>
  <div class="kv"><span>这次算不算价格</span>
    <b>{'算（要连 CRM）' if s['with_price'] else '只填参数'}</b></div>
  <form method="post" action="/rehead{qk()}" style="margin-top:4px">
    <input type="hidden" name="sid" value="{s['sid']}">
    <label>表头在第几行（改这里，下面的列名会跟着变）</label>
    <select name="hr" onchange="this.form.submit()">{hr_opts}</select>
  </form>
</div>

<form method="post" action="/run{qk()}">
<input type="hidden" name="sid" value="{s['sid']}">
<input type="hidden" name="price" value="{1 if s['with_price'] else 0}">
<div class="card">
  <label>① 型号在哪一列</label>
  <select name="mc">{col_opts(s['cm'], heads, maxc)}</select>

  <label>② 参数写到哪一列</label>
  <select name="pc">{col_opts(s['cp'], heads, maxc)}</select>

  <label>③ 单价写到哪一列</label>
  <select name="kc">{col_opts(s['ck'], heads, maxc)}</select>

  <div class="hint">认错就改这里，<b>不用重新上传</b>。选项后面跟的是这一列的表头文字，
    写「（空列）」的说明那列本来没表头 —— 参数可以放进空列。</div>
</div>

<div class="card">
{pricing_block(s.get('p_basis'), s.get('p_sign'), s.get('p_pts'),
                s.get('c_sign'), s.get('c_pts'), pmsg)}
</div>

<div class="card">
  <label>前几行长这样，核对下型号列对不对</label>
  <table>
    <tr><th style="width:38px">行</th><th style="width:40%">型号</th><th>同行其他内容</th></tr>
    {trs}
  </table>
</div>

<button class="btn" type="submit">确认，开始填</button>
</form>
<a class="btn gray" href="/">重新上传</a>
"""


def result_body(s, ok, log, elapsed, report=None):
    advice = ''
    if ok:
        head = ('<h1>填好了</h1><div class="sub">用时 %.1f 秒</div>' % elapsed)
        dl = '<a class="btn" href="/dl/%s%s">下载填好的清单</a>' % (s['sid'], qk())
        if 'CRM 登录失败' in (log or ''):
            advice = ('<div class="warn"><b>CRM 没登进去，价格一行都没填</b><br>'
                      '账号或密码不对、或账号被停用。去「我的」里改一次再重跑；'
                      '参数已经正常填好了，不受影响。</div>') + advice
        if '填参数 0 行' in (log or ''):
            advice = ('<div class="warn"><b>这次一行参数都没写进去。</b><br>'
                      '最常见的原因：目标参数列本来就有内容 —— 脚本按规矩不覆盖非空单元格。'
                      '清空那一列后重新上传，或在确认页换一个空列。</div>')
    else:
        head = '<h1>没填成</h1><div class="sub">下面这段是脚本输出，原样保留</div>'
        dl = ''
    # 停产排在最前面：型号是废的，参数来源对不对已经不重要了
    note = (halt_note(report) + param_source_note(report)) if ok else ''
    return """
%s
<div class="card">
  <div class="kv"><span>文件</span><b>%s</b></div>
  <div class="kv"><span>处理行数</span><b>%d 行</b></div>
</div>
%s
%s
%s
<div class="card">
  <label>处理记录</label>
  <pre>%s</pre>
</div>
<div class="tip">这份清单有没有要调整的？<br>
参数不准 / 缺 ★ 加分项 / 价格口径不对，都直接跟维护的人说一句，会改进规则。<br>
规则改了以后，下次同样的清单就不会再犯。</div>
<a class="btn gray" href="/">再传一份</a>
""" % (head, s['fname'], s['nrows'], dl, advice, note,
       (log or '(无输出)').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def err_body(msg, sid=''):
    return '<h1>出错了</h1><div class="err">%s</div><a class="btn gray" href="/">返回</a>' % msg


# ---------------------------------------------------------------- 业务

def build_rate_spec(p_rate, p_base, c_rate, c_base):
    """两档口径 → autofill_quote.py 的 --rate-spec。

    · 两档完全一样 → 退化成单档（none / flat / dynamic），日志也更好读
    · 两档不同     → by_field 按 CRM 的 seriesValue 自动分流（项目机型 / 其余=渠道机）
    · 任一档可以写成动态表达式，如 {'from':'business','offset':-0.02}
      （业务折扣率每型号不同，不能用固定折扣率表达）
    """
    p_base = (p_base or 'price').strip().lower()
    c_base = (c_base or 'price').strip().lower()
    pe, ce = rate_expr(p_rate), rate_expr(c_rate)

    if pe == ce and p_base == c_base:
        if pe is None:
            return {'mode': 'none'}
        if isinstance(pe, dict):
            if pe.get('from') == 'business':
                return {'mode': 'from_business', 'offset': pe.get('offset') or 0,
                        'base': p_base}
            return {'mode': 'dynamic', 'rate': pe, 'base': p_base}
        return {'mode': 'flat', 'rate': pe, 'base': p_base}

    return {'mode': 'by_field', 'field': 'seriesValue', 'values': list(PROJECT_VALUES),
            'rate': pe, 'else_rate': ce,
            'hit': {'base': p_base}, 'else': {'base': c_base}}


def run_fill(s, hr, mc, pc, kc, with_price,
             p_rate=None, p_base='price', c_rate=None, c_base='price',
             creds=('', '')):
    """调用 autofill_quote.py 子进程。返回 (ok, log, report)。

    p_rate / c_rate 为 None 表示该档不打折、取基准价原样；
    有值则 基准价 × 折扣率，带小数的由脚本向个位 0/2/5 进位（整数原样）。

    report 是 `--report` 写出的台账（list[dict]），每条带 kind / model / source，
    页面据此告诉用户「哪些型号控标库没命中、参数改用了官网描述」。
    """
    script = find_autofill()
    if not script:
        return False, '找不到 autofill_quote.py，请把本文件放在仓库根目录，或先 install.py 装好技能。', []
    src = s['src']
    out = os.path.join(s['dir'], 'result_' + os.path.basename(src))
    rep_dir = tempfile.mkdtemp(prefix='tpkit_rep_')
    rep = os.path.join(rep_dir, 'report.json')
    spec = json.dumps(build_rate_spec(p_rate, p_base, c_rate, c_base), ensure_ascii=False)
    cmd = [sys.executable, '-X', 'utf8', script,
           '--xlsx', src, '--out', out,
           '--header-row', str(hr),
           '--model-col', get_column_letter(mc),
           '--param-col', get_column_letter(pc),
           '--price-col', get_column_letter(kc),
           '--rate-spec', spec,
           '--report', rep,
           '--no-gate']                        # 网页没有交互，门禁由页面自己承担
    if not with_price:
        cmd.append('--no-crm')
    # 每个人的 CRM 账号走环境变量：argv 全机可见，密码不能塞命令行
    env = dict(os.environ)
    if with_price and creds and creds[0]:
        env['TPKIT_CRM_EMAIL'] = creds[0]
        env['TPKIT_CRM_PASSWORD'] = creds[1] or ''
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=900,
                           cwd=os.path.dirname(script), env=env)
    except subprocess.TimeoutExpired:
        shutil.rmtree(rep_dir, ignore_errors=True)
        return False, '处理超时（15 分钟），可能是 CRM 卡住或行数太多。可先取消"同时算价格"重试。', []
    log = ''
    for b in (p.stdout, p.stderr):
        if b:
            log += b.decode('utf-8', 'replace')
    report = []
    if os.path.exists(rep):
        try:
            with io.open(rep, encoding='utf-8') as f:
                report = json.load(f) or []
        except Exception:
            report = []
    shutil.rmtree(rep_dir, ignore_errors=True)
    ok = p.returncode == 0 and os.path.exists(out)
    if ok:
        s['out'] = out
    return ok, log.strip() or ('退出码 %d' % p.returncode), report


def halt_note(report):
    """清单里出现停产型号的提示（2026-10-27）。

    停产行**参数和价格都没填**，所以这条必须排在参数来源提示之前 ——
    否则用户先看到"参数采用官网描述"，以为自己只要补参数就行。
    """
    halts, warns = [], []
    seen_h, seen_w = set(), set()
    for e in report or []:
        k = e.get('kind')
        if k not in ('停产', '停产预警'):
            continue
        m = (e.get('model') or e.get('halt_model') or '').strip()
        if not m:
            continue
        key = m.upper()
        if k == '停产':
            if key in seen_h:
                continue
            seen_h.add(key)
            halts.append(e)
        else:
            if key in seen_w:
                continue
            seen_w.add(key)
            warns.append(e)
    out = []
    if halts:
        rows = []
        for e in halts:
            alts = e.get('alts') or []
            tail = ('　→ 在产候选：%s' % '、'.join(alts[:4])) if alts \
                else '　→ 同系列没有在产型号，**需要你指定替代款**'
            nm = ('（%s）' % e.get('product_name')) if e.get('product_name') else ''
            rows.append('<li><b>第 %s 行 %s</b>%s%s</li>'
                        % (e.get('row'), esc(e.get('model') or ''),
                           esc(nm, 60), _md_bold(tail[:300])))
        out.append('<div class="warn red"><b>⛔ 有 %d 个型号已停产（HALT），'
                   '参数和价格都没有填</b><ul class="hl">%s</ul>'
                   '换成在产款（或你指定）后重新上传一次。'
                   '原表里这两格如果是客户自己填的，脚本没有改动。</div>'
                   % (len(halts), ''.join(rows)))
    if warns:
        out.append('<div class="warn"><b>⚠ 有 %d 个型号是停产预警（HALT_WARNING）</b>：'
                   '参数和价格已填，目前还能买；<b>长周期项目要留意交付时会不会已经停产</b>。<br>%s</div>'
                   % (len(warns), esc('、'.join((e.get('model') or '') for e in warns), 400)))
    return ''.join(out)


def param_source_note(report):
    """清单里参数不是取自控标库的，点名列出来（用户 2026-10-26 要求）。

    分两种：
      · 控标库未命中、官网抓到了 → 参数 = 官网描述（不带 ★）
      · 控标库未命中、官网也没有 → 参数留空
    """
    web, miss, seen_w, seen_m = [], [], set(), set()
    for e in report or []:
        if e.get('kind') != '参数':
            continue
        m = (e.get('model') or '').strip()
        if not m:
            continue
        k = m.upper()
        if (e.get('source') or '').startswith('官网'):
            if k not in seen_w:
                seen_w.add(k)
                web.append(m)
        elif not e.get('source'):
            if k not in seen_m:
                seen_m.add(k)
                miss.append(m)
    out = []
    if web:
        out.append('<div class="warn"><b>有 %d 个型号控标库未命中，参数采用官网描述</b><br>'
                   '%s<br>官网描述<b>不带 ★ 加分项</b>，正式投标前建议人工过一遍（官网原文没做控标改写）。</div>'
                   % (len(web), esc('、'.join(web), 400)))
    if miss:
        out.append('<div class="warn"><b>有 %d 个型号控标库未命中，官网也没抓到 → 参数留空</b><br>'
                   '%s<br>这几个没编内容，需要人工补。</div>'
                   % (len(miss), esc('、'.join(miss), 400)))
    return ''.join(out)


# ---------------------------------------------------------------- HTTP

class H(BaseHTTPRequestHandler):
    server_version = 'tpkit-web/1.0'

    def log_message(self, fmt, *a):
        """请求日志只写文件，**绝不写 stdout/stderr**。

        原因（2026-10-24 踩过，现象是"点上传转很久"）：Windows 控制台开了
        QuickEdit，只要鼠标在窗口里点一下、把文字刷成选中状态，进程下一次
        往 stdout/stderr 写就会**阻塞**，直到用户取消选中。
        log_message 每个请求都调，一卡就是所有请求全不响应 ——
        端口还在 LISTENING、CPU 几乎为 0、浏览器一直转圈，
        看起来像"网页慢"，其实是控制台把进程冻住了。
        """
        _write_log('[%s] %s %s\n' % (time.strftime('%m-%d %H:%M:%S'),
                                     self.address_string(), fmt % a))

    # ---- 工具 ----
    def _send(self, code, data, ctype='text/html; charset=utf-8', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        # 身份 token 从 URL 验过一次，就把响应里的 cookie 种上：
        # 页面里「开始填」「下载」「返回」这些链接/表单原来不带 ?k=，
        # 一点就被弹回「需要口令」。种了 cookie 之后后续请求全都过。
        tok = (getattr(self, 'user', None) or {}).get('token') or ''
        if tok and getattr(self, '_grant', False):
            self.send_header('Set-Cookie',
                             'tpkit_k=%s; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax'
                             % tok)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _page(self, title, body, code=200, sid='', refresh=None):
        """渲染整页，自动带上顶部身份条（谁在用 / 绑没绑 CRM / 管理入口）。"""
        return self._send(code, page(title, bar(self.user, qk()) + body, sid, refresh))

    def _auth_ok(self):
        """解析身份 → self.user。

        一个 token 就是一个人的身份，链接即身份（同事打开链接就能用，不用记账号）。
        token 放 /admin 里生成，管理员发给谁，谁的点就算谁的。
        """
        self.user = None
        self._grant = False
        _TLS.token = ''
        if not ACCESS_PASS:
            self.user = {'id': '', 'name': '本机调试', 'role': 'admin',
                         'token': '', 'active': True, 'email': '', 'pwd': ''}
            return True
        tok = parse_qs(urlparse(self.path).query).get('k', [''])[0]
        if not tok:
            for part in (self.headers.get('Cookie') or '').split(';'):
                name, _, val = part.strip().partition('=')
                if name == 'tpkit_k':
                    tok = val.strip()
                    break
        u = WU.by_token(tok)
        if not u or not u.get('active', True):
            return False
        self.user = u
        self._grant = True
        _TLS.token = u.get('token') or ''
        _TLS.host = self.headers.get('Host') or ''
        return True

    def _deny(self):
        self._send(403, page('链接不对',
                             '<h1>链接不对</h1><div class="err">这条链接无效或已被停用，'
                             '向维护的人要一条新的。</div>'
                             '<div class="hint">链接里的 <span class="mono">?k=</span> '
                             '就是你的身份，<b>一个人一条</b>，别互相转发。</div>'))

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if not self._auth_ok():
            return self._deny()

        if path == '/':
            return self._page('清单填参数', upload_body('', qk()))

        if path == '/one':
            return self._page('查单个型号', one_body('', qk()))

        if path == '/me':
            return self._send(200, page('我的', me_body(self.user, qk())))

        if path == '/admin':
            if (self.user or {}).get('role') != 'admin':
                return self._send(403, page('没有权限', err_body(
                    '这一页只有维护者能看。要用自己的链接正常算清单，回首页就行。')))
            return self._admin_page()

        if path == '/admin/cred':
            if (self.user or {}).get('role') != 'admin':
                return self._send(403, page('没有权限', err_body(
                    '这一页只有维护者能看。')))
            return self._admin_cred(parse_qs(u.query))

        if path == '/admin/usage.csv':
            if (self.user or {}).get('role') != 'admin':
                return self._send(403, page('没有权限', err_body('只有维护者能导。')))
            data = WU.csv_text().encode('utf-8')
            return self._send(200, data, 'text/csv; charset=utf-8',
                              {'Content-Disposition':
                               "attachment; filename*=UTF-8''%s"
                               % quote('使用记录_%s.csv' % time.strftime('%Y%m%d'))})

        if path.startswith('/dl/'):
            sid = path[4:]
            s = get_session(sid)
            if not s or not s.get('out') or not os.path.exists(s['out']):
                return self._send(404, page('文件没了',
                                            err_body('下载链接已过期或文件已清理，请重新上传。')))
            fn = s.get('outname') or os.path.basename(s['out'])
            with open(s['out'], 'rb') as f:
                data = f.read()
            disp = "attachment; filename*=UTF-8''%s" % quote(fn)
            return self._send(200, data,
                              'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                              {'Content-Disposition': disp})

        return self._send(404, page('404', err_body('页面不存在')))

    # ---- POST ----
    def do_POST(self):
        u = urlparse(self.path)
        if not self._auth_ok():
            return self._deny()

        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_UPLOAD + 1024 * 1024:
            return self._send(413, page('文件太大',
                                        err_body('文件超过 20 MB，请精简后再传。')))
        body = self.rfile.read(n)

        if u.path == '/upload':
            return self._on_upload(body)
        if u.path == '/rehead':
            return self._on_rehead(body)
        if u.path == '/run':
            return self._on_run(body)
        if u.path == '/one':
            return self._on_one(body)
        if u.path == '/me/save':
            return self._on_me_save(body)
        if u.path == '/admin/new':
            return self._on_admin_new(body)
        if u.path == '/admin/toggle':
            return self._on_admin_toggle(body)
        return self._send(404, page('404', err_body('页面不存在')))

    # ---- 我的 / 管理 ----
    def _admin_page(self):
        users = WU.load()['users']
        return self._send(200, page('使用情况', admin_body(
            self.user, qk(), users, WU.summary(), WU.recent(40),
            host=self.headers.get('Host') or '')))

    def _admin_cred(self, f):
        """看某人的 CRM 凭据。forget=1 就清掉他的密码。"""
        uid = (f.get('uid') or [''])[0]
        info = WU.reveal(uid)
        if not info.get('id'):
            return self._send(404, page('没这人', err_body('用户不存在，或者刚被删了。')))
        me = self.user or {}
        ip = self.client_address[0]
        if (f.get('forget') or [''])[0] == '1':
            WU.update(uid, pwd='')
            WU.log(me.get('id'), me.get('name'), 'viewcred',
                   note='清掉 %s 的密码' % (info.get('name') or uid), ip=ip)
            return self._send(302, b'', 'text/plain',
                              {'Location': '/admin/cred' + qk() + '&uid=' + quote(uid)})
        WU.log(me.get('id'), me.get('name'), 'viewcred',
               note='看了 %s 的凭据' % (info.get('name') or uid), ip=ip)
        return self._send(200, page('CRM 凭据', cred_body(self.user, qk(), info)))

    def _on_me_save(self, body):
        """保存自己的 CRM 账号。密码留空 = 不改；勾了"记住"才落盘。"""
        f = parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True)

        def g(k):
            v = f.get(k)
            return ((v[0] if v else '') or '').strip()

        email, pwd = g('email'), g('pwd')
        remember = g('remember') == '1'
        if not email:
            return self._send(400, page('我的', me_body(
                self.user, qk(), '<div class="err">CRM 账号不能空。</div>')))
        uid = (self.user or {}).get('id') or ''
        if not uid:                     # --no-pass 调试模式没有用户记录
            return self._send(400, page('我的', me_body(
                self.user, qk(), '<div class="err">当前是无口令调试模式，不存账号。</div>')))
        old = WU.by_id(uid) or {}
        forget = g('forget') == '1'
        if forget:
            WU.update(uid, email=email, pwd='')
        elif pwd:
            WU.set_creds(uid, email, pwd, remember)
        else:
            WU.update(uid, email=email)     # 只改邮箱，密码保持原样
        WU.touch(WU.by_id(uid) or {})
        WU.log(uid, (self.user or {}).get('name'), 'bind', ok=1,
               ip=self.client_address[0])
        msg = ('<div class="ok">已保存。' +
               ('已删掉记住的密码，下次要重新输。' if forget else
                ('密码已记住，下次不用再输。' if (pwd and remember) else
                 ('密码没记住，下次还要输一次。' if pwd else '只更新了账号，密码没动。'))) +
               '</div>')
        self.user = WU.by_id(uid) or self.user
        return self._send(200, page('我的', me_body(self.user, qk(), msg)))

    def _on_admin_new(self, body):
        if (self.user or {}).get('role') != 'admin':
            return self._send(403, page('没有权限', err_body('只有维护者能加人。')))
        f = parse_qs(body.decode('utf-8', 'replace'))
        name = ((f.get('name') or [''])[0] or '').strip()
        if not name:
            return self._admin_page()
        u = WU.add(name, role='user')
        WU.log((self.user or {}).get('id'), (self.user or {}).get('name'),
               'admin', ok=1, ip=self.client_address[0])
        host = self.headers.get('Host') or ''
        msg = ('<div class="ok">已加 <b>%s</b>，把这条发给他：<br>'
               '<span class="mono">http://%s/?k=%s</span></div>'
               % (esc(name), esc(host), esc(u['token'])))
        return self._send(200, page('使用情况', admin_body(
            self.user, qk(), WU.load()['users'], WU.summary(), WU.recent(40),
            msg=msg, host=host)))

    def _on_admin_toggle(self, body):
        if (self.user or {}).get('role') != 'admin':
            return self._send(403, page('没有权限', err_body('只有维护者能改。')))
        f = parse_qs(body.decode('utf-8', 'replace'))
        uid = ((f.get('uid') or [''])[0] or '').strip()
        u = WU.by_id(uid)
        if u and u.get('id') != (self.user or {}).get('id'):
            WU.update(uid, active=not u.get('active', True))
        return self._admin_page()

    def _on_one(self, body):
        """查单个型号：只读查询，不落文件、不建会话。"""
        f = parse_qs(body.decode('utf-8', 'replace'))

        def g(k, d=''):
            v = f.get(k)
            v = (v[0] if v else '')
            return (v or '').strip() or d

        raw = g('models')
        with_price = g('price', '0') == '1'
        # 型号分隔：换行 / 逗号 / 顿号 / 分号 / 空格
        toks = [t for t in re.split(r'[\s,，、;；]+', raw) if t]
        seen, models = set(), []
        for t in toks:
            if t.upper() not in seen:
                seen.add(t.upper())
                models.append(t)
        if not models:
            return self._send(400, page('没填型号', one_body(
                '<div class="err">型号是空的，填一个再查。</div>', qk(), raw, with_price)))
        cut = len(models) > MODEL_MAX
        models = models[:MODEL_MAX]

        # 查价格要用自己的账号；没绑先让他绑（参数部分不需要账号，不拦）
        creds = ('', '')
        if with_price:
            creds = WU.creds(self.user)
            if not (creds[0] and creds[1]):
                return self._send(400, page('先绑 CRM 账号', me_body(
                    self.user, qk(),
                    '<div class="err">这次要查价格，但你的 CRM 账号还没绑好。'
                    '在下面填一次，勾上"记住密码"，以后就不用再输。</div>')))

        t0 = time.time()
        d, err = run_one_query(models, with_price, creds)
        if err:
            return self._send(400, page('查不了', one_body(
                '<div class="err">%s</div>' % esc(err), qk(), raw, with_price)))
        html = one_result_body(d, qk(), raw, with_price)
        if cut:
            html = ('<div class="warn">一次最多查 %d 个型号，这次只查了前 %d 个。</div>'
                    % (MODEL_MAX, MODEL_MAX)) + html
        items = (d or {}).get('items') or []
        WU.touch(self.user)
        WU.log((self.user or {}).get('id'), (self.user or {}).get('name'), 'one',
               models=len(models), secs=time.time() - t0, ok=1,
               kb_hit=sum(1 for x in items if (x.get('src_kind') == 'kb')),
               kb_web=sum(1 for x in items if (x.get('src_kind') == 'web')),
               kb_none=sum(1 for x in items if (x.get('src_kind') == 'none')),
               ip=self.client_address[0])
        return self._page('查询结果', html)

    def _on_upload(self, body):
        f = parse_multipart(body, self.headers.get('Content-Type', ''))
        if 'file' not in f:
            return self._send(400, page('没收到文件', err_body('没选到文件，返回重试。')))
        fname, data = f['file']
        if not data:
            return self._send(400, page('空文件', err_body('文件是空的。')))
        if len(data) > MAX_UPLOAD:
            return self._send(413, page('文件太大', err_body('超过 20 MB。')))
        low = (fname or '').lower()
        if not low.endswith(('.xlsx', '.xlsm')):
            return self._send(400, page('格式不支持',
                                        err_body('只支持 .xlsx / .xlsm。老格式 .xls 请先用 Excel'
                                                 '另存为 .xlsx。')))

        sid = new_session(fname=fname or '清单.xlsx', with_price=('price' in f))
        s = get_session(sid)
        src = os.path.join(s['dir'], 'src.xlsx')
        with open(src, 'wb') as fh:
            fh.write(data)
        s['src'] = src

        try:
            wb = openpyxl.load_workbook(src, data_only=True)
        except Exception as e:
            drop_session(sid)
            return self._send(400, page('打不开',
                                        err_body('Excel 打不开：%s。可能加密了或已损坏。' % e)))

        # 选行数最多的表（清单通常在第一个，但有人会放封面页）
        best_ws, best_n, best_hr, best_cols = None, -1, 2, (9, 10, 11)
        best_notes, best_heads = [], {}
        for ws in wb.worksheets:
            hr, cm, cp, ck, notes, heads = detect_columns(ws)
            n = count_rows(ws, hr, cm)
            if n > best_n:
                best_ws, best_n, best_hr = ws, n, hr
                best_cols, best_notes, best_heads = (cm, cp, ck), notes, heads
        wb.close()

        if best_n <= 0:
            drop_session(sid)
            return self._send(400, page('没认出型号',
                                        err_body('这份表里没找到像"型号"的内容（比如 TL-xxx）。<br>'
                                                 '如果型号是图片或合并单元格，网页版处理不了。')))

        nfilled, ntotal = param_filled(best_ws, best_hr, best_cols[1], best_cols[1])
        if nfilled:
            best_notes = list(best_notes) + [
                '参数列（%s）已经有 %d/%d 行填了内容。脚本**不会覆盖非空单元格**，'
                '这些行会被跳过；要让它们重新填，得先清空该列。'
                % (get_column_letter(best_cols[1]), nfilled, ntotal)]

        s.update(nrows=best_n, header_row=best_hr,
                 cm=best_cols[0], cp=best_cols[1], ck=best_cols[2],
                 notes=best_notes or [], sheet=best_ws.title, heads=best_heads,
                 maxc=max(best_heads) if best_heads else 20,
                 preview=preview_rows(best_ws, best_hr, best_cols[0]))

        # 两档定价口径：先算一次用于展示，真正取值在 /run 里再读一遍（用户可能改）
        def _fld(k, default=''):
            v = f.get(k)
            if isinstance(v, tuple):
                v = v[1]
            if isinstance(v, bytes):
                v = v.decode('utf-8', 'replace')
            return (v or '').strip() or default
        s.update(read_pricing(_fld))

        s['outname'] = re.sub(r'\.(xlsx|xlsm)$', '', fname or '清单', flags=re.I) + '_已填.xlsx'
        WU.touch(self.user)
        WU.log((self.user or {}).get('id'), (self.user or {}).get('name'), 'upload',
               rows=s.get('nrows') or 0, ok=1, ip=self.client_address[0])
        return self._page('确认列位', confirm_body(s))

    def _on_rehead(self, body):
        """改表头行 → 重算表头文字 / 行数 / 预览，再回到确认页。"""
        f = parse_qs(body.decode('utf-8', 'replace'))
        sid = (f.get('sid') or [''])[0]
        s = get_session(sid)
        if not s:
            return self._send(400, page('会话过期',
                                        err_body('上传状态已过期，请重新上传。')))
        try:
            hr = max(1, min(int((f.get('hr') or ['2'])[0]), 50))
        except Exception:
            hr = 2
        try:
            wb = openpyxl.load_workbook(s['src'], data_only=True)
            ws = wb[s['sheet']] if s.get('sheet') in wb.sheetnames else wb.worksheets[0]
            maxc = min(ws.max_column or 1, 40)
            s['heads'] = {c: str(ws.cell(hr, c).value or '').strip()
                          for c in range(1, maxc + 1)}
            s['maxc'] = maxc
            s['header_row'] = hr
            s['nrows'] = count_rows(ws, hr, s['cm'])
            s['preview'] = preview_rows(ws, hr, s['cm'])
            wb.close()
        except Exception as e:
            return self._send(400, page('读不了',
                                        err_body('重新打开文件失败：%s' % e)))
        return self._page('确认列位', confirm_body(s))

    def _on_run(self, body):
        f = parse_qs(body.decode('utf-8', 'replace'))
        sid = (f.get('sid') or [''])[0]
        s = get_session(sid)
        if not s:
            return self._send(400, page('会话过期',
                                        err_body('上传状态已过期（或服务重启过），请重新上传。')))
        try:
            hr = s['header_row']                       # 表头行以 /rehead 存下的为准
            mc = column_index_from_string((f.get('mc') or ['I'])[0].upper())
            pc = column_index_from_string((f.get('pc') or ['J'])[0].upper())
            kc = column_index_from_string((f.get('kc') or ['K'])[0].upper())
            wp = (f.get('price') or ['0'])[0] == '1'
        except Exception as e:
            return self._send(400, page('参数不对', err_body(str(e))))

        # 参数列 == 型号列 会把型号覆盖掉，这是最贵的一种错，先拦
        if len({mc, pc, kc}) < 3:
            s['notes'] = list(s.get('notes') or []) + [
                '⚠ 三个列不能选同一个：型号列 %s、参数列 %s、单价列 %s。换一个再提交。'
                % (get_column_letter(mc), get_column_letter(pc), get_column_letter(kc))]
            return self._send(400, page('列选重了', confirm_body(s)))

        # 口径：下拉/符号/点数都是短值，表单没带就用会话里上次的
        def _gf(k, default):
            v = f.get(k)
            v = (v[0] if v else '')
            v = (v or '').strip()
            if v:
                return v
            old = s.get(k)
            return str(old) if old not in (None, '') else default

        pr = read_pricing(_gf)
        s.update(pr)
        if pr['pricing_err']:
            return self._send(400, page('口径看不懂', confirm_body(s)))

        # 算价格必须用"这个人自己的" CRM 账号。
        # 没绑就直接让他绑，**绝不回落到维护者的账号** —— 那是别人的账。
        creds = ('', '')
        if wp:
            creds = WU.creds(self.user)
            if not (creds[0] and creds[1]):
                return self._send(400, page('先绑 CRM 账号', me_body(
                    self.user, qk(),
                    '<div class="err">这次要算价格，但你的 CRM 账号还没绑好。'
                    '在下面填一次，勾上"记住密码"，以后就不用再输。</div>')))

        t0 = time.time()
        ok, log, report = run_fill(s, hr, mc, pc, kc, wp,
                                   s['pricing_p_rate'], 'price',
                                   s['pricing_c_rate'], 'price', creds)
        secs = time.time() - t0

        # 统计：只记数字（几行、几命中），不记型号原文
        hit = web = none = 0
        seen = set()
        for e in report or []:
            if e.get('kind') != '参数':
                continue
            m = (e.get('model') or '').strip().upper()
            if not m or m in seen:
                continue
            seen.add(m)
            src = e.get('source') or ''
            if src.startswith('官网'):
                web += 1
            elif not src:
                none += 1
            else:
                hit += 1
        WU.touch(self.user)
        WU.log((self.user or {}).get('id'), (self.user or {}).get('name'), 'run',
               rows=s.get('nrows') or 0, secs=secs, ok=1 if ok else 0,
               kb_hit=hit, kb_web=web, kb_none=none, ip=self.client_address[0])
        return self._page('结果', result_body(s, ok, log, secs, report))


def _cfg_creds():
    """读维护者自己的 CRM 账号（复用技能里的查找顺序：配置 → MEMORY.md）。

    只在升级迁移时用一次：老版本只有一个全局口令，升级后把它建成"管理员"，
    账号沿用你平时命令行用的那个 —— 你自己的链接和行为都不用变。
    """
    script = find_autofill()
    if not script:
        return '', ''
    d = os.path.dirname(script)
    try:
        if d not in sys.path:
            sys.path.insert(0, d)
        import autofill_quote as A       # 模块级只有常量，import 是安全的
        em, pw = A.resolve_crm_creds('', '')
        return (em or ''), (pw or '')
    except Exception:
        return '', ''


# ---------------------------------------------------------------- 启动

def _ip_kind(ip):
    """粗判这个地址大概是什么网络，帮用户挑对访问地址。"""
    p = ip.split('.')
    if len(p) == 4:
        try:
            if p[0] == '100' and 64 <= int(p[1]) <= 127:
                return '← 100.64/10 组网段（Tailscale 就用这段）'
            if p[0] == '10':
                return '← 10 段（常见组网 / 内网）'
            if p[0] == '172' and 16 <= int(p[1]) <= 31:
                return '← 172.16/12 段（常见组网 / 内网）'
            if p[0] == '192' and p[1] == '168':
                return '← 192.168 段（本地局域网）'
        except ValueError:
            pass
    return ''


def local_ips():
    """本机所有 IPv4，去重，排除回环。"""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ':' in ip or ip.startswith('127.'):
                continue
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips


class _Server(ThreadingHTTPServer):
    """Windows 的 SO_REUSEADDR 语义允许两个进程绑同一个端口。

    标准库默认开着它，结果就是「旧的那个没退干净，新的又绑上了」，
    请求随机落到某个进程里 —— 表现为「改了代码没生效」或「会话时有时无」，
    极难排查。这里显式关掉：重复启动直接报错，不静默双绑。
    """
    allow_reuse_address = False


def main():
    global ACCESS_PASS, LOG_FILE
    # 行缓冲：双击运行时本来就即时显示，但输出被重定向到文件/管道时
    # 默认是块缓冲，启动横幅会「消失」到进程退出才出现，排查时很误导
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description='弱电清单报价 · 内网网页版')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--pass', dest='pwd', default='', help='访问口令（不填则自动生成并记住）')
    ap.add_argument('--no-pass', action='store_true', help='不要口令（只在自己电脑上调试时用）')
    ap.add_argument('--log', default='', help='日志文件，默认写到用户目录')
    a = ap.parse_args()

    if a.no_pass:
        ACCESS_PASS = ''
    else:
        ACCESS_PASS = load_or_make_pass(a.pwd)
        # 升级迁移：老版本只有一个全局口令，把它建成「管理员」，
        # CRM 账号沿用配置文件里的 → 维护者自己的链接不用改。
        try:
            if not any(u.get('token') == ACCESS_PASS for u in WU.load()['users']):
                _em, _pw = _cfg_creds()
                WU.add('维护者', role='admin', token=ACCESS_PASS,
                       email=_em, pwd=_pw)
        except Exception as _e:
            print('⚠ 用户表初始化失败（不影响使用，只是不记身份）：%s' % _e)

    LOG_FILE = a.log or os.path.join(STATE_DIR, 'webapp.log')
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    except Exception:
        LOG_FILE = None

    if not find_autofill():
        print('⚠ 没找到 autofill_quote.py，填表会失败。')
        print('  请把 webapp.py 放在仓库根目录，或先跑 install.py 装好技能。')
    os.makedirs(WORK_DIR, exist_ok=True)
    threading.Thread(target=janitor, daemon=True).start()

    try:
        srv = _Server((a.host, a.port), H)
    except OSError as e:
        print('=' * 60)
        print('启动失败：%s:%d 已经被占用了（%s）' % (a.host, a.port, e))
        print('=' * 60)
        print('  先确认是不是已经有一个在跑：浏览器打开下面这条')
        print('    http://127.0.0.1:%d/' % a.port)
        print('  能打开 → 不用再启动，直接用')
        print('  打不开 → 换端口：编辑 start.bat，把 --port %d 改成 %d' % (a.port, a.port + 1))
        print('  查是谁占的：netstat -ano | findstr :%d' % a.port)
        print('=' * 60)
        sys.exit(2)                     # 2 = 端口占用，start.bat 见到这个码就不再重启
    q = ('?k=' + ACCESS_PASS) if ACCESS_PASS else ''
    print('=' * 60)
    print('清单填参数 · 网页版已启动')
    print('=' * 60)
    if a.host in ('0.0.0.0', ''):
        for ip in local_ips():
            kind = _ip_kind(ip)
            print('  http://%s:%d/%s  %s' % (ip, a.port, q, kind))
        print('  ↑ 同事用哪个地址，取决于异地组网软件给这台机分配的 IP')
        print('    组网互通性自测：让同事在手机浏览器直接开上面任意一条')
    else:
        print('  本机打开：http://%s:%d/' % (a.host, a.port))
    if ACCESS_PASS:
        print('  你自己的链接：%s' % q)
        print('  口令：%s（已写进 %%USERPROFILE%%\\.workbuddy\\tplink-toolkit\\web_pass.txt）' % ACCESS_PASS)
        print('  发链接 / 看谁在用：http://127.0.0.1:%d/admin%s' % (a.port, q))
        print('  每人一条 /?k= 链接，各用自己的 CRM 账号（/me 里填，加密存本机）')
    else:
        print('  ⚠ 未设口令：能访问到这个地址的人都能用。只在调试时这样开。')
    print('  日志：%s' % (LOG_FILE or '未启用'))
    print('  停止：Ctrl+C（开机自启见 部署-内网.md）')
    print('=' * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')
        shutil.rmtree(WORK_DIR, ignore_errors=True)


if __name__ == '__main__':
    main()
