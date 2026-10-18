---
name: tplink-crm-readonly
summary: TP-LINK 合作伙伴销售 CRM ($CRM_BASE) 只读查询 + 官网 (www.tp-link.com.cn) 规格参数兜底——API/直连，无需浏览器
description: 当用户要在 TP-LINK 销售 CRM（$CRM_BASE/crm）里查询产品价格手册/标准价、业务折扣等只读数据，或在控标库查不到时去官网（www.tp-link.com.cn）拉规格参数，用本技能。沙箱无法装浏览器（公网被代理白名单拦截），改用 Python 标准库直连 HTTP。涵盖 CRM 登录/会话/接口、官网搜索/产品页/规格表提取、工程清单批量取数流程。价格**查到即自动落盘本地备份**（price_cache.py），CRM 不可达时可离线查价。
agent_created: true
---

# TP-LINK CRM 只读查询（API 直连，无需浏览器）

## 适用场景
- 查产品价格手册（标准价 / 阶梯价）、业务折扣、折扣产品、价格增量等只读数据。
- 沙箱环境无法 `npm install -g agent-browser`（公网被本地代理 127.0.0.1:7897 白名单拦截，仅 TP-LINK 内网域名可达），所以**不要用浏览器自动化**，改用 Python 标准库。

## 关键事实（已实测 2026-08-27）
- 站点 `$CRM_BASE` 是**内网域名**（公网 DNS 解析不到），但本地代理可通，沙箱能访问。
- 登录接口：`POST $CRM_BASE/api/login`
  - 表单格式 `application/x-www-form-urlencoded`，参数 `email` + `password`（实测字段名即 email/password，无验证码/无额外必填项）。
  - **基础登录无验证码/MFA**。
  - 成功返回 200 + JSON `sessionInfo`（含 userId/userName/chineseName/officeName/permissions），并在响应头 `Set-Cookie: SESSION=...`。
  - 接口上下文是 `/api`，不是根路径（根 `/login` 返回 404）。
- 所有数据接口在 `/api/` 下，多为 `GET`，分页参数 `start`/`limit`（部分用 `page`）。

## 工作流程
1. **登录并建会话**：用 `http.cookiejar.CookieJar` + `urllib.request.build_opener(ProxyHandler(getproxies()), HTTPCookieProcessor(cj))`。`getproxies()` 自动读取 `HTTP_PROXY/HTTPS_PROXY` 环境变量（本地代理）。
2. **带会话查询**：后续 GET 自动带 `SESSION` cookie。
3. **翻接口**：若不知某模块接口，抓 `$CRM_BASE/crm/app.js`（约 5.5MB，Sencha ExtJS 压缩单文件），用正则找 `Ext.cmd.derive('Tpcrm.store.<模块>...'` 及其 `proxy:{url:'/<实体>/<方法>'}` 即可定位端点。模型字段用 `name:'<字段>'` 找。

## 已知可用端点（GET，带 SESSION cookie）
- `/api/line/findByPage` —— **价格手册条目 / 标准价 / 业务折扣**。⚠️ 返回体结构为 `{"results":[...], "totalCounts":N}`，取数键是 **`results`**（不是 data/items/list，2026-09-18 实测）。字段含：`productModel, productName, brandValue, seriesValue, price(标准价), ladderPrice, lineInfo(如 "0~9999999:￥161"), startQty, endQty, lineCode, valid, businessDiscount(业务折扣率, 字符串如 "0.39"), catalogPrice, wholesalePrice`。`seriesValue='项目机型'` 表示该条目带业务折扣（非项目机型 businessDiscount 多为空或 1）。⚠️ `productModel` 过滤是**模糊匹配**（查 TL-NAC500 会带出 TL-NAC5000S），客户端须按 `productModel` 精确相等再筛。totalCounts 约 8283。
- `/api/discountProduct/findPage` —— 折扣产品列表（字段偏产品主数据，业务折扣率需进详情/另查）。
- `/api/discountProduct/findAllProductPage` —— 同上全量（缺参会 500，需带参数）。
- `/api/customer/findPriceListIncrement` —— 客户价格增量。
- `/api/line/findShortLineByPage` —— 价格手册条目的简版。

## 💾 取价必须「查一次、备份一次」（2026-10-13 用户新规，**推翻「不做本地保留」**）

