#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub 仓库流量归档：把 14 天滚动窗口变成永久历史

GitHub 的 /traffic/* 接口只回最近 14 天，**过期即删、无法补抓**。
所以这个脚本干的不是"统计"，是"每天抢救一次" —— 攒得越久才越有长期趋势。

跑一次会做三件事：
  1. 抓：流量（views / clones / referrers / paths）+ 仓库快照（stars / forks / 语言 / 发布）
  2. 并：读回已有的 data/*.json，按日期合并，旧数据一条都不丢
  3. 画：生成一张纯静态的 index.html（SVG 图表，数据内联，没有 CDN、没有 JS 依赖）

零依赖（只用标准库），Windows / Linux 都能跑。

用法：
    export TRAFFIC_TOKEN=ghp_xxx          # 见下方权限说明
    python traffic/archive.py --repo owner/name --out .traffic-data

TRAFFIC_TOKEN 权限（这一步最容易踩坑）：
    - 默认的 GITHUB_TOKEN **读不了 traffic**，会 403
      （traffic 要 Administration 权限，workflow 的自动 token 给不了）
    - 所以必须建 Personal Access Token：
        classic  → 勾 repo
        fine-grained → 仓库权限 Administration: Read
    - token 从环境变量读，别放命令行（进程列表全机可见）

输出的数字怎么读（写简历时别吹错）：
    views / clones 的"次数"可以相加，是准的；
    uniques **可以直接报**：接口顶层的 uniques 是整个 14 天窗口去重后的值
    （实测每日相加 77 > 顶层 72，说明跨天重复的人已被合并），
    它就是"这 14 天有多少个不同的人"。但它和次数一样包含 CI / 镜像 / 爬虫，
    所以只能说"独立克隆者 72 人"，不能说"72 个同事在用"。
"""

import argparse
import datetime
import html
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from string import Template

API = 'https://api.github.com'
UA = 'traffic-archive/1.0 (+https://github.com)'


# ------------------------------------------------------------------ HTTP
class ApiError(Exception):
    pass


def api(path, token='', retries=3):
    """调 GitHub API。带 token 时用它（traffic 必须带）。"""
    url = path if path.startswith('http') else API + path
    req = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': UA,
        'X-GitHub-Api-Version': '2022-11-28',
        **({'Authorization': 'Bearer ' + token} if token else {}),
    })
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read().decode('utf-8')
                return json.loads(body) if body.strip() else {}, r.headers
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = json.loads(e.read().decode('utf-8')).get('message', '')
            except Exception:
                pass
            if e.code in (403, 401):
                raise ApiError(
                    '%s %s\n  → 403/401 基本都是权限问题：traffic 接口要求对仓库有 push 权限，\n'
                    '    classic PAT 勾 repo；fine-grained PAT 勾 Administration: Read。\n'
                    '    默认的 GITHUB_TOKEN 不行（给不了 Administration）。'
                    % (e.code, detail or e.reason))
            if e.code == 404:
                raise ApiError('404 %s\n  → 仓库名写错了，或者 token 看不到这个仓库。'
                               % (detail or path))
            last = '%s %s' % (e.code, detail or e.reason)
        except Exception as e:      # 网络抖动，重试
            last = str(e)
        time.sleep(1.5 * (i + 1))
    raise ApiError('请求失败（重试 %d 次）：%s' % (retries, last))


def link_last_page(headers):
    """从 Link 头里取最后一页页码 —— 用来数 commits / contributors 总数。"""
    link = (headers or {}).get('Link') or ''
    for part in link.split(','):
        if 'rel="last"' in part:
            try:
                return int(part.split('&page=')[-1].split('>')[0])
            except Exception:
                return 0
    return 0


def count_of(path, token):
    """数一个列表接口有多少条（不真拉全量，靠 Link 头）。"""
    try:
        items, hd = api(path + ('&' if '?' in path else '?') + 'per_page=1&page=1', token)
        last = link_last_page(hd)
        if last:
            return last
        return len(items or [])
    except ApiError:
        return 0


# ------------------------------------------------------------------ 存储
def load_json(path, default):
    try:
        with io.open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write('\n')


def merge_days(old, new):
    """按日期合并。同一天重复抓到时逐字段取大值（防中途抓、防抖动）。"""
    out = dict(old or {})
    for d, v in (new or {}).items():
        cur = out.get(d) or {}
        merged = dict(cur)
        for k, x in v.items():
            merged[k] = max(int(cur.get(k) or 0), int(x or 0))
        out[d] = merged
    return out


# ------------------------------------------------------------------ 抓取
def fetch(repo, token):
    print('抓取 %s' % repo)
    out = {}

    # 1) 流量：每天一条
    views = api('/repos/%s/traffic/views?per=day' % repo, token)[0]
    clones = api('/repos/%s/traffic/clones?per=day' % repo, token)[0]
    daily = {}
    for it in views.get('views') or []:
        d = (it.get('timestamp') or '')[:10]
        if d:
            daily.setdefault(d, {})['views'] = it.get('count') or 0
            daily[d]['view_uniques'] = it.get('uniques') or 0
    for it in clones.get('clones') or []:
        d = (it.get('timestamp') or '')[:10]
        if d:
            daily.setdefault(d, {})['clones'] = it.get('count') or 0
            daily[d]['clone_uniques'] = it.get('uniques') or 0
    out['daily'] = daily
    # 接口顶层返回的 count/uniques 是**整个 14 天窗口**的汇总，而且 uniques 已经
    # 跨天去重过（实测：每日 uniques 相加 77 > 顶层 72）。这才是能对外说的"人数"。
    # 注意它随窗口滚动，跟 daily 里累加的历史总量不是一个东西，别混用。
    out['totals'] = {
        'views_count': views.get('count') or 0,
        'views_uniques': views.get('uniques') or 0,
        'clones_count': clones.get('count') or 0,
        'clones_uniques': clones.get('uniques') or 0,
        'window_days': len(daily),
    }
    print('  流量：%d 天（views %d / clones %d）'
          % (len(daily), views.get('count') or 0, clones.get('count') or 0))

    # 2) 来源与热门页面：本身就是 14 天滚动聚合，存快照
    refs = api('/repos/%s/traffic/popular/referrers' % repo, token)[0]
    paths = api('/repos/%s/traffic/popular/paths' % repo, token)[0]
    out['referrers'] = [{'site': r.get('referrer'), 'count': r.get('count'),
                         'uniques': r.get('uniques')} for r in (refs or [])]
    out['paths'] = [{'path': p.get('path'), 'title': p.get('title'),
                     'count': p.get('count'), 'uniques': p.get('uniques')}
                    for p in (paths or [])]
    print('  来源：%d 个 / 页面：%d 个' % (len(out['referrers']), len(out['paths'])))

    # 3) 仓库快照：这些不需要 traffic 权限，用自动 token 就行
    info = api('/repos/%s' % repo, token)[0]
    langs = api('/repos/%s/languages' % repo, '')[0] or {}
    releases = api('/repos/%s/releases?per_page=100' % repo, '')[0] or []
    dl = sum((a.get('download_count') or 0)
             for r in releases for a in (r.get('assets') or []))
    lic = (info.get('license') or {}).get('spdx_id') or ''
    out['snapshot'] = {
        'stars': info.get('stargazers_count') or 0,
        'forks': info.get('forks_count') or 0,
        'watchers': info.get('subscribers_count') or 0,
        'open_issues': info.get('open_issues_count') or 0,
        'size_kb': info.get('size') or 0,
        'created_at': (info.get('created_at') or '')[:10],
        'pushed_at': (info.get('pushed_at') or '')[:10],
        'description': info.get('description') or '',
        'license': lic,
        'topics': info.get('topics') or [],
        'languages': langs,
        'default_branch': info.get('default_branch') or 'main',
        'releases': len(releases),
        'downloads': dl,
        'commits': count_of('/repos/%s/commits' % repo, ''),
        'contributors': count_of('/repos/%s/contributors?anon=1' % repo, ''),
    }
    s = out['snapshot']
    print('  快照：★%d ⑂%d issues %d 发布 %d（附件下载 %d）提交 %d'
          % (s['stars'], s['forks'], s['open_issues'], s['releases'],
             s['downloads'], s['commits']))
    return out


# ------------------------------------------------------------------ 画图
def _nice_max(v):
    if v <= 0:
        return 1
    step = 10 ** (len(str(int(v))) - 1)
    for m in (1, 2, 2.5, 5, 10):
        if v <= step * m:
            return int(step * m)
    return int(v)


def svg_line(dates, series, width=680, height=200):
    """多序列折线（带面积）。纯静态 SVG，无 JS。"""
    if not dates:
        return '<p class="empty">还没有数据</p>'
    pl, pr, pt, pb = 46, 10, 14, 26
    iw, ih = width - pl - pr, height - pt - pb
    vmax = _nice_max(max([max(s['data']) if s['data'] else 0 for s in series] + [1]))
    n = max(len(dates) - 1, 1)

    def x(i):
        # 只有一个数据点时没有"跨度"可言，居中放着才像张图（否则贴着左轴）
        if len(dates) == 1:
            return pl + iw / 2
        return pl + iw * i / n

    def y(v):
        return pt + ih - ih * (v / vmax)

    parts = ['<svg viewBox="0 0 %d %d" class="ch" role="img" style="width:100%%;height:auto;display:block">' % (width, height)]
    # 网格 + 纵轴刻度
    for k in range(5):
        v = vmax * k / 4
        yy = y(v)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>'
                     % (pl, yy, width - pr, yy))
        parts.append('<text x="%d" y="%.1f" class="ax" text-anchor="end">%d</text>'
                     % (pl - 6, yy + 4, round(v)))
    # 横轴日期：首、中、尾。两端必须改对齐方式 —— 一直用 middle 的话，
    # 最后一个标签以 x(尾) 为中心，右半截会画出 viewBox 被裁掉（"10-27" 变 "10-2"）。
    marks = sorted(set([0, len(dates) // 2, len(dates) - 1]))
    last = len(dates) - 1
    for i in marks:
        if i == 0 and last > 0:
            anchor = 'start'
        elif i == last and last > 0:
            anchor = 'end'
        else:
            anchor = 'middle'
        parts.append('<text x="%.1f" y="%d" class="ax" text-anchor="%s">%s</text>'
                     % (x(i), height - 8, anchor, dates[i][5:]))
    # 折线
    for s in series:
        d = s['data']
        pts = ' '.join('%.1f,%.1f' % (x(i), y(v)) for i, v in enumerate(d))
        area = '%s %.1f,%.1f %.1f,%.1f' % (pts, x(len(d) - 1), y(0), x(0), y(0))
        parts.append('<polygon points="%s" fill="%s" opacity="0.12"/>'
                     % (area, s['color']))
        parts.append('<polyline points="%s" fill="none" stroke="%s" '
                     'stroke-width="2" stroke-linejoin="round"/>' % (pts, s['color']))
        for i, v in enumerate(d):
            parts.append('<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s">'
                         '<title>%s %s：%d</title></circle>'
                         % (x(i), y(v), s['color'], dates[i], s['label'], v))
    parts.append('</svg>')
    return ''.join(parts)


def svg_bars(items, label_key, value_key, color='#2563eb', width=680, row=26):
    """横向条形图。"""
    items = [x for x in (items or []) if (x.get(value_key) or 0) > 0][:8]
    if not items:
        return '<p class="empty">还没有数据</p>'
    top = max(x.get(value_key) or 1 for x in items)
    lab_w, val_w = 200, 46
    bar_w = width - lab_w - val_w - 10
    h = row * len(items) + 8
    parts = ['<svg viewBox="0 0 %d %d" class="ch" role="img" style="width:100%%;height:auto;display:block">' % (width, h)]
    for i, it in enumerate(items):
        y = i * row + 4
        name = str(it.get(label_key) or '')
        if len(name) > 30:
            name = name[:29] + '…'
        v = it.get(value_key) or 0
        w = max(bar_w * v / top, 2)
        parts.append('<text x="0" y="%d" class="ax">%s</text>'
                     % (y + 15, html.escape(name)))
        parts.append('<rect x="%d" y="%d" width="%.1f" height="13" rx="3" fill="%s" '
                     'opacity="0.85"><title>%s %d</title></rect>'
                     % (lab_w, y + 3, w, color, html.escape(name), v))
        parts.append('<text x="%.1f" y="%d" class="ax num">%d</text>'
                     % (lab_w + w + 6, y + 15, v))
    parts.append('</svg>')
    return ''.join(parts)


def fmt_kb(kb):
    kb = kb or 0
    return ('%.1f MB' % (kb / 1024.0)) if kb >= 1024 else ('%d KB' % kb)


# ------------------------------------------------------------------ 汇总
def build_summary(daily, snaps, latest):
    dates = sorted(daily)
    ssum = {}
    if not dates:
        return {'dates': [], 'span': 0}
    last = snaps.get(dates[-1]) or (snaps[sorted(snaps)[-1]] if snaps else {}) or {}
    ssum['dates'] = dates
    ssum['first'] = dates[0]
    ssum['last'] = dates[-1]
    ssum['span'] = len(dates)
    ssum['views'] = sum(v.get('views') or 0 for v in daily.values())
    ssum['clones'] = sum(v.get('clones') or 0 for v in daily.values())
    ssum['uniques_sum'] = sum(v.get('view_uniques') or 0 for v in daily.values())
    ssum['clone_uniques_sum'] = sum(v.get('clone_uniques') or 0 for v in daily.values())
    # 真实人数用接口顶层的 uniques（跨天已去重）；老数据没存 totals 时才退回累加值
    tot = (latest or {}).get('totals') or {}
    ssum['uv_views'] = int(tot.get('views_uniques') or 0) or ssum['uniques_sum']
    ssum['uv_clones'] = int(tot.get('clones_uniques') or 0) or ssum['clone_uniques_sum']
    peak = max(daily.items(), key=lambda kv: (kv[1].get('views') or 0))
    ssum['peak_date'] = peak[0]
    ssum['peak_views'] = peak[1].get('views') or 0
    ssum['peak_uniques'] = peak[1].get('view_uniques') or 0
    ssum.update({k: last.get(k) or 0 for k in
                 ('stars', 'forks', 'watchers', 'open_issues', 'commits',
                  'contributors', 'releases', 'downloads', 'size_kb')})
    ssum['languages'] = last.get('languages') or {}
    ssum['license'] = last.get('license') or ''
    ssum['referrers'] = latest.get('referrers') or []
    ssum['paths'] = latest.get('paths') or []
    # 漏抓：从首次抓取那天到今天，中间缺了几天
    try:
        d0 = datetime.date.fromisoformat(dates[0])
        d1 = datetime.date.fromisoformat(dates[-1])
        want = (d1 - d0).days + 1
        ssum['missing'] = max(want - len(dates), 0)
    except Exception:
        ssum['missing'] = 0
    return ssum


PAGE = Template(r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>$repo · 仓库流量</title>
<style>
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;background:#f6f7f9;color:#111827;
 font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#6b7280;font-size:13px;margin-bottom:18px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px 18px;
 margin-bottom:14px;overflow:hidden;max-width:100%}
.card>h2{font-size:14px;margin:0 0 12px;color:#374151;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}
.k{background:#f9fafb;border:1px solid #f0f1f3;border-radius:10px;padding:10px 12px}
.k b{display:block;font-size:20px;line-height:1.25;font-variant-numeric:tabular-nums}
.k span{font-size:12px;color:#6b7280}
.ch{width:100%;max-width:100%;height:auto;display:block}
.grid-line,.grid{stroke:#eef0f3;stroke-width:1}
text.ax{font-size:10.5px;fill:#9ca3af;font-family:inherit}
text.ax.num{fill:#6b7280}
.empty{color:#9ca3af;font-size:13px;margin:6px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:6px 8px;border-bottom:1px solid #f0f1f3;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:#6b7280;font-weight:500;font-size:12px}
td.mono{font-variant-numeric:tabular-nums}
.note{color:#6b7280;font-size:12.5px;line-height:1.75}
.note b{color:#374151}
.tag{display:inline-block;font-size:11px;color:#6b7280;background:#f3f4f6;
 border-radius:6px;padding:1px 7px;margin-left:6px;vertical-align:middle}
.quote{background:#f9fafb;border-left:3px solid #d1d5db;padding:10px 14px;
 border-radius:0 8px 8px 0;font-size:13.5px;color:#374151;margin:0}
.foot{color:#9ca3af;font-size:12px;text-align:center;margin-top:24px}
</style></head><body><div class="wrap">
<h1>$title</h1>
<div class="sub">$desc</div>

<div class="card">
  <h2>流量总览<span class="tag">$first ~ $last，$span 天</span></h2>
  <div class="grid">
    <div class="k"><b>$views</b><span>累计浏览（次）</span></div>
    <div class="k"><b>$uv_views</b><span>独立访客（人）</span></div>
    <div class="k"><b>$clones</b><span>累计克隆（次）</span></div>
    <div class="k"><b>$uv_clones</b><span>独立克隆者（人）</span></div>
  </div>
  <div class="note" style="margin-top:12px">
    <b>人数</b>取接口按 14 天窗口去重后的值，可以直接说"这 14 天有 $uv_clones 个不同的人克隆过"。
    单日最高浏览 $peak_views 次（$peak_date），当天 $peak_uniques 人。
    <b>次数和人数都包含 CI、镜像与爬虫</b>，别等同于"有多少同事在用"。
  </div>
</div>

<div class="card">
  <h2>每天浏览 / 克隆</h2>
  $chart_flow
  <div class="note" style="margin-top:8px">
    <span style="color:#2563eb">━</span> 浏览　<span style="color:#f59e0b">━</span> 克隆
  </div>
</div>

<div class="card">
  <h2>Star 增长</h2>
  $chart_star
</div>

<div class="card">
  <h2>仓库现在的样子</h2>
  <div class="grid">
    <div class="k"><b>$stars</b><span>Stars</span></div>
    <div class="k"><b>$forks</b><span>Forks</span></div>
    <div class="k"><b>$watchers</b><span>Watchers</span></div>
    <div class="k"><b>$issues</b><span>Open Issues</span></div>
    <div class="k"><b>$commits</b><span>提交数</span></div>
    <div class="k"><b>$contributors</b><span>贡献者</span></div>
    <div class="k"><b>$releases</b><span>发布版本</span></div>
    <div class="k"><b>$downloads</b><span>附件下载（次）</span></div>
  </div>
  <div class="note" style="margin-top:12px">语言：$langs　许可证：$license　体积：$size</div>
</div>

<div class="card">
  <h2>流量从哪来<span class="tag">最近 14 天</span></h2>
  $refs
</div>

<div class="card">
  <h2>最常被打开的页面<span class="tag">最近 14 天</span></h2>
  $paths
</div>

<div class="card">
  <h2>最近 14 天明细</h2>
  <div style="overflow-x:auto">
  <table>
    <tr><th>日期</th><th>浏览</th><th>访客</th><th>克隆</th><th>克隆者</th></tr>
    $rows
  </table>
  </div>
</div>

<div class="card">
  <h2>可以拿去写简历的一句话</h2>
  <p class="quote">$quote</p>
  <div class="note" style="margin-top:10px">
    数字来源：GitHub Traffic / Repository API，$first ~ $last。别把"累计浏览"说成"用户量"；
    <b>人数可以直接说</b>（接口已跨天去重），但要说"独立克隆者"，不是"同事"——
    里面混着爬虫和镜像。
  </div>
</div>

<div class="foot">
  数据每天自动归档，来自 GitHub 官方接口，无第三方统计脚本<br>
  <a href="https://github.com/$repo" style="color:#9ca3af">$repo</a>　·　最近更新 $updated$warn
</div>
</div></body></html>
""")


def render(repo, daily, snaps, latest, ssum, updated):
    dates = ssum['dates']
    series_flow = [
        {'label': '浏览', 'color': '#2563eb',
         'data': [daily[d].get('views') or 0 for d in dates]},
        {'label': '克隆', 'color': '#f59e0b',
         'data': [daily[d].get('clones') or 0 for d in dates]},
    ]
    star_dates = sorted(snaps)
    series_star = [{'label': 'Stars', 'color': '#10b981',
                    'data': [(snaps[d] or {}).get('stars') or 0 for d in star_dates]}]

    rows = []
    for d in reversed(dates[-14:]):
        v = daily[d]
        rows.append('<tr><td class="mono">%s</td><td class="mono">%d</td>'
                    '<td class="mono">%d</td><td class="mono">%d</td>'
                    '<td class="mono">%d</td></tr>'
                    % (d, v.get('views') or 0, v.get('view_uniques') or 0,
                       v.get('clones') or 0, v.get('clone_uniques') or 0))

    langs = ssum.get('languages') or {}
    tot = sum(langs.values()) or 1
    lang_txt = '、'.join('%s %.0f%%' % (k, v * 100.0 / tot)
                        for k, v in sorted(langs.items(), key=lambda kv: -kv[1])[:3]) or '—'

    refs = ssum.get('referrers') or []
    top_ref = refs[0]['site'] if refs else ''
    quote = ('独立维护开源项目 %s：%s 至 %s 期间累计被浏览 %d 次、被克隆 %d 次，'
             '期间 %d 个独立克隆者，单日最高 %d 次浏览 / %d 人访问%s，'
             '仓库获 %d Star、%d Fork。'
             % (repo.split('/')[-1], ssum['first'], ssum['last'], ssum['views'],
                ssum['clones'], ssum['uv_clones'], ssum['peak_views'], ssum['peak_uniques'],
                ('，主要入口为 %s' % top_ref) if top_ref else '',
                ssum['stars'], ssum['forks']))

    warn = ''
    if ssum.get('missing'):
        warn = '　·　⚠ 中间有 %d 天没抓到（那天没跑，数据已随 14 天窗口丢失）' \
               % ssum['missing']

    return PAGE.substitute(
        repo=repo, title=html.escape(repo),
        desc=html.escape((snaps[max(snaps)].get('description') or '') if snaps else '')
        or 'GitHub 仓库流量长期归档',
        first=ssum['first'], last=ssum['last'], span=ssum['span'],
        views=ssum['views'], clones=ssum['clones'],
        uv_views=ssum['uv_views'], uv_clones=ssum['uv_clones'],
        peak_views=ssum['peak_views'], peak_uniques=ssum['peak_uniques'],
        peak_date=ssum.get('peak_date') or '',
        chart_flow=svg_line(dates, series_flow),
        chart_star=svg_line(star_dates, series_star),
        stars=ssum['stars'], forks=ssum['forks'], watchers=ssum['watchers'],
        issues=ssum['open_issues'], commits=ssum['commits'],
        contributors=ssum['contributors'], releases=ssum['releases'],
        downloads=ssum['downloads'], langs=html.escape(lang_txt),
        license=html.escape(ssum.get('license') or '—'),
        size=fmt_kb(ssum['size_kb']),
        refs=svg_bars(refs, 'site', 'count', '#2563eb'),
        paths=svg_bars(ssum.get('paths'), 'path', 'count', '#8b5cf6'),
        rows=''.join(rows) or '<tr><td colspan="5">还没有数据</td></tr>',
        quote=html.escape(quote),
        updated=updated, warn=warn)


def render_summary_md(repo, ssum):
    refs = '、'.join('%s %d' % (r['site'], r['count'])
                    for r in (ssum.get('referrers') or [])[:5]) or '—'
    paths = '、'.join('%s %d' % (p['path'], p['count'])
                     for p in (ssum.get('paths') or [])[:5]) or '—'
    return """# 仓库流量摘要

仓库：`%s`
统计区间：%s ~ %s（%d 天）

| 指标 | 数值 |
|---|---|
| 累计浏览 | %d 次 |
| 独立访客 | %d 人 |
| 单日最高浏览 | %d 次（%s） |
| 单日最高访客 | %d 人 |
| 累计克隆 | %d 次 |
| 独立克隆者 | %d 人 |
| Stars | %d |
| Forks | %d |
| Open Issues | %d |
| 提交数 | %d |
| 贡献者 | %d |
| 发布版本 | %d（附件下载 %d 次） |
| 仓库体积 | %s |
| 主入口 | %s |
| 最常看的页面 | %s |

**可写进简历的一句话**

> 独立维护开源项目 %s：%s 至 %s 累计被浏览 %d 次、被克隆 %d 次，
> 期间 %d 个独立克隆者，单日最高 %d 次浏览 / %d 人访问，获 %d Star、%d Fork。

**口径提醒**：`独立访客 / 独立克隆者` 取接口按 14 天窗口**去重后**的值，
可以直接说"这 14 天有 N 个不同的人克隆过"；但**次数和人数都包含 CI、镜像与爬虫**，
所以别说成"有 N 个同事在用"。`累计` 那几个是历史累加，范围比 14 天宽。
""" % (repo, ssum['first'], ssum['last'], ssum['span'],
       ssum['views'], ssum['uv_views'], ssum['peak_views'], ssum['peak_date'],
       ssum['peak_uniques'], ssum['clones'], ssum['uv_clones'],
       ssum['stars'], ssum['forks'], ssum['open_issues'],
       ssum['commits'], ssum['contributors'], ssum['releases'], ssum['downloads'],
       fmt_kb(ssum['size_kb']), refs, paths,
       repo.split('/')[-1], ssum['first'], ssum['last'], ssum['views'],
       ssum['clones'], ssum['uv_clones'], ssum['peak_views'], ssum['peak_uniques'],
       ssum['stars'], ssum['forks'])


# ------------------------------------------------------------------ main
def selftest():
    """不联网跑一遍完整渲染链路，确认页面和汇总没坏。"""
    import tempfile
    d0 = datetime.date(2026, 10, 1)
    daily, snaps = {}, {}
    for i in range(12):
        d = (d0 + datetime.timedelta(days=i)).isoformat()
        daily[d] = {'views': 3 + i * 2, 'view_uniques': 1 + i // 3,
                    'clones': i, 'clone_uniques': i // 2}
        snaps[d] = {'stars': i // 4, 'forks': i // 6, 'watchers': 1,
                    'open_issues': 0, 'commits': 40 + i, 'contributors': 1,
                    'releases': 1, 'downloads': i * 3, 'size_kb': 1024 + i,
                    'languages': {'Python': 9000, 'Batchfile': 100},
                    'license': 'MIT', 'description': '自测用假仓库'}
    latest = {'at': '2026-10-12',
              'referrers': [{'site': 'github.com', 'count': 30, 'uniques': 12},
                            {'site': 'google.com', 'count': 9, 'uniques': 5}],
              'paths': [{'path': '/README.md', 'count': 20, 'uniques': 8,
                         'title': 'README'}]}
    ssum = build_summary(daily, snaps, latest)
    page = render('owner/name', daily, snaps, latest, ssum, '2026-10-12 00:00 UTC')
    md = render_summary_md('owner/name', ssum)
    # 接口顶层 uniques 是跨天去重的，必须盖过"每日独立数相加"
    latest_tot = dict(latest)
    latest_tot['totals'] = {'views_count': 999, 'views_uniques': 77,
                            'clones_count': 999, 'clones_uniques': 66,
                            'window_days': 12}
    ssum_tot = build_summary(daily, snaps, latest_tot)
    checks = [
        ('总览数字', '累计浏览' in page and '独立克隆者' in page),
        ('折线图', '<svg' in page and 'polyline' in page),
        ('条形图', '<rect' in page),
        ('明细表', '最近 14 天明细' in page),
        ('简历话术', '独立维护开源项目 name' in page),
        ('摘要 md', '可写进简历的一句话' in md),
        ('天数统计', ssum['span'] == 12),
        ('浏览合计', ssum['views'] == sum(v['views'] for v in daily.values())),
        ('最高单日', ssum['peak_views'] == max(v['views'] for v in daily.values())),
        # 老数据没有 totals 时退回累加值；有 totals 时必须用去重值
        ('人数回退(无totals)', ssum['uv_clones'] == ssum['clone_uniques_sum']),
        ('人数取去重值', ssum_tot['uv_clones'] == 66 and ssum_tot['uv_views'] == 77),
        ('日期标签不被裁', 'text-anchor="end"' in page and 'text-anchor="start"' in page),
    ]
    out = os.path.join(tempfile.gettempdir(), 'traffic-selftest.html')
    with io.open(out, 'w', encoding='utf-8', newline='\n') as f:
        f.write(page)
    bad = 0
    for name, good in checks:
        print('  %s %s' % ('OK  ' if good else 'FAIL', name))
        bad += 0 if good else 1
    print('\n样例页面：%s' % out)
    print('自检：%d/%d 通过' % (len(checks) - bad, len(checks)))
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser(description='GitHub 仓库流量归档')
    ap.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY', ''),
                    help='owner/name，默认读 GITHUB_REPOSITORY')
    ap.add_argument('--out', default='.traffic-data', help='输出目录（数据 + 页面）')
    ap.add_argument('--token', default=os.environ.get('TRAFFIC_TOKEN', ''),
                    help='建议用环境变量 TRAFFIC_TOKEN，别放命令行')
    ap.add_argument('--selftest', action='store_true', help='不联网，自测渲染链路')
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.repo or '/' not in a.repo:
        print('缺 --repo owner/name', file=sys.stderr)
        return 2
    token = (a.token or '').strip()
    if not token:
        print('⚠ 没有 TRAFFIC_TOKEN：流量接口会 403，只能抓 stars/forks 这类公开数据。\n'
              '  建一个 classic PAT 勾 repo（或 fine-grained 勾 Administration: Read）。',
              file=sys.stderr)

    data_dir = os.path.join(a.out, 'data')
    f_daily = os.path.join(data_dir, 'daily.json')
    f_snap = os.path.join(data_dir, 'snapshots.json')
    f_latest = os.path.join(data_dir, 'latest.json')

    old_daily = load_json(f_daily, {})
    old_snaps = load_json(f_snap, {})
    latest_all = load_json(f_latest, {})

    try:
        got = fetch(a.repo, token)
    except ApiError as e:
        print('抓取失败：%s' % e, file=sys.stderr)
        return 1

    daily = merge_days(old_daily, got['daily'])
    today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    snaps = dict(old_snaps)
    snaps[today] = got['snapshot']
    latest_all['referrers'] = got['referrers']
    latest_all['paths'] = got['paths']
    latest_all['totals'] = got['totals']      # 滚动窗口汇总，每次覆盖
    latest_all['at'] = today

    save_json(f_daily, daily)
    save_json(f_snap, snaps)
    save_json(f_latest, latest_all)

    ssum = build_summary(daily, snaps, latest_all)
    updated = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    html_txt = render(a.repo, daily, snaps, latest_all, ssum, updated)
    with io.open(os.path.join(a.out, 'index.html'), 'w', encoding='utf-8',
                 newline='\n') as f:
        f.write(html_txt)
    with io.open(os.path.join(a.out, 'summary.md'), 'w', encoding='utf-8',
                 newline='\n') as f:
        f.write(render_summary_md(a.repo, ssum))

    print('\n累积：%d 天（%s ~ %s）' % (ssum['span'], ssum['first'], ssum['last']))
    print('      浏览 %d 次 / 克隆 %d 次 / 单日最高 %d 次 %d 人'
          % (ssum['views'], ssum['clones'], ssum['peak_views'], ssum['peak_uniques']))
    if ssum.get('missing'):
        print('      ⚠ 有 %d 天没抓到（那次没跑，窗口过期就补不回来了）' % ssum['missing'])
    print('已写：%s/{data/index.html/summary.md}' % a.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
