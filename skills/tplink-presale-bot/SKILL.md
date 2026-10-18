---
name: tplink-presale-bot
summary: 搭建/维护 TP-LINK 售前助手（Agent+工具层架构，控标库+官网+CRM 三源，接入企业微信）
description: 当用户要"做个群里答TP产品问题的小助手""售前机器人""接入企业微信/微信群""查控标库答问题""让机器人能处理工作而不只对话""把TP-LINK所有技能都封装进去""根据参数选型号""某型号支不支持某功能"时使用。含三层架构（wecom_bot 接入层 / agent LLM内核 / tools 工具层，共21个工具）、★两项主力能力（kb_filter 按参数条件选型 + feature_check 型号功能三态判定）、★技能热更新（_load_skill/_load_crm_module/get_kb 全按 mtime 重载，技能或控标库改完不用重启）、技能桥机制（运行时从 ~/.workbuddy/skills 加载技能脚本，不复制代码；10个技能工具 + skill_list/skill_doc 知识层）、选型三坑（量词归一/数字边界/名称加权排序）、控标库脏数据过滤规则（107条bad model）、型号族二次校验、停产核查族聚合、清单列位自动探测、控标参数品牌词中性化、OpenAI兼容LLM接入（DeepSeek/通义/Kimi/Ollama）、企业微信智能机器人长连接（无需公网IP）、doctor.py 上线体检、离线mock自检方案（51项）。能力开关按用户 2026-09-24 决定已全部放开（CRM/价格可用）。
agent_created: true
---

# TP-LINK 售前助手

## 何时用

- 用户要在群聊里挂一个自动回答 TP-LINK 产品问题的小助手。
- 用户要一个"贴型号出参数"的本地查询工具。
- 用户问"机器人只能对话吗，能不能做 agent 处理工作"——即要**从问答升级为能办事**。
- 需要复用控标库（`tplink_kongbiao_data.json`）做问答式检索。

## 三层架构（2026-09-24 升级，务必照此分层）

```
企业微信群
   │  @机器人
   ▼
wecom_bot.py ── 接入层：消息清洗 · msgid排重 · 会话隔离(chatid:userid) · 流式回执 · 回复降级
   │
   ▼
agent.py ──── 内核层：LLM 决定「查什么、怎么答」+ 多轮记忆   ← 有 key 走这条
   │  ↘
   │    engine.py ── 规则匹配降级路径                        ← 没 key / LLM挂了走这条
   ▼
tools.py ──── 工具层：取数与计算（★所有能力的唯一实体）
```

**核心原则：LLM 只负责「理解 + 组织语言」，所有事实必须从工具拿。**
这样模型不会编造型号——查不到就是查不到。也正因如此，工具层可以脱离 LLM 独立复用（规则引擎调的也是同一批函数）。

### 升级为 Agent 的关键点（用户会问"能不能做 agent"）
1. **企业微信机器人本身只是消息管道，没有能力上限** —— 只会对话是因为后端接的是规则引擎。
2. Agent 化只需加两层：LLM 大脑 + 工具集。工具集是地基，**先建工具层不吃亏**（LLM 有无都能用）。
3. **工具用 OpenAI function-calling 规格描述**（`TOOL_SPECS`），DeepSeek / 通义 / Kimi / Ollama 全是 OpenAI 兼容协议，一套代码通吃。
4. **必须做降级**：没 key 时调 `engine.answer()`，不能让人干等。
5. **长任务要先回执**：LLM+多轮工具要 10~30 秒，用 `client.reply_stream(frame, stream_id, "🔍 正在查询…", finish=False)` 先发一条，最后 `finish=True` 收尾。
6. **离线 mock 自检**：设 `LLM_MOCK=1`，让 `chat_completion` 返回预置的假响应（第一轮要 tool_calls、第二轮给文本），从而**不花一分钱、不联网**就验证工具链路与消息回填格式是否正确。

## 能力开关（用户 2026-09-24 决定：全部放开）

原先的三条硬性约束**已解除**，改为可配置：

| 开关 | 默认 | 说明 |
|---|---|---|
| `TL_HELPER_WEB` | 1 | 官网查询 |
| `TL_HELPER_PRICE` | 1 | CRM 查价与算价 |
| `WECOM_ALLOW_GROUPS` / `WECOM_ALLOW_USERS` | 空 | 群/用户白名单（留空=全应答） |

