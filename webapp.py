#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""弱电清单报价 · 内网网页版（手机 / 异地组网都能访问）

一个文件、零额外依赖（除 openpyxl），用 Python 标准库起 HTTP 服务。
同事手机连公司 WiFi，或通过异地组网（蒲公英 / ZeroTier / Tailscale 等），
浏览器打开 http://<这台机的 IP>:8000 就能用，什么都不用装。

用法：
    python webapp.py                    # 监听 0.0.0.0:8000
    python webapp.py --port 8080        # 换端口
    python webapp.py --no-pass          # 跳过登录（只在自己电脑上调试时用）

登录（2026-10-29 起）：
    打开网页必须先填**自己的 CRM 邮箱 + 密码**，服务端拿它去 CRM 真登一次，
    验过才发会话 cookie。不填就什么都看不到 —— 这就是"鉴权"。
    会话落盘（logins.json），服务崩溃自动重启不会把所有人踢下线。

长期跑在分部机器上：双击 启动.bat 即可（崩溃自动重启），
外网访问 / 内网穿透（NATAPP / 花生壳）见 外网访问-内网穿透.md。

流程分两段（和命令行版一致）：
    ① 上传清单 → 服务自动探测列位 → 你确认探测对不对
    ② 确认后才真填，填完给下载链接

安全边界（挂公网前必须读，别只看"能跑通"）：
    · 文件落在本机临时目录，处理完（或超时）自动删；**用户点「反馈」才会留一份**，
      且页面上写明"会把这次上传的清单副本一起发给我"。
    · 只在"算价格"时连公司 CRM。
    · 密码加密存在这台机器上（DPAPI）。**被攻破 = 所有同事的 CRM 密码一起泄露**，
      不是"只能查价格" —— 攻击者拿到密码可以直接去 CRM 用，权限和同事本人一样。
      所以 admin 页的「查看凭据」在公网模式下会被强制关掉（见 ADMIN_CRED_LOCAL_ONLY）。
