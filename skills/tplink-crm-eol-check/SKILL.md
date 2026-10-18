---
name: tplink-crm-eol-check
description: 在 TP-LINK 销售 CRM（$CRM_BASE/crm）批量核查产品是否停产。当用户说"查一下这些型号停产没""CRM 里查生命周期""核对在产状态""哪些型号停了""lifeCycle 停产核查"时使用。读取 productModel 精确匹配后的 lifeCycle / lifeCycleMeaning 字段（PRODUCTION=在产、HALT=停产、HALT_WARNING=停产预警）；⚠️ 按用户既定口径，**停产与停产预警各自独立成状态、独立色标，不得合并**。
---

# TP-LINK CRM 停产核查工作流

跨对话复用的"批量查停产"技能。本对话（2026-09-16）已验证可用，固化如下。

## 适用场景
- 用户给了一批 TL- 型号（来自 Excel / 工程清单 / 直接粘贴），要确认哪些已停产。
- 需要区分"在产(量产) / 停产预警(在产中) / 停产 / 官网无此型号"。

## 前置条件
- **凭证**（跨对话复用，存于 user memory）：
  - 站点：`$CRM_BASE/crm`（内网域名，公网解析不到）
  - 账号：`you@example.com` / 密码：`YOUR_PASSWORD`
  - 登录：`POST /api/login`，表单 `email`+`password`，无验证码，返回 `SESSION` cookie。
- **网络**（2026-09-21 更新，两条路都实测过，脚本要兼容「有代理 / 无代理」两种）：
  1. 走本机 **Clash 代理 `http://127.0.0.1:7897`**（Windows 注册表 `ProxyEnable=0` 但端口在；audit-log 里历次成功记录都是这个端口）。
  2. 公司网络（有线/内网）在时，**直连 `$CRM_BASE:443` 也能通**，此时 `getproxies()` 返回空。
  - 写法：`PROXY = getproxies().get('https') or ''`，**PROXY 为空时不要挂 `ProxyHandler`**（挂空串会报 `URLError: no host given`）。
  - ⚠️ **Clash 没启动 + 不在公司网络 = CRM 完全不可达**（TCP 443 直接超时，`dangerouslyDisableSandbox` 也一样）。这时别改脚本、别反复重试，直接告诉用户"代理/内网没通"，等他恢复网络即可。
  - ⚠️ 若登录/查询返回 `502 Bad Gateway`：代理到 CRM 的上游临时故障，稍后重试。
- Python：用受管运行时 `~/.workbuddy\binaries\python\envs\default\Scripts\python.exe`（含 openpyxl，必要时 `pip install openpyxl`）。


## 关键字段与逻辑（已验证）
- 接口：`GET /api/line/findByPage?productModel=<模糊>&start=0&limit=100`，分页用 `start`/`limit`，总数看 `totalCounts`。
- `productModel` 过滤是**模糊匹配**（会返回同系列套装/件装/工业级等变体），**必须客户端按精确型号再筛**，否则会把"套装版停产"误判到本体。
- 生命周期字段：`lifeCycle`（枚举）+ `lifeCycleMeaning`（中文）：
  - `PRODUCTION` → 在产(量产)
  - `HALT` → 停产
  - `HALT_WARNING` → 停产预警(仍在产)
- 精确匹配：`norm(s)=re.sub(r'[\s\-]','',s).upper()`，用 `norm(pm)==norm(query)` 判定同型号。
- 一个型号可能有多条（不同 OU/区域/版本），只要任一精确匹配条目为 `HALT` 即判"停产"；否则取集合。

## ⚠️ 型号族聚合（2026-09-21 新增，摄像机/NVR 必须用）
**CRM 里安防摄像机是按「焦距后缀」建货号的**：PPT/清单写 `TL-IPC544EP-W`，CRM 实为
`TL-IPC544EP-W4 / -W6 / -W8 / -W12`（`-4/-6/-8/-12` 是焦距）。NVR 也常有 `-D4S`、`-L16PE` 之类。
直接做精确匹配会让整片摄像机线全部落到"无精确匹配"（实测 21/70 误判），看起来像停产，其实是命名维度不同。

**判定规则（三步）**
1. 先精确匹配，命中即用精确结果。
2. 未命中 → **族聚合**：取 fuzzy 结果中满足
   `norm(pm).startswith(norm(目标))` **且** 不以「同一批清单里其它更长型号」前缀开头的条目。
   - 这一步排除子型号串扰：查 `TL-IPC544EP` 时不能把 `TL-IPC544EP-W4` 算进来，因为 `TL-IPC544EP-W` 本身是另一个目标型号；查 `TL-IPC544EP-W` 时不能把 `TL-IPC544EP-WB4` 算进来。
   - 也顺手覆盖"套装/双只装/V1"等后缀（如 `TL-E201固定端&TL-E201轿厢端套装`、`TL-ZJ800&TL-K234`、`TL-IPC534SP-WB4 双只装`）。