**仍需守住**：
- **不臆造型号** —— 查不到就明说（用户曾抓到闭门器造假的教训）。
- **折扣率等内部口径不主动展开** —— 输出给单价即可。
- **控标库只读**、**CRM 只读**。

⚠️ 群里发价格属商务信息暴露，若群含外部人员，提醒用户收紧白名单。

## 关键事实

### 数据源
- 控标库：数据包里的 `数据/1-控标参数库.json`（程序内原名 `tplink_kongbiao_data.json`，路径由配置决定）
  - 结构：`list`，每项 `{category, product_name, model, params_raw, param_items:[{text,is_bonus}]}`
  - 原始 658 条 → **有效型号仅 551 个**（107 条 model 是脏数据）
  - **161 个型号含 `is_bonus=True` 的 ★加分项**（早期误判为"全是 false"，务必按型号实测）
- 官网：`https://www.tp-link.com.cn/`，搜索用 `search.html?keywords=<型号>`（**不是 `/search/?q=`**）

### 控标库脏数据模式（必须过滤，否则品类检索被严重污染）
| 脏数据 | 例子 | 成因 |
|---|---|---|
| 表头行 | `型号` | 表格表头被当数据行 |
| 换行截断 | `（年后出货` / `检测报告可以做）` | 单元格内换行被拆成两行 |
| 规格后缀拆分 | `DC易展版` / `薄款（方）` / `M` / `R` | 规格与型号在不同列被拆 |
| 误分类 | `TL-SPK061C-PoE/DC`（6W吸顶音箱）category 写成 `吸顶AP` | 源表分类填错 |

过滤正则：
```python
MODEL_VALID_RE = re.compile(r"^(TL-|YL|TP-|NS\d|NBS|AUD|NMS|SAS|AAA|VMS)[A-Za-z0-9\-\./\+]*$")
MODEL_SHORT_RE = re.compile(r"^[A-Z]{1,6}[0-9]{2,6}[A-Za-z0-9\-]*$")
```

## 实现要点（踩过的坑）

### 1. 型号正则不能用 `\b`
`\b(TL-...)` 在中文与字母间**不构成词边界**，`TL-AC100和TL-NAC500对比` 只会匹配到一个型号。
```python
MODEL_RE = re.compile(r"((?:TL-|YL|TP-)[A-Za-z0-9][A-Za-z0-9\-\./]*)", re.IGNORECASE)
```

### 2. 品类检索要"命中即返回"，不可再走退化路径
否则「吸顶AP」先命中吸顶类，第二轮又用整词匹配到「吸顶音箱」。
命中 CATEGORY_MAP 的强特征词后直接 return。

### 3. 型号族二次校验（兜住源库分类错误）
```python
FAMILY_GUARD = [
    (["吸顶AP","面板AP","室外AP","工业AP","AP"], ["AP"]),
    (["三层","网管交换机","交换机"], ["S","N"]),
    (["枪机","半球","球机"], ["IPC"]),
]
```
过滤后剩 <3 条则退回原列表（防 guard 过严误杀）。

### 4. 问句剥壳取品类核心词
`吸顶AP有哪些型号` → 去掉 `有哪些/型号/的` 等疑问词 → `吸顶AP`。
剥空则回退原句。

### 5. Windows 中文输出编码
- PowerShell 工具在部分会话**不回显 stdout** → 一律写文件再用 Read 读。
- 直接 `>` 重定向会写成 UTF-16 变二进制 → 用 `Out-File -Encoding utf8`。
- 脚本内打印用 `io.open(..., encoding='utf-8')` 写文件最稳。

### 6. 沙箱不允许常驻服务
后台起 HTTP 服务会被杀或沙箱拒绝（`LsSDK_CreateSandbox` / `CreatePseudoConsole` 访问拒绝）。
**验证逻辑直接跑 `engine.py`/`selftest.py` 并读输出文件**，不要在沙箱里试图长期驻留服务。

## 工具集（tools.py，21 个工具）

### 数据查询（9）

