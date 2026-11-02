# -*- coding: utf-8 -*-
"""TP-LINK KBS 知识库查询 CLI（HTTP 直连，无需浏览器）。

用法：
    python kbs.py login                          # 验证账号是否可用
    python kbs.py search 防溺水 --pages 3         # 检索（默认「与」匹配）
    python kbs.py search 防溺水 --or --pages 5    # 「或」匹配（噪音大，慎用）
    python kbs.py doc <fdId>                     # 看文档元信息 + 附件清单
    python kbs.py att <fdId> -o ./att            # 下载该文档全部附件
    python kbs.py report 防溺水 -o 清单.html      # 检索并生成单文件 HTML 清单

凭据读取顺序（先命中先用）：
    1) 环境变量 TP_KBS_USER / TP_KBS_PASSWORD
    2) ~/.workbuddy/tplink-toolkit.config.json 里的  kbs: {user, password}
    3) 本技能目录 scripts/config.local.json 里的  kbs: {user, password}

设计要点（都是踩过的坑，改代码前先看 references/api-notes.md）：
    · 登录后必须先 GET 一次 searchBuilder 页面初始化会话，否则接口返回空 JSON 且不报错
    · 默认 bond=or 会把中文拆字，噪音放大两个数量级；默认走 and
    · 标题字段不固定：有的在 subject，有的只有 fileName
    · 「正文页」与「附件页」是同 fdId 的两条记录，按 fdId 去重
"""
import argparse
import html
import http.cookiejar
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# 站点地址与账号都不写死在代码里（仓库是公开的）。
# 读取顺序（先命中先用）：环境变量 > ~/.workbuddy/tplink-toolkit.config.json > 本目录 config.local.json
_CFG_PATHS = [
    os.path.join(os.path.expanduser("~"), ".workbuddy",
                 "tplink-toolkit.config.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json"),
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_cj = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))
_opener.addheaders = [("User-Agent", UA)]


# ─────────────────────────── 配置 / 登录 ───────────────────────────
def _cfg_kbs():
    """把各配置文件里的 kbs 段合并起来（后者覆盖前者）。"""
    out = {}
    for path in _CFG_PATHS:
        if not os.path.exists(path):
            continue
        try:
            seg = json.load(io.open(path, encoding="utf-8")).get("kbs") or {}
        except Exception:
            continue
        out.update(seg)
    return out


def _resolve_base():
    v = (os.environ.get("TP_KBS_BASE") or "").strip()
    if not v:
        v = str(_cfg_kbs().get("base") or "").strip()
    return v.rstrip("/")


BASE = _resolve_base()

_BASE_HINT = (
    "未配置 KBS 站点地址。本技能不内置内网地址，需要你自己填：\n"
    "  方式一  环境变量    export TP_KBS_BASE=https://kbs.<你的域名>\n"
    '  方式二  配置文件    "kbs": {"base": "https://kbs.<你的域名"}   '
    "（放在 ~/.workbuddy/tplink-toolkit.config.json 或 scripts/config.local.json）"
)


def _load_cred():
    u = os.environ.get("TP_KBS_USER")
    p = os.environ.get("TP_KBS_PASSWORD")
    if u and p:
        return u, p
    k = _cfg_kbs()
    if k.get("user") and k.get("password"):
        return k["user"], k["password"]
    sys.exit("未配置 KBS 账号。请设置环境变量 TP_KBS_USER / TP_KBS_PASSWORD，"
             "或在 ~/.workbuddy/tplink-toolkit.config.json 增加：\n"
             '  "kbs": {"base": "https://kbs.<你的域名>", '
             '"user": "you@example.com", "password": "***"}')


def get(url, referer=None):
    req = urllib.request.Request(url)
    if referer:
        req.add_header("Referer", referer)
    return _opener.open(req, timeout=90).read()