"""

import os
import re
import io
import sys
import json
import time
import uuid
import ssl
import shutil
import random
import socket
import tempfile
import threading
import subprocess
import argparse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, urlencode
from urllib.request import getproxies

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
SESSION_TTL = 3 * 3600             # 上传会话（确认页 → 填表）保留 3 小时
WORK_DIR = os.path.join(tempfile.gettempdir(), 'tpkit_web')

# ---- 登录 ----
REQUIRE_LOGIN = True               # --no-pass 时置 False（本机调试）
LOGIN_TTL = 30 * 86400             # 勾了"保持登录"：30 天
LOGIN_TTL_SHORT = 12 * 3600        # 没勾：12 小时
FAIL_MAX = 5                       # 同一个"账号+IP"连续错几次就锁
FAIL_WIN = 600                     # 统计窗口（秒）
FAIL_LOCK = 600                    # 锁多久（秒）
_login_fail = {}                   # key -> [次数, 首次时间, 锁到时间]
_login_lock = threading.Lock()
# 公网模式下「查看凭据」是否还允许：默认允许，但经穿透/代理访问时强制只认本机
ADMIN_CRED_LOCAL_ONLY = True

_sessions = {}                     # sid -> dict
_lock = threading.Lock()


def fmt_kb(kb):
    """KB 数 → 人能看的字串。穿透线路按月算流量，这事得看得见。"""
    try:
        kb = float(kb or 0)
    except Exception:
        return '0 KB'
    if kb <= 0:
        return '0 KB'
    if kb < 1024:
        return '%d KB' % round(kb)
    if kb < 1024 * 1024:
        return '%.1f MB' % (kb / 1024.0)
    return '%.2f GB' % (kb / 1024.0 / 1024.0)

# 口令 / 日志放在用户目录，不在仓库里，也不随技能重装丢
STATE_DIR = os.path.join(os.path.expanduser('~'), '.workbuddy', 'tplink-toolkit')
LOG_FILE = None                    # 由 --log 设置


def client_ip(h):
    """真实来源 IP。

    NATAPP / 花生壳 / nginx 这类穿透走的是本机回环，`client_address` 永远是 127.0.0.1。
    它们通常会加 X-Forwarded-For / X-Real-IP，有就取第一个。
    注意：这个头客户端可以伪造，只用于"看个大概"和粗限速，
    **不要**拿它做安全判断（真做判断就会被人绕过去）。
    """
    for name in ('X-Forwarded-For', 'X-Real-IP'):
        v = (h.headers.get(name) or '').strip()
        if v:
            return v.split(',')[0].strip()[:60]
    return (h.client_address[0] if h.client_address else '') or ''


def _ip_private(ip):
    """是不是内网/本机地址。"""
    p = (ip or '').split('.')
    if len(p) != 4:
        return False
    try:
        a, b = int(p[0]), int(p[1])
    except Exception:
        return False
    return (a == 127 or a == 10
            or (a == 192 and b == 168)
            or (a == 172 and 16 <= b <= 31)
            or (a == 100 and 64 <= b <= 127))     # 100.64/10 = 运营商/组网网段


def is_external(h):
    """这次请求是不是从"外网"进来的。

    判据是 Host：域名 → 外网；IP 且是内网段 → 内网。
    这个判断很粗，但用对了地方就够：它只用来决定"能不能看明文密码"，
    默认拒绝，误判成外网只是多一步限制，不会多开一个口子。
    """
    host = (h.headers.get('Host') or '').split(':')[0].strip().lower()
    if not host:
        return False
    if host in ('localhost', '127.0.0.1', '::1'):
        return False
    if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', host):
        return not _ip_private(host)
    return True          # 域名 / 穿透给的二级域名 → 当外网处理


def _safe_next(nxt):
    """登录后跳哪：只允许站内路径。

    必须同时挡掉 `//evil.com` —— 它 startswith('/')，但浏览器会当成
    协议相对地址跳到外站，是个标准的开放重定向。
    """
    nxt = (nxt or '/').strip()
    if not nxt.startswith('/') or nxt.startswith('//') or nxt.startswith('/\\'):
        return '/'
    return nxt


def _fail_state(key):
    """→ (是否被锁, 还要锁多少秒)"""
    with _login_lock:
        v = _login_fail.get(key)
        if not v:
            return False, 0
        now = time.time()
        if v[2] and v[2] > now:
            return True, int(v[2] - now)
        return False, 0


def _fail_add(key):
    """记一次登录失败；窗口内连续 FAIL_MAX 次就锁 FAIL_LOCK 秒。"""
    now = time.time()
    with _login_lock:
        v = _login_fail.get(key) or [0, now, 0]
        if now - v[1] > FAIL_WIN:
            v = [0, now, 0]
        v[0] += 1
        if v[0] >= FAIL_MAX:
            v[2] = now + FAIL_LOCK
        _login_fail[key] = v
        # 防止字典无限长（有人拿垃圾邮箱刷）
        if len(_login_fail) > 500:
            for k in [k for k, x in _login_fail.items()
                      if now - x[1] > FAIL_WIN and not x[2]]:
                _login_fail.pop(k, None)


def _fail_clear(key):
    with _login_lock:
        _login_fail.pop(key, None)


_CRM_BASE = None


def crm_base():
    """CRM 根地址。复用技能的 tpkit_config，代码里不写死任何内网域名。"""
    global _CRM_BASE
    if _CRM_BASE is not None:
        return _CRM_BASE
    _CRM_BASE = ''
    script = find_autofill()
    dirs = []
    if script:
        dirs.append(os.path.dirname(script))
    dirs.append(os.path.join(os.path.expanduser('~'), '.workbuddy', 'skills',
                             'tplink-crm-readonly', 'scripts'))
    for d in dirs:
        if not os.path.exists(os.path.join(d, 'tpkit_config.py')):
            continue
        try:
            if d not in sys.path:
                sys.path.insert(0, d)
            import tpkit_config as C
            _CRM_BASE = C.require_base('crm_base', 'CRM')
            break
        except Exception:
            continue
    return _CRM_BASE


def crm_login(email, pwd):
    """拿这组账号密码**真登一次** CRM。返回 (ok, 中文名, 部门, 错误说明)。

    这是登录鉴权的核心：不是本地比对一个哈希，而是让 CRM 自己说"这人对不对"。
    CRM 登录没有验证码，所以服务端可以直接代登。
    """
    base = crm_base()
    if not base:
        return False, '', '', ('这台机器没配 CRM 地址（crm_base），没法验证账号。'
                               '让维护的人先跑一遍 生成配置.py。')
    url = base.rstrip('/') + '/api/login'
    req = urllib.request.Request(
        url, data=urlencode({'email': email, 'password': pwd}).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'User-Agent': 'Mozilla/5.0'})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE      # 内网自签证书，和技能里的做法一致
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler(getproxies()),
        urllib.request.HTTPSHandler(context=ctx))
    try:
        raw = op.open(req, timeout=25).read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        msg = ''
        try:
            msg = (e.read() or b'').decode('utf-8', 'replace')[:160]
        except Exception:
            pass
        # 4xx = 账号密码不对；其它码 = 服务端/网关问题，措辞必须分开，
        # 否则同事会一直以为是自己密码错
        if e.code in (400, 401, 403):
            return False, '', '', '账号或密码不对'
        return False, '', '', 'CRM 返回 %s %s' % (e.code, msg)
    except Exception as e:
        return False, '', '', '连不上 CRM（%s）。' % e
    try:
        j = json.loads(raw)
    except Exception:
        return False, '', '', 'CRM 回的不是 JSON，可能被网关拦了'
    s = (j.get('sessionInfo') or {}) if isinstance(j, dict) else {}
    name = (s.get('chineseName') or '').strip()
    if not name:
        m = ''
        if isinstance(j, dict):
            m = j.get('msg') or j.get('message') or j.get('error') or ''
        return False, '', '', str(m or '账号或密码不对')[:120]
    return True, name, (s.get('officeName') or '').strip(), ''


_TLS = threading.local()            # 当前请求是谁（每个请求线程各存一份）


def qk():
    """URL 上带身份的查询串 —— 现在恒为空。

    2026-10-29 改：身份不再是 `?k=` 链接，而是登录后的会话 cookie。
    "链接即身份"有个致命问题：链接谁拿到都能用，转一次就等于共享账号，
    而且链接会留在聊天记录里。现在必须用 CRM 账号密码登录。
    保留函数名只是因为几十处 `"/run" + qk()` 的拼接不想全翻一遍。
    """
    return ''


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
        try:
            WU.purge_logins()          # 顺手清过期登录会话
        except Exception:
            pass


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
/* 本次耗时/用量：4 格数字条，手机上两列 */
.pbar.small .pi{padding:8px 10px}
.pbar.small .pi b{font-size:15px}
.pbar.small .pi span{font-size:11.5px}
/* 登录页 */
.loginbox{max-width:420px;margin:6vh auto 0}
.loginbox h1{font-size:22px}
.loginbox .card{padding:22px}
.remember{display:flex;align-items:center;gap:9px;margin:14px 0 0;font-weight:400;font-size:14px;color:#374151}
.remember input{width:18px;height:18px;margin:0;flex:0 0 auto}
/* 反馈 */
.fbnote{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:9px 11px;
 font-size:12.5px;color:#92400e;line-height:1.65;margin:8px 0}
.fbnote b{color:#7c2d12}
.fbitem{border:1px solid #eef0f3;border-radius:10px;padding:10px 12px;margin-top:10px;background:#fcfcfd}
.fbitem h4{margin:0 0 3px;font-size:14px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.fbitem .t{font-size:11.5px;color:#9ca3af;margin-left:auto;font-weight:400}
.fbitem .n{font-size:13px;color:#374151;line-height:1.65;word-break:break-word;
 display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#ef4444}
@media (max-width:440px){                          /* 手机窄屏：并排的控制改成上下 */
 .row{flex-direction:column;gap:0}
 .card{padding:16px}
 .loginbox{margin-top:3vh}
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
    """顶部切换：上传清单 / 查单个型号。q 保留形参，现在恒为空（身份走 cookie）。"""
    items = (('up', '/', '上传清单'), ('one', '/one', '查单个型号'))
    return '<div class="tabs">%s</div>' % ''.join(
        '<a href="%s%s" class="%s">%s</a>'
        % (u, q, 'on' if k == cur else '', t) for k, u, t in items)


def bar(u, q=''):
    """身份条：谁在用、CRM 账号、管理入口、退出。"""
    if not u:
        return ''
    bits = ['<span class="tag">%s</span>' % esc(u.get('name') or '')]
    em = (u.get('email') or '').strip()
    bits.append('<a href="/me">%s</a>' % (('CRM：' + esc(em)) if em else 'CRM 账号'))
    if u.get('role') == 'admin':
        n = WU.pending_feedback()
        bits.append('<a href="/admin">使用情况%s</a>'
                    % (('（%d 条新反馈）' % n) if n else ''))
    bits.append('<a href="/logout">退出</a>')
    return '<div class="who">%s</div>' % '<span class="sep">・</span>'.join(bits)


def login_body(email='', err='', nxt='/', notice=''):
    """登录页。就两栏：CRM 邮箱 + 密码。没有注册、没有找回密码。"""
    tip = '<div class="err">%s</div>' % esc(err) if err else ''
    return """
<div class="loginbox">
<h1>清单填参数</h1>
<div class="sub">用你自己的 CRM 账号登录，别人替不了你</div>
%s%s
<div class="card">
  <form method="post" action="/login">
    <input type="hidden" name="next" value="%s">
    <label>CRM 邮箱</label>
    <input type="text" name="email" value="%s" placeholder="你的 CRM 登录邮箱"
           autocomplete="username" autocapitalize="off" spellcheck="false" required>
    <label>CRM 密码</label>
    <input type="password" name="pwd" value="" placeholder="你的 CRM 登录密码"
           autocomplete="current-password" required>
    <label class="remember">
      <input type="checkbox" name="keep" value="1" checked>
      <span>保持登录 30 天（不勾就 12 小时后重新登录）</span>
    </label>
    <button class="btn" type="submit">登录</button>
  </form>