3. 聚合结论：
   | 族内 lifeCycle 集合 | 结论 |
   |---|---|
   | 全 PRODUCTION | 在产(量产) |
   | 全 HALT | 停产 |
   | 全 HALT_WARNING | 停产预警(在产中) |
   | 含 HALT 且还有其它 | **部分停产**（必须把变体明细写进备注） |
   | PRODUCTION + HALT_WARNING | 部分停产预警 |
   | fuzzy 命中 0 条 | CRM无此型号（需人工确认，可能是型号笔误或未入价格手册） |

**备注列写法**：把族内变体逐条列出，例如
`CRM 部分货号已停产，其余在产/停产预警，按具体货号确认。TL-IPC544EP-4:停产; TL-IPC544EP-6:停产; TL-IPC544EP-8:停产预警; TL-IPC544EP-12:停产预警`

## ⚠️ 输出口径与色标（用户既定）
- **`停产`（HALT）与 `停产预警`（HALT_WARNING）必须分开，各自独立成状态、独立色标。**
  演变过程（别再反复横跳）：2026-09-22 上午用户曾要求"预警并入停产"，当日下午明确**回滚**为"还是分开"。
  → **默认按分开处理**；要合并必须用户当次明确说。
- 交付表状态集合：`在产(量产)` / `停产预警(在产中)` / `停产` / `部分停产` / `CRM无此型号` / `不适用（非型号条目）`。
  统计 sheet 里**必须有"停产预警"这一行/列**。
- 「部分停产」**始终独立**——含义是"部分货号停产、部分在产"，与预警是两回事。

### 配色（两张表必须一致，2026-09-22 定稿）
**填充范围 = 整个数据行（A 列到最后一列），不是只填状态列**（用户明确要求，便于一眼扫出停产行）。

| 状态 | 整行填充色 | 状态列字体色 | 加粗 |
|---|---|---|---|
| 在产(量产) / 在产 | `E2EFDA` 浅绿 | 默认黑 | 否 |
| 停产预警(在产中) / 停产预警 | `FFEB9C` 浅黄 | `9C6500` 深黄 | 是 |
| 停产 | `FFC7CE` 浅红 | `9C0006` 深红 | 是 |
| 部分停产 | `FCE4D6` 浅橙 | `C55A11` 深橙 | 是 |
| CRM无此型号 | `F2F2F2` 浅灰 | 默认黑 | 否 |
| 不适用（非型号条目） | 不着色 | 默认黑 | 否 |

- 字体色只在**状态列**上做强调，其余列保持默认黑字（浅底黑字可读性最好）。
- 仅填充不改边框：用 `PatternFill('solid', fgColor=…)` 逐格赋值，边框是独立属性不受影响；要清色用 `PatternFill(fill_type=None)`。
- ⚠️ **整行染色会覆盖「替代型号（在产）」列原有的 `FFF2CC` 独立黄底**（该列原设计靠黄底+深红加粗字突出）。整行统一后，改靠**深红加粗字体**保持突出。若用户要求保留该列独立黄底，则整行范围收缩为 A~G。

⚠️ **历史坑**：网络表（`TP-LINK小微ICT产品功能与在售状态.xlsx`）最初生成时**状态列完全没设填充色**，连 `font.color.rgb` 都是无效值（读取会报 `Values must be of type <class 'str'>`），只有安防表有色。用户会明确要求"像安防一样"。**新建/改表后必须回读校验每行的 `fill.fgColor.rgb` 集合与 `font.bold`**，别只看数据对不对。

⚠️ **改旧表状态口径时，统计 sheet 要跟着改**：删/加列后 `COUNTIF/COUNTIFS` 的表头引用（形如 `B$11`）会错位、合计行易写成 `SUM(A..)` 少一列，**必须回读校验公式与合计值**再交付。

⚠️ **回滚旧表时的正确姿势**：若表上已追加了「替代型号（在产）」列，**不要直接覆盖 `backup_before_rule.xlsx`**（备份里没有该列）。正确做法是**原地改状态值 + 备注回滚 + 重套色**，只把 **统计 sheet 用备份版全量覆盖**（删旧 sheet → `create_sheet(名, index)` → 逐格复制 `value` 与 `_style` → 复制列宽/行高/合并/冻结）。备注原文直接从备份对应行读回来，保证文案精确还原。

