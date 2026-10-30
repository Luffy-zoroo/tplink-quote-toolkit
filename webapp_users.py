#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页版 · 多用户（每人用自己的 CRM 账号）+ 使用统计

改之前先读这四条，都是踩过的设计决定：

1. **身份 = CRM 账号密码**（2026-10-29 改）。打开网页先登录，填自己的 CRM 邮箱密码，
   服务端真去 CRM 登一次，验过才发会话 cookie。**不填就什么都看不到**。
   以前的 `?k=` 链接即身份已经作废 —— 那条链接谁拿到都能用，等于没有鉴权。
   验证通过后密码会加密存下来（算价必须拿它去登录 CRM），所以这不是"验一次就忘掉"。

2. **CRM 密码加密落盘**，用 Windows DPAPI（绑定当前 Windows 账户）。
   换 Windows 账户 / 换机器就解不开 → 只能重新填。这不是"绝对安全"：
   **能拿到这台机器的这个 Windows 账户、或者攻破服务进程的人，仍能解密全部人的密码**。
   这是"把密码集中放在一台机器"的架构，不是零知识，挂公网前必须想清楚。

3. **使用记录进 sqlite**（usage.db），记「谁、什么时候、跑了几行、耗时、命中情况、
   文件多大、上传收多久」，**不记型号原文、不记文件名**。
   用户主动点「反馈」是唯一的例外：那一次会留清单副本 + 运行台账，页面上写明了才点。

4. 所有文件都在 `%USERPROFILE%\\.workbuddy\\tplink-toolkit\\`，不进仓库；
   `install.py` 重装技能不会删这里（技能目录会被 rmtree，这个目录不会）。