- **旧规则（作废）**：价格每次查 CRM、**不做本地保留**。
- **新规则**：**每次仍然必须查 CRM**（不信任历史值），**但查到后必须落盘一份本地 JSON 备份**，
  方便 CRM 无法访问时查询。**用户明确：不用再考虑安全问题。**

| 项 | 值 |
|---|---|
| 备份文件（固定） | `<控标数据目录>\tplink_crm_price_backup.json` |
| 工具 | 本技能 `scripts/price_cache.py`（复用同目录 `crm_query.py` 登录/查询） |

```bash
python price_cache.py get TL-A TL-B     # 联机优先：查 CRM → 自动落盘；CRM 不通 → 自动读备份
python price_cache.py --offline TL-A    # 强制只读备份（绝不联网）
python price_cache.py save TL-A TL-B    # 只刷新落盘，不打印明细
python price_cache.py stats             # 备份概况（收录数 / stale / fuzzy / 系列分布）
python price_cache.py --path            # 打印备份文件路径
```

- 每个型号存：`price`(标准价) / `catalogPrice` / `wholesalePrice` / `businessDiscount` /
  `seriesValue` / `lifeCycle` / `unit` / `ladderPrice` / `lineInfo` / `valid` / `fetchedAt`。
- **只增不删**：本次查不到的型号保留旧记录并标 `stale=true`，**不覆盖成空**。
- 精确命中优先；只有精确查不到才给模糊候选并标 `match=fuzzy`（**须人工确认**）。
- ⚠️ 降级读备份时**必须声明「这是本地备份，取数时间 X，非实时价」**，别默默用旧值。
- ⚠️ 备份**只解决"CRM 查不到"**，**不改变「折扣口径每次问用户」**这条铁律。

### 🔐 安全类「多合一特征库」授权 SKU 要查官网，不要猜（2026-10-13）

防火墙与上网行为管理**共用同一个多合一特征库**，但 SKU 分两支，**由产品自己的规格决定**：

| SKU | 年费 | 平台特征 |
|---|---|---|
| `TL-FW-LIS-ALL` | 200 元/年 | 内存 ≤ 2GB DDR4 |
| `TL-FW-LIS-ALL-E` | 800 元/年 | 内存 ≥ 4GB DDR4 |

**权威来源 = 官网产品规格里 `License授权（<SKU>，多合一）` 分组名**（SKU 直接写在标题里）：
`https://www.tp-link.com.cn/publicservice/productinfo?brand=TPLINK&productModel=<officialRelateId>`
→ 解析 `specification` 字段找该分组。

**一键查**（映射表缓存在 `tplink_license_map.json`，离线秒查）：

```bash
cd "<控标数据目录>\dealer-tool"
python license.py TL-NASG6003 TL-FW5800 --price   # 查 SKU + 年费
python license.py --all                           # 全量刷新安全类
python license.py --list                          # 离线看映射表
```

- **必须逐型号查**：`TL-FW5800`(2GB)=200 而 `TL-FW5900`(4GB)=800；`NASG5003/5005`=200 而 `NASG5007/6003/6007`=800。
- 官网分组名没写 SKU（`TL-NR4914` 系列）→ 按内存档推测并**标注待确认**，别当结论。
- 无 License 授权分组 = 不支持多合一（`TL-NR510-2C-S`、`SAR500G`）。

## 折扣计算规则（投标算价，2026-08-27 / 2026-09-18 修订）
- **业务底折价 = 标准价 × 业务折扣率**（仅 `seriesValue='项目机型'` 有业务折扣）。
- **业务底折率 → 片区折扣率 → 总部折扣率** 对照表：
  - 0.45 → 片区 0.43 → 总部 0.40
  - 0.43 → 片区 0.40 → 总部 0.40
  - 0.39 → 片区 0.34 → 总部 0.32
  - 0.32 → 片区 0.27 → 总部 0.25