</div>
<div class="card" style="font-size:13px;color:#6b7280;line-height:1.75">
  · 账号密码会拿去<b>真登一次 CRM</b>，验过才放你进来，所以不存在"随便填个邮箱就能用"<br>
  · 密码会加密存在这台机器上 —— 算价格时必须拿它替你登录 CRM，不留就查不了价<br>
  · 输错 5 次会锁 10 分钟（防别人拿你的邮箱试密码）
</div>
</div>
""" % (tip, notice, esc(nxt or '/'), esc(email))


def me_body(u, q='', msg=''):
    """「我的」：登录账号、改记住的密码、退出。"""
    email, has_pwd = (u.get('email') or ''), bool(u.get('pwd'))
    host = getattr(_TLS, 'host', '') or ''
    link = ('http://%s/' % host) if host else '(当前访问的网址)'
    pwd_note = ('<div class="ok">密码已记住（加密存在这台机器上），下面留空就是不改。</div>'
                if has_pwd else
                '<div class="hint">没记住密码 —— 下次算价格会失败，重新登录一次就好。</div>')
    weak = ''
    if has_pwd and WU.mem_level() == 'weak':
        weak = ('<div class="warn">这台机用不了 Windows 加密接口，密码是<b>弱加密</b>存的。'
                '能登录这台电脑的人可以解出来。</div>')
    return """
%s<h1>我的</h1>
<div class="sub">登录身份就是你的 CRM 账号</div>
%s%s%s
<div class="card">
  <div class="kv"><span>登录账号</span><b class="mono">%s</b></div>
  <div class="kv"><span>访问地址</span><b class="mono">%s</b></div>
  <div class="kv"><span>用过的次数</span><b>%s 次</b></div>
  <div class="kv"><span>最近一次</span><b>%s</b></div>
</div>
<div class="card">
  <label>改一下记住的密码（CRM 里改过密码后才需要）</label>
  <form method="post" action="/me/save">
    <input type="text" name="email" value="%s" placeholder="CRM 登录邮箱"
           autocomplete="username">
    <label>新密码</label>
    <input type="password" name="pwd" value="" placeholder="%s"
           autocomplete="current-password">
    <div class="hint">密码用 Windows 加密接口存在<b>维护者这台机器</b>上，
      只用来替你登录 CRM，换台机器或换 Windows 账户就解不开。</div>
    <div class="warn">如实说一句：密码是加密存的，但<b>维护者能解出来看</b>，
      因为服务就是在他这台机上跑。介意的话就别让他开公网，或者每次用完让他清掉。</div>
    <button class="btn" type="submit">保存</button>
  </form>
</div>
<a class="btn gray" href="/logout">退出登录</a>
""" % (bar(u, q), msg, pwd_note, weak, esc(email or '—'), esc(link, 120),
       int(u.get('runs') or 0), esc(u.get('last') or '—'),
       esc(email), '已记住，留空不改' if has_pwd else '输入密码')


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


def admin_body(u, q, users, stats, recs, msg='', host='', fbs=None, fb_open=0):
    """管理员：谁在用、用了多少、反馈了什么。"""
    base = 'http://%s/' % (host or '这台机的IP:8000')
    blocks = []
    for x in users:
        on = x.get('active', True)
        blocks.append(
            '<div class="ub">'
            '<h4>%s%s<span class="st%s">%s</span></h4>'
            '<div class="lk">%s</div>'
            '<div class="meta">最近登录：%s ・ 操作 %s 次 ・ 建档：%s</div>'
            '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">'
            '<a class="btn gray" style="width:auto;padding:8px 14px;font-size:13px" '
            'href="/admin/cred&amp;uid=%s">查看 CRM 凭据</a>'
            '%s</div>'
            '</div>' % (
                esc(x.get('name') or ''),
                ' <span class="tag">管理员</span>' if x.get('role') == 'admin' else '',
                '' if on else ' off', '在用' if on else '已停用',
                esc(x.get('email') or '没登录过，还没有邮箱'),
                esc(x.get('last') or '—'), int(x.get('runs') or 0),
                esc(x.get('created') or '—'), esc(x.get('id') or ''),
                '' if x.get('id') == (u or {}).get('id') else
                ('<form method="post" action="/admin/toggle" style="display:inline;margin:0">'
                 '<input type="hidden" name="uid" value="%s">'
                 '<button class="btn gray" type="submit" '
                 'style="width:auto;padding:8px 14px;font-size:13px">%s</button></form>'
                 % (esc(x.get('id') or ''), '停用' if on else '启用'))))
    prow = []
    for p in stats.get('people') or []:
        prow.append('<tr><td class="nw">%s</td><td class="num">%s</td><td class="num">%s</td>'
                    '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                    '<td class="num">%s</td><td class="num">%s</td><td class="nw">%s</td></tr>' % (
                        esc(p['name']), p['n'], p['rows'], p['models'],
                        '%.1f' % (p['run_s'] or 0), '%.2f' % (p['up_s'] or 0),
                        '%.2f' % (p['pa_s'] or 0), fmt_kb(p['kb']), esc(p['last'])))
    rrow = []
    for r in recs:
        rrow.append('<tr><td class="mono nw">%s</td><td class="nw">%s</td><td class="nw">%s</td>'
                    '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                    '<td class="num">%s</td><td class="num">%s</td><td>%s</td></tr>' % (
                        esc(r['ts']), esc(r['name'] or ''), WU.kind_cn(r['kind']),
                        r['rows'] or 0, r['models'] or 0,
                        ('%.1f' % (r['secs'] or 0)),
                        ('%.2f' % (r['up_secs'] or 0)) if r.get('up_secs') else '—',
                        ('%d' % (r['kb'] or 0)) if r.get('kb') else '—',
                        esc(r.get('note') or '')))
    fbh = []
    for f in (fbs or []):
        fbh.append(
            '<div class="fbitem">'
            '<h4>%s%s<span class="tag">#%d</span><span class="t">%s</span></h4>'
            '<div class="n">%s</div>'
            '<div class="meta">清单：%s ・ %d 行 ・ %s%s</div>'
            '<a class="btn gray" style="width:auto;padding:7px 13px;font-size:13px;'
            'display:inline-block" href="/admin/fb&amp;id=%d">看详情 / 下载原件</a>'
            '</div>' % (
                esc(f.get('name') or ''), '' if f.get('done') else ' <span class="dot"></span>',
                f.get('id') or 0, esc(f.get('ts') or ''),
                esc((f.get('note') or '(没写说明)')[:220]),
                esc(f.get('fname') or '—'), f.get('rows') or 0,
                fmt_kb(f.get('kb') or 0),
                '' if f.get('has_file') else ' ・ <b>没留原件</b>',
                f.get('id') or 0))
    t = stats.get('total') or {}
    return """