| 工具名 | 作用 | 关键点 |
|---|---|---|
| `kb_query` | 控标库精确查参数 | `only_bonus=True` 只取★加分项 |
| `kb_search` | 品类/关键词 → 型号清单 | 已内置型号合法性与型号族二次校验 |
| **`kb_filter`** | **★按参数条件选型（给条件找型号）** | `kb_search` 的反向操作。见下方「选型实现三坑」 |
| **`feature_check`** | **★型号功能判定（三态 yes/no/unknown）** | 检索范围必须覆盖产品名+分类+每条参数+规格原文 |
| **`official_search`** | **★官网关键词检索（支持参数词）** | 实测 `?keywords=PoE供电 吸顶AP` 能出结果；全文检索含弱相关项，要二次筛 |
| `official_lookup` | 官网产品特性 + 彩页 PDF 直链 | 失败返回 `{ok:False,error}`，不抛异常 |
| `crm_price` | CRM 标准价 + 业务折扣（只读） | 复用 `tplink-crm-readonly/scripts/crm_query.py`；productModel 模糊匹配**必须按精确型号二次筛** |
| `quote_price` | 折扣算价 + 取整 | 自动判产品线选折扣率；0.40 档返回 `need_confirm` |
| `compare` | 多型号参数并排 | 最多 4 个型号 |

### ⭐ 选型实现三坑（2026-09-24 实测踩出来的，改这块必看）

用户 2026-09-24 明确：**对话 bot 的主力是「按参数选型」和「型号功能判定」**。
两个能力都在 `kb.py`，三条坑都是实测踩出来的：

1. **量词必须归一** —— 库里写「提供 **48个** 千兆SFP口」，用户说「**48口**」。
   不归一会**整片漏匹配**（实测漏掉 25 个型号，`[48口,千兆]` 直接触发放宽）。
   → `_norm_q()` 把 `数字+（端口|个|口|路）` 统一成 `数字+PORT`。
2. **数字量词必须加边界断言** —— 归一后纯子串匹配会让「**8**PORT」命中「**48**0PORT」、
   「24口」的机器混进「8口」的结果。
   → `_find()` 对 `\d+PORT` 用 `(?<!\d)…(?!\d)`；非数字条件仍走普通子串。
3. **结果要按「名称命中」加权** —— 「8口」的正确结果 `YLS220PE-9G1F 8口全千兆云管理PoE交换机`
   原本排在若干「参数里提到8个万兆口」的 24/48 口机器后面。
   → 排序键 `(-命中条件数, -产品名命中数)`，并把 `in_name` 返回给上层。
4. **放宽必须显式声明** —— 全满足的结果为空时自动放宽为「满足任一」，
   返回 `relaxed=True` + 每条 `miss`（缺哪项）。**回答时不说明就是骗人**。
5. **框式交换机的坑** —— `NS7606E` 这类机框「支持48口板卡」，会被当成「48口整机」。
   规则引擎输出层对含「框式」的结果加提示：端口数由业务板卡决定。

### 功能判定的三态纪律

`feature_check` 只返回 `yes / no / unknown`，**`unknown` 绝不能说成「不支持」**：

- 控标库是**控标参数**，不是完整规格书 —— 没写 ≠ 没有。
- `unknown` 时必须：①说明「参数里未提及」②强调「不代表不支持」
  ③给 `has_features`（该型号实际具备的功能点）供换问法 ④建议查官网/问产品线。
- 检索范围别只扫 `param_items`：`TL-SPK061C-PoE/DC` 的「吸顶」在产品名里，
  只扫参数会误判 unknown（第一版就是这么错的）。

### 技能桥（10）—— 2026-09-24 新增，把本机 TP-LINK 技能全部接进来

| 工具名 | 作用 |
|---|---|
| `crm_eol` | 批量停产核查，**含型号族聚合** |
| `crm_pool` | 按前缀拉 CRM 全量池（找在产替代款） |
| `sanitize_params` | 控标参数品牌词中性化 |
| `list_estimate` | 工程清单 TP-LINK 设备估价 |
| `nat_classify` | 清单设备网关 NAT 分类 |
| `capacity_check` | 网关带机量核算（负载率） |
| `quote_autofill` | 清单批量补参数 + 填价 → 新 Excel |
| `week_log_add` / `week_draft` / `week_list` | 周报登记 / 出稿 / 查看 |

