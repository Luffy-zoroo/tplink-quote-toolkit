# TP-LINK 销售 CRM —— 登录与取价接口

> 这部分是**稳定的技术事实**（接口地址、字段名、取数方式），可以长期复用。
> **不包含**任何"该给几折"的商务规则——折扣口径每次由用户当场指定，见 SKILL.md。

## 站点与凭证

| 项 | 值 |
|---|---|
| 站点 | `$CRM_BASE/crm` |
| 域名性质 | **内网域名**，公网 DNS 解析不到；须走本机代理 / 内网 |
| 账号 | `you@example.com` |
| 密码 | 见 `~/.workbuddy/MEMORY.md`（不写进本技能） |
| 身份 | 刘泽义 / 南昌办，带 `VIEW_BUSINESS_DISCOUNT`（业务折扣查看权） |

沙箱无法装浏览器（公网被代理白名单拦截），所以用 **Python 标准库 urllib 直连**，
不要试图用 playwright / selenium。

## 登录

```
POST $CRM_BASE/api/login
Content-Type: application/x-www-form-urlencoded; charset=UTF-8

email=<账号>&password=<密码>
```

- **无验证码**，登录成功后返回 `SESSION` cookie。
- 必须用 `http.cookiejar.CookieJar()` + `HTTPCookieProcessor` 保存会话。
- 走代理：`urllib.request.ProxyHandler(getproxies())`。
- 返回体里可读到 `sessionInfo.chineseName` / `sessionInfo.officeName` 确认身份。
- **400 = 凭证错**（不是网络问题）。

参考实现（脚本内 `crm_login`）：

```python
def crm_login(email, pwd):
    import urllib.request, urllib.parse, http.cookiejar
    from urllib.request import getproxies
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler(getproxies()),
        urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [('User-Agent', 'Mozilla/5.0'), ('Accept', 'application/json')]
    data = urllib.parse.urlencode({'email': email, 'password': pwd}).encode()
    req = urllib.request.Request(
        '$CRM_BASE/api/login', data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'})
    j = json.loads(op.open(req, timeout=60).read().decode('utf-8'))
    return op, j
```

## 价格手册 / 标准价 / 业务折扣

```
GET $CRM_BASE/api/line/findByPage?productModel=<型号>&start=0&limit=200
```

### 返回体结构

```json
{ "results": [ {...}, {...} ], "totalCounts": 2 }
```

> ⚠️ 取数键是 **`results`**，不是 `data` / `items` / `list`。

### 关键字段

| 字段 | 含义 | 用法 |
|---|---|---|
| `productModel` | 型号 | **模糊匹配**！返回结果里可能有别的型号，必须客户端再按精确型号筛一遍 |
| `price` | **标准价（面价）** | 不打折的型号直接用它；可能带 .5 小数 |
| `businessDiscount` | 业务折扣率 | 可能为 `null`（无折扣） |
| `seriesValue` | 系列值 | 如 `项目机型` / `域联产品` / `双频AP` / `其他附件`；可用来筛"哪些有折扣资格" |
| `lifeCycle` | 生命周期 | 查停产用（见 `tplink-crm-eol-check` 技能） |

### 注意事项

1. **`productModel` 是模糊匹配**，同一个字符串会返回一批近似型号。
   必须 `str(x['productModel']).strip().upper() == model.upper()` 精确筛。
2. **同一型号可能返回多条**（不同 OU / 区域 / 版本）。
   不要自己挑一条——**全部列出来交用户裁决**。
3. 型号名可能**本身带 `/`**（如 `TL-AP1206GC-PoE/DC易展版`）。
   查询时要**原样传**，不要规范化成连字符，否则查不到。
4. `businessDiscount` 有值 ≠ 一定打折——**打不打、打几折由用户本次指定**。

参考实现（脚本内 `crm_price`）：

```python
def crm_price(op, model):
    p = urllib.parse.urlencode({'productModel': model, 'start': 0, 'limit': 200})
    url = '$CRM_BASE/api/line/findByPage?' + p
    j = json.loads(op.open(url, timeout=60).read().decode('utf-8'))
    rows = j.get('results') or j.get('data') or []
    return [x for x in rows
            if str(x.get('productModel', '')).strip().upper() == model.upper()]
```

## 备用价格来源

CRM 多价格 / 查不到时，可参考 `$DEALER_BASE/product`。
⚠️ 该站**需手机号 + 验证码登录**，暂未接入，只能用户手动兜底。