## 给停产型号补「替代型号（在产）」（2026-09-22 新增，投标场景常用）
用户会在交付表上追加一列，要求给每个停产型号找一个"规格相近的在产量产型号"。

### 关键字段：`seriesValue` 与 `productName`
CRM 记录里两个字段决定选型质量：
- **`seriesValue`** —— 产品系列名。安防摄像机主力系列取值就是 **`G/F系列IPC&NVR`**（用户口中的"G 系列/F 系列摄像头"即指此），NVR 同理；其余为 `400万像素IPC`、`无线IPC`、`4G IPC`、`T&X系列高速球机` 等。
- **`productName`** —— 官方规格全名，如 `400万PoE筒型音频双光全彩网络摄像机`。**选型靠它对齐规格**（像素/形态/光源/音频/警戒/PoE 全在里面）。
- 另外 `catalogPrice`（目录价）、`price`（成交价）也在同一接口返回，需要时报价。

### 拉全量池（一次拉够，别再逐型号查）
`GET /api/line/findByPage?productModel=TL-IPC&start=0&limit=100` 按**型号前缀**模糊拉全量并分页。
实测规模（2026-09）：TL-IPC 1549 条（在产 309）/ TL-NVR 128（44）/ TL-SP 55（34）/ TL-FW 15（10）/ TL-NFW 8（5）/ TL-ER 50（11）/ TL-SG 188（111）/ TL-SH 63（45）/ TL-NR 37（24）/ TL-R5 12（3）/ TL-XVR 8（2）。
→ 拉完落成 `{model: {life, name, series, ...}}` 字典，后续全部在本地做，不要再打 CRM。

### 选型三原则（顺序不可换）
1. **形态必须对齐**：枪机/筒机 ↔ 枪机，半球 ↔ 半球，球机 ↔ 球机，高速球 ↔ 高速球。形态错了整条建议就废（打分算法容易把"球机"推给"枪机"，必须硬约束）。
2. **像素就近**：优先同像素；无同像素时取最接近的一档并**在备注写明差了几档**。
3. **功能对齐**：双光全彩 / 暗夜全彩(AI-ISP) / 红外 / 全彩警戒 / 音频 / PoE 逐项比。**优先 G/F 系列**（当季主力，且用户明确偏好）。

### ⚠️ 替代型号三条铁律（2026-09-22 用网络 PPT 替换时踩出来，适用于所有品类）
1. **落盘前必须回 CRM 复核 `lifeCycle == 'PRODUCTION'`**，一个不通过就先改映射。（本次 9 个替代型号全部复核通过才写 PPT。）
2. **替代型号不能和"同一个画面/页面里已有的型号"撞车**。给 PPT 换型号时同一页 5 张卡常常已经把整个系列摆满了 —— 表格里写的替代款可能就躺在隔壁卡上。撞了就**另选同类在产款顶上**（用户明确认可这个处理方式），并把"表格替代 vs 实际采用"不一致的原因交代清楚。
   实例：TL-WVR1200G-4G 表格写 XVR3000G（撞第4卡）→ 改用 4G 同类的 TL-NR1200W-4G-SD；TL-XVR1800L 表格写 XVR3000G（撞）→ 改用 TL-XVR3000L；TL-SG6428 表格写 SH6428（撞）→ 改用 TL-SH6428F。
3. **绝不沿用前代型号的参数**。官网**不是每个型号都发布全部字段**：TL-XVR3000L 官网就**没有**「带机量 / 可管理AP数量 / 推荐最大带宽」三项 —— 这时只能写官网确实有的特性，不能把 XVR1800L 的 150/60/2Gbps 抄过去。
   同理，**原 PPT 的营销话术不等于新机型的参数**：原 FW6600/5600/7600 卡都写"最大可接入带宽数量64条宽带"，但 FW5900/FW5600E/NFW8600E 官网**一条宽带/线路描述都查不到** → 该整条删除。
   再如"双电源冗余设计"属过度表述：FW5900 / NFW8600E 官网明确"出厂自带一个电源，要形成 1+1 冗余电源组合需**额外选配** TL-PSM100-AC 电源" → 应写「支持1+1冗余电源（选配）」。

### ⚠️ 参数"降级"要如实报告，不要粉饰
换代常常是**降级**的。本次 TL-R5010PE-AC（带机量 300 / 可管理 AP 100）→ TL-R5009PE-AC（100 / 50），原因是 **2.5G PoE·AC 一体机在产只剩 TL-R5005PE-AC / TL-R5009PE-AC 两款**，R5406PE-AC 已是停产预警、R5408/R5408PEF/R5010PE-AC 全停，**没有 300 档的在产继任者**。
→ 遇到这种情况**照实写官网参数 + 明确告诉用户这是降级**，不要为了好看而沿用旧数字。