- ⚠️ **【2026-09-18 制定 / 2026-09-22、2026-10-13 修订】到底用片区还是总部，按产品线分**：
  - **监控类 → 用片区折扣率**（IPC/枪机/半球/高速球/NVR/AI硬盘录像机/存储服务器/高清视频解码器/框式解码矩阵/拼接屏/监视器/流媒体服务器/网络控制键盘/TUMS管理一体机/视频综合管理平台 等）
  - **网络类 → 用片区折扣率**（AP/交换机/路由/AC/行为管理。2026-09-22 用户口径：「**监控和网络用片区折扣，门禁和停车用总部折扣**」，网络被划进片区）
  - **其他类 → 用总部折扣率**（门禁/考勤/梯控/道闸/人行闸）
  - 实测验证：TL-TUMS2500 48000×0.27=12960、TL-TUMS2700 64000×0.27=17280、TL-NIPC3236-440A 37500×0.34=12750（监控=片区 ✓）；
    TL-KBG300-R 4200×0.40=1680、TL-EC100 2350×0.32=752、TL-NK2000 7500×0.40=3000（门禁等=总部 ✓）；
    **网络=片区**（2026-10-13 由 `0914无线网络设备清单` 核价**逐项验证吻合**）：TL-S7803E 26400×0.27=7128、
    TL-SH8630-LPU 18000×0.27=4860、TL-NASG6003 43750×0.34=14875（另 +800 授权）
  - 判产品线时**不要只信控标库 category**：库里命名不规范（如 TL-VS7500 存为「TL-VS7500（含TL-NVDU7606S业务板卡）」、TL-NVD6004C 根本不在库里）。
    推荐三级兜底：① 控标库精确型号 → ② 控标库前缀/包含模糊匹配 → ③ 表格自身系统分区（如「三、视频监控系统」区间内的行一律按监控）。
- 片区折扣偶有"上两个点"（+0.02）变体，**非固定规则**，遇到再与用户确认。
- 用法：取型号 `businessDiscount` 作业务底折率 → 按产品线查片区或总部折扣率 → × `price`(标准价) 得折扣价。业务折扣率若不在表内四档，需向用户确认归哪档或是否线性。

## 报价对外展示与价格取整规则（硬性，2026-08-28 用户规定，所有折扣工作流必须遵守）
- **不对外展示折扣**：产出 Excel 等文件里**不写任何折扣明细**（业务/片区/总部折扣率、标准价对照等），备注列不要暴露；价格列标题用中性"单价(元)"，**不要带"折扣"字样**。控标参数照常填。
- **价格无小数、向上取整到 0/2/5**：最终展示价不得有小数；有小数则向上取整到个位 0/2/5 的整数（212.85→215，331.10→332）。本身整数则保持（如 1326 保持）。无折扣直接取面价；用户"加几个点"产生小数同样处理。
- 实现：
  ```python
  import math
  def clean_price(v):
      f=float(v)
      if abs(f-round(f))<1e-9: return int(round(f))
      n=math.ceil(f)
      while n%10 not in (0,2,5): n+=1
      return n
  ```


## 清单核价（用户给一份已填好型号+价格的单子，让"核对价格"）2026-09-20

### 🎯 核价的判定基准（2026-10-08 用户明确，最高优先级）
- **核对时统一按「总部折扣率」算，只要清单价不低于总部价就算通过** —— 核心原则是**卖方不能亏**。
- **不要**拿片区档去比，更**不要**因为"清单价比片区价低"就报差异（会大量误报，实测一次误报 5 行）。
- 片区折扣率仍然保留在下面的对照表里，但**核价场景默认只用总部档**；只有当用户当次明确说"按片区核"才用片区。
- 高于总部价的行：**不算错**（只是赚多），最多提示"高于总部价 XX 元"供其自行判断。
- 低于总部价的行：**才是真正要看的问题**，逐行列出并说明差多少。
- ⚠️ 若某项目"监控类也是走总部档"属**项目特例**，**不要据此去改上面的产品线分档规则**（用户 2026-10-08 明确：那是特例，规则不动）。

用现成脚本取数（凭据自动从 `~/.workbuddy/MEMORY.md` 读，也可用 `--email/--password` 或环境变量 `TP_CRM_EMAIL/TP_CRM_PASSWORD`）：

```bash
<PY> <SKILL_DIR>/scripts/crm_query.py TL-KDP710-I TL-TUMS1500 ...   # 精确型号（自动按精确值再筛）
<PY> <SKILL_DIR>/scripts/crm_query.py --like 授权                   # 模糊搜索，找 SKU 用
<PY> <SKILL_DIR>/scripts/crm_query.py --fields TL-XXX               # 打印返回字段
```

