#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页版 · 多用户（每人用自己的 CRM 账号）+ 使用统计

改之前先读这四条，都是踩过的设计决定：

1. **身份 = 每人一个随机 token**，就是链接里的 `?k=`。
   不做"用户名 + 密码登录页"——同事打开链接就是身份，少一步就少一堆求助。
   链接由管理员在 /admin 生成，发给人就行。

2. **CRM 密码加密落盘**，用 Windows DPAPI（绑定当前 Windows 账户）。
   换 Windows 账户 / 换机器就解不开 → 只能重新填。这不是"绝对安全"：
   能登录这台 Windows 的人仍能解密，所以别把服务开到公网。

3. **使用记录进 sqlite**（usage.db），只记「谁、什么时候、跑了几行、耗时、命中情况」，
   **不记型号原文、不记文件名**。管理页能看汇总，能导 CSV。

4. 所有文件都在 `%USERPROFILE%\\.workbuddy\\tplink-toolkit\\`，不进仓库；
   `install.py` 重装技能不会删这里（技能目录会被 rmtree，这个目录不会）。
"""

import base64
import csv
import io
import json
import os
import secrets
import sqlite3
import time

STATE_DIR = os.path.join(os.path.expanduser('~'), '.workbuddy', 'tplink-toolkit')
USERS_FILE = os.path.join(STATE_DIR, 'users.json')
DB_FILE = os.path.join(STATE_DIR, 'usage.db')

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


def set_creds(uid, email, pwd, remember=True):
    """写 CRM 账号。remember=False 时只写邮箱、不存密码（下次还要输）。"""
    return update(uid, email=(email or '').strip(),
                  pwd=enc(pwd) if (pwd and remember) else '')


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
        kb_none integer default 0, ip text default '', note text default '')""")
    # 老库补列（note 是后加的，用来记"看了谁的凭据"这种少量文字）
    try:
        cols = [r[1] for r in c.execute('pragma table_info(usage)').fetchall()]
        if 'note' not in cols:
            c.execute("alter table usage add column note text default ''")
            c.commit()
    except Exception:
        pass
    return c


def log(uid, name, kind, rows=0, models=0, secs=0.0, ok=1,
        kb_hit=0, kb_web=0, kb_none=0, ip='', note=''):
    """记一条使用。kind: upload / run / one / bind / viewcred / admin。

    **清单/型号原文一律不记**，只记数字。唯一的自由文本是 note，
    目前只用来记"谁查看了谁的 CRM 凭据"（审计用）。
    """
    try:
        c = _conn()
        try:
            c.execute('insert into usage(ts,uid,name,kind,rows,models,secs,ok,'
                      'kb_hit,kb_web,kb_none,ip,note) values(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (time.strftime('%Y-%m-%d %H:%M:%S'), uid or '', name or '',
                       kind, int(rows or 0), int(models or 0), float(secs or 0),
                       int(1 if ok else 0), int(kb_hit or 0), int(kb_web or 0),
                       int(kb_none or 0), ip or '', note or ''))
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
                                   max(ts) last, avg(secs) secs
                            from usage group by uid order by n desc""").fetchall()
    finally:
        c.close()
    people = [{'name': r[1] or '(未知)', 'n': r[2], 'rows': r[3] or 0,
               'models': r[4] or 0, 'bad': r[5] or 0, 'last': r[6] or '',
               'secs': round(r[7] or 0, 1)} for r in rows]
    total = {'n': sum(p['n'] for p in people),
             'rows': sum(p['rows'] for p in people),
             'models': sum(p['models'] for p in people)}
    return {'people': people, 'total': total}


def recent(limit=40):
    try:
        c = _conn()
    except Exception:
        return []
    try:
        rows = c.execute("""select ts,name,kind,rows,models,secs,ok,kb_hit,kb_web,kb_none,note
                            from usage order by id desc limit ?""", (limit,)).fetchall()
    finally:
        c.close()
    return [{'ts': r[0], 'name': r[1], 'kind': r[2], 'rows': r[3], 'models': r[4],
             'secs': r[5], 'ok': r[6], 'kb_hit': r[7], 'kb_web': r[8],
             'kb_none': r[9], 'note': r[10] or ''} for r in rows]


def csv_text():
    """导全量 CSV（Excel 能直接开，带 BOM）。"""
    try:
        c = _conn()
    except Exception:
        return ''
    try:
        rows = c.execute("""select ts,name,kind,rows,models,secs,ok,kb_hit,kb_web,
                                   kb_none,ip,note from usage order by id""").fetchall()
    finally:
        c.close()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator='\n')
    w.writerow(['时间', '用户', '动作', '清单行数', '型号数', '耗时秒', '成功',
                '控标库命中', '官网兜底', '都没命中', '来源IP', '备注'])
    for r in rows:
        r = list(r)
        r[2] = kind_cn(r[2])          # 动作写中文，Excel 里好看
        w.writerow(['' if v is None else v for v in r])
    return '\ufeff' + buf.getvalue()


def kind_cn(k):
    return {'upload': '上传清单', 'run': '填参数+价格', 'one': '查型号',
            'bind': '绑 CRM 账号', 'admin': '管理页',
            'viewcred': '查看凭据'}.get(k, k or '')
