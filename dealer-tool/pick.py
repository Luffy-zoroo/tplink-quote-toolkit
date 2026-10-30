# -*- coding: utf-8 -*-
"""选型检索助手：在本地 dealer 备份库中按关键词/品类检索候选型号。

用法：
    pick.py 关键词1 关键词2 ...            # 全库 AND 检索（型号/名称/描述/规格全文）
    pick.py --cat 交换机 --kw 万兆         # 限品类
    pick.py --model LT-XXX                 # 精确看某型号全部规格
"""
import sys, re, json, os, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as S


def _load_gate():
    """跨目录加载交付门禁（deliver_gate.py 只在技能目录里放一份，不复制）。

    找不到就返回 None —— 选型检索不能因为门禁模块缺失而用不了。
    """
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        # 安装后的标准位置（install.py 装技能到这里）
        os.path.join(os.path.expanduser('~'), '.workbuddy', 'skills',
                     'tplink-quote-autofill', 'scripts', 'deliver_gate.py'),
        # 仓库/开发态：dealer-tool 与 skills 同级
        os.path.join(here, '..', 'skills', 'tplink-quote-autofill',
                     'scripts', 'deliver_gate.py'),
    ]
    for p in cands:
        p = os.path.normpath(p)
        if os.path.exists(p):
            try:
                spec = importlib.util.spec_from_file_location('deliver_gate', p)
                m = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(m)
                return m
            except Exception:
                return None
    return None


gate = _load_gate()

NET_CATS = None


def sm(r):
    """规格 -> dict(name -> ' / '.join(values))"""
    d = {}
    for it in (r.get('spec') or []):
        d.setdefault(it['name'], []).extend(it['values'] or [])
    return {k: ' / '.join(v) for k, v in d.items()}


def blob(r):
    parts = [r.get('model') or '', r.get('name') or '', r.get('desc') or '',
             r.get('cat1') or '', r.get('cat2') or '']
    # 规格大分组名（如「图像与视频参数」）也进检索范围，2026-10-19 起 spec 才带上 group
    for it in (r.get('spec') or []):
        parts.append('%s %s=%s' % (it.get('group') or '', it.get('name') or '',
                                   ' / '.join(it.get('values') or [])))
    return '\n'.join(parts)


def load():
    st = S.load()
    ps = st['products']
    if not ps:
        # 库是空的要说清楚原因（2026-10-19 实测：路径配错时只报"命中 0 条"，
        # 会被误读成"库里没有这个产品"）。store.load() 在文件不存在时静默返回空库，
        # 这里补一层诊断，不改变上层逻辑。
        p = S.DEFAULT_STORE
        sys.stderr.write(
            '[选型库] 读到 0 条：%s\n'
            '  只可能是这三种原因，按顺序查：\n'
            '    1) 文件不存在 → 数据包里的 数据/2-选型库.json 有没有拷到该位置\n'
            '    2) 路径配错   → ~/.workbuddy/tplink-toolkit.config.json 的 dealer_backup_json\n'
            '    3) 还没备份过 → 跑 backup.py refresh 拉一次（需合作伙伴平台账号）\n'
            '  查当前实际读的是哪个文件：python tpkit_config.py\n' % p)
    return ps


def search(kws, cat=None, brand=None, industry=None, limit=40, kbs=None,
           drop_halt=False):
    ps = load()
    out = []
    for m, r in ps.items():
        if drop_halt and (r.get('crm') or {}).get('lifecycle') == 'HALT':
            continue
        if cat and cat not in ((r.get('cat1') or '') + '/' + (r.get('cat2') or '')):
            continue
        if brand and r.get('brand') != brand:
            continue
        if industry is not None and bool(r.get('industry')) != industry:
            continue
        b = blob(r)
        if kws and not all(k in b for k in kws):
            continue
        if kbs and not all(k in b for k in kbs):
            continue
        out.append(r)
    # 排序：TP 优先 → 停产/停产预警沉到最后（能配的排前面）→ 型号
    def _rank(r):
        lc = (r.get('crm') or {}).get('lifecycle')
        return (r.get('brand') != 'TP', {'HALT': 2, 'HALT_WARNING': 1}.get(lc, 0),
                r.get('model') or '')
    out.sort(key=_rank)
    return out[:limit]