%s<h1>使用情况</h1>
<div class="sub">谁登录过、跑了多少、反馈了什么</div>
%s
%s
<div class="card">
  <label>反馈（%d 条待看）</label>
  %s
  <div class="hint">用户点「结果有问题」时会把那次的清单副本和运行记录一起留在这里，
    点开能下载原件、看他当时填了什么。</div>
</div>
<div class="card">
  <label>人（%d 个）</label>
  %s
  <form method="post" action="/admin/new" style="margin-top:14px">
    <div class="row">
      <div><input type="text" name="name" placeholder="姓名" required></div>
      <div><input type="text" name="email" placeholder="CRM 邮箱" required></div>
    </div>
    <button class="btn" type="submit">预先建档</button>
    <div class="hint">不建也行 —— 他用 CRM 账号登录一次就自动出现。
      预先建档只是为了先把姓名写对，别在列表里显示成邮箱前缀。</div>
  </form>
</div>
<div class="card">
  <label>汇总：共 %s 次操作 ・ %s 行清单 ・ 上传 %s</label>
  <div style="overflow-x:auto">
  <table class="tb">
    <tr><th>用户</th><th class="num">次数</th><th class="num">行数</th>
        <th class="num">型号</th><th class="num">均处理</th><th class="num">均接收</th>
        <th class="num">均解析</th><th class="num">文件量</th><th>最近</th></tr>
    %s
  </table>
  </div>
  <a class="btn gray" href="/admin/usage.csv">导出全部记录 CSV</a>
</div>
<div class="card">
  <label>最近 40 条</label>
  <div style="overflow-x:auto">
  <table class="tb">
    <tr><th>时间</th><th>用户</th><th>动作</th><th class="num">行数</th>
        <th class="num">型号</th><th class="num">处理s</th><th class="num">接收s</th>
        <th class="num">KB</th><th>备注</th></tr>
    %s
  </table>
  </div>
</div>
<div class="hint" style="margin-top:-4px">对外地址：%s</div>
""" % (bar(u, q), msg, '' if not fb_open else
       '<div class="warn">有 %d 条新反馈还没看。</div>' % fb_open,
       fb_open, ''.join(fbh) or '<div class="hint">还没有人反馈</div>',
       len(users),
       ''.join(blocks) or '<div class="hint">还没有人登录过</div>',
       t.get('n', 0), t.get('rows', 0), fmt_kb(t.get('kb')),
       ''.join(prow) or '<tr><td colspan="9">还没有记录</td></tr>',
       ''.join(rrow) or '<tr><td colspan="9">还没有记录</td></tr>',
       esc(base, 120))


def feedback_body(u, sid, ctx='', fname='', kb=0, rows=0, ok=True, msg=''):
    """「结果有问题」的反馈页。写明会上传什么，不偷偷留。"""
    quota = ''
    if kb:
        quota = ('<div class="kv"><span>这次的文件</span><b>%s</b></div>'
                 '<div class="kv"><span>清单行数</span><b>%d 行</b></div>'
                 % (fmt_kb(kb), rows or 0))
    return """
%s<h1>反馈这次结果</h1>
<div class="sub">说一句哪里不对就行，不用写长</div>
%s
<div class="card">
  %s
  <form method="post" action="/feedback">
    <input type="hidden" name="sid" value="%s">
    <input type="hidden" name="ctx" value="%s">
    <label>哪里有问题？</label>
    <textarea name="note" rows="5" placeholder="比如：TL-IPC544FP-W4 的参数里少了音频编码；H3 的价格不对，应该是 3.4 折"></textarea>
    <div class="fbnote"><b>提交会把这次的东西一起留给我：</b>
      清单副本（%s）、运行日志、每行的命中情况。
      别的清单不会带，只有这一次。</div>
    <button class="btn" type="submit">提交</button>
  </form>
  <a class="btn gray" href="/">算了，返回</a>
</div>
""" % (bar(u, ''), msg, quota, esc(sid or ''),
       esc(ctx or '', 4000), esc(fname) if fname else '这次没有文件')



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
    """上传页。q 保留形参，现在恒为空（身份走 cookie）。"""
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

    stat_html = ('<div class="card"><div class="pbar small">'
                 '<div class="pi"><span>文件大小</span><b>%s</b></div>'
                 '<div class="pi"><span>传上来</span><b>%.1fs</b></div>'
                 '<div class="pi"><span>打开文件</span><b>%.2fs</b></div>'
                 '<div class="pi"><span>认出型号</span><b>%d 行</b></div>'
                 '</div></div>'
                 % (fmt_kb(s.get('kb')), s.get('recv_secs') or 0,
                    s.get('parse_secs') or 0, s['nrows']))

    return f"""
<h1>确认列位</h1>
<div class="sub">{esc(s['fname'], 40)} ・ 共 {s['nrows']} 行数据 ・ 工作表 {esc(s['sheet'])}</div>
{stat_html}
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