### 技能知识（2）

| 工具名 | 作用 |
|---|---|
| `skill_list` | 列出本机所有相关技能 |
| `skill_doc` | 读某技能 SKILL.md（可按关键词取段落） |

**统一入口 `call_tool(name, args)`，任何工具都不抛异常**（失败返回 `{ok:False, error}`）——这样 LLM 与规则引擎都能安全调。

### ⭐ 技能桥机制（关键设计，别改成复制代码）

**核心原则：不复制技能代码，运行时从 `~/.workbuddy/skills/` 加载。**
技能一更新，助手自动跟着变，杜绝两套逻辑打架。

```python
SKILLS_ROOT   = os.environ.get("TP_SKILLS_ROOT", "~/.workbuddy/skills")
SKILL_SCRIPTS = { "eol": "tplink-crm-eol-check/crm_eol.py",
                  "quote": "tplink-quote-autofill/scripts/autofill_quote.py", ... }
_load_skill(key)          # importlib.util 当模块加载（★按 mtime 热重载）
_run_skill(key, argv)     # 子进程跑 CLI 技能（capture_output + timeout + UTF-8 强制）
```

### ⭐ 热更新（用户 2026-09-24 明确问过，答案必须是"能"）

用户要求：「不给 bot 做单独 skill，就用本机 skill；踩坑或更新 skill 后 bot 也要更新」。

| 调用方式 | 更新技能后 | 原因 |
|---|---|---|
| `_run_skill`（子进程） | **天然热更新**，不用重启 | 每次执行都重新读文件 |
| `_load_skill`（import） | **需 mtime 检查**，否则读旧版 | Python 模块缓存 |
| `_load_crm_module` | 同上，已加 mtime | 同上 |
| 控标库 `get_kb()` | 已加 mtime，改库后自动重载 | 用户常直接改源 JSON |

→ 所有缓存入口都按 `os.path.getmtime()` 判失效，**技能/控标库一改，下一次调用就生效，无需重启机器人**。
   写新缓存时照这个模式做，别只按 key 缓存（第一版就这么错，隐蔽且难查）。

两种调用方式按技能形态选：

| 技能形态 | 用哪种 | 例子 |
|---|---|---|
| 有干净的纯函数接口 | `_load_skill` + 直接调函数 | `sanitize_params` 的 `sanitize_text()`、`tp_estimate` 的 `estimate_sheet()` |
| CLI 工作流 | `_run_skill` 子进程 | `quote_autofill`、`week_log`、`draft_report` |
| 只有文档没脚本 | `skill_doc` 喂规范给 LLM | 拓扑图、PPT 插页、CAD 点数 |

**三个必踩的坑（都踩过）**：

1. **别用技能里硬编码的凭据**。`crm_eol.py` 模块里写着 `EMAIL/PASS` 明文，
   直接 import 会绕过统一的凭据逻辑。正确做法：用 `crm_query.py` 的
   `make_opener()`/`login()` 登录一次拿到已认证的 `op`，再 `eol_mod.op = op`
   **注入**进去复用它的 `extract_models/query_crm/norm`。既要复用逻辑、又要统一凭据。
2. **注意模块级副作用**。`tp_estimate.py` 顶部有 `sys.stdout.reconfigure(...)`，
   import 会改当前进程 stdout 编码；CLI-only 的脚本用子进程更干净。
3. **技能产物别落在技能目录**。`nat_classify` 的 `--out-json` 默认写 `items_nat.json`
   到 CWD（即技能脚本目录）。工具层统一指到自己的 `_work/` 目录。

### 技能侧被顺带修掉的两个缺陷（2026-09-24）

① **`crm_eol.py` 缺「型号族聚合」** —— SKILL.md 写了摄像机按焦距后缀建货号
（`TL-IPC544EP-W` 实为 `-W4/-W6/-W8/-W12`），只精确匹配会整片误判"无此型号"，
但脚本里没实现。助手侧补了三步判定：精确 → 族聚合（前缀命中且不被同批更长目标前缀开头）
→ 按 lifeCycle 集合给结论（全 PRODUCTION=在产 / 全 HALT=停产 / 含 HALT 且还有其它=**部分停产**）。