### 命名规律（读懂就能手工挑）
`TL-IPC` + `系列数字` + `形态字母` + `P(是否PoE)` + `-焦距/代数`
- 数字首位定形态：`5xx` 筒机/枪机、`4xx` 半球/吸顶、`6xx` 球机/云台、`7xxx/8xxx` 高速球。
- `F` = F 系列（如 534F/544F/544FP/445F），`G` = G 系列（如 538G/548G/435G/445G/548GP）。
- 末尾 `P` = PoE 款；`-W4/-W6/-W8/-W12` = 焦距 4/6/8/12mm，`-4/-6` = 定焦。
- `AI` 前缀 = 双光警戒款；`AT/WB` = 暗夜全彩(AI-ISP)；`X/XLH` = 变焦双目。

### ⚠️ 已知"无替代"的产品线
- **一体式太阳能供电系统**（TL-SP620H / SP630H / SP930H 等）**已整体退出**，在产只剩配件（电池包 TL-SP105H/SP115H、升压器 TL-SPB100W、级联器 TL-SPC212、防水箱 TL-SPWPB、支架 TL-SPZJ）。此类型号替代列写 **"无（产品线退出）"**，备注建议改用第三方供电或调整点位。
  - **如果用户接受换产品形态**：可以顶上「**太阳能监控套装**」（`TL-IPC…-S…` 系列）。挑型看 4G 命名：
    **`A4G` / `A4GY` = 外置卡槽**（可插自己的 4G 卡、**非终身免流**）；**`F4GE` = 终身免流**（内置卡）。
    用户说"要外置 4G 卡的"就是在指 `A4G/A4GY`。已被用户采纳的两款：
    `TL-IPC662XL-A4GY-S08C33`（600 万警戒全彩）、`TL-IPC6C2XLH-WB4GY-S16C83`（800 万可变像素暗夜全彩）。
- **「视频解码单元」整条产品线只剩框式板卡**：`TL-NVDU04`（1U 整机）停产，在产只有 `TL-NVDU7606S`（**框式解码板卡**）。
  ⚠️ 这是**产品形态变化**（整机 → 板卡），不是同形态换代，写替代列时要标注清楚。
- 无 500 万枪机的现役款（枪机在产只有 300/400/600 万），500 万停产机型要往 400 万或 600 万靠。

### 写入纪律
- 新列放**最后一列**，表头命名 `替代型号（在产）`；**「停产」和「停产预警」两种状态的行都要填值**（预警=即将停产的，投标同样要备选），其余行留空（补边框即可）；该列原本设计为醒目浅黄底 `FFF2CC` + 深红加粗字 `9C0006`，但**整行状态染色会覆盖这个黄底**，最终靠深红加粗字保持突出（顺序上：先写替代列 → 最后再统一整行套色）。
- **写入前必须程序化校验**：每个推荐型号在池里查到且 `lifeCycle == 'PRODUCTION'`，任何一条不通过就先修映射，别带着未在产型号交付。
- 网络表/安防表表头行位置不同（安防表头在第 1 行、网络表第 1 行是合并标题、表头在第 2 行），加列时先 unmerge 标题行再按新末列重新 merge，并同步扩展 `auto_filter.ref`。
- 需要给用户佐证时，**官网产品详情页 `https://www.tp-link.com.cn/m/product_<ID>.html` 是服务端渲染的、可直接抓规格**（例：TL-IPC548GP-W = `product_3749.html`）；官网首页与 `products.html` 是 JS 渲染，静态抓不到，别在那儿浪费时间。

### ⭐ 官网「规格参数」整表直抓（2026-09-22 新增，写参数必备）
同一个详情页的 **`?v=specification`** 变体才是宝地，**也是服务端渲染**：

```
https://www.tp-link.com.cn/product_<官网ID>.html?v=specification
```

- 它含完整 **「硬件规格」+「软件功能」/「性能参数」** 两张表：`端口`、`无线速率`、`最大带机量`、`最大并发连接数`、`应用识别吞吐量`、`IPS吞吐量`、`全威胁吞吐量`、`应用层吞吐量`、`整机最大PoE输出功率`、`可管理AP数量`、`推荐带宽`、`最大带宽` …… 写投标参数时**优先用这里的原文**。
- ⚠️ **`/m/product_<ID>.html` 没有这张表**，只有上方的营销特性条目。两个 URL 都要抓：营销条目给卡片文案，`?v=specification` 给硬参数。
- 找入口的办法：主页里搜 `<a href="/product_3613.html?v=specification#tag">规格参数</a>`。
- ⚠️ **别试图走 `/product/<ID>.html`、`_spec.html`、`apidetail/`、`api/product/`** —— 全部 404。只有 `/product_<ID>.html`（桌面版）可用。
- 同系列可能**共用一张参数表**（如 PoE 交换机页里同时列 9 口和 24 口的 PoE 功率）。取数时按「型号对应端口数」挑，别把 24 口机的 225W 写到 8 口机头上（SG2210PE 正确值是 120W，225W 属 SG2226P）。