流程：逐行取 CRM `price` + `businessDiscount` → 按产品线选片区/总部折扣率 → 与清单单价对比 → 列出差异。

### 🚨 核价前必做第 0 步：先判断「型号栏是不是塞了多件」（2026-10-08 踩坑，代价很大）

**坑**：清单里**一个价格单元格常常对应型号栏里的多个型号**（套装 / 主机+配件 / 收发器成对）。
如果不逐件拆开、只拿型号栏里**第一个**型号去比价，会算出巨额假差异。

**实测（进贤县医科园邻里中心项目，停车管理系统第 1 项「快速道闸」）**：
- 型号栏写的是 **三件**：`TL-PMG440-T4.5`（道闸套装）+ `TL-PMR300`（雷达）+ `TL-VD100`（车检），
  备注也明写「包含防砸雷达和地感线圈」；单价 2200。
- 只算道闸本体：`3930 × 0.40 = 1572` → 误判"高报 +628 元/台、共 +2512 元"。
- 三件合计再算：`(3930+1000+550) × 0.40 = 2192` → **与清单 2200 只差 8 元，完全正常**。
- → 因为漏看型号栏的另外两件，**报出了一个完全不存在的大额风险点**。

**判定规则（逐行机械执行，别靠肉眼）**：
1. 把型号单元格原文取出，**按 `换行 / "/" / "+" / "&" / "，"` 切分**，统计其中以 `TL` 开头的片段数；
2. **`TL` 片段 ≥ 2 个 → 该行是多件**，必须：逐件查 `price` + `businessDiscount` → **各件折后价求和** → 再与清单单价比；
3. 切分时要处理这些真实写法：
   - 换行分隔：`TL-IPC538GP-4\nTL-ZJ110`（枪机+支架）
   - 中文顿号：`TL-PMR300，TL-VD100`
   - 斜杠：`TL-FC311A-3/TL-FC314B-3`（收发器 A/B 成对，**注意 A 端和 B 端是两个不同货号、两个价格**）
   - 加号：`TL-S7803E+TL-SH8630F-LPU+TL-SH8630-LPU`（框式交换机主机+业务板卡，板卡各算一块）
   - `&`：`TL-E201固定端&TL-E201轿厢端套装`（**是 CRM 里一个完整货号，不是两件**，别误拆）
4. **反查校验**：若单件×折扣率与清单价差得多（比如 20% 以上），**先怀疑漏拆多件**，再下"价格有误"的结论。
   技巧：`清单价 ÷ 所有件标准价之和` 若落在 0.25~0.45，基本就是"多件合计×正常折扣"，不是错价。
5. 型号栏里的**非型号文字**（`（16口）`、`工业级`、`免驱版`、`一光四电`）要先去括号 / 忽略，别当型号查。

**差异识别的三个高频模式**（实测三联四联概算表）：
1. **`清单价 = 应付折扣价 × 1.05`** → 疑似该行**加了 5 个点**。一批表里常只有个别行这样，要和用户确认是有意加价还是笔误。
   实测三处都精确命中：350×0.40×1.05=147、31250×0.32×1.05=10500、4400×0.40×1.05=1928.85(取 1848)+77=1925。
2. **阶梯价取错档**：CRM 里同一型号会有多条 `lineInfo` 档位记录，**只保留 `valid=True` 的那条**（其余是历史档）。
   实测 MSL10CP-M：valid 记录为 `0~40:￥92` 与 `40~9999999:￥89` 两条；数量 122 台应取 **89**，清单按 92 报就偏高。
3. **面价取整**：无业务折扣的型号直接取 `price`，再按 0/2/5 向上取整（484.5→485、221.5→222）。清单若写成 490 / 223 就是没取整。

**`businessDiscount = 0.40` 这一档**：常用对照表（0.45/0.43/0.39/0.32）里没有，**大量 TUMS 授权类 SKU 都是 0.40**（见下表）。
按"≥0.40 归总部 0.40 档"推（即 0.40×0.40），**报数前必须让用户确认**，不要当既定规则。

**TUMS / 授权类 SKU 速查**（标准价，bd 除注明外均为 0.40）：