② **`nat_classify.py` / `capacity_check.py` 列位写死** —— 默认列位只对一种模板。
实测 0914 清单 `A=序号 B=项目名称 C=品牌 D=型号 F=参数 G=数量 H=单位 I=单价 J=小计`
与默认值**四列错位**，整表按「品牌」列当设备名判定，13/14 条落到"未命中规则"，
**看起来像规则失效，其实是列读错**（静默无报错，最坑）。
- 已给两脚本补 `--seq-col/--name-col/--model-col/--qty-col/--unit-col/--price-col`
- 工具层加 `detect_cols()`：前 8 行按「序号/设备名称·材料名称·项目名称/型号/数量/单位/小计」
  提示词探列号，探不到才退回默认
- 顺手补 4 条规则缺口（`无线控制器`、`上网行为管理`、`定向天线|射频线缆`、`POE供电模块`）
  + 跳过「合计」行。修正后同一清单 **13 条全部命中、0 条待人工确认**（原来 6 条）

### 加新能力的标准动作

1. **装个新技能**到 `~/.workbuddy/skills/` → 助手立刻能 `skill_list` 看到、`skill_doc` 读到；
2. **技能加脚本** → 在 `SKILL_SCRIPTS` 登记一行 + 包个工具函数（`_load_skill` 或 `_run_skill`）；
3. **纯本地逻辑** → `tools.py` 加函数 + `call_tool` 加分支 + `TOOL_SPECS` 加描述。Agent 立刻会用，不用改 `agent.py`。规则引擎要另加 `engine.py` 意图分支。

### 折扣与取整（硬规则，已固化在 tools.py）
- 业务折扣率 → 折扣率：`0.45→片区0.43/总部0.40`、`0.43→0.40/0.40`、`0.39→0.34/0.32`、`0.32→0.27/0.25`
- **监控类用片区、其他类用总部**（监控标识：IPC/NIPC/NVR/TUMS/解码/拼接/监视器/摄像机 等）
- **0.40 档不在表内 → 返回 `need_confirm`，不瞎猜**
- 取整：向上到个位 0/2/5（`clean_price`）

## 六类意图路由（规则引擎 engine.py）

| 意图 | 触发 | 动作 |
|---|---|---|
| **★按条件选型** | 无型号 + 含同义词表条件词/`数字+口` + 需求描述 | `kb_filter(conditions, category)`；结果带 relaxed/miss |
| **★型号功能判定** | 有型号 + 含 `支持/能不能/符不符合/有没有…吗` | `feature_check` 三态；剥壳后无有效功能词则退回参数查询 |
| 型号问参数 | 命中 `TL-` 型号 | 控标库精确 → 前缀候选 → 官网兜底 |
| 型号问加分项 | 含 `加分/加分项/星标/★` | 只列 `is_bonus=True` 的 |
| 品类列型号 | 含 `有哪些/有什么/型号/清单/选型` | `kb_search()` 出清单，最多 20 条 |
| 多型号对比 | 含 `对比/区别/差异` 且 ≥2 型号 | 并排参数，各取前 6 条 |
| 价格/算价 | 含 `价格/多少钱/报价/折扣/底价/面价` | 走 `quote_price`（不再拒答） |
| 重置上下文 | 含 `重置/清空/新话题/reset` | 清空该会话记忆 |

**顺序陷阱**：功能判定必须排在「型号问参数」之前，否则「TL-AC100 支持IPv6吗」会被
当成参数查询。但「TL-AC100 支持什么」要能识别出**没有有效功能词**而退回参数查询
（`_FEAT_STOP` 剥壳后为空即退回）。

## 官网兜底

- 搜索：`https://www.tp-link.com.cn/search.html?keywords=<型号>` → 正则抓 `product_(\d+)\.html`
- 产品特性：主页面（**不加 `?v=`**）正则 `<div id="smbproductFeature">(.*?)</div>\s*</div>` 抽 `<li>`
- 彩页 PDF 直链：`https://service.tp-link.com.cn/download/pdf/<productId>.pdf`
- 必须带浏览器 UA；失败返回 `None`，不抛异常（保证助手不崩）