### 官网产品图（无鉴权，可直接下）
```
https://media.tp-link.com.cn/webresource/www_data/products/800/<官网ID>/<N>.jpg    # N = 1..5，>5 即 404
```
另有详情页大图 `.../webresource/www_data/pc/detail/<ID>/<N>.jpg`（部分型号带 `?x-oss-process=image/interlace,1`）。**替换 PPT 里的产品图就用 800 目录那组**，白底，可直接抠透明。

### 白底图抠成透明 PNG（可复用）
`阈值 = 255 - min(R,G,B)` → 二值化 → `scipy.ndimage.label` 找**与画布边框连通**的背景块（避免把产品内部的白色面板也抠掉）→ 取反 + `binary_fill_holes` + `binary_opening` 去噪 → 边缘处按距离做软 alpha。
⚠️ 原 PPT 里导出的产品图**本来就是透明底 PNG**（RGBA，四角 alpha=0），不是黑底；用深色图片查看器看会误判成"黑底"。



## 嵌入脚本（可直接复用）
把下面脚本存为 `crm_eol.py`，用法：
- 从文本文件读型号（每行一个，支持 `TL-SF1005+`、`TL-MC311B20工业级`、`TL-FC1420/双电源/14槽` 这类带说明/括号/斜杠的，脚本会自动 extract 干净型号）：
  `python crm_eol.py --models models.txt`
- 或直接传型号串：`python crm_eol.py --models "TL-SG1005P,TL-WR842N"`
- 输出 markdown 表格到 stdout，并写 `crm_eol_result.md`。

```python
import urllib.request, urllib.parse, ssl, json, re, sys, time, argparse
from http.cookiejar import CookieJar

EMAIL="you@example.com"; PASS="YOUR_PASSWORD"
BASE="$CRM_BASE"
PROXY=(urllib.request.getproxies().get("https") or urllib.request.getproxies().get("http") or "")  # 端口每次会话变，动态读，勿写死
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
cj=CookieJar()
op=urllib.request.build_opener(urllib.request.ProxyHandler({'https':PROXY,'http':PROXY}),
                               urllib.request.HTTPSHandler(context=ctx),
                               urllib.request.HTTPCookieProcessor(cj))

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
        first=get(f"{BASE}/api/line/findByPage?productModel={urllib.parse.quote(model)}&start=0&limit=100")
    except Exception:
        return rows
    total=first.get('totalCounts',0); rows=list(first.get('results') or [])
    st=100
    while len(rows)<total and st<total+200:
        try:
            pg=get(f"{BASE}/api/line/findByPage?productModel={urllib.parse.quote(model)}&start={st}&limit=100")
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
    ap.add_argument('--skip', default='', help='跳过行号/型号，逗号分隔')
    a=ap.parse_args()
    skip=set(x.strip() for x in a.skip.split(',') if x.strip())
    if a.models.endswith('.txt') and __import__('os').isfile(a.models):
        items=[l.strip() for l in open(a.models,encoding='utf-8') if l.strip()]
    else:
        items=[x.strip() for x in a.models.split(',') if x.strip()]
    post(f"{BASE}/api/login", {"email":EMAIL,"password":PASS})
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
    main()
```

## 配套：从 pptx 批量取型号（2026-09-21 验证）
不依赖任何 Office 组件，直接 `zipfile` + `ElementTree` 解析 `ppt/slides/slideN.xml`：
- 遍历 `p:cSld/p:spTree`，**必须递归进入 `<p:grpSp>`**，否则组合（group）里的形状会被整块漏掉（实例：电梯摄像机 `TL-NAIPC4333P-S2.1` 整组在组合里）。
- 组合内子形状坐标要按 `chOff/chExt` 比例换算：`abs = grp_off + (child_off - chOff) * grp_ext/ch_ext`。幻灯片宽 13.33in（12192000EMU），坐标算出 >13.33 说明漏算缩放。
- 表格（`a:tbl`）按 `a:tr`/`a:tc` 逐行聚合，别用全局 `a:p` 平铺。
- 输出「形状文本 + 坐标」后，按 x 坐标把「型号框 ↔ 参数框」配对（同列中心 x 相差 <0.5in 即同列），能准确还原多栏页面。
- 型号清洗：`re.sub(r'[（(][^）)]*[）)]','',raw)` 去括号 → 按 `/+空格` 切分取 `TL` 开头的片段。注意 PPT 常写错形如 `TLNVD6001/6004/6012`（缺连字符），要把 6004/6012 补成独立型号再查。

