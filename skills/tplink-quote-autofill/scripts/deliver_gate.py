# -*- coding: utf-8 -*-
"""
交付门禁 / 反馈闭环 —— 把「每段交付必须问用户」从口头规则变成代码行为

为什么需要这个模块
    SKILL.md 里写「交付后记得问用户有没有要改的」，这只是**提示**，模型会跳过去。
    本模块把三件事做成机械约束：

      ① 交付必回执 —— 脚本跑完自己打印「回执块」，里面是**必须原样转述**的问句。
         模型可以不理 SKILL.md，但脚本输出就摆在屏幕上，跳过去等于当众无视。
      ② 未确认不许进下一段 —— 下一段脚本启动先调 require_clear()，
         上一段没确认就 **退出码 3**，直接拒绝干活（不是警告，是阻断）。
      ③ 通用问题必须落成 Issue 素材 —— issue 命令生成标准 Markdown，
         不会用 git 的同事也能复制粘贴给作者，反馈不会烂在对话里。

状态放哪
    ~/.workbuddy/tplink-toolkit/state/flow_state.json   每段的交付/确认状态，按项目分开
    ~/.workbuddy/tplink-toolkit/feedback/               反馈记录（Issue 素材）

命令行
    python deliver_gate.py status                         看当前项目各段状态
    python deliver_gate.py clear params --verdict ok      确认该段（ok / changed / skip）
    python deliver_gate.py issue --stage params \
        --title "摄像机 F 系漏了 W6 型号" --detail "..."    生成 Issue 素材
    python deliver_gate.py feedback                       列出所有没提交的反馈
    python deliver_gate.py reset --project 某清单         清空某个项目（换新单时用）

设计取舍
    - 门禁**只挡顺序**，不挡人。真想跳过：`clear <stage> --verdict skip`，留痕不追问。
    - 模块缺失时调用方必须降级（只打回执、不做阻断），不能因为门禁坏了就不干活。
    - 状态文件坏了不抛异常，按空状态处理 —— 门禁不该成为新的故障点。
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "tplink-toolkit")
STATE_FILE = os.path.join(BASE_DIR, "state", "flow_state.json")
FEEDBACK_DIR = os.path.join(BASE_DIR, "feedback")

# 流程顺序（autofill = 参数+价格一次跑完，排在最后）
# eol（停产核验）单独成段：放在选型之后、参数之前 ——
# 型号是废的，参数和价格填得再对也没用（2026-10-27 用户定调「停产肯定不能填进去」）。
ORDER = ['select', 'eol', 'params', 'price', 'autofill']

LABEL = {
    'select':   '选型',
    'eol':      '停产核验',
    'params':   '参数列',
    'price':    '价格列',
    'autofill': '参数 + 价格',
}

# 每段必须问的话（原话，不许模型自己改写）
QUESTIONS = {
    'select': [
        '这版选型有没有要调整的？型号、数量、档次都可以改。',
        '  · 该用渠道机 / 项目机 / 域联机，有没有搞错档次',
        '  · 数量按清单原样，还是按系统实际点位数',
    ],
    'eol': [
        '停产核验这一版，有没有要处理的？',
        '  · 已停产（HALT）的型号换成哪个替代款 —— 这个必须你定，我不替你选',
        '  · 停产预警（HALT_WARNING）的要不要提前换掉（项目周期长更要换）',
        '  · 有没有 CRM 里查不到、要人工确认真死没死的型号',
    ],
    'params': [
        '参数这版有没有要改的？',
        '  · 哪几条写得不对或不够（带机量 / 转发性能 / 端口规格 / 容量）',
        '  · 哪几条该有 ★ 加分项却没有',
        '  · 有没有该写没写、或不该写却写了的',
    ],
    'price': [
        '价格口径对不对？',
        '  · 折扣档（渠道 / 项目 / 域联）对不对',
        '  · 向上取整到 0/2/5，这个规则要不要调',
        '  · License 年限、配件（电源/模块/授权）是否都算进去了',
    ],
    'autofill': [
        '参数这版有没有要改的？（下面几条逐条过）',
        '  · 哪几条写得不对或不够（带机量 / 转发性能 / 端口规格 / 容量）',
        '  · 哪几条该有 ★ 加分项却没有',
        '价格口径对不对？',
        '  · 折扣档（渠道 / 项目 / 域联）对不对',
        '  · License 年限、配件（电源/模块/授权）是否都算进去了',
    ],
}

# 反馈建议改哪里（帮作者快速定位，也让提反馈的人知道该看哪节）
WHERE = {
    'select':   'SKILL.md「🧭 选型阶段」/「选型取数」',
    'eol':      'SKILL.md「🚨 停产核验」',
    'params':   'SKILL.md「📝 参数来源与写法」/「品牌词中性化」',
    'price':    'SKILL.md「💴 定价默认策略」',
    'autofill': 'SKILL.md「📝 参数来源与写法」或「💴 定价默认策略」',
}


# ---------------------------------------------------------------- 状态读写

def _ensure_dirs():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    os.makedirs(FEEDBACK_DIR, exist_ok=True)


def _load():
    """读状态。文件不存在 / 坏了 / 格式不对 → 一律当空状态，不抛异常。"""
    try:
        with io.open(STATE_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault('projects', {})
            d.setdefault('feedback', [])
            return d
    except Exception:
        pass
    return {'projects': {}, 'feedback': []}


def _save(d):
    _ensure_dirs()
    tmp = STATE_FILE + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def project_key(project=''):
    """项目标识。**两种传法必须得到同一个 key**，否则门禁形同虚设。

    传完整路径（autofill_quote.py 的 --xlsx）和传文件名（pick.py 的 --project）
    都可能出现，所以一律：取 basename → 去掉表格扩展名。

    ⚠ 曾经的坑：只在"含路径分隔符"时才取 basename，
      导致 `D:\\x\\某清单.xlsx` → "某清单"，而 `某清单.xlsx` → "某清单.xlsx"，
      两个 key 各记各的，门禁永远不触发。
    """
    s = str(project or '').strip()
    if not s:
        return 'default'
    s = os.path.basename(s.replace('\\', '/')).strip()
    stem, ext = os.path.splitext(s)
    if ext.lower() in ('.xlsx', '.xls', '.xlsm', '.csv', '.xltx'):
        s = stem
    return s[:80] or 'default'


def _proj(state, project):
    key = project_key(project)
    return state['projects'].setdefault(key, {'stages': {}, 'updated': ''})


# ---------------------------------------------------------------- 核心动作

def deliver(stage, project='', rows=0, note='', quiet=False):
    """登记一次交付，并打印回执块。

    调用方（autofill_quote.py / pick.py）在保存完文件、打完正常汇报后调用。
    返回该段的问句列表，便于调用方继续用。
    """
    if stage not in LABEL:
        return []
    state = _load()
    p = _proj(state, project)
    p['stages'][stage] = {
        'at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'rows': int(rows or 0),
        'note': note,
        'cleared': None,
        'verdict': None,
    }
    p['updated'] = p['stages'][stage]['at']
    _save(state)

    if not quiet:
        _print_receipt(stage, project_key(project), rows)
    return QUESTIONS[stage]


def _print_receipt(stage, key, rows):
    qs = QUESTIONS[stage]
    bar = '━' * 64
    print()
    print(bar)
    print('🔁 交付回执 · 这一段必须问用户，不许直接往下走')
    print(bar)
    print('本段：%s%s' % (LABEL[stage], ('（%d 行）' % rows) if rows else ''))
    print('项目：%s' % key)
    print()
    print('【把下面这段原样发给用户，不要概括、不要省略】')
    print()
    for q in qs:
        print('  ' + q)
    print()
    print('【用户回答后，按情况执行】')
    print()
    print('  没问题        python deliver_gate.py clear %s --verdict ok' % stage)
    print('  有修改        改完重跑本脚本，再问一次，直到用户说没问题')
    print('  是通用问题    先问「这条要不要写进 skill、提交到 GitHub」；')
    print('                用户同意 → 生成 Issue 素材：')
    print('                python deliver_gate.py issue --stage %s \\' % stage)
    print('                    --title "一句话说清问题" --detail "怎么复现、期望怎样"')
    print()
    print('⚠ 这一段没 clear 之前，下一段脚本会直接拒绝执行（退出码 3）。')
    print(bar)
    print()


def require_clear(stage, project='', quiet=False):
    """进入 stage 前调用。上一段没确认 → stderr 报错并 sys.exit(3)。

    「上一段」= 同项目里 ORDER 中位置在 stage 之前、已交付但没 clear 的最后一段。
    """
    if stage not in ORDER:
        return True
    state = _load()
    key = project_key(project)
    proj = state['projects'].get(key)
    if not proj:
        return True

    stages = proj.get('stages', {})
    idx = ORDER.index(stage)
    pending = None
    for s in reversed(ORDER[:idx]):
        rec = stages.get(s)
        if rec and not rec.get('cleared'):
            pending = (s, rec)
            break
    if not pending:
        return True

    s, rec = pending
    bar = '━' * 64
    msg = [
        '',
        bar,
        '🛑 被门禁挡住：上一段还没确认，不能继续',
        bar,
        '项目：%s' % key,
        '未确认的段：%s（%s 交付，%s 行）'
        % (LABEL[s], rec.get('at', '?'), rec.get('rows') or 0),
        '',
        '先把这个问出去，拿到用户回答再继续：',
        '',
    ]
    for q in QUESTIONS.get(s, []):
        msg.append('  ' + q)
    msg += [
        '',
        '用户回答之后：',
        '  python deliver_gate.py clear %s --verdict ok        用户说没问题' % s,
        '  python deliver_gate.py clear %s --verdict skip      用户表示这段不用看' % s,
        '  （要改就先改，改完重跑上一段脚本，再问一次）',
        '',
        '看全部状态：python deliver_gate.py status',
        bar,
        '',
    ]
    print('\n'.join(msg), file=sys.stderr)
    raise SystemExit(3)


def clear(stage, project='', verdict='ok', note=''):
    """解除某段的待确认状态。verdict: ok / changed / skip。"""
    if stage not in LABEL:
        print('未知阶段: %s（可选：%s）' % (stage, ' / '.join(ORDER)))
        return 2
    state = _load()
    p = _proj(state, project)
    rec = p['stages'].get(stage) or {}
    rec.update({
        'cleared': time.strftime('%Y-%m-%d %H:%M:%S'),
        'verdict': verdict,
    })
    if note:
        rec['clear_note'] = note
    p['stages'][stage] = rec
    p['updated'] = rec['cleared']
    _save(state)

    tip = {
        'ok':      '已确认，可以进下一段。',
        'changed': '已记录「改过」。改完记得重跑对应脚本，再问用户一次。',
        'skip':    '已跳过（留痕，不再追问这一段）。',
    }.get(verdict, '已确认。')
    print('[%s] %s → %s' % (project_key(project), LABEL[stage], tip))

    # 提醒：还有没有别的段悬着
    left = [s for s in ORDER if (p['stages'].get(s) or {}).get('at')
            and not (p['stages'].get(s) or {}).get('cleared')]
    if left:
        print('⚠ 本项目还有未确认的段：%s' % '、'.join(LABEL[x] for x in left))
    return 0


# ---------------------------------------------------------------- 反馈落地

def _slug(text, n=40):
    s = re.sub(r'[\\/:*?"<>|\s]+', '-', str(text or '')).strip('-')
    s = re.sub(r'-+', '-', s)
    return (s[:n] or 'feedback')


def record_feedback(stage, title, detail, project='', expect='', severity='一般',
                    submitted=False):
    """生成一段标准 Issue 素材，落盘并打印（方便直接粘到 GitHub / 微信给作者）。"""
    key = project_key(project)
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    day = time.strftime('%Y%m%d')
    fn = os.path.join(FEEDBACK_DIR, '%s-%s-%s.md' % (day, _slug(title), stage))
    body = '\n'.join([
        '## 问题',
        '',
        title,
        '',
        '## 复现信息',
        '',
        '- 阶段：%s' % LABEL.get(stage, stage),
        '- 清单/项目：%s' % key,
        '- 发现时间：%s' % ts,
        '- 严重程度：%s' % severity,
        '',
        '## 具体现象',
        '',
        detail or '（未填写）',
        '',
        '## 期望行为',
        '',
        expect or '（未填写）',
        '',
        '## 建议改哪里',
        '',
        WHERE.get(stage, '（待定位）'),
        '',
        '---',
        '> 由 deliver_gate.py 自动生成。提交方式二选一：',
        '> ① 有 GitHub 账号：仓库 Issues → New issue → 把上面内容粘进去',
        '> ② 不想用 git：把这一段直接发给维护者',
        '',
    ])
    _ensure_dirs()
    with io.open(fn, 'w', encoding='utf-8', newline='\n') as f:
        f.write(body)

    state = _load()
    state['feedback'].append({
        'at': ts, 'project': key, 'stage': stage, 'title': title,
        'severity': severity, 'file': fn, 'submitted': bool(submitted),
    })
    _save(state)

    bar = '━' * 64
    print()
    print(bar)
    print('📮 反馈素材已生成，直接复制下面这段（粘 GitHub Issue 或发维护者）')
    print(bar)
    print(body)
    print(bar)
    print('已存：%s' % fn)
    print('提交后在 GitHub 上点一下，或本地标记：')
    print('  python deliver_gate.py submit %d' % (len(state['feedback']) - 1))
    print()
    return fn


def list_feedback(only_pending=True):
    state = _load()
    items = state.get('feedback', [])
    if only_pending:
        items = [x for x in items if not x.get('submitted')]
    if not items:
        print('没有未提交的反馈。')
        return 0
    print('未提交的反馈 %d 条：' % len(items))
    for i, x in enumerate(state['feedback']):
        if only_pending and x.get('submitted'):
            continue
        print('  [%d] %s  %s  %s  ← %s'
              % (i, x['at'], LABEL.get(x['stage'], x['stage']),
                 x['title'], os.path.basename(x.get('file', ''))))
    print()
    print('看全文：python deliver_gate.py feedback --show <序号>')
    print('标记已提交：python deliver_gate.py submit <序号>')
    return 0


def mark_submitted(idx):
    state = _load()
    if not (0 <= idx < len(state['feedback'])):
        print('序号超出范围')
        return 2
    state['feedback'][idx]['submitted'] = True
    _save(state)
    print('已标记提交：%s' % state['feedback'][idx]['title'])
    return 0


# ---------------------------------------------------------------- 状态展示

def show_status(project=''):
    state = _load()
    if not state['projects']:
        print('还没有任何交付记录。')
        return 0
    keys = [project_key(project)] if project else list(state['projects'])
    for key in keys:
        p = state['projects'].get(key)
        if not p:
            print('项目「%s」没有记录。' % key)
            continue
        print('项目：%s' % key)
        for s in ORDER:
            rec = p['stages'].get(s)
            if not rec or not rec.get('at'):
                print('  %-10s —' % LABEL[s])
                continue
            if rec.get('cleared'):
                mark = '✔ 已确认（%s）' % rec.get('verdict')
            else:
                mark = '⏳ 待确认 ← 下一段会被挡住'
            print('  %-10s %s  %s 行  %s'
                  % (LABEL[s], rec['at'], rec.get('rows') or 0, mark))
        print()
    pending = [x for x in state.get('feedback', []) if not x.get('submitted')]
    if pending:
        print('另有 %d 条未提交反馈：python deliver_gate.py feedback' % len(pending))
    return 0


def reset_project(project):
    state = _load()
    key = project_key(project)
    if key not in state['projects']:
        print('没有这个项目的记录：%s' % key)
        return 2
    del state['projects'][key]
    _save(state)
    print('已清空项目记录：%s' % key)
    return 0


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(
        description='交付门禁：每段交付必须问用户、确认后才许进下一段；通用问题落成 Issue 素材')
    sub = ap.add_subparsers(dest='cmd')

    sp = sub.add_parser('status', help='看各段交付/确认状态')
    sp.add_argument('--project', default='', help='只看某个项目')

    sp = sub.add_parser('clear', help='确认某段（用户回答之后）')
    sp.add_argument('stage', choices=ORDER)
    sp.add_argument('--verdict', default='ok', choices=['ok', 'changed', 'skip'])
    sp.add_argument('--note', default='')
    sp.add_argument('--project', default='')

    sp = sub.add_parser('check', help='检查能否进入某段（被挡则退出码 3）')
    sp.add_argument('stage', choices=ORDER)
    sp.add_argument('--project', default='')

    sp = sub.add_parser('deliver', help='手动登记一次交付并打回执（脚本内部调用）')
    sp.add_argument('stage', choices=ORDER)
    sp.add_argument('--rows', type=int, default=0)
    sp.add_argument('--project', default='')

    sp = sub.add_parser('issue', help='生成反馈 Issue 素材')
    sp.add_argument('--stage', default='params', choices=ORDER)
    sp.add_argument('--title', required=True)
    sp.add_argument('--detail', default='')
    sp.add_argument('--expect', default='')
    sp.add_argument('--severity', default='一般', choices=['一般', '严重', '建议'])
    sp.add_argument('--submitted', action='store_true', help='已经提了，直接标记')
    sp.add_argument('--project', default='')

    sp = sub.add_parser('feedback', help='列出未提交的反馈')
    sp.add_argument('--all', action='store_true', help='含已提交的')
    sp.add_argument('--show', type=int, default=None, help='看某条全文')

    sp = sub.add_parser('submit', help='标记某条反馈已提交')
    sp.add_argument('idx', type=int)

    sp = sub.add_parser('reset', help='清空某项目的记录（换新单时用）')
    sp.add_argument('project', nargs='?', default='')

    a = ap.parse_args()
    cmd = a.cmd or 'status'
    proj = getattr(a, 'project', '') or ''

    if cmd == 'status':
        return show_status(proj)
    if cmd == 'clear':
        return clear(a.stage, proj, a.verdict, a.note or '')
    if cmd == 'check':
        require_clear(a.stage, proj)
        print('OK：可以进入「%s」' % LABEL[a.stage])
        return 0
    if cmd == 'deliver':
        deliver(a.stage, proj, a.rows)
        return 0
    if cmd == 'issue':
        record_feedback(a.stage, a.title, a.detail, proj, a.expect,
                        a.severity, a.submitted)
        return 0
    if cmd == 'feedback':
        if a.show is not None:
            state = _load()
            if not (0 <= a.show < len(state['feedback'])):
                print('序号超出范围')
                return 2
            f = state['feedback'][a.show]['file']
            print(io.open(f, 'r', encoding='utf-8').read() if os.path.exists(f) else '文件已丢失')
            return 0
        return list_feedback(only_pending=not a.all)
    if cmd == 'submit':
        return mark_submitted(a.idx)
    if cmd == 'reset':
        return reset_project(a.project or '')
    return 0


if __name__ == '__main__':
    sys.exit(main())