## 企业微信接入（推荐路线，2026-09-24 实测打通）

### 四条路线能力对比（选错就白做）
| 路线 | 能收群消息 | 需公网服务器 | 结论 |
|---|---|---|---|
| 群机器人 Webhook | ❌ **只出不进** | 否 | 只能发通知，做不了问答 |
| **智能机器人 + 长连接** | ✅ 群内 @触发 | **否** | ✅ **首选** |
| 自建应用 + 回调 URL | ✅ | 是（HTTPS+备案域名） | 太重 |
| 会话内容存档 | ✅ 全量 | 是 | 偏离需求 |

⚠️ 最大的坑：**群机器人 Webhook 收不到消息**，只有 Webhook 是做不成问答机器人的。

### 长连接方案（内网可跑，无需公网 IP）
- **官方 SDK**：`pip install wecom-aibot-python-sdk`
- **包名是 `aibot`**（不是 `wecom_aibot_sdk`，网上二手示例常写错）
- **连接地址**：`wss://openws.work.weixin.qq.com`（出站连接即可，实测家用宽带可通）
- **创建机器人**：管理后台 → 安全与管理 → 管理工具 → 智能机器人 → 创建 → **选「API 模式」**
  （不是普通群机器人）→ 拿 **Bot ID** + **Secret**

### SDK 真实 API（实测 1.0.2 / 包内 `aibot/`）
```python
from aibot import WSClient, WSClientOptions

client = WSClient(WSClientOptions(
    bot_id=BOT_ID, secret=BOT_SECRET,
    heartbeat_interval=30000,
    max_reconnect_attempts=-1,   # -1 = 无限重连，常驻必设
))
```
**事件名（`@client.on(...)`）**：
- 连接类：`connected` / `authenticated` / `disconnected` / `reconnecting` / `error`
- 消息类：`message`（全部）、`message.text` / `.image` / `.mixed` / `.voice` / `.file`
- 事件类：`event`、`event.enter_chat` / `.template_card_event` / `.feedback_event`

**关键方法**：
- `client.reply(frame, body, cmd=None)` —— 通用回复，透传 `frame["headers"]["req_id"]`
- `client.reply_stream(frame, stream_id, content, finish=False)` —— 流式打字机
- `client.reply_welcome(frame, body)` —— 进会话欢迎语，**须 5 秒内发**
- `client.send_message(chatid, body)` —— 主动推送，不依赖回调帧
- `client.download_file(url, aes_key)` —— 下载并 AES 解密媒体
- `client.run()` —— 阻塞运行

**收到的消息结构**（`frame["body"]`）：
```python
{"msgid":..., "aibotid":..., "chatid":..., "chattype":"group|single",
 "from":{"userid":...}, "msgtype":"text", "text":{"content":"@机器人 xxx"},
 "response_url":..., "quote":{...}}
```
- `chattype` 区分单聊/群聊；`chatid` **仅群聊返回**
- **群内 @机器人 时，`text.content` 里会带 `@机器人名` 前缀，必须清洗**
  （用 `re.sub(r"^@\S+\s*", "", raw)`）
- 有 `msgid` 的必须排重（可能重复回调）

### 消息格式能力
| 类型 | 限制 | 备注 |
|---|---|---|
| `text` | 2048 字节 | 支持 `<@userid>` @人 |
| `markdown` | 4096 字节 | 支持 `**加粗**`、链接、引用、行内代码、`<font color="info\|comment\|warning">`（仅3色） |
| `markdown_v2` | 4096 字节 | 支持表格/列表/分割线，**不支持颜色和 @** |
| `template_card` | — | 模板卡片 |

回复策略：参数列表用 markdown + 加分项 `<font color="warning">` 染色；型号清单可用 markdown_v2 表格。
**超长必须截断**（按字节裁，别按字符）。

### 实测踩坑
- **假凭据也会连上 WebSocket**，认证失败报 `853000 invalid bot_id or secret`。
  → 可据此区分"网络不通"vs"凭据错"。
- 认证失败后 SDK 会**持续重连**，不会退出。想快速排查可加超时。
- `frame` 的 `headers.req_id` 是回复的必需透传项，自己构造 frame 时必须带。