def crm_line(r):
    """把 CRM 在产状态 + 参考价压成一行。没有 CRM 数据就明说没有。"""
    c = r.get('crm') or {}
    if not c.get('hit'):
        return '   CRM: 价格手册无此型号（未入册/套装名），停产状态未知'
    lc = c.get('lifecycle')
    if lc == 'HALT':
        st = '⛔ 停产 —— 不要往清单里放'
    elif lc == 'HALT_WARNING':
        st = '⚠ 停产预警（还能配，但先想好替代）'
    else:
        st = c.get('lifecycle_cn') or '在产'
    bits = [st]
    cat, pz = c.get('catalog_price'), c.get('price')
    if pz is not None:
        bits.append('现价¥%s' % _money(pz))
    if cat is not None and cat != pz:
        bits.append('面价¥%s' % _money(cat))
    tags = c.get('label_cn') or []
    if c.get('is_project_model'):
        bits.append('项目机型')
    extra = [t for t in tags if t not in ('项目机型',)]
    if extra:
        bits.append('/'.join(extra[:3]))
    if c.get('business_discount') not in (None, ''):
        bits.append('业务折扣 %s' % c['business_discount'])
    return '   CRM: ' + ' | '.join(bits)


def _money(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return ('%.0f' % f) if f == int(f) else ('%.2f' % f)


def brief(r, keys=None):
    s = sm(r)
    head = '%-26s %-6s %-6s %-14s %-18s %s' % (
        (r.get('model') or '')[:26], r.get('brand'), '行业' if r.get('industry') else '渠道',
        (r.get('cat1') or '')[:14], (r.get('cat2') or '')[:18], (r.get('name') or '')[:40])
    line2 = '   desc: ' + (r.get('desc') or '').replace('\n', ' / ')[:150]
    lines = [head, line2, crm_line(r)]
    # 有控标库参数的，提示首选哪份；只有备用参数的，提醒是备用
    pb = r.get('param_backup') or {}
    if pb.get('from_kongbiao'):
        lines.append('   参数: 控标库有（%d 条，配单首选）' % pb.get('count', 0))
    elif pb.get('count'):
        lines.append('   参数: ⚠ 仅备用（官网规格自动拼 %d 条，非配单首选）' % pb['count'])
    if keys:
        for k in keys:
            if k == 'siblings':          # 同系列型号（detail_raw.productGroupList 提取）
                sib = [x.get('model') for x in (r.get('siblings') or [])]
                if sib:
                    lines.append('   同系列: %s' % ' / '.join(sib))
            elif k in s:
                lines.append('   %s: %s' % (k, s[k][:160]))
    return '\n'.join(lines)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('kw', nargs='*')
    ap.add_argument('--cat')
    ap.add_argument('--brand')
    ap.add_argument('--industry', type=int, default=None)
    ap.add_argument('--limit', type=int, default=40)
    ap.add_argument('--keys', default='')
    ap.add_argument('--model')
    ap.add_argument('--show', action='store_true')
    ap.add_argument('--drop-halt', action='store_true',
                    help='过滤掉 CRM 判定停产的型号（默认不过滤，只是沉到后面并标红）')
    ap.add_argument('--project', default='',
                    help='清单文件名（和 autofill_quote.py 的 --xlsx 用同一个），'
                         '用于交付门禁按项目记账；不填记到 default')
    ap.add_argument('--no-gate', action='store_true', help='不登记交付（纯查询）')
    a = ap.parse_args()
    if a.model:
        ps = load()
        r = ps.get(a.model)
        if not r:
            cand = [m for m in ps if a.model.upper() in m.upper()]
            print('未精确命中，相近型号：', cand[:20])
        else:
            print(json.dumps({k: v for k, v in r.items() if k != 'spotlight'},
                             ensure_ascii=False, indent=1))
        sys.exit()
    keys = [k for k in a.keys.split(',') if k]
    res = search(a.kw, cat=a.cat, brand=a.brand, industry=a.industry, limit=a.limit,
                 drop_halt=a.drop_halt)
    print('命中 %d 条（显示前 %d）%s\n'
          % (len(res), a.limit, '，已过滤停产' if a.drop_halt else ''))
    for r in res:
        print(brief(r, keys))
        print()

    # ---- 交付登记（门禁）----
    # 每次检索都记一次「选型待确认」：下一段填参数/价格会被强制拦住（退出码 3），
    # 直到把选型结果拿去问用户、拿到回答再 clear。这是流程断点，不是提示。
    if gate and not a.no_gate:
        gate.deliver('select', a.project, rows=len(res), quiet=True)
        print('🔁 已登记为「选型待用户确认」——填参数/价格前必须先问用户。')
        if not a.project:
            print('   ⚠ 没传 --project，记在 default 下。'
                  '要和 autofill_quote.py 对上，请传同一个清单文件名：')
            print('     pick.py ... --project "某清单.xlsx"')
        print('   用户确认后：python deliver_gate.py clear select --verdict ok --project "%s"'
              % (a.project or 'default'))
        print()