## 配套：把 PPT 里停产型号批量换成在产款（2026-09-22 验证，投标常用）
- **网络 PPT**：完整成品脚本 `.TP-LINK小微ICT产品功能与在售状态.ref/edit_ppt.py`（幂等可重跑；另存新文件，原文件不动）。
- **安防 PPT**：`.TP-LINK小微ICT安防产品功能与在售状态.ref/edit_aw.py`（同上；内置 4 类 run 模板 + 段落级模板 + `IMGADJ` 图片微调后门，**做同类替换直接以它为基线**）。

### 安防 PPT 已知的行内强调规律（改文案时照抄）
- 相机参数卡：`400万`/`500万`/`600万` 像素数 → **`h`（红加粗）**；`2560*1440@25fps`、`1/3英寸`、`IP66`/`IP67` → **`b`（只加粗黑）**；
  `H.265+` / `Super H.265+` → **`h`**；`焦距` 后面那串 `4、6、8、12mm` → **`h`**；`360°`/`30倍`/`300个`/`150m`/`4mm` → **`h`**；
  `256GB`/`6颗`/`2颗` 在球机长焦卡（s8/16）里是 **`b`**。
- NVR 卡（s20）有**两套颜色语义**，别混：
  **红加粗 `h`** = 接入带宽 / 接入路数 / AIPC / 存储容量 / RAID / AI / H.265+；
  **黑加粗 `b`** = SATA 口数 / 解码路数 / HDMI·4K / 预览回放路数。
- s9 有一处 **`r`（仅红不加粗）**：`支持双向语音，声光报警`。

### 运行环境（别用错解释器）
- 主解释器 `...\python\versions\3.13.12\python.exe` **没有 python-pptx**。
- 用 venv：`~/.workbuddy\binaries\python\envs\default\Scripts\python.exe`（含 python-pptx 1.0.2 / pillow / numpy / scipy）。

### 五个必做动作
1. **文本框坐标要用递归组合换算**
   `abs = 父off + (子off − chOff) × (父ext / chExt)`，且 `chOff` 必须与 `off/ext` 读自**同一个 `xfrm`**，父的 chExt 要往下传。写错了会出现 y=−31cm 这种荒谬值。
2. **改文案要守"长度预算"**
   先量原文本框的字符宽度（CJK=1，其余=0.55）作为 `BUDGET`，新文案应当 ≤ 原预算 —— 文本框是固定尺寸，超了就溢出。每次跑完打印 `bad=N`。
   实例：s11 防火墙卡预算 87.0、s14 交换机卡 108.6，调文案就是往这两个数上贴。
   **关于 `bad` 的目标值（2026-09-22 修订）**：网络轮做到了 `bad=0`；安防轮最终 `bad=3`，其中 **3 个都是误报/有意为之**，所以**不能机械地追求 `bad=0`，要逐条看清是什么再决定**：
   - `(26,'12')` 总超 10.5 → 该框是**两段独立换行**的型号名，`maxseg` 才是有效指标（13.8 ≤ 预算 17.3），"总宽"对它无意义；
   - `(22,'13')` 段超 0.6 → 新型号 `TL-NVDU7606S` 是拉丁串，`cw()` 按 0.55/字符估算**不随字号缩放**，而预算值取自 14pt 的 `视频解码单元`；实际渲染一行放得下（**看渲染图确认**）；
   - `(9,'10')` 段数超 (11>10) → 我**故意**把原文的一行拆回两行（`支持SD卡存储，最大512G` + `焦距2.8/4/6mm可选`）以避免末行折行，渲染后行数比原文还少。
   → 结论：**预算检查是"提醒"不是"闸门"**；凡报超预算，一律回到渲染图上看是否真的溢出/折行/压字。
   多段框的"总宽"只作粗筛（`tot > budget*1.2` 才提示），单行框和型号框分别用零容差和 1.6 字符容差。