def login(verbose=True):
    user, pwd = _load_cred()
    get(BASE + "/login.jsp")
    data = urllib.parse.urlencode({
        "j_username": user, "j_password": pwd, "j_redirectto": "",
    }).encode()
    req = urllib.request.Request(BASE + "/j_acegi_security_check", data=data)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    _opener.open(req, timeout=90).read()
    # 关键：初始化检索页会话
    get(BASE + "/sys/ftsearch/searchBuilder.do?method=search")
    get(BASE + "/sys/portal/page.jsp")
    if verbose:
        ok = any(c.name == "LtpaToken" for c in _cj)
        print("登录%s：%s" % ("成功" if ok else "可能失败（未拿到 LtpaToken）", user))
    return True


# ─────────────────────────── 检索 ───────────────────────────
SEARCH_URL = BASE + "/sys/ftsearch/searchBuilder.do"


def _val(fm, key):
    v = fm.get(key)
    if isinstance(v, dict):
        v = v.get("value")
    return v or ""


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def search(keyword, page=1, bond="and", model=None):
    p = {"method": "search", "queryString": keyword, "newLUI": "true",
         "searchAll": "true", "pageno": str(page), "resultType": "json"}
    if bond:
        p["bond"] = bond
    if model:                                   # 限定模型，如 KmsWikiMain;KmsMultidocKnowledge
        p["searchKnowledge"] = "true"
        p["modelName"] = model
    url = SEARCH_URL + "?" + urllib.parse.urlencode(p)
    raw = get(url, referer=SEARCH_URL + "?method=search")
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}


def collect(keyword, maxpage=10, bond="and", model=None, sleep=0.15):
    """翻页收集并去重（按 docKey）。返回 (docs, total)。"""
    docs, total, seen = [], None, set()
    for pg in range(1, maxpage + 1):
        d = search(keyword, pg, bond, model)
        qp = d.get("queryPage") or {}
        if total is None:
            total = qp.get("totalrows")
        lst = qp.get("list") or []
        if not lst:
            break
        for it in lst:
            fm = it.get("lksFieldsMap") or {}
            dk = _val(fm, "docKey") or it.get("docId") or ""
            title = strip_tags(_val(fm, "subject") or _val(fm, "fileName")
                               or _val(fm, "docSubject"))
            snip = strip_tags(_val(fm, "fullText") or _val(fm, "content")
                              or _val(fm, "xmlcontent"))
            link = _val(fm, "linkStr")
            # 正文页/附件页同 fdId，合并取信息更全的一条
            fid = re.search(r"fdId=([0-9a-z]+)", link or "")
            key = fid.group(1) if fid else dk
            if key in seen:
                for x in docs:
                    if x["_key"] == key and len(snip) > len(x["snippet"]):
                        x["snippet"] = snip[:400]
                continue
            seen.add(key)
            docs.append({
                "_key": key,
                "title": title,
                "file": strip_tags(_val(fm, "fileName")),
                "creator": _val(fm, "creator"),
                "date": _val(fm, "createTime")[:10],
                "model": (_val(fm, "modelName") or "").split(".")[-1],
                "snippet": snip[:400],
                "url": (BASE + link) if link else "",
                "fdId": fid.group(1) if fid else "",
            })
        time.sleep(sleep)
    return docs, total


# ─────────────────────────── 文档 / 附件 ───────────────────────────
VIEW_URL = BASE + "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId="
DL_URL = BASE + "/sys/attachment/sys_att_main/sysAttMain.do?method=download&fdId="


def attachments(fd_id):
    """返回 [(文件名, 附件fdId, mime, 字节数)]"""
    page = get(VIEW_URL + fd_id,
               referer=BASE + "/sys/portal/page.jsp").decode("utf-8", "replace")
    out = []
    for name, aid, mime, size in re.findall(
            r'addDoc\("([^"]+)","([^"]+)",(?:true|false),"([^"]*)","([^"]*)"', page):
        if "\\u" in name:
            try:
                name = name.encode().decode("unicode_escape")
            except Exception:
                pass
        out.append((name, aid, mime, size))
    return out