| 型号 | 名称 | 标准价 |
|---|---|---|
| `TL-TUMS-P-LIS` | TUMS 停车管理扩展点位授权 | 2500 / 路 |
| `TL-TUMS-A-LIS` | TUMS 门禁管理扩展点位授权 | 150 / 点 |
| `TL-TUMS-V-LIS` | TUMS 安防管理扩展点位授权 | 100 / 点 |
| `TL-TUMS-IMS-LIS` | TUMS 对讲管理扩展点位授权 | 50 / 点 |
| `TL-TUMS-LMS-LIS` | TUMS 梯控管理扩展点位授权 | 100 / 点 |
| `TL-TUMS-N-LIS` | TUMS 网络管理扩展点位授权 | 200 / 点 |
| `TL-TUMS-B-LIS` / `TL-TUMS-IPS-LIS` | 广播 / 信息发布扩展点位授权 | 25 / 63 |
| `TL-TUMS-SWP` | TUMS 商用管理系统（基础应用） | 25000 |
| `TL-TUMS-P-SWP` 等 `*-SWP` | 各子系统基础应用授权 | 25000~50000 |
| `TUMS车辆管理拓展应用授权` | 车辆管理模块（注意：不是型号前缀） | 37500 |
| `TUMS可视化大屏 / 国标管理拓展应用授权` | 模块授权 | 50000 / 30000 |

⚠️ **"图门" = TP-LINK 的 TUMS 图门平台**（CRM 里有 `TL-TUMS2800 图门平台一体机`、`TL-TUMS6900-A500 图门大模型一体机`、`TL-TUMS2500-EP TUMS图门中小企业融合平台`）。用户说"图门加两路停车授权"即 **TUMS 平台新增 2 路停车点位** = `TL-TUMS-P-LIS` × 2。
判断要不要加：**`TL-TUMS1500` 出厂自带 4 路停车点位**（见其 params），现场道闸数 > 4 时才需扩展。

**"停车点位"与"道闸数"的对应**：一个道闸 = 一路。核对时先数清单里的道闸/抓拍机数量，再减平台自带路数。

## 官网参数查询（控标库查不到时兜底，2026-08-27）
- ⭐ **拿「完整参数 + 官方彩页 PDF」的最快直链（2026-09-21 实测）**：`https://service.tp-link.com.cn/download/pdf/<productId>.pdf`
  - `<productId>` 就是产品页 `product_<id>.html` 里的数字。例：`TL-IR4005` → `.../pdf/3004.pdf`；`TL-R483G工业级` → `.../pdf/1653.pdf`。
  - 返回的就是官方**产品彩页 PDF**，含 产品简介 / 功能特性 / **完整技术参数规格** / **完整软件功能**（比 HTML 规格页更全，是同一份对外资料）。
  - 抽文本：`pip install pypdf -t ./pylibs` 后 `PYTHONPATH=./pylibs python -c "from pypdf import PdfReader; ..."`。**注意 Read 工具读不了这种 PDF**（提示 binary），必须走 pypdf。
  - 型号对比/写参数表优先走这条，必要时把彩页 PDF 直接交付给用户。
- 官网：`https://www.tp-link.com.cn/`
- 搜索：`https://www.tp-link.com.cn/search.html?keywords=<型号>`（**是 `search.html` + `keywords=`，不是 `/search/?q=`**；后者会跳"无法获取"页）。
- 产品页：`https://www.tp-link.com.cn/product_<id>.html`（id 从搜索结果抓，如 YLAP550G易展版 → product_2346.html）。
- **产品特性（用户要的控标参数文本）**：在主产品页 `https://www.tp-link.com.cn/product_<id>.html`（**不加 `?v=`**），抽取 `<div id="smbproductFeature">` 容器下所有 `<li>` 即官网「产品特性」文案（如「新一代 Wi-Fi 6 技术 / 频谱导航 / 胖瘦一体 / FIT模式支持智能漫游」等营销功能句）。**这才是用户明确要的参数，不是规格表。** 方法：`re.search(r'<div id="smbproductFeature">(.*?)</div>\s*</div>', html, re.S)` 再 `findall(r'<li[^>]*>(.*?)</li>', chunk, re.S)` 去标签。
- **规格表（次要，仅当页面无 #smbproductFeature 时兜底）**：`?v=specification` 视图规格表为图片渲染，但参数结构化在 `<li>` 里，用正则可抽（型号/速率/MHz/端口/重量/尺寸等）。
- 详情图：`https://www.tp-link.com.cn/content/images/detail/<id>/<model>_0X.jpg`（最多 ~21 张，**全是营销功能图，无独立规格表图**）。注意：
  - 域必须用 `www.tp-link.com.cn`（`media.tp-link.com.cn` 域 404）。
  - 必须带 `Referer: https://www.tp-link.com.cn/product_<id>.html` + 浏览器 UA，否则防盗链。