def result_body(s, ok, log, elapsed, report=None, stat=None):
    """填完的结果页。

    stat 里是"这次干了多少活"：文件多大、收文件花了多久、处理多久、命中多少。
    用户提过穿透线路有流量限制，所以这些数字必须摆在页面上，不能只躺在后台。
    """
    stat = stat or {}
    advice = ''
    if ok:
        head = '<h1>填好了</h1><div class="sub">生成好了，往下滑下载</div>'
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

    kb = stat.get('kb') or 0
    recv = stat.get('recv') or 0
    parse = stat.get('parse') or 0
    total = (recv or 0) + (parse or 0) + (elapsed or 0)
    hit, web, none = stat.get('hit') or 0, stat.get('web') or 0, stat.get('none') or 0
    np = stat.get('nprice') or 0
    stats_html = """
<div class="card">
  <label>这次的时间与流量</label>
  <div class="pbar small">
    <div class="pi"><span>文件大小</span><b>%s</b></div>
    <div class="pi"><span>传上来</span><b>%.1fs</b></div>
    <div class="pi"><span>处理</span><b>%.1fs</b></div>
    <div class="pi"><span>合计</span><b>%.1fs</b></div>
  </div>
  <div class="kv"><span>清单行数</span><b>%d 行</b></div>
  <div class="kv"><span>参数：控标库 / 官网 / 没查到</span><b>%d / %d / %d</b></div>
  <div class="kv"><span>价格填了</span><b>%d 行</b></div>
  <div class="hint">「传上来」是服务端收完整个文件的耗时，包含网络传输；
    文件越大、网越慢，这段越长。走内网穿透时这段还会受那条线路的带宽限制。</div>
</div>
""" % (fmt_kb(kb), recv, elapsed or 0, total,
       s.get('nrows') or 0, hit, web, none, np)

    return """
%s
%s
%s
%s
%s
<div class="card">
  <label>处理记录</label>
  <pre>%s</pre>
</div>
<div class="tip">觉得哪里填得不对？点下面这个，把这次的东西直接留给我 ——
参数不准、缺 ★、价格口径不对，都能改规则，下次就对了。</div>
<a class="btn gray" href="/feedback?sid=%s">结果有问题，反馈一下</a>
<a class="btn gray" href="/">再传一份</a>
""" % (head, stats_html, dl, advice, note,
       (log or '(无输出)').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'),
       quote(s['sid']))


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
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _page(self, title, body, code=200, sid='', refresh=None):
        """渲染整页，自动带上顶部身份条（谁在用 / 管理入口 / 退出）。"""
        return self._send(code, page(title, bar(self.user, qk()) + body, sid, refresh))

    def _cookie(self, name):
        """从 Cookie 头里取一个值（标准库没有解析器，手工拆够用）。"""
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == name:
                return v.strip()
        return ''

    def _cookie_hdr(self, sid, days):
        """种会话 cookie。经 HTTPS 访问时加 Secure，免得明文线路把 cookie 泄了。"""
        secure = ((self.headers.get('X-Forwarded-Proto') or '').lower() == 'https'
                  or self.headers.get('X-Forwarded-Scheme', '').lower() == 'https')
        return ('tpkit_s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s'
                % (sid, int(days * 86400), '; Secure' if secure else ''))

    def _auth_ok(self):
        """解析身份 → self.user。

        身份来自登录会话（cookie tpkit_s），而会话只能通过 CRM 账号密码验证拿到。
        老版本的 `?k=` 链接**不再有任何作用** —— 链接是能转发的，转发就等于共享账号。
        """
        self.user = None
        _TLS.token = ''
        _TLS.host = self.headers.get('Host') or ''
        _TLS.user = None
        if not REQUIRE_LOGIN:
            self.user = {'id': '', 'name': '本机调试', 'role': 'admin',
                         'token': '', 'active': True, 'email': '', 'pwd': ''}
            _TLS.user = self.user
            return True
        sid = self._cookie('tpkit_s')
        if not sid:
            return False
        rec = WU.by_login(sid)
        if not rec:
            return False
        u = WU.by_id(rec.get('uid') or '')
        if not u or not u.get('active', True):
            return False
        self.user = u
        self._sid = sid
        _TLS.user = u
        return True

    def _to_login(self):
        """没登录 → 302 到登录页，带上原本要去的地址。

        POST 不读请求体直接回，所以必须关连接，否则下一个请求会读到半个包。
        """
        nxt = self.path if self.command == 'GET' else '/'
        self.close_connection = True
        self._send(302, b'', 'text/plain',
                   {'Location': '/login?next=' + quote(nxt, safe='')})

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path

        if path == '/login':
            # 已经登录了就别再显示登录页（也不能跳回 /login，那会死循环）
            if self._auth_ok():
                return self._send(302, b'', 'text/plain',
                                  {'Location': _safe_next((parse_qs(u.query).get('next')
                                                           or ['/'])[0])})
            return self._login_page(parse_qs(u.query))

        if path == '/logout':
            sid = self._cookie('tpkit_s')
            if sid:
                rec = WU.by_login(sid)
                if rec:
                    me = WU.by_id(rec.get('uid') or '') or {}
                    WU.log(me.get('id'), me.get('name'), 'logout',
                           ip=client_ip(self))
                WU.drop_login(sid)
            return self._send(302, b'', 'text/plain',
                              {'Location': '/login',
                               'Set-Cookie': 'tpkit_s=; Path=/; Max-Age=0; HttpOnly'})

        if not self._auth_ok():
            return self._to_login()

        if path == '/':
            return self._page('清单填参数', upload_body('', qk()))

        if path == '/one':
            return self._page('查单个型号', one_body('', qk()))

        if path == '/me':
            return self._send(200, page('我的', me_body(self.user, qk())))

        if path == '/feedback':
            return self._feedback_page(parse_qs(u.query))

        if path == '/admin':
            if (self.user or {}).get('role') != 'admin':
                return self._send(403, page('没有权限', err_body(
                    '这一页只有维护者能看。要算清单回首页就行。')))
            return self._admin_page()

        if path == '/admin/cred':
            if (self.user or {}).get('role') != 'admin':
                return self._send(403, page('没有权限', err_body(
                    '这一页只有维护者能看。')))
            return self._admin_cred(parse_qs(u.query))

        if path == '/admin/fb':
            if (self.user or {}).get('role') != 'admin':
                return self._send(403, page('没有权限', err_body('只有维护者能看。')))
            return self._admin_fb(parse_qs(u.query))

        if path == '/admin/fb/file':
            if (self.user or {}).get('role') != 'admin':
                return self._send(403, page('没有权限', err_body('只有维护者能取。')))
            return self._admin_fb_file(parse_qs(u.query))

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
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_UPLOAD + 2 * 1024 * 1024:
            return self._send(413, page('文件太大',
                                        err_body('文件超过 20 MB，请精简后再传。')))

        # 登录必须在鉴权之前，否则登录页自己都进不去
        if u.path == '/login':
            t0 = time.time()
            body = self.rfile.read(n)
            self._recv_secs = time.time() - t0
            return self._on_login(body)

        if not self._auth_ok():
            return self._to_login()

        # 收请求体的耗时 = "文件传上来花了多久"（服务端视角，含网络传输）
        t0 = time.time()
        body = self.rfile.read(n)
        self._recv_secs = time.time() - t0
        self._body_len = n

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
        if u.path == '/feedback':
            return self._on_feedback(body)
        if u.path == '/admin/new':
            return self._on_admin_new(body)
        if u.path == '/admin/toggle':
            return self._on_admin_toggle(body)
        if u.path == '/admin/fb/done':
            return self._on_fb_done(body)
        return self._send(404, page('404', err_body('页面不存在')))

    # ---- 登录 ----
    def _login_page(self, f, err='', email=''):
        nxt = _safe_next((f.get('next') or ['/'])[0])
        return self._send(200, page('登录', login_body(email, err, nxt)))

    def _on_login(self, body):
        """用 CRM 账号密码登录。真的去 CRM 登一次，登不上就不放行。"""
        f = parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True)

        def g(k):
            v = f.get(k)
            return (v[0] if v else '') or ''

        email = g('email').strip().lower()
        pwd = g('pwd')                       # 密码原样，不 strip（前后空格可能是密码的一部分）
        keep = g('keep') == '1'
        nxt = _safe_next(g('next'))
        if not email or not pwd:
            return self._login_page(f, '邮箱和密码都要填。', email)

        ip = client_ip(self)
        # 两个键一起记：按 IP 防同一台机器乱试，按邮箱防换 IP 绕过
        # （X-Forwarded-For 是客户端能伪造的，只按 IP 限速等于没限）
        keys = [ip + '|' + email, '*|' + email]
        for k in keys:
            locked, left = _fail_state(k)
            if locked:
                return self._login_page(
                    f, '这个账号连续输错太多次，%d 分钟后再试。' % max(1, left // 60), email)

        u = WU.by_email(email)
        stored = WU.creds(u)[1] if u else ''
        if stored and pwd == stored:
            # 本地比对通过就不再去敲 CRM —— 多个账号同一出口 IP 频繁登录，
            # 容易被 CRM 当成异常。代价：他在 CRM 里改过密码后，这里还能用旧的登。
            ok, name, office, err = True, '', '', ''
        else:
            ok, name, office, err = crm_login(email, pwd)
        if not ok:
            for k in keys:
                _fail_add(k)
            WU.log((u or {}).get('id'), (u or {}).get('name'), 'login', ok=0,
                   ip=ip, note='登录失败：%s' % (err or '')[:80])
            return self._login_page(f, err or '账号或密码不对', email)

        for k in keys:
            _fail_clear(k)
        if not u:
            u = WU.ensure(email, name)
        elif name and not (u.get('name') or '').strip():
            WU.update(u['id'], name=name)
            u = WU.by_id(u['id'])
        u = WU.by_id(u.get('id')) or u
        if not u.get('active', True):
            return self._login_page(f, '这个账号被停用了，找维护的人开一下。', email)

        # 密码必须留下：算价格时服务端要拿它去登录 CRM。
        if pwd != stored:
            WU.set_creds(u['id'], email, pwd, True)
        days = (LOGIN_TTL if keep else LOGIN_TTL_SHORT) / 86400.0
        sid = WU.new_login(u['id'], days=days, ip=ip,
                           ua=self.headers.get('User-Agent') or '')
        WU.log(u['id'], u.get('name'), 'login', ip=ip,
               note=(office or '')[:40])
        return self._send(302, b'', 'text/plain',
                          {'Location': nxt,
                           'Set-Cookie': self._cookie_hdr(sid, days)})

    # ---- 我的 / 管理 ----
    def _admin_page(self):
        users = WU.load()['users']
        return self._send(200, page('使用情况', admin_body(
            self.user, qk(), users, WU.summary(), WU.recent(40),
            host=self.headers.get('Host') or '',
            fbs=WU.list_feedback(15), fb_open=WU.pending_feedback())))

    def _admin_cred(self, f):
        """看某人的 CRM 凭据。forget=1 就清掉他的密码。

        默认**只允许在本机/内网看**：这是整套系统里唯一会把明文密码吐出来的页面，
        而它一旦经公网可达，就等于"只要攻破一个管理员会话，所有人密码全拿到"。
        确实需要远程看，启动时加 --allow-cred-remote。
        """
        if ADMIN_CRED_LOCAL_ONLY and is_external(self):
            return self._send(403, page('这一页只能在局域网看', err_body(
                '查看明文密码这一页，默认禁止从外网打开。<br>'
                '在本机上打开 http://127.0.0.1:8000/admin 就能看；'
                '确实要远程看，启动时加 <span class="mono">--allow-cred-remote</span>。')))
        uid = (f.get('uid') or [''])[0]
        info = WU.reveal(uid)
        if not info.get('id'):
            return self._send(404, page('没这人', err_body('用户不存在，或者刚被删了。')))
        me = self.user or {}
        ip = client_ip(self)
        if (f.get('forget') or [''])[0] == '1':
            WU.update(uid, pwd='')
            WU.log(me.get('id'), me.get('name'), 'viewcred',
                   note='清掉 %s 的密码' % (info.get('name') or uid), ip=ip)
            return self._send(302, b'', 'text/plain',
                              {'Location': '/admin/cred?uid=' + quote(uid)})
        WU.log(me.get('id'), me.get('name'), 'viewcred',
               note='看了 %s 的凭据' % (info.get('name') or uid), ip=ip)
        return self._send(200, page('CRM 凭据', cred_body(self.user, qk(), info)))

    def _on_me_save(self, body):
        """改记住的密码。密码留空 = 只改邮箱/不动密码。"""
        f = parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True)

        def g(k):
            v = f.get(k)
            return (v[0] if v else '') or ''

        email, pwd = g('email').strip().lower(), g('pwd')
        if not email:
            return self._send(400, page('我的', me_body(
                self.user, qk(), '<div class="err">CRM 邮箱不能空。</div>')))
        uid = (self.user or {}).get('id') or ''
        if not uid:                     # --no-pass 调试模式没有用户记录
            return self._send(400, page('我的', me_body(
                self.user, qk(), '<div class="err">当前是免登录调试模式，不存账号。</div>')))
        # 换成别人的邮箱 = 冒用，拦住；只允许改自己和没被占用的邮箱
        other = WU.by_email(email)
        if other and other.get('id') != uid:
            return self._send(400, page('我的', me_body(
                self.user, qk(), '<div class="err">这个邮箱已经是另一个账号了。</div>')))
        if pwd:
            ok, name, office, err = crm_login(email, pwd)
            if ok:
                WU.set_creds(uid, email, pwd, True)
                msg = '<div class="ok">已保存，新密码也验证过能登录 CRM。</div>'
            else:
                # 验证不过不落盘：否则他下次算价会一直失败却不知道为什么
                WU.update(uid, email=email)
                msg = ('<div class="err">CRM 没认这个密码（%s），所以没存。'
                       '密码确认对了再填一次。</div>' % esc(err))
        else:
            WU.update(uid, email=email)
            msg = '<div class="ok">只更新了邮箱，密码没动。</div>'
        WU.touch(WU.by_id(uid) or {})
        WU.log(uid, (self.user or {}).get('name'), 'bind', ok=1,
               ip=client_ip(self))
        self.user = WU.by_id(uid) or self.user
        return self._send(200, page('我的', me_body(self.user, qk(), msg)))

    def _on_admin_new(self, body):
        if (self.user or {}).get('role') != 'admin':
            return self._send(403, page('没有权限', err_body('只有维护者能加人。')))
        f = parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True)

        def g(k):
            v = f.get(k)
            return (v[0] if v else '') or ''

        name, email = g('name').strip(), g('email').strip().lower()
        if not (name and email):
            return self._admin_page()
        if WU.by_email(email):
            msg = '<div class="warn">这个邮箱已经建过档了，不用再加。</div>'
        else:
            WU.add(name, role='user', email=email)
            msg = ('<div class="ok">已建 <b>%s</b>（%s）。他用自己的 CRM 密码登录一次就进来了。</div>'
                   % (esc(name), esc(email)))
        WU.log((self.user or {}).get('id'), (self.user or {}).get('name'),
               'admin', ok=1, ip=client_ip(self))
        return self._send(200, page('使用情况', admin_body(
            self.user, qk(), WU.load()['users'], WU.summary(), WU.recent(40),
            msg=msg, host=self.headers.get('Host') or '',
            fbs=WU.list_feedback(15), fb_open=WU.pending_feedback())))

    def _on_admin_toggle(self, body):
        if (self.user or {}).get('role') != 'admin':
            return self._send(403, page('没有权限', err_body('只有维护者能改。')))
        f = parse_qs(body.decode('utf-8', 'replace'))
        uid = ((f.get('uid') or [''])[0] or '').strip()
        u = WU.by_id(uid)
        if u and u.get('id') != (self.user or {}).get('id'):
            WU.update(uid, active=not u.get('active', True))
            if not u.get('active', True):      # 刚被停用 → 立刻踢下线
                WU.drop_logins_of(uid)
        return self._admin_page()

    # ---- 反馈 ----
    def _feedback_page(self, f):
        sid = (f.get('sid') or [''])[0]
        ctx = (f.get('ctx') or [''])[0]
        s = get_session(sid) if sid else None
        if s:
            return self._send(200, page('反馈', feedback_body(
                self.user, sid, ctx=ctx, fname=s.get('fname'),
                kb=s.get('kb') or 0, rows=s.get('nrows') or 0)))
        return self._send(200, page('反馈', feedback_body(
            self.user, '', ctx=ctx)))

    def _on_feedback(self, body):
        """用户点「结果有问题」→ 留一条反馈，连清单副本一起。"""
        f = parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True)

        def g(k):
            v = f.get(k)
            return (v[0] if v else '') or ''

        sid, ctx, note = g('sid'), g('ctx'), g('note').strip()
        if not note:
            s = get_session(sid) if sid else None
            return self._send(400, page('反馈', feedback_body(
                self.user, sid, ctx=ctx, fname=(s or {}).get('fname'),
                kb=(s or {}).get('kb') or 0, rows=(s or {}).get('nrows') or 0,
                msg='<div class="err">写一句哪里不对吧，不然我不知道要改什么。</div>')))
        s = get_session(sid) if sid else None
        me = self.user or {}
        fid = WU.add_feedback(
            me.get('id'), me.get('name'), note, sid=sid,
            fname=(s or {}).get('fname') or '',
            kb=(s or {}).get('kb') or 0,
            rows=(s or {}).get('nrows') or 0,
            models=(s or {}).get('models') or 0,
            ok=1 if (s or {}).get('last_ok', 1) else 0,
            log_txt=(s or {}).get('last_log') or '',
            report=(s or {}).get('last_report') or ctx,
            src_path=(s or {}).get('src') or '')
        WU.log(me.get('id'), me.get('name'), 'feedback', ip=client_ip(self),
               note='#' + str(fid))
        if not fid:
            # 存不进去必须说出来。静默返回"收到了"是最坑的一种假成功。
            return self._send(500, page('没存上', """
<h1>没存上</h1>
<div class="err">这条反馈没写进数据库（可能是文件被占用或者正在重启）。<br>
你写的内容还在这页上，先别关：<b>%s</b></div>
<div class="hint">复制上面这段话，直接微信发给维护的人，比等我看后台快。</div>
<a class="btn gray" href="/">回首页</a>
""" % esc(note[:1200])))
        return self._send(200, page('反馈已提交', """
<h1>收到了</h1>
<div class="sub">编号 #%d</div>
<div class="card">
  <div class="ok">这条反馈连这次的清单副本、运行记录一起留在了后台，
    我会照着改规则。改完下次同样的清单就不会再犯。</div>
  <div class="hint">如果这次是急用（马上要交报价），直接微信喊我一声，比等我看后台快。</div>
</div>
<a class="btn gray" href="/">回首页</a>
""" % fid))

    def _admin_fb(self, f):
        fid = (f.get('id') or ['0'])[0]
        d = WU.get_feedback(fid or 0)
        if not d:
            return self._send(404, page('没有这条', err_body('反馈不存在。')))
        rep = d.get('report') or ''
        try:                               # 台账是 JSON，摊成人能读的行
            arr = json.loads(rep) if rep.strip().startswith('[') else []
        except Exception:
            arr = []
        rows = []
        for e in arr[:300]:
            if not isinstance(e, dict):
                continue
            rows.append('<tr><td class="num">%s</td><td class="mono">%s</td>'
                        '<td>%s</td><td>%s</td></tr>'
                        % (e.get('row') or '', esc(str(e.get('model') or '')),
                           esc(str(e.get('kind') or '')),
                           esc(str(e.get('source') or e.get('note') or e.get('final') or ''))))
        body = """
<h1>反馈 #%s</h1>
<div class="sub">%s ・ %s</div>
<div class="card">
  <div class="kv"><span>清单</span><b class="mono">%s</b></div>
  <div class="kv"><span>文件大小</span><b>%s</b></div>
  <div class="kv"><span>行数 / 型号</span><b>%d / %d</b></div>
  <div class="kv"><span>状态</span><b>%s</b></div>
  %s
</div>
<div class="card">
  <label>他写的说明</label>
  <div style="white-space:pre-wrap;line-height:1.8">%s</div>
</div>
%s
%s
<a class="btn gray" href="/admin">← 回使用情况</a>
""" % (d['id'], esc(d.get('name') or ''), esc(d.get('ts') or ''),
       esc(d.get('fname') or '—'), fmt_kb(d.get('kb')),
       d.get('rows') or 0, d.get('models') or 0,
       '已处理' if d.get('done') else '待看',
       ('<div class="kv"><span>原件</span><b><a href="/admin/fb/file?id=%s">下载他上传的那份</a></b></div>'
        % d['id']) if d.get('file') else
       '<div class="hint">这次没留原件（可能是查型号的反馈，或者文件已被清理）。</div>',
       esc(d.get('note') or ''),
       ('<div class="card"><label>每行台账</label><div style="overflow-x:auto">'
        '<table class="tb"><tr><th class="num">行</th><th>型号</th><th>类型</th>'
        '<th>来源/结果</th></tr>%s</table></div></div>' % ''.join(rows)) if rows else '',
       ('<div class="card"><label>运行日志</label><pre>%s</pre></div>'
        % esc(d.get('log') or '(无)')) if d.get('log') else
       ('<div class="card"><label>他填的内容</label><pre>%s</pre></div>'
        % esc(rep[:4000])) if rep and not arr else '')
        if d.get('done'):
            body += ('<form method="post" action="/admin/fb/done"><input type="hidden"'
                     ' name="id" value="%s"><input type="hidden" name="done" value="0">'
                     '<button class="btn gray" type="submit">标记为「待看」</button></form>'
                     % d['id'])
        else:
            body += ('<form method="post" action="/admin/fb/done"><input type="hidden"'
                     ' name="id" value="%s"><input type="hidden" name="done" value="1">'
                     '<button class="btn" type="submit">标记已处理</button></form>'
                     % d['id'])
        return self._send(200, page('反馈 #%s' % d['id'], bar(self.user, qk()) + body))

    def _admin_fb_file(self, f):
        fid = (f.get('id') or ['0'])[0]
        d = WU.get_feedback(fid or 0)
        p = (d or {}).get('file') or ''
        if not p or not os.path.exists(p):
            return self._send(404, page('文件没了', err_body('这份原件已经被删了。')))
        with open(p, 'rb') as fh:
            data = fh.read()
        name = '%s_%s' % (d['id'], os.path.basename(p).split('_', 1)[-1])
        return self._send(200, data,
                          'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                          {'Content-Disposition':
                           "attachment; filename*=UTF-8''%s" % quote(name)})

    def _on_fb_done(self, body):
        if (self.user or {}).get('role') != 'admin':
            return self._send(403, page('没有权限', err_body('只有维护者能改。')))
        f = parse_qs(body.decode('utf-8', 'replace'))
        fid = ((f.get('id') or ['0'])[0] or '0').strip()
        done = ((f.get('done') or ['1'])[0] or '1') == '1'
        WU.mark_feedback(fid, 1 if done else 0)
        return self._send(302, b'', 'text/plain',
                          {'Location': '/admin/fb?id=' + quote(fid)})

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
                    '<div class="err">这次要查价格，但这台机器上没有你的 CRM 密码。'
                    '在下面填一次（会真的去 CRM 验一遍），以后就不用再输。</div>')))

        t0 = time.time()
        d, err = run_one_query(models, with_price, creds)
        if err:
            return self._send(400, page('查不了', one_body(
                '<div class="err">%s</div>' % esc(err), qk(), raw, with_price)))
        html = one_result_body(d, qk(), raw, with_price)
        html += ('<a class="btn gray" href="/feedback?ctx=%s">结果有问题，反馈一下</a>'
                 % quote('查型号：' + raw[:600]))
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
               ip=client_ip(self))
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
        s['kb'] = len(data) / 1024.0
        s['recv_secs'] = float(getattr(self, '_recv_secs', 0) or 0)
        t_parse = time.time()

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
                 parse_secs=time.time() - t_parse,
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
               rows=s.get('nrows') or 0, ok=1, ip=client_ip(self),
               up_secs=s.get('recv_secs') or 0,
               parse_secs=s.get('parse_secs') or 0,
               kb=int(len(data) / 1024))
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
                    '<div class="err">这次要算价格，但这台机器上没有你的 CRM 密码。'
                    '在下面填一次（会真的去 CRM 验一遍），以后就不用再输。</div>')))

        t0 = time.time()
        ok, log, report = run_fill(s, hr, mc, pc, kc, wp,
                                   s['pricing_p_rate'], 'price',
                                   s['pricing_c_rate'], 'price', creds)
        secs = time.time() - t0

        # 统计：只记数字（几行、几命中），不记型号原文
        hit = web = none = 0
        nprice = 0
        seen = set()
        for e in report or []:
            if e.get('kind') == '价格':
                if e.get('final'):
                    nprice += 1
                continue
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
        # 留着给「反馈」用：用户点反馈时把这次的日志和台账一起带上
        s['last_log'] = log or ''
        s['last_ok'] = 1 if ok else 0
        s['models'] = len(seen)
        try:
            s['last_report'] = json.dumps(report or [], ensure_ascii=False)
        except Exception:
            s['last_report'] = ''
        WU.touch(self.user)
        WU.log((self.user or {}).get('id'), (self.user or {}).get('name'), 'run',
               rows=s.get('nrows') or 0, secs=secs, ok=1 if ok else 0,
               kb_hit=hit, kb_web=web, kb_none=none, ip=client_ip(self),
               up_secs=s.get('recv_secs') or 0,
               parse_secs=s.get('parse_secs') or 0,
               kb=int((s.get('kb') or 0)))
        stat = {'kb': s.get('kb') or 0, 'recv': s.get('recv_secs') or 0,
                'parse': s.get('parse_secs') or 0, 'hit': hit, 'web': web,
                'none': none, 'nprice': nprice}
        return self._page('结果', result_body(s, ok, log, secs, report, stat))


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
    global LOG_FILE, REQUIRE_LOGIN, ADMIN_CRED_LOCAL_ONLY
    # 行缓冲：双击运行时本来就即时显示，但输出被重定向到文件/管道时
    # 默认是块缓冲，启动横幅会「消失」到进程退出才出现，排查时很误导
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description='弱电清单报价 · 网页版')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--no-pass', action='store_true',
                    help='跳过登录（只在自己电脑上调试时用，别对外开）')
    ap.add_argument('--pass', dest='pwd', default='',
                    help='已废弃：现在用 CRM 账号密码登录，这个参数不再生效')
    ap.add_argument('--allow-cred-remote', action='store_true',
                    help='允许从外网打开「查看 CRM 凭据」（默认只允许本机/内网）')
    ap.add_argument('--log', default='', help='日志文件，默认写到用户目录')
    a = ap.parse_args()

    if a.pwd:
        print('⚠ --pass 已经不用了：现在必须用 CRM 账号密码登录。')
    if a.no_pass:
        REQUIRE_LOGIN = False
        print('⚠ --no-pass：谁都能进，不记录身份。只在本机调试时这样开。')
    else:
        REQUIRE_LOGIN = True
        try:
            if WU.mem_level() == 'weak':
                print('⚠ 这台机调不到 Windows DPAPI，密码只能弱加密保存。')
            # 维护者账号：用配置文件里的 CRM 邮箱当管理员。
            # 没有就自动建一个 name=维护者，第一次登录时按邮箱认领。
            _em, _pw = _cfg_creds()
            if _em:
                _u = WU.by_email(_em)
                if not _u:
                    _u = WU.add('维护者', role='admin', email=_em, pwd=_pw)
                elif _u.get('role') != 'admin' or (_pw and not _u.get('pwd')):
                    WU.update(_u['id'], role='admin')
                    if _pw and not _u.get('pwd'):
                        WU.set_creds(_u['id'], _em, _pw, True)
        except Exception as _e:
            print('⚠ 用户表初始化失败（不影响登录，只是没预设管理员）：%s' % _e)
    ADMIN_CRED_LOCAL_ONLY = not a.allow_cred_remote

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
    print('=' * 60)
    print('清单填参数 · 网页版已启动')
    print('=' * 60)
    if a.host in ('0.0.0.0', ''):
        for ip in local_ips():
            kind = _ip_kind(ip)
            print('  http://%s:%d/  %s' % (ip, a.port, kind))
        print('  ↑ 同事用哪个地址，取决于组网/穿透给这台机分配的地址')
    else:
        print('  本机打开：http://%s:%d/' % (a.host, a.port))
    if REQUIRE_LOGIN:
        print('  登录方式：打开上面的地址 → 填自己的 CRM 邮箱 + 密码')
        print('  维护者的管理页：http://127.0.0.1:%d/admin（用你自己的账号登进去）' % a.port)
        print('  查看明文密码只允许本机/内网打开%s'
              % ('' if ADMIN_CRED_LOCAL_ONLY else '（已放开：--allow-cred-remote）'))
        print('  外网映射（NATAPP / 花生壳）见 外网访问-内网穿透.md')
    else:
        print('  ⚠ 免登录模式：能访问到这个地址的人都能用，不记录身份。')
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