def download(att_id):
    return get(DL_URL + att_id, referer=BASE + "/")


# ─────────────────────────── 清单 HTML ───────────────────────────
CORE_KW = ["防溺水", "溺水"]


def build_report(keyword, docs, total, out, bond="and"):
    def tier(d):
        t = d["title"]
        if any(k in t for k in CORE_KW):
            return 0
        if any(k in t for k in ("防汛", "河道", "水位", "落水", "水域", "水库")):
            return 1
        return 2

    for d in docs:
        d["tier"] = tier(d)
    docs.sort(key=lambda d: (d["tier"], -int(d["date"][:4] or 0)))
    TN = {0: "核心命中（标题直接相关）", 1: "强相关（同类场景）", 2: "其他（正文提及）"}

    def hl(s):
        return re.sub(r"(%s)" % "|".join(CORE_KW), r"<em>\1</em>", html.escape(s))

    blocks = []
    for t in (0, 1, 2):
        g = [d for d in docs if d["tier"] == t]
        if not g:
            continue
        rows = "".join(
            '<tr><td class="dt">%s</td>'
            '<td><a href="%s" target="_blank">%s</a>%s</td>'
            '<td class="au">%s</td><td><span class="tag">%s</span></td></tr>'
            % (d["date"] or "—", html.escape(d["url"]), hl(d["title"]),
               ('<div class="snip">%s</div>' % hl(d["snippet"][:260])) if d["snippet"] else "",
               html.escape(d["creator"]),
               (d["file"].rsplit(".", 1)[-1].upper() if "." in d["file"] else "—"))
            for d in g)
        blocks.append(
            '<section><h2>%s<span class="cnt">%d</span></h2><table><thead><tr>'
            '<th style="width:96px">日期</th><th>标题 / 摘要</th>'
            '<th style="width:78px">作者</th><th style="width:56px">格式</th>'
            '</tr></thead><tbody>%s</tbody></table></section>' % (TN[t], len(g), rows))

    html_doc = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>KBS 检索：%s</title><style>