- **资料中心/彩页下载链接（控标彩页交付用）**：`https://resource.tp-link.com.cn/pc/docCenter/showDoc?productId=<id>&showQuestion=true` 是官方「资料中心」页面，顶部有「产品彩页 / 安装手册 / 用户手册」等标签，可点入下载彩页 PDF。`<id>` 即官网产品页 `product_<id>.html` 的数字。⚠️ 该站是 Nuxt SPA，彩页 PDF 真实地址由前端 JS 异步加载（静态 HTML / `__NUXT__` 里只有 `docOrder:["产品彩页",...]`，没有直链），所以**直接给这个资料中心页面链接**即可，不要试图从静态 HTML 抽 PDF 直链。当表格要"彩页下载链接"而抽不到直链时，填这个资料中心页链接。

## 工程清单（投标/报价）批量取数流程（2026-08-27）
- 用户后续会发工程清单表格（产品型号大概率 TL- 开头已填好），按以下顺序取数：
  1. **价格** → CRM `/api/line/findByPage` 取 `price`、`businessDiscount`、`seriesValue`（注意模糊匹配+客户端精确筛）。
  2. **控标参数** → 本地 `<控标数据目录>\tplink_kongbiao_data.json` 优先；查不到转官网（见上节）。
  3. CRM 多价格/查不到标准价 → 参考 `$DEALER_BASE/product`（需手机号+验证码，暂未接入）。
  4. **折扣价**：`seriesValue='项目机型'` 才有业务折扣；**按产品线选折扣率——监控类用片区折扣率，其他类用总部折扣率**（详见上「折扣计算规则」）；无业务折扣的型号直接取 `price` 标准价。

## 型号参数对比（"A 和 B 贵在哪/差在哪"类需求，2026-09-21）
1. 先定 productId：`https://www.tp-link.com.cn/search.html?keywords=<型号>`，抓 `product_<id>.html`。
   注意 `TL-R483G` 与其工业版是**两个页面**（`product_444` / `product_1653`），用户说"XX工业级"要按工业版取。
2. 拉两份彩页 PDF（见上）抽文本，逐条 diff「技术参数规格 + 软件功能」。
3. CRM 补标准价：`crm_query.py --like <型号前缀>`（模糊匹配能带出工业版等衍生型号）。
4. 结论只讲**官网写明的差异**；官网没公布的吞吐量/带机量/NAT 并发不要编。差异常集中在：三防漆涂层、CE 认证、OPC UA 透传、NTP 中继、SNMP、包装配件、固件维护节奏。
5. 若两个型号**折扣口径不同**（一个有业务折扣、一个无），要同时给出标准价与折扣后口径，并提示用户折扣口径需当场确认。

## 注意 / 坑
- **Bash 环境 PATH 可能不全**：报 `dirname: command not found` / `cd: null directory`（宿主 shim 报错）时，命令前先 `export PATH="/c/Users/admin/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Windows/System32:$PATH"`；**PowerShell 工具在部分会话里不回显 stdout**，改用 Bash 并把输出重定向到文件再读。
- **业务折扣 `businessDiscount` 字段**：`/api/line/findByPage` **会返回**该字段（字符串，如 "0.39"），无需另查报价/项目报备接口。先前判断"不返回"是只看了首条非项目机型记录所致；实测在项目机型条目上明确存在。
- 每个 Python 脚本是独立进程，SESSION cookie 不跨进程保留——没有"持久登录"残留，天然符合"用完即失效"。
- **安全**：凭证按用户要求存于用户级 `~/.workbuddy/MEMORY.md`（明文、仅本机可读），跨对话复用；脚本运行时从此处取用，不在工作区文件里硬编码明文。聊天记录可能服务端留存，敏感操作仍须谨慎。
- 登录失败会返回 400（凭证错）或 404（路径错），据此排查。
