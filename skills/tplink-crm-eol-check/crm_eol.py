import urllib.request, urllib.parse, ssl, json, re, sys, time, argparse, os
from http.cookiejar import CookieJar

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tpkit_config as cfg  # noqa: E402

def crm_base():
    """CRM 站点根地址。内部系统地址不写死在代码里 —— 从配置项 crm_base 读取；
    未配置时抛出 cfg.NotConfigured，由上层决定是报错还是降级。"""
    return cfg.require_base("crm_base", "CRM")

# 代理：优先系统/环境代理，其次配置文件里的 proxy 字段，最后退回本地默认端口
_PROXIES=urllib.request.getproxies()
PROXY=_PROXIES.get('https') or _PROXIES.get('http') or cfg.get('proxy') or "http://127.0.0.1:57228"
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
cj=CookieJar()
op=urllib.request.build_opener(urllib.request.ProxyHandler({'https':PROXY,'http':PROXY}),
                               urllib.request.HTTPSHandler(context=ctx),
                               urllib.request.HTTPCookieProcessor(cj))


def resolve_creds():
    """CRM 凭据：配置文件 crm 段 → 环境变量 TP_CRM_* → ~/.workbuddy/MEMORY.md。

    原先此处内置了明文账号密码，已移除。使用者请在
    ~/.workbuddy/tplink-toolkit.config.json 里配置自己的 crm 账号。
    """
    email, pwd = cfg.crm_creds()
    email = email or os.environ.get("TP_CRM_EMAIL", "")
    pwd = pwd or os.environ.get("TP_CRM_PASSWORD", "")
    if not email or not pwd:
        mem = os.path.join(os.path.expanduser("~"), ".workbuddy", "MEMORY.md")
        try:
            t = open(mem, encoding="utf-8", errors="replace").read()
        except OSError:
            t = ""
        if not email:
            m = re.search(r"[\w\.\-]+@[\w\.\-]+\.\w+", t)
            if m: email = m.group(0)
        if not pwd:
            m = re.search(r"密码\**\s*[：:]\s*`([^`\n]+)`", t)
            if m: pwd = m.group(1).strip()
    if not email or not pwd:
        sys.exit("缺少 CRM 凭据：请在 ~/.workbuddy/tplink-toolkit.config.json 的 crm 段填写 "
                 "(email/password)，或设置环境变量 TP_CRM_EMAIL / TP_CRM_PASSWORD。")
    return email, pwd

def _open(req, tries=4):
    last=None
    for i in range(tries):
        try: return op.open(req, timeout=30)
        except Exception as e:
            last=e; time.sleep(2*(i+1))
    raise last

def post(url, data):
    req=urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type":"application/x-www-form-urlencoded","User-Agent":"Mozilla/5.0"})
    return json.loads(_open(req).read().decode())

def get(url):
    return json.loads(_open(urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})).read().decode())

LC_MAP={'PRODUCTION':'在产(量产)','HALT':'停产','HALT_WARNING':'停产预警(在产中)'}

def norm(s): return re.sub(r'[\s\-]','',(s or '')).upper()

def extract_models(raw):
    raw=re.sub(r'[（(][^）)]*[）)]','',raw or '')
    out=[]
    for p in re.split(r'[/\+\s]', raw):
        p=p.strip()
        if p.upper().startswith('TL') and len(norm(p))>=5:
            if p not in out: out.append(p)
    return out

def query_crm(model):
    rows=[]
    try:
        first=get(f"{crm_base()}/api/line/findByPage?productModel={urllib.parse.quote(model)}&start=0&limit=100")
    except Exception:
        return rows
    total=first.get('totalCounts',0); rows=list(first.get('results') or [])
    st=100
    while len(rows)<total and st<total+200:
        try:
            pg=get(f"{crm_base()}/api/line/findByPage?productModel={urllib.parse.quote(model)}&start={st}&limit=100")
            add=pg.get('results') or []
            if not add: break
            rows+=add
        except Exception:
            break
        st+=100
    return rows

def check_one(raw):
    models=extract_models(raw)
    exact=[]
    for m in models:
        for r in query_crm(m):
            pm=r.get('productModel','')
            if norm(pm)==norm(m):
                exact.append((pm, r.get('lifeCycle'), r.get('lifeCycleMeaning'), r.get('productLabelName')))
    if not exact:
        return ('无精确匹配', '-', f"CRM 未找到精确型号 {models}")
    lcs=set(e[1] for e in exact)
    if 'HALT' in lcs: concl='停产'
    elif 'HALT_WARNING' in lcs: concl='停产预警(在产中)'
    elif lcs=={'PRODUCTION'}: concl='在产(量产)'
    else: concl='/'.join(LC_MAP.get(x,x) for x in lcs)
    detail="; ".join(f"{e[0]}:{e[2] or e[1]}" for e in exact)
    return (concl, ','.join(sorted(lcs)), detail)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--models', required=True, help='型号文件(每行一个) 或 逗号分隔串')
    ap.add_argument('--skip', default='', help='跳过型号，逗号分隔')
    a=ap.parse_args()
    skip=set(x.strip() for x in a.skip.split(',') if x.strip())
    if a.models.endswith('.txt') and os.path.isfile(a.models):
        items=[l.strip() for l in open(a.models,encoding='utf-8') if l.strip()]
    else:
        items=[x.strip() for x in a.models.split(',') if x.strip()]
    post(f"{crm_base()}/api/login", {"email":EMAIL,"password":PASS})
    lines=["| 序号 | 型号 | CRM结论 | lifeCycle | 依据(精确匹配条目) |",
           "|---|---|---|---|---|"]
    summ={'停产':0,'在产(量产)':0,'停产预警(在产中)':0,'无精确匹配':0}
    for i,raw in enumerate(items,1):
        if raw in skip:
            lines.append(f"| {i} | {raw} | (跳过) | - | - |"); continue
        concl,lc,detail=check_one(raw)
        lines.append(f"| {i} | {raw} | {concl} | {lc} | {detail} |")
        for k in summ:
            if concl.startswith(k): summ[k]+=1
    lines.append(f"\n### 统计: {summ}")
    out="\n".join(lines)
    print(out)
    open('crm_eol_result.md','w',encoding='utf-8').write(out)

if __name__=='__main__':
    try:
        main()
    except cfg.NotConfigured as e:
        sys.exit("配置缺失：%s" % e)