body{margin:0;background:#f5f6f8;color:#1d2129;font:14px/1.65 -apple-system,"Microsoft YaHei",sans-serif}
.wrap{max-width:1060px;margin:0 auto;padding:28px 20px 60px}
header{background:#fff;border:1px solid #e5e6eb;border-radius:12px;padding:22px 26px;margin-bottom:16px}
h1{margin:0 0 6px;font-size:20px}.sub{color:#6b7280;font-size:13px}
input{width:100%%;margin-top:14px;padding:9px 13px;border:1px solid #dcdfe6;border-radius:8px;font-size:13px}
section{background:#fff;border:1px solid #e5e6eb;border-radius:12px;padding:6px 0 10px;margin-bottom:16px}
h2{font-size:14px;margin:14px 22px 10px;font-weight:600}
.cnt{margin-left:8px;background:#fdecea;color:#c0392b;border-radius:10px;padding:0 8px;font-size:12px}
table{width:100%%;border-collapse:collapse}
th{text-align:left;font-size:12px;color:#86909c;font-weight:500;padding:8px 12px;border-bottom:1px solid #eef0f3;background:#fafbfc}
td{padding:10px 12px;border-bottom:1px solid #f2f3f5;vertical-align:top}
th:first-child,td:first-child{padding-left:22px}a{color:#1c5fbf;text-decoration:none}
a:hover{text-decoration:underline}em{font-style:normal;color:#c0392b;font-weight:600}
.snip{margin-top:4px;font-size:12px;color:#8d95a1}.dt,.au{color:#5c6570;font-size:13px;white-space:nowrap}
.tag{font-size:11px;color:#5c6570;background:#f2f3f5;border-radius:4px;padding:1px 6px}
</style></head><body><div class="wrap"><header>
<h1>KBS 检索：%s</h1>
<div class="sub">命中 %s 条 · 去重后 %d 份 · 检索条件 bond=%s · %s</div>
<input id="q" placeholder="过滤标题或作者">
</header>%s
<footer style="color:#a0a6b0;font-size:12px;text-align:center;margin-top:24px">
数据来自 TP-LINK KBS 全文检索接口</footer></div>
<script>var q=document.getElementById('q');q.addEventListener('input',function(){
var v=this.value.trim().toLowerCase();
document.querySelectorAll('tbody tr').forEach(function(tr){
tr.style.display=(!v||(tr.innerText.toLowerCase().indexOf(v)>-1))?'':'none';});
document.querySelectorAll('section').forEach(function(s){
var any=[].some.call(s.querySelectorAll('tbody tr'),function(t){return t.style.display!=='none'});
s.style.display=any?'':'none';});});</script></body></html>""" % (
        html.escape(keyword), html.escape(keyword), total if total is not None else "?",
        len(docs), bond, time.strftime("%Y-%m-%d %H:%M"), "".join(blocks))

    io.open(out, "w", encoding="utf-8", newline="\n").write(html_doc)
    return out


# ─────────────────────────── CLI ───────────────────────────
def main():
    ap = argparse.ArgumentParser(description="TP-LINK KBS 查询")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login")

    s = sub.add_parser("search")
    s.add_argument("keyword")
    s.add_argument("--pages", type=int, default=3)
    s.add_argument("--or", dest="use_or", action="store_true",
                   help="使用「或」匹配（默认「与」）")
    s.add_argument("--model", default=None,
                   help="限定模型，如 KmsWikiMain;KmsMultidocKnowledge")
    s.add_argument("--json", default=None, help="结果另存 JSON")

    d = sub.add_parser("doc")
    d.add_argument("fd_id")

    a = sub.add_parser("att")
    a.add_argument("fd_id")
    a.add_argument("-o", "--out", default="./kbs_attachments")

    r = sub.add_parser("report")
    r.add_argument("keyword")
    r.add_argument("--pages", type=int, default=8)
    r.add_argument("-o", "--out", default=None)

    args = ap.parse_args()
    if not BASE:
        sys.exit(_BASE_HINT)
    login(verbose=(args.cmd != "search"))

    if args.cmd == "login":
        return

    if args.cmd == "search":
        docs, total = collect(args.keyword, args.pages,
                              "or" if args.use_or else "and", args.model)
        print("命中 %s 条，去重 %d 份（前 %d 页）" % (total, len(docs), args.pages))
        for i, x in enumerate(docs, 1):
            print("%3d. %s | %-58s | %s | %s"
                  % (i, x["date"] or "----------", x["title"][:58],
                     x["creator"], x["model"]))
        if args.json:
            io.open(args.json, "w", encoding="utf-8").write(
                json.dumps(docs, ensure_ascii=False, indent=1))
            print("已存：", args.json)
        return

    if args.cmd == "doc":
        atts = attachments(args.fd_id)
        print("附件 %d 个：" % len(atts))
        for n, aid, mime, size in atts:
            print("  - %s  [%s]  (fdId=%s)" % (n, mime.split("/")[-1], aid))
        return

    if args.cmd == "att":
        os.makedirs(args.out, exist_ok=True)
        for i, (n, aid, mime, size) in enumerate(attachments(args.fd_id)):
            ext = os.path.splitext(n)[1] or ".bin"
            fn = os.path.join(args.out, "%02d%s" % (i, ext))
            data = download(aid)
            io.open(fn, "wb").write(data)
            print("下载 %s -> %s (%d KB)" % (n, fn, len(data) // 1024))
        return

    if args.cmd == "report":
        docs, total = collect(args.keyword, args.pages)
        out = args.out or ("KBS_%s_清单.html" % re.sub(r"[\\/:*?\"<>|]", "_", args.keyword))
        build_report(args.keyword, docs, total, out)
        print("命中 %s 条，去重 %d 份，已生成：%s" % (total, len(docs), out))
        return


if __name__ == "__main__":
    main()