3. **改文案/改段落要保**「**段落属性 + 行内 run 强调**」**两层格式**（2026-09-22 修订，旧版只写"三套 rPr"是不够的）
   - **段落层**：克隆原段落的 `pPr`（含 `buChar` 项目符号、缩进、行距、对齐）。改完用 `pPr` 的 XML 逐段比对（见「三道硬校验」）。
   - **run 层**：原 PPT 的强调**不是整段级，而是行内 run 级**。必须从**被改的那个文本框自己**里提取 **4 类** run 模板：
     | 代号 | 含义 | 判定 |
     |---|---|---|
     | `n` | 普通 | 首个 `b != "1"` 的 run |
     | `b` | 加粗黑 | `b == "1"` 且 `srgbClr != C00000` |
     | `h` | 加粗深红 | `b == "1"` 且 `srgbClr == C00000` |
     | `r` | 仅红不加粗 | `b != "1"` 且 `srgbClr == C00000` |
     缺哪类就从 `n` 派生（设 `b="1"` / 追加 `solidFill/srgbClr val="C00000"`，注意按 CT_TextCharacterProperties 的 schema 顺序插在 `latin/ea/cs` 之前）。
     文案结构写成 `段落 = [(文本, 样式), …]`，例：`[('400万','h'),('像素','n')]`、`[('支持Super ','n'),('H.265+','h'),('智能编码','n')]`。
   - **必须按「段落下标」取模板，不能整框共用一套**：同一个框里各段字号可能不同
     （实例：安防 s22 型号框里 `解码器`/`视频解码单元` 是 **sz=1400**，型号 `TL-NVD…` 是 **sz=1200**）。
     整框共用会把新型号放大到 14pt → 撑宽折行 → `TL-NVDU7606S` 被拆成 `TL-` / `NVDU7606S`，文字还会上移压到产品图上。
     做法：`ptl = [para_templates(p) for p in 原有段落]`，第 i 段用 `ptl[min(i, len-1)]`。
   - **先诊断**：写个一次性的 dump 脚本，打印「每个目标框 · 每段 · 首个 run 的 `sz`」，一眼看出哪些框是混排字号。
   - ⚠️ **踩过的坑**：`use = tpl.get(kind) or tpl['n']` —— `or` 会对 lxml 元素做真值判断，**无子节点的元素是 falsy**，
     会把 `b`/`h` 模板**静默**退化成普通样式（红色加粗全丢）。必须写成 `use = tpl.get(kind)` → `if use is None`。
4. **换图别覆盖原 media**：往包里**新增** `image290.png…` 这类新 part，再把形状的 rId 指过去。原 294 张图一张不动，只用 `zipfile` 比 sha1 就能证明"只动了该动的"。
   落位要匹配原图"可见物体包围盒的宽高比"，再按同位置贴到同尺寸画布上，否则替换后大小/位置会漂。
   - **留一个微调后门**（本轮新增，很值）：`IMGADJ = {(slide, path): (缩放, dx, dy)}`，`dx/dy` 是画布宽/高的比例。
     只用"贴合原包围盒"一招搞不定**旋转过的图片框**：安防 s22 的板卡框 `rot=29°` 且夹在两行说明之间，
     原图 alpha 几乎满幅、产品偏左上，而抠好的新图默认**居中**放 → 整体右下移，正好压在「解码板卡」文字上。
     设 `(0.80, -0.03, -0.13)` 后干净落位。
   - **优先换"更贴合原图的取图角度"再谈缩放**：同一型号官网通常有 5 张图、角度/比例差别很大。
     安防 s22 原来选 n=1（斜视角含支架，宽高比 2.17），换成 **n=3（接近原图的扁平视角，宽高比 3.09）** 后明显更协调。
     换图前先拼个 contact sheet 扫一眼五张图再选。
   - ⚠️ **别用 PIL 自己模拟 PowerPoint 的旋转贴合来预演** —— 我试过，位置对不上（丢失了原图在框内的偏移信息），
     会得出错误结论。直接改脚本 → 真渲染 → 看图，最快。
5. **改完必须看**：用 PowerPoint COM 把改动页导成 PNG 对照
   ```powershell
   $ppt = New-Object -ComObject PowerPoint.Application
   $pres = $ppt.Presentations.Open($src, $true, $false, $false)   # 只读打开
   $pres.Slides.Item(4).Export($png, 'PNG', 1920, 1080)
   $pres.Close(); $ppt.Quit()
   ```
   ⚠️ **必须内联执行**（整段塞进一次 PowerShell 调用），**写成 `.ps1` 文件跑不了** —— 返回码 1、无输出、无日志。想留痕就把结果 `Out-File` 到 log 再读。