## 个人微信接入（无官方接口）

**个人微信没有官方机器人接口**，只有三条路：

| 方案 | 风险 | 说明 |
|---|---|---|
| A 手动转发 | 零 | 本地网页，贴问题→复制答案。**推荐先用** |
| B wxauto UI 自动化 | 中 | `pip install wxauto`，控 PC 微信。违反协议，风控可能限登。**工作号别碰** |
| C hook 注入 | 高 | wechaty 等。封号率最高，不推荐 |

## 产物结构

```
tplink-helper/
  tools.py             工具层 ★21 个工具（数据查询 9 + 技能桥 10 + 技能知识 2）
  agent.py             Agent 内核（OpenAI兼容 + function calling + 多轮记忆 + 降级 + mock）
  engine.py            规则引擎（降级路径，纯离线；两类主力问题也能答）
  kb.py                控标库内核（型号分级检索、品类检索、选型 filter_by_params、
                       feature_check 三态判定、脏数据过滤、量词归一）
  wecom_format.py      企业微信消息格式化（markdown / 表格 / @前缀清洗）
  wecom_bot.py         企业微信接入（WebSocket 长连接、会话管理、流式回执）
  doctor.py            上线体检（Python/依赖/控标库/技能/网络/凭据，出 doctor_report.txt）
  selftest.py          规则引擎自检（11 项）
  selftest_agent.py    Agent + 技能桥 + 选型/功能判定自检（51 项，含 mock 工具链路）
  wecom_selftest.py    企微接入自检（14 项，含端到端模拟）
  server.py            本地 HTTP 服务 + 网页 UI（不需要企微凭据也能试）
  _work/               技能产物落地（NAT 分类 JSON 等）
  .env.example         配置模板（BotID/Secret + LLM + 开关 + 技能目录）
  体检.bat             上线体检
  启动企微助手.bat      企微版启动（自动装依赖 + 无 .env 时自动生成并打开记事本）
  启动助手.bat          本地网页版启动（无凭据也能跑）
```

**自检共 76 项，全绿才交付**：11（规则）+ 51（Agent/技能桥/选型/功能判定）+ 14（企微接入）。
技能侧另有 31 项：`nat_classify.py --selftest` 21 项 + `capacity_check.py --selftest` 10 项。

参考实现：本仓库 `dealer-tool/`（选型）+ `skills/`（工具层），按上文 `call_tool(name, args)` 约定包一层即可。

## 部署形态与常驻要求（用户 2026-09-24 问过）

- 机器人是**本机进程**走 WebSocket，**电脑关机/睡眠即离线**，需设「从不睡眠」+ `shell:startup` 自启。
- 不需要公网 IP、不需要备案域名（长连接只需出站）。
- 换机器跑：拷目录 + 改 `kb.py` 顶部 `KB_PATH` + 装依赖（`体检.bat` 会逐项报缺什么）。

## 待接能力（用户可能追问）

截至 2026-09-24，**本机 TP-LINK 相关技能已全部接入**（见上文「工具集」）：

| 技能 | 状态 |
|---|---|
| `tplink-crm-readonly` | ✅ 工具 `crm_price` |
| `tplink-crm-eol-check` | ✅ 工具 `crm_eol` / `crm_pool`（并补了族聚合） |
| `tplink-quote-autofill` | ✅ 工具 `sanitize_params` / `nat_classify` / `capacity_check` / `quote_autofill` |
| `tplink-weekly-report` | ✅ 工具 `list_estimate` / `week_log_add` / `week_draft` / `week_list` |
| `tplink-topology-diagram` | ⚠️ **知识封装**（技能目录里只有 SKILL.md，没有 build 脚本）→ `skill_doc` 取规范，生成仍靠人工/新写脚本 |
| `pptx-insert-slides` / `cad-pdf-device-tally` / `mercury-security-spec` | ⚠️ 知识封装（同上，无脚本可桥） |

**剩下真正没做的**：拓扑图与实际产出文件（PPT/Excel）在企微群里只能回"已生成 + 本地路径"，
因为企微机器人回复不支持直接传本地文件（要传文件得走 `upload_media` + 文件消息，
且必须先在群里让用户拿到，属下一步可做的增强）。