"""

import base64
import csv
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time

STATE_DIR = os.path.join(os.path.expanduser('~'), '.workbuddy', 'tplink-toolkit')
USERS_FILE = os.path.join(STATE_DIR, 'users.json')
DB_FILE = os.path.join(STATE_DIR, 'usage.db')
SESS_FILE = os.path.join(STATE_DIR, 'logins.json')   # 登录会话（可重启不丢）
FB_DIR = os.path.join(STATE_DIR, 'feedback')          # 反馈时留下的清单副本

_L = threading.Lock()

ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'   # 去掉了 l/i/o/0/1，手抄不容易错


# ------------------------------------------------------------------ 加解密
def _dpapi(raw, protect):
    """调 Windows DPAPI。成功返回 bytes；非 Windows 或失败抛异常，由上层降级。"""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(bytes(raw), len(raw))
    src = BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect \
        else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise OSError('DPAPI 调用失败')
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _weak_key():
    """DPAPI 用不了时的兜底密钥：机器名 + Windows 账户。

    只是"不让密码明文躺在 json 里"，挡不住真想看的人 —— mem_level() 会把这种情况标出来。
    """
    seed = (os.environ.get('COMPUTERNAME', '') + '|' +
            os.environ.get('USERNAME', '') + '|tpkit').encode('utf-8')
    return (seed * 40)[:64]


def _xor(data, key):
    k = (key * (len(data) // len(key) + 1))[:len(data)]
    return bytes(a ^ b for a, b in zip(data, k))


def enc(plain):
    """明文 → 存盘字符串。格式：dpapi:<b64> / weak:<b64>（弱加密，会在页面上标）。"""
    raw = plain.encode('utf-8')
    try:
        return 'dpapi:' + base64.b64encode(_dpapi(raw, True)).decode('ascii')
    except Exception:
        key = _weak_key()
        return 'weak:' + base64.b64encode(_xor(raw, key)).decode('ascii')


def dec(stored):
    """存盘字符串 → 明文。解不开返回 ''。"""
    if not stored or not isinstance(stored, str):
        return ''
    tag, _, b64 = stored.partition(':')
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return ''
    try:
        if tag == 'dpapi':
            return _dpapi(raw, False).decode('utf-8', 'replace')
        if tag == 'weak':
            return _xor(raw, _weak_key()).decode('utf-8', 'replace')
    except Exception:
        return ''
    return ''


def mem_level():
    """当前这台机的加密强度：'dpapi' 正常 / 'weak' 降级。"""
    try:
        _dpapi(b'probe', True)
        return 'dpapi'
    except Exception:
        return 'weak'


# ------------------------------------------------------------------ 用户表
def _blank():
    return {'users': [], 'version': 1}


def load():
    try:
        with io.open(USERS_FILE, encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict) or not isinstance(d.get('users'), list):
            return _blank()
        return d
    except Exception:
        return _blank()


def save(d):
    os.makedirs(STATE_DIR, exist_ok=True)
    with io.open(USERS_FILE, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write('\n')


def _new_token(taken):
    while True:
        t = ''.join(secrets.choice(ALPHABET) for _ in range(8))
        if t not in taken:
            return t


def add(name, role='user', token=None, email='', pwd=''):
    d = load()
    taken = {u.get('token') for u in d['users']}
    t = token or _new_token(taken)
    u = {
        'id': 'u%d' % int(time.time() * 1000) + str(len(d['users'])),
        'name': (name or '').strip() or '未命名',
        'token': t,
        'role': role,
        'active': True,
        'email': (email or '').strip(),
        'pwd': enc(pwd) if pwd else '',
        'created': time.strftime('%Y-%m-%d %H:%M'),
        'last': '',
        'runs': 0,
    }
    d['users'].append(u)
    save(d)
    return u


def by_token(token):
    if not token:
        return None
    for u in load()['users']:
        if u.get('token') == token:
            return u
    return None


def by_id(uid):
    for u in load()['users']:
        if u.get('id') == uid:
            return u
    return None


def update(uid, **kw):
    d = load()
    hit = None
    for u in d['users']:
        if u.get('id') == uid:
            u.update(kw)
            hit = u
    save(d)
    return hit


def by_email(email):
    """按 CRM 邮箱找人（邮箱就是登录账号，大小写不敏感）。"""
    e = (email or '').strip().lower()
    if not e:
        return None
    for u in load()['users']:
        if (u.get('email') or '').strip().lower() == e:
            return u
    return None


def ensure(email, name='', role='user'):
    """登录成功后建档：邮箱没注册过就建一个，注册过就复用（姓名空着才补）。"""
    u = by_email(email)
    if u:
        if name and not (u.get('name') or '').strip():
            update(u['id'], name=name)
            u = by_id(u['id'])
        return u
    return add(name or (email or '').split('@')[0] or '未命名', role=role,
               email=(email or '').strip())


def set_creds(uid, email, pwd, remember=True):
    """写 CRM 账号。remember=False 时只写邮箱、不存密码（下次还要输）。"""
    return update(uid, email=(email or '').strip(),
                  pwd=enc(pwd) if (pwd and remember) else '')


# ------------------------------------------------------------------ 登录会话
def _load_sess():
    try:
        with io.open(SESS_FILE, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_sess(d):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with io.open(SESS_FILE, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(d, f, indent=1, sort_keys=True)
    except Exception:
        pass


def new_login(uid, days=30.0, ip='', ua=''):
    """发一个登录会话。落盘而不是只放内存 —— start.bat 崩一次就重启，
    内存会话会让所有人平白掉线。"""
    with _L:
        d = _load_sess()
        now = time.time()
        d = {k: v for k, v in d.items() if (v.get('exp') or 0) > now}
        sid = secrets.token_urlsafe(24)
        d[sid] = {'uid': uid, 'exp': now + days * 86400,
                  'ip': ip or '', 'ua': (ua or '')[:120],
                  'at': time.strftime('%Y-%m-%d %H:%M')}
        _save_sess(d)
        return sid


def by_login(sid):
    """会话 → 记录。过期就顺手删掉。"""
    if not sid:
        return None
    d = _load_sess()
    v = d.get(sid)
    if not v:
        return None
    if (v.get('exp') or 0) <= time.time():
        with _L:
            d = _load_sess()
            if d.pop(sid, None) is not None:
                _save_sess(d)
        return None
    return v


def drop_login(sid):
    if not sid:
        return
    with _L:
        d = _load_sess()
        if d.pop(sid, None) is not None:
            _save_sess(d)


def drop_logins_of(uid):
    """停用某人时把他的会话全踢掉，不用等他重开浏览器。"""
    with _L:
        d = _load_sess()
        n = len(d)
        d = {k: v for k, v in d.items() if v.get('uid') != uid}
        if len(d) != n:
            _save_sess(d)


def purge_logins():
    """清过期会话（janitor 定时调）。"""
    with _L:
        d = _load_sess()
        now = time.time()
        alive = {k: v for k, v in d.items() if (v.get('exp') or 0) > now}
        if len(alive) != len(d):
            _save_sess(alive)


def creds(u):
    """用户记录 → (email, pwd)。没绑就是 ('', '')。"""
    if not u:
        return '', ''
    return (u.get('email') or '').strip(), dec(u.get('pwd'))


def reveal(uid):
    """管理员查看某人凭据 → {name,email,pwd,has_pwd,level}。

    密码是能解开的（服务进程本来就要解密去登录 CRM），这里只是把结果摊出来。
    只给 /admin 用，调用方负责校验 role 并记审计。
    """
    u = by_id(uid) or {}
    pwd = dec(u.get('pwd'))
    return {
        'id': u.get('id') or '',
        'name': u.get('name') or '',
        'email': (u.get('email') or '').strip(),
        'pwd': pwd,
        'has_pwd': bool(pwd),
        'stored': bool(u.get('pwd')),
        'level': mem_level(),
        'last': u.get('last') or '',
    }


def touch(u):
    """记一次访问：更新 last / runs。失败不影响请求。"""
    if not u:
        return
    try:
        d = load()
        for x in d['users']:
            if x.get('token') == u.get('token'):
                x['last'] = time.strftime('%Y-%m-%d %H:%M')
                x['runs'] = int(x.get('runs') or 0) + 1
        save(d)
    except Exception:
        pass


# ------------------------------------------------------------------ 使用统计
def _conn():
    os.makedirs(STATE_DIR, exist_ok=True)
    c = sqlite3.connect(DB_FILE, timeout=5)
    c.execute("""create table if not exists usage(
        id integer primary key autoincrement,
        ts text not null, uid text, name text, kind text,
        rows integer default 0, models integer default 0,
        secs real default 0, ok integer default 1,
        kb_hit integer default 0, kb_web integer default 0,
        kb_none integer default 0, ip text default '', note text default '',
        up_secs real default 0, parse_secs real default 0, kb integer default 0)""")
    # 老库补列。都走 try：某台机器上库可能正被另一个进程占着，加不上也不能让请求挂掉
    try:
        cols = [r[1] for r in c.execute('pragma table_info(usage)').fetchall()]
        for name, ddl in (('note', "text default ''"),
                          ('up_secs', 'real default 0'),
                          ('parse_secs', 'real default 0'),
                          ('kb', 'integer default 0')):
            if name not in cols:
                c.execute('alter table usage add column %s %s' % (name, ddl))
        c.commit()
    except Exception:
        pass
    return c


def log(uid, name, kind, rows=0, models=0, secs=0.0, ok=1,
        kb_hit=0, kb_web=0, kb_none=0, ip='', note='',
        up_secs=0.0, parse_secs=0.0, kb=0):
    """记一条使用。kind: upload / run / one / login / bind / viewcred / admin / feedback。

    **清单/型号原文一律不记**，只记数字。唯一的自由文本是 note，
    目前只用来记"谁查看了谁的 CRM 凭据"这类审计文字。
    up_secs = 服务端收完请求体的耗时（含网络传输），parse_secs = 解析 Excel 的耗时，
    kb = 上传文件大小。这三项是用户问"一次上传花了多少流量/时间"时要看的。
    """
    try:
        c = _conn()
        try:
            c.execute('insert into usage(ts,uid,name,kind,rows,models,secs,ok,'
                      'kb_hit,kb_web,kb_none,ip,note,up_secs,parse_secs,kb) '
                      'values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (time.strftime('%Y-%m-%d %H:%M:%S'), uid or '', name or '',
                       kind, int(rows or 0), int(models or 0), float(secs or 0),
                       int(1 if ok else 0), int(kb_hit or 0), int(kb_web or 0),
                       int(kb_none or 0), ip or '', note or '',
                       float(up_secs or 0), float(parse_secs or 0), int(kb or 0)))
            c.commit()
        finally:
            c.close()
    except Exception:
        pass


def summary():
    """每人一行汇总 + 全局合计，给 /admin 用。"""
    try:
        c = _conn()
    except Exception:
        return {'people': [], 'total': {}}
    try:
        rows = c.execute("""select uid,name,count(*) n,
                                   sum(case when kind='run' then rows else 0 end) rows,
                                   sum(models) models,
                                   sum(case when ok=0 then 1 else 0 end) bad,
                                   max(ts) last,
                                   avg(case when kind='run' then secs end) run_s,
                                   avg(case when kind='upload' then up_secs end) up_s,
                                   avg(case when kind='upload' then parse_secs end) pa_s,
                                   sum(case when kind='upload' then kb else 0 end) kb
                            from usage group by uid order by n desc""").fetchall()
    finally:
        c.close()
    people = [{'name': r[1] or '(未知)', 'n': r[2], 'rows': r[3] or 0,
               'models': r[4] or 0, 'bad': r[5] or 0, 'last': r[6] or '',
               'run_s': round(r[7] or 0, 1), 'up_s': round(r[8] or 0, 2),
               'pa_s': round(r[9] or 0, 2), 'kb': r[10] or 0} for r in rows]
    total = {'n': sum(p['n'] for p in people),
             'rows': sum(p['rows'] for p in people),
             'models': sum(p['models'] for p in people),
             'kb': sum(p['kb'] for p in people)}
    return {'people': people, 'total': total}


def recent(limit=40):
    try:
        c = _conn()
    except Exception:
        return []
    try:
        rows = c.execute("""select ts,name,kind,rows,models,secs,ok,kb_hit,kb_web,
                                   kb_none,note,up_secs,parse_secs,kb
                            from usage order by id desc limit ?""", (limit,)).fetchall()
    finally:
        c.close()
    return [{'ts': r[0], 'name': r[1], 'kind': r[2], 'rows': r[3], 'models': r[4],
             'secs': r[5], 'ok': r[6], 'kb_hit': r[7], 'kb_web': r[8],
             'kb_none': r[9], 'note': r[10] or '', 'up_secs': r[11] or 0,
             'parse_secs': r[12] or 0, 'kb': r[13] or 0} for r in rows]


def csv_text():
    """导全量 CSV（Excel 能直接开，带 BOM）。"""
    try:
        c = _conn()
    except Exception:
        return ''
    try:
        rows = c.execute("""select ts,name,kind,rows,models,secs,ok,kb_hit,kb_web,
                                   kb_none,ip,note,kb,up_secs,parse_secs
                            from usage order by id""").fetchall()
    finally:
        c.close()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator='\n')
    w.writerow(['时间', '用户', '动作', '清单行数', '型号数', '处理秒', '成功',
                '控标库命中', '官网兜底', '都没命中', '来源IP', '备注',
                '文件KB', '接收秒', '解析秒'])
    for r in rows:
        r = list(r)
        r[2] = kind_cn(r[2])          # 动作写中文，Excel 里好看
        w.writerow(['' if v is None else v for v in r])
    return '\ufeff' + buf.getvalue()


def kind_cn(k):
    return {'upload': '上传清单', 'run': '填参数+价格', 'one': '查型号',
            'bind': '绑 CRM 账号', 'admin': '管理页', 'login': '登录',
            'logout': '退出登录', 'feedback': '提交反馈',
            'viewcred': '查看凭据'}.get(k, k or '')


# ------------------------------------------------------------------ 反馈
def _fb_dir():
    os.makedirs(FB_DIR, exist_ok=True)
    return FB_DIR


def _fb_keep(fid, src_path, fname):
    """把这次上传的原件复制一份留底。返回落盘路径（失败返回 ''）。"""
    try:
        if not (src_path and os.path.exists(src_path)):
            return ''
        ext = os.path.splitext(fname or src_path)[1] or '.xlsx'
        base = re.sub(r'[^\w\u4e00-\u9fa5.-]+', '_',
                      os.path.splitext(fname or 'qingdan')[0])[:60] or 'qingdan'
        dst = os.path.join(_fb_dir(), '%d_%s%s' % (fid, base, ext))
        shutil.copyfile(src_path, dst)
        return dst
    except Exception:
        return ''


def add_feedback(uid, name, note, sid='', fname='', kb=0, rows=0, models=0,
                 ok=1, log_txt='', report='', src_path=''):
    """存一条反馈。src_path 给了就把清单原件复制进 feedback/ 留底。"""
    fid = 0
    try:
        c = _conn()
        try:
            c.execute("""create table if not exists feedback(
                id integer primary key autoincrement,
                ts text not null, uid text, name text, sid text,
                note text, fname text, kb integer default 0,
                rows integer default 0, models integer default 0,
                ok integer default 1, log_txt text, report text,
                file text default '', done integer default 0)""")
            cur = c.execute(
                'insert into feedback(ts,uid,name,sid,note,fname,kb,rows,'
                'models,ok,log_txt,report) values(?,?,?,?,?,?,?,?,?,?,?,?)',
                (time.strftime('%Y-%m-%d %H:%M:%S'), uid or '', name or '',
                 sid or '', note or '', fname or '', int(kb or 0),
                 int(rows or 0), int(models or 0), int(1 if ok else 0),
                 (log_txt or '')[:20000], (report or '')[:60000]))
            # lastrowid 在 cursor 上，不在 connection 上 —— 写错这一步会让整条反馈
            # 静默丢掉（except 吞掉 AttributeError，页面还显示"收到了"）。踩过一次。
            fid = cur.lastrowid
            p = _fb_keep(fid, src_path, fname)
            if p:
                c.execute('update feedback set file=? where id=?', (p, fid))
            c.commit()
        finally:
            c.close()
    except Exception:
        return 0
    return fid


def list_feedback(limit=30, only_open=False):
    try:
        c = _conn()
    except Exception:
        return []
    try:
        sql = """select id,ts,name,note,fname,kb,rows,models,ok,done,file
                 from feedback %s order by id desc limit ?"""
        sql = sql % ('where done=0' if only_open else '')
        rows = c.execute(sql, (limit,)).fetchall()
    except Exception:
        rows = []
    finally:
        c.close()
    return [{'id': r[0], 'ts': r[1], 'name': r[2], 'note': r[3] or '',
             'fname': r[4] or '', 'kb': r[5] or 0, 'rows': r[6] or 0,
             'models': r[7] or 0, 'ok': r[8], 'done': r[9],
             'has_file': bool(r[10])} for r in rows]


def get_feedback(fid):
    try:
        c = _conn()
    except Exception:
        return None
    try:
        r = c.execute("""select id,ts,uid,name,sid,note,fname,kb,rows,models,
                                ok,log_txt,report,file,done
                         from feedback where id=?""", (int(fid),)).fetchone()
    except Exception:
        r = None
    finally:
        c.close()
    if not r:
        return None
    return {'id': r[0], 'ts': r[1], 'uid': r[2], 'name': r[3], 'sid': r[4],
            'note': r[5] or '', 'fname': r[6] or '', 'kb': r[7] or 0,
            'rows': r[8] or 0, 'models': r[9] or 0, 'ok': r[10],
            'log': r[11] or '', 'report': r[12] or '', 'file': r[13] or '',
            'done': r[14]}


def mark_feedback(fid, done=1):
    try:
        c = _conn()
        try:
            c.execute('update feedback set done=? where id=?', (int(done), int(fid)))
            c.commit()
        finally:
            c.close()
        return True
    except Exception:
        return False


def pending_feedback():
    try:
        c = _conn()
        try:
            r = c.execute('select count(*) from feedback where done=0').fetchone()
        finally:
            c.close()
        return int(r[0] or 0)
    except Exception:
        return 0