### 交付前的三道硬校验
- **逐形状文本 diff**（原 vs 新）：输出"页数 / 形状键数 / 差异形状数 + 涉及页码"。合格的形态是：差异形状数与预期改动数吻合，且**涉及页码集合等于预期页集合**。
  - 实例①（网络轮）：33 页 / 894 形状两边一致，差异 23 处，全在 4/5/11/14/15 页。
  - 实例②（安防轮）：差异 35 个形状，页码集合正好 `{5,6,7,8,9,10,20,22,26}`，未波及其它页。
- **形状几何 diff**：比对每个形状的 `left/top/width/height`，**必须"无改动"**。这条能证明"没有靠挪框/改框来塞文字"。
  （若确实需要挪框，就要在报告里明确说明并给出理由。）
- **media sha1 diff**：确认只有预期数量的图片发生变化，且是"**新增 part**"而非"覆盖原 part" / "删除原 part"。
  - 实例②：原有 262 个媒体部件 —— **改写 0 / 删除 0**，新增 17 个。
- **段落属性 `pPr` 保真校验**（2026-09-22 新增，强烈建议）：对本轮改过的每个文本框，逐段比对 `pPr` 的 XML。
  除"新增段落（复制自末段，应与原末段一致）"外应**完全一致**。
  这条专门兜住**缩略图上看不出来的东西** —— 项目符号 `buChar`、缩进、行距、对齐。我这次一度在缩略图上误判"项目符号丢了"，
  靠这条 XML 校验才确认没丢（是缩略图分辨率造成的错觉）。

### ⚠️ 头号操作坑
**同一条消息里对同一个文件发两个 Edit 会互相覆盖**：两个 Edit 都基于同一份旧内容，后写的赢，前一个**静默丢失**，而工具**仍然报"成功"**。
→ 改同一个文件必须**串行**，一条改完确认了再发下一条。

## 踩坑清单（更新此技能时保留）
1. **【头号坑】型号族**：摄像机型号在 CRM 里带焦距后缀，直接精确匹配会整片误判"查无此型号"。见上面「型号族聚合」。
2. **路径**：CRM API 根是 `$CRM_BASE/api/`，不是 `/crm/api/`（后者 404）。
3. **模糊匹配陷阱**：`productModel` 是模糊的。例：`TL-WDR7650千兆易展版` 本体是 `PRODUCTION`，但同系列"套装"版本是 `HALT`——不精确匹配就会误报停产。务必 `norm(pm)==norm(query)` 或走族聚合规则再判定。
4. **代理空串**：`ProxyHandler({'https':''})` 会抛 `URLError: no host given`；无代理时不要挂 ProxyHandler。
5. **502 处理**：登录/查询 502 = 代理上游抖动或 CRM 临时不可达，脚本内置 4 次退避重试；仍失败就稍后重跑，别改脚本。
6. **网络不通时别硬试**：Clash 未启动且不在公司网络时，443 直接超时；`dangerouslyDisableSandbox` 也救不了。直接回报用户。
7. **型号清洗**：表内型号常带 `（16口）`/`工业级`/`免驱版`/斜杠套装，先去括号、再按 `/+空格` 切分为多个 TL- 候选去查。
8. **凭证明文**：存于本地 memory，仅本机可读；按用户要求跨对话复用，建议定期改密兜底。
9. **交付前回读校验**：改完判定/备注逻辑后，务必回读 xlsx 抽查「备注列非空」，避免旧代码分支残留导致备注为空。
10. **【踩过】同一条消息里并行 Edit 同一个文件**：两个 Edit 都基于同一份旧内容，后写的覆盖前一个，前一次改动**静默丢失**而工具仍报"成功"。改同一文件一律串行。
11. **【踩过】删临时文件被沙箱拦**：批量 `Remove-Item` / `shutil.rmtree` 会弹确认并被用户拒绝（拒绝后**不要重试等价删法**）。用户可能想先看效果 —— 那就改成"只做标记不删除"：生成一份 `_临时文件标记_可删.md` 清单，列清「可删（`_*` 临时产物）」与「必须保留（交付物 + `.ref*` + `.TP-LINK*.ref` + 源渲染图 + `.workbuddy/`）」，等用户发话再删。
12. **【踩过】PowerShell 跑 `.ps1` 文件静默失败**：返回码 1、无输出、无日志。渲染类脚本一律**内联执行**整段命令。
13. **【踩过】Bash 工具 coreutils 可能整片挂掉**（`ls`/`cd`/`dirname`/`head` 全 `command not found`）：别浪费时间排查，改成「python 写文件 + Read 读回」取输出。

## 输出约定
- 对话里返回 markdown 表格即可，默认不落盘；若用户要文件再写 `crm_eol_result.md`。
- "无精确匹配"≠停产，需人工确认（可能型号笔误/非标/已彻底下架页面）。
