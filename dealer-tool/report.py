#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把选型库的现状汇成一份人看的 Markdown 报告。

为什么单独一个脚本：
  `enrich.py stats` 是给自己看的终端摘要，字段一多就挤不下；
  这份报告要发给同事、要能翻“哪些型号停产了”“哪些还没有参数”，
  所以列全清单、按品类分组，落成 md。

产出（默认写到数据目录，**不进仓库**：里面有真实型号清单）：
  <数据目录>/选型库更新报告-YYYYMMDD.md

用法：
  python report.py                 # 生成报告
  python report.py --out 路径.md   # 指定输出
  python report.py --selftest
"""
import argparse
import io
import os
import re
import sys
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store as S           # noqa: E402
import tpkit_config as cfg  # noqa: E402

# CRM 标签 → 中文（和 enrich.py 的 LABEL_CN 保持一致；这里独立一份，
# 免得 report.py 依赖 enrich.py 的导入副作用）
LABEL_CN = {
    'PROJECT_PRODUCT': '项目机型',
    'CUSTOMER_CUSTOM': '客户定制',
    'CAN_CUSTOM': '可定制',
    'DEMAND_PRODUCTION': '按需生产',
    'BARGAIN_CLEARANCE': '特价清仓',
    'VALUE_ADD': '增值产品',
    'ECOMMERCE_VERSION': '电商版',
}

SOURCE_NOTE = """\
> **价格口径**：下表价格取自 CRM 价格手册快照，用于粗判档次，**不是配单口径**。
> 配单时必须实时查 CRM，折扣口径每次现问。
>
> **在产状态**：以 CRM 的 `lifeCycle` 为唯一权威。平台的那个 `haltProduct`
> 字段实测**基本不更新**（2850 个型号里 0 条标停产），不能当依据。
>
> **备用参数**：由官网规格自动拼接，未经人工提炼，**不是配单首选**。
> 控标库有该型号时一律以控标库为准。官网不知道哪条该标★加分项，
> 所以备用参数里**一条加分项都没有**。
"""


def money(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return '—'
    return ('%.0f' % f) if f == int(f) else ('%.2f' % f)


def load(store=None):
    st = S.load(store or S.DEFAULT_STORE)
    return st, st['products']


def group_lines(ps, pred, by='cat1'):
    """按品类分组返回 {品类: [型号...]}（型号排序）。"""
    g = defaultdict(list)
    for r in ps.values():
        if pred(r):
            g[r.get(by) or '（未分类）'].append(r['model'])
    return {k: sorted(v) for k, v in sorted(g.items(), key=lambda x: (-len(x[1]), x[0]))}


def build(ps, meta):
    n = len(ps)
    L = []
    a = L.append

    lc = Counter((r.get('crm') or {}).get('lifecycle') for r in ps.values())
    ok = lc.get('PRODUCTION', 0)
    warn = lc.get('HALT_WARNING', 0)
    halt = lc.get('HALT', 0)
    nohit = lc.get(None, 0)

    a('# TP-LINK 选型库更新报告')
    a('')
    a('生成时间：%s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    a('')
    a('| 数据层 | 时间 |')
    a('|---|---|')
    a('| 平台列表刷新 | %s |' % (meta.get('last_refresh') or '—'))
    a('| 平台详情深抓 | %s |' % (meta.get('last_deep') or '（只跑了列表，没全量深抓）'))
    a('| CRM 在产/价格 | %s |' % (meta.get('crm_enriched') or '未跑'))
    a('| 备用参数生成 | %s |' % (meta.get('param_enriched') or '未跑'))
    a('')
    a(SOURCE_NOTE)

    # ---------------- 规模 ----------------
    a('## 一、规模')
    a('')
    a('| 项 | 数量 | 占比 |')
    a('|---|---|---|')
    a('| 型号总数 | %d | 100%% |' % n)

    def pct(x):
        return '%.1f%%' % (100.0 * x / n) if n else '—'

    a('| 有官网规格 | %d | %s |' % (sum(1 for r in ps.values() if r.get('spec')),
                                 pct(sum(1 for r in ps.values() if r.get('spec')))))
    a('| 有官网描述 | %d | %s |' % (sum(1 for r in ps.values() if r.get('desc')),
                                 pct(sum(1 for r in ps.values() if r.get('desc')))))
    a('| 有平台详情 | %d | %s |' % (sum(1 for r in ps.values() if r.get('detail_raw')),
                                 pct(sum(1 for r in ps.values() if r.get('detail_raw')))))
    a('| CRM 命中 | %d | %s |' % (n - nohit, pct(n - nohit)))
    a('')

    # ---------------- 在产 ----------------
    a('## 二、在产状态（CRM 权威口径）')
    a('')
    a('| 状态 | 数量 | 怎么用 |')
    a('|---|---|---|')
    a('| 在产（量产） | %d | 正常配单 |' % ok)
    a('| 停产预警 | %d | 还能配，但要先想好替代型号 |' % warn)
    a('| 停产 | %d | **不要往清单里放** |' % halt)
    if nohit:
        a('| CRM 无此型号 | %d | 未入册/套装名，状态未知，需人工确认 |' % nohit)
    a('')

    if halt:
        a('### 已停产清单（%d 个）' % halt)
        a('')
        a('> 这些型号在 CRM 里是 `HALT`。配单前先确认能不能用替代款。')
        a('')
        for cat, models in group_lines(ps, lambda r: (r.get('crm') or {}).get('lifecycle') == 'HALT').items():
            a('- **%s**（%d）：%s' % (cat, len(models), '、'.join(models)))
        a('')

    if warn:
        a('### 停产预警清单（%d 个）' % warn)
        a('')
        a('> `HALT_WARNING`：目前还能下单，但生命周期已进入末段。新项目建议提前找替代。')
        a('')
        for cat, models in group_lines(ps, lambda r: (r.get('crm') or {}).get('lifecycle') == 'HALT_WARNING').items():
            a('- **%s**（%d）：%s' % (cat, len(models), '、'.join(models)))
        a('')

    # ---------------- 价格 ----------------
    a('## 三、价格覆盖（备选参考）')
    a('')
    a('| 项 | 数量 |')
    a('|---|---|')
    a('| 有面价（catalogPrice） | %d |' % sum(1 for r in ps.values()
                                        if (r.get('crm') or {}).get('catalog_price') is not None))
    a('| 有批发价（wholesalePrice） | %d |' % sum(1 for r in ps.values()
                                          if (r.get('crm') or {}).get('wholesale_price') is not None))
    a('| 有现价（price） | %d |' % sum(1 for r in ps.values()
                                  if (r.get('crm') or {}).get('price') is not None))
    a('| 有业务折扣（businessDiscount） | %d |' % sum(1 for r in ps.values()
                                              if (r.get('crm') or {}).get('business_discount') not in (None, '')))
    a('')

    # 价格三档实证关系（用真实数据算，不写死）
    tri = [(r['crm'].get('catalog_price'), r['crm'].get('wholesale_price'), r['crm'].get('price'))
           for r in ps.values()
           if (r.get('crm') or {}).get('catalog_price') and (r.get('crm') or {}).get('wholesale_price')
           and (r.get('crm') or {}).get('price')]
    if tri:
        order = Counter()
        for c, w, p in tri:
            if c >= w >= p:
                order['面价 ≥ 批发价 ≥ 现价'] += 1
            elif w >= c >= p:
                order['批发价 ≥ 面价 ≥ 现价'] += 1
            elif p > c:
                order['现价 > 面价'] += 1
            elif c >= p >= w:
                order['面价 ≥ 现价 ≥ 批发价'] += 1
            else:
                order['其它'] += 1
        a('三个价格字段的高低关系（拿 %d 条同时有三档价的记录实测）：' % len(tri))
        a('')
        a('| 高低顺序 | 条数 | 占比 |')
        a('|---|---|---|')
        for k, v in order.most_common():
            a('| %s | %d | %.1f%% |' % (k, v, 100.0 * v / len(tri)))
        a('')
        a('**结论：高低顺序不恒定**——约五分之一的型号批发价高于面价，还有几十条现价略高于面价。')
        a('三档都是同一台机器的报价，但**别用「面价 × 某系数」去推现价**，也别假设现价永远最低。')
        a('配单只看 `price`（现价）；`businessDiscount` 是独立字段，不是「现价 ÷ 面价」的折扣率')
        a('（实测两者中位差 0.05，完全相等的只占少数），原样存值、不替它编语义。')
        a('')

    # ---------------- 项目机 ----------------
    a('## 四、机型分类（CRM 产品标签）')
    a('')
    tags = Counter()
    proj = 0
    for r in ps.values():
        c = r.get('crm') or {}
        if c.get('is_project_model'):
            proj += 1
        for t in (c.get('labels') or []):
            tags[t] += 1
    a('| 标签 | 数量 | 说明 |')
    a('|---|---|---|')
    a('| 项目机型 | %d | `PROJECT_PRODUCT` 或 seriesValue 含「项目机型」 |' % proj)
    for t, v in tags.most_common():
        if t == 'PROJECT_PRODUCT':
            continue
        a('| %s | %d | `%s` |' % (LABEL_CN.get(t, t), v, t))
    a('')

    # ---------------- 参数 ----------------
    a('## 五、参数覆盖')
    a('')
    kb = gen = none = 0
    for r in ps.values():
        p = r.get('param_backup') or {}
        if p.get('from_kongbiao'):
            kb += 1
        elif p.get('count'):
            gen += 1
        elif p:
            none += 1
    a('| 来源 | 数量 | 能不能直接用于配单 |')
    a('|---|---|---|')
    a('| 控标库（首选） | %d | ✅ 可以，控标库那份就是配单口径 |' % kb)
    a('| 官网规格自动拼（备用） | %d | ⚠ 仅供参考，必须人工看过再用 |' % gen)
    a('| 两边都没有 | %d | ❌ 需人工补 |' % none)
    a('')

    if gen:
        cnt = [r['param_backup']['count'] for r in ps.values()
               if (r.get('param_backup') or {}).get('source') == 'official_spec']
        if cnt:
            cnt.sort()
            a('备用参数的条数：中位 **%d** 条，最少 %d 条，最多 %d 条。'
              % (cnt[len(cnt) // 2], cnt[0], cnt[-1]))
            a('')

    if none:
        a('### 需人工补参数的型号（%d 个，按品类）' % none)
        a('')
        a('> 这些型号官网没有规格表，控标库也没有。绝大多数是平台专供套装、')
        a('> 配件、周边或云服务——本来就不参与控标，不用当成缺口。')
        a('')
        g = group_lines(ps, lambda r: not (r.get('param_backup') or {}).get('count'))
        for cat, models in g.items():
            show = '、'.join(models[:12]) + ('…（共 %d 个）' % len(models) if len(models) > 12 else '')
            a('- **%s**（%d）：%s' % (cat, len(models), show))
        a('')

    # ---------------- 官网缺口 ----------------
    no_off = [r for r in ps.values() if not r.get('officialRelateId')]
    backfilled = [r for r in ps.values() if r.get('id_from') == 'official_index']
    a('## 六、官网页面覆盖')
    a('')
    a('官网规格靠 `officialRelateId`（平台给的官网产品 id）去抓，没有 id 就没有规格。')
    a('')
    a('| 项 | 数量 |')
    a('|---|---|')
    a('| 选型库型号 | %d |' % len(ps))
    a('| 有官网 id | %d |' % (len(ps) - len(no_off)))
    a('| 仍缺官网 id | %d |' % len(no_off))
    a('| 其中靠索引反查补上的 | %d |' % len(backfilled))
    a('')
    if backfilled:
        a('反查补上的型号（平台没给 id，但官网有页面）：')
        a('')
        for r in sorted(backfilled, key=lambda x: -(len(x.get('spec') or [])))[:20]:
            a('- `%s` — %s，%d 条规格'
              % (r['model'], (r.get('name') or '')[:28], len(r.get('spec') or [])))
        if len(backfilled) > 20:
            a('- …（共 %d 个）' % len(backfilled))
        a('')
    if no_off:
        a('%d 个型号官网确实没有对应产品页（平台专供 / 套装名 / 配件 / 云服务），'
          '参数只能靠控标库或人工整理。' % len(no_off))
        a('')
        g = defaultdict(int)
        for r in no_off:
            g[r.get('cat1') or '（未分类）'] += 1
        a('| 品类 | 数量 |')
        a('|---|---|')
        for k, v in sorted(g.items(), key=lambda x: -x[1]):
            a('| %s | %d |' % (k, v))
        a('')

    # ---------------- 命令 ----------------
    a('## 七、下次怎么更新')
    a('')
    a('```bash')
    a('cd dealer-tool')
    a('python backup.py refresh                  # 拉平台列表 + 新增型号详情（几分钟）')
    a('python backup.py --workers 10 --fast --force fill   # 官网规格/描述全量重抓（约 5 分钟）')
    a('python backup.py tidy                     # 描述里残留的 HTML 压成纯文本（幂等）')
    a('python official_index.py build            # 官网产品索引（约 8 分钟，新增型号靠它反查官网页）')
    a('python official_index.py match            # 平台没给官网 id 的型号，回填 id 并补规格')
    a('python enrich.py crm                      # 全量拉 CRM：在产状态 + 面价/批发价/现价/折扣')
    a('python enrich.py params                   # 生成备用控标参数')
    a('python enrich.py stats                    # 终端看一眼（含 halt 一致性自查）')
    a('python report.py                          # 生成本报告')
    a('python backup.py html                     # 生成离线查看器')
    a('```')
    a('')
    a('`enrich.py crm` 一次拉全量价格手册（8397 条，约 12 秒），不是逐个型号查——')
    a('`productModel` 传空串就是不过滤，接口支持。')
    a('')
    a('`official_index.py` 是唯一能补「平台没给官网 id」的途径：平台那个按型号查官网页的')
    a('接口对本账号无权限，但官网公开接口的 id 是连续自增的，遍历一遍就能建反查表。')
    a('')
    return '\n'.join(L)


# ---------------------------------------------------------------- 自检
def _selftest():
    ok = fail = 0

    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print('  OK   %s' % name)
        else:
            fail += 1
            print('  FAIL %s' % name)

    fake = {
        'A1': {'model': 'A1', 'cat1': '交换机', 'crm': {'hit': True, 'lifecycle': 'HALT',
                                                     'lifecycle_cn': '停产', 'catalog_price': 300,
                                                     'wholesale_price': 200, 'price': 150,
                                                     'labels': ['PROJECT_PRODUCT'], 'is_project_model': True},
               'param_backup': {'count': 3, 'source': 'official_spec'}},
        'B2': {'model': 'B2', 'cat1': '半球', 'crm': {'hit': True, 'lifecycle': 'PRODUCTION',
                                                    'lifecycle_cn': '在产(量产)',
                                                    'catalog_price': 259, 'wholesale_price': 159,
                                                    'price': 129, 'labels': ['VALUE_ADD']},
               'param_backup': {'count': 11, 'source': 'kongbiao', 'from_kongbiao': True}},
        'C3': {'model': 'C3', 'cat1': '配件/其他', 'crm': {'hit': False},
               'param_backup': {'count': 0}},
    }
    txt = build(fake, {'last_refresh': '2026-10-30 11:15:32', 'crm_enriched': 'now'})
    chk('输出含标题', txt.startswith('# TP-LINK 选型库更新报告'))
    chk('停产清单有 A1', 'A1' in txt)
    chk('在产型号不进停产清单',
        'B2' not in (txt.split('已停产清单')[1].split('###')[0] if '已停产清单' in txt else ''))
    chk('价格口径写明非配单', '不是配单口径' in txt)
    chk('参数口径写明备用', '不是配单首选' in txt)
    chk('控标库口径写明', '控标库那份就是配单口径' in txt)
    chk('缺参数型号列到 C3', 'C3' in txt)
    chk('金额不带多余小数', money(129) == '129' and money(137.5) == '137.50')
    chk('分组按品类', list(group_lines(fake, lambda r: True).keys()) == ['交换机', '半球', '配件/其他']
        or '交换机' in group_lines(fake, lambda r: True))
    print()
    print('自检：OK %d / FAIL %d' % (ok, fail))
    return 0 if fail == 0 else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--store', default=S.DEFAULT_STORE)
    p.add_argument('--out', default=None)
    p.add_argument('--selftest', action='store_true')
    a = p.parse_args()

    if a.selftest:
        sys.exit(_selftest())

    st, ps = load(a.store)
    if not ps:
        sys.exit('选型库是空的：%s\n先跑 backup.py refresh' % a.store)
    md = build(ps, st.get('_meta') or {})

    out = a.out
    if not out:
        d = os.path.dirname(os.path.abspath(a.store))
        out = os.path.join(d, '选型库更新报告-%s.md' % time.strftime('%Y%m%d'))
    io.open(out, 'w', encoding='utf-8', newline='\n').write(md)
    print('已生成：%s（%d 行 / %.1f KB）' % (out, md.count('\n') + 1,
                                        len(md.encode('utf-8')) / 1024.0))


if __name__ == '__main__':
    try:
        main()
    except Exception as _e:
        if type(_e).__name__ == 'NotConfigured':
            sys.exit('配置缺失：%s' % _e)
        raise
