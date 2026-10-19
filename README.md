# tplink-quote-toolkit

> 丢一个弱电工程清单 Excel 进来 → 自动**选型**、补**控标参数**、按指定口径算**含税报价**。
> 一套给 WorkBuddy 用的技能，外加一个命令行选型工具。

---

## ⚠️ 先看这条

本仓库**只有工具代码和工作流文档**，不包含任何产品数据、价格数据、客户信息或账号凭据。

数据（控标库、选型库、价格备份、License 映射、防火墙规格）**需要单独准备**：
内部同事找同事要**数据包 zip**（跑包里的 `生成配置.py` 就配好），
外部则按需自己攒 —— 见 [数据文件从哪来](#数据文件从哪来)。
`.gitignore` 已经把数据文件和本地配置全部兜底排除，**请永远不要把数据文件提交进来**。

---

## 它能干什么

| 能力 | 它是怎么做到的 | 在哪 |
|---|---|---|
| 📋 **填控标参数** | 按型号查本地控标库（含 ★ 加分项）；查不到自动去官网抓「产品特性」 | `skills/tplink-quote-autofill` |
| 🧼 **品牌词中性化** | 自动把「商云 APP / TUMS / 算法商城 / TP-SRC」等换成中性说法，避免排他性条款 | 同上 |
| 💰 **算含税报价** | 查 CRM 标准价 + 业务折扣率，按**你本次指定的口径**换算，结果向上取整到 0/2/5 | 同上 + `skills/tplink-crm-readonly` |
| 🧩 **组合型号合计** | `TL-VS7500+TL-NVDU7606S*4` 这类，参数认主型号、价格把配件全加起来 | 同上 |
| 🔍 **选型检索** | 在本地选型库里按关键词/品类/规格全文找候选型号 | `dealer-tool/pick.py` |
| 🚦 **过停产** | 批量查 CRM 的 `lifeCycle`，分「在产 / 停产预警 / 停产」 | `skills/tplink-crm-eol-check` |
| 📅 **配单周报** | 登记每天收到的清单，周末自动生成周报草稿 | `skills/tplink-weekly-report` |
| 🤖 **群里答题** | 把上面的能力接成企业微信售前机器人 | `skills/tplink-presale-bot` |

### 它会主动追问你（这是设计，不是啰嗦）

选型、参数、价格，**每交付一段都会停下来问一次**「这版有没有要改的」，
并且给出可调项（比如"这几个型号按 F 系选的，要 G 系可以换"）。

不是客套——**你不回答，它不会往下走**：

| 你看到的 | 意思 |
|---|---|
| `🔁 交付回执` | 这一段做完了，把下面那段话转述给用户 |
| `🛑 被门禁挡住` | 上一段还没确认，脚本拒绝继续（退出码 3） |

```bash
python deliver_gate.py status                 # 卡在哪一段
python deliver_gate.py clear params --verdict ok   # 用户说没问题，放行
```

反过来，**你提了通用问题**（换个清单还会犯的），它也必须问你
「要不要写进 skill、提交到 GitHub」，同意后生成一份标准 Issue 素材。

> 这一套是强制的（`skills/tplink-quote-autofill/scripts/deliver_gate.py`），
> 不是"善意提醒"。见 [协作改动与审核](#协作改动与审核)。

---

## 五分钟上手

**0）前置**：Python 3.8+，装一个库

```bash
pip install openpyxl            # 读写 xlsx 必需
pip install xlrd                # 可选，只有要读老的 .xls 才需要
```

**1）安装**

```bash
git clone <本仓库地址>
cd tplink-quote-toolkit
python install.py               # 会先打印要做什么；加 --dry-run 可只看不动手
```

它会把 `skills/*` 装到 `~/.workbuddy/skills/`，把 `dealer-tool` 装到
`~/.workbuddy/tplink-toolkit/dealer-tool/`，并生成一份初始配置。

**2）装数据包**（同事照做就行，数据在内部共享盘）

数据文件不在本仓库。找同事要 **`TP-LINK清单工作流-数据包-<日期>.zip`**，解压到任意位置
（**不用放进技能目录**），然后在该目录下跑一次：

```bash
cd D:\TP-LINK工作流数据          # 换成你解压的位置
python 生成配置.py               # 自动把配置里的数据路径指向本包；加 --dry-run 可先看会改什么
```

换电脑 / 挪了文件夹 → 重跑一次 `生成配置.py`，不用手改路径。
包里有 `00-先读我.md`，写了三步安装、文件对照表和 FAQ。

> 没有数据包也不影响跑起来，只是能力会减弱——见 [数据文件从哪来](#数据文件从哪来)。

**3）填配置**（内部域名和账号是唯一必须手填的部分）

编辑 `~/.workbuddy/tplink-toolkit.config.json`，填 `crm_base` / `dealer_base` 和账号。
格式照 `config.example.json` 的注释填。**不填也能用**：参数功能正常，取价会跳过并提示。

**4）自检**：确认装对了再动手

```bash
python 自检.py
```

它会分两组查：**代码卫生**（有没有内部域名 / 手机号 / 本机路径、数据文件有没有误入库、10 份配置副本是否一致）
和**运行能力**（Python 与 openpyxl、配置文件在不在、五个数据文件读不读得到、三项自测）。

最后给 `OK / WARN / FATAL` 三个结论：

- **FATAL** 必须先解决，通常会直接指出「哪个路径不对」以及怎么修
- **WARN** 可以带着跑，但能力会弱（比如没配防火墙规格，带机量核算会退回控标库）
- 全部 OK 就是可以交付的状态

> 只查仓库卫生、不需要数据文件时用 `python 自检.py --repo-only`，push 前跑这个就够。

**5）重启 WorkBuddy**，然后在对话里直接说：

> 把这个清单的控标参数和价格补一下（把 Excel 拖进来）

---

## 数据文件从哪来

**内部同事：直接拿数据包，不用自己攒。**

找同事要 `TP-LINK清单工作流-数据包-<日期>.zip`，解压后跑包里的 `生成配置.py` 就全配好了
（见[上手第 2 步](#五分钟上手)）。包内文件名加了序号只是方便认，**程序看的是配置里的路径**：

| 包内文件 | 程序原名 |
|---|---|
| `数据/1-控标参数库.json` | `tplink_kongbiao_data.json` |
| `数据/2-选型库.json` | `tplink_dealer_backup.json` |
| `数据/3-价格备份.json` | `tplink_crm_price_backup.json` |
| `数据/4-License映射.json` | `tplink_license_map.json` |
| `数据/5-防火墙规格.json` | `tplink_fw_spec_20251120.json` |

> ⚠️ 数据包是内部数据：**别外传、别上传到任何公开位置**。

**没有数据包时**：下面这五个按需自己准备 —— **一个都没有也能用**，只是能力会减弱：

| 文件 | 作用 | 没有会怎样 | 怎么获得 |
|---|---|---|---|
| `tplink_kongbiao_data.json` | 控标参数库 | 参数全走官网抓，慢、覆盖少、**没有 ★ 加分项** | 内部整理维护，按 `{category, product_name, model, params_raw, param_items}` 结构往里加 |
| `tplink_dealer_backup.json` | 选型库 | **选型阶段找不到候选型号**（`pick.py` 检索不到） | `python dealer-tool/backup.py all`（首次全量，需合作伙伴平台账号）；之后每周 `backup.py refresh`（20 秒增量） |
| `tplink_crm_price_backup.json` | 价格本地备份 | 无网时查不到历史价格，只能实时连 CRM | 用 `price_cache.py` 查过就会自动落盘，慢慢攒 |
| `tplink_fw_spec_*.json` | 防火墙官方规格 | 带机量核算退回控标库 | 从官网整理 |
| `tplink_license_map.json` | 多合一 License 映射 | 防火墙授权 SKU 只能逐个联网查 | `python dealer-tool/license.py --all` |

> ⚠️ 现在工作流是**选型前置**，所以 **`tplink_dealer_backup.json` 是最关键的一个**——
> 没有它，第一步「选型」就没有候选可挑，只能凭型号直接查参数。

> 🔄 **选型库要定期刷新**（平台会新增型号、下架老品、改类目）。每周跑一次 `python backup.py refresh`，
> 约 20 秒，只拉列表：更新在售/停产/行业/类目，**新增型号**才补详情，下架的**保留但打标记**。
> 新增型号若带出新系列/新焦距，记得同步更新选型规则。详见
> [`quote-autofill/SKILL.md`](skills/tplink-quote-autofill/SKILL.md) 的「选型取数」章节。

> 💡 **最省事的起手式**：先只配 `kongbiao_json`（或干脆先不配），
> 把清单丢进去跑一遍看效果；价格那部分等你有了 CRM 账号再开。

---

## 配置说明

配置文件读取顺序（先命中先用，**只读第一个存在的文件**，后面的不再合并）：

1. 环境变量 `TPKIT_CONFIG` 指向的 JSON
2. 技能根目录的 `config.local.json`（如 `~/.workbuddy/skills/tplink-quote-autofill/config.local.json`）
3. 技能 `scripts/` 下的 `config.local.json`
4. **`~/.workbuddy/tplink-toolkit.config.json`** ← **推荐用这个**

> ⚠️ **别用第 2、3 种**：`install.py` 升级时会 `rmtree` 整个技能目录再重建，
> 放在里面的 `config.local.json` 会被一起删掉。用户级那个（第 4 种）不在技能目录里，升级不受影响。

看看当前实际生效的是哪份、字段有没有配错：见本节末尾「自查当前生效配置」。

| 字段 | 说明 |
|---|---|
| `kongbiao_json` | 控标参数库 JSON |
| `fw_spec_json` | 防火墙/行为管理官方规格 JSON |
| `dealer_backup_json` | 选型库备份 JSON |
| `price_backup_json` | CRM 价格本地备份 JSON |
| `license_map_json` | 多合一 License 映射 JSON |
| `weekly_report_root` | 周报根目录 |
| `crm_base` | **内部 CRM 站点根地址**（如 `https://你们组织的域名`） |
| `dealer_base` | **合作伙伴平台根地址** |
| `crm.email` / `crm.password` | 内部 CRM 账号（也可用环境变量 `TP_CRM_EMAIL` / `TP_CRM_PASSWORD`） |
| `dealer.phone` / `dealer.password` | 合作伙伴平台账号 |
| `pricing_default` | 未说明定价策略时**默认执行**的策略（自然语言），如 `"监控和网络走片区折扣，其余走总部折扣"`；留空 = 每次仍必问 |
| `proxy` | 需要走代理时填，如 `http://127.0.0.1:7890` |

**留空的行为**：
- 只读源（控标库、规格表）留空 → 用到时会提示"未配置"，不会瞎猜路径
- 可写备份（价格备份、选型库、License 映射）留空 → 自动落到 `~/.workbuddy/tplink-toolkit/data/`
- `crm_base` / `dealer_base` 留空 → 取价类功能提示"未配置"并跳过，**参数功能不受影响**
- `pricing_default` 留空 → 定价策略每次都会问，不预设

> 📌 **关于文档里的 `$CRM_BASE` / `$DEALER_BASE`**
>
> 本仓库**不内置任何内部系统域名**。文档和代码里出现的 `$CRM_BASE`、`$DEALER_BASE`
> 都是占位符，实际值由你在配置文件里填写，脚本运行时读取：
>
> | 占位符 | 配置项 | 文档里的写法 |
> |---|---|---|
> | `$CRM_BASE` | `crm_base` | `$CRM_BASE/api/line/findByPage` |
> | `$DEALER_BASE` | `dealer_base` | `$DEALER_BASE/partner/login` |
>
> 公开官网 `www.tp-link.com.cn` 等是真实地址，不需要配置。

自查当前生效配置：

```bash
python skills/tplink-quote-autofill/scripts/tpkit_config.py
```

---

## 保持更新

**代码/技能**和**数据文件**分开更新，互不影响。

### 代码 / 技能更新

```bash
git pull                  # 拉最新代码
python install.py         # 重新装进 ~/.workbuddy/skills/
# 重启 WorkBuddy —— 技能是启动时加载的，不重启读不到新版
```

`install.py` 会**先把技能目录整个删掉再复制**，所以：

| 放在哪 | 升级后 |
|---|---|
| 配置：`~/.workbuddy/tplink-toolkit.config.json` | ✅ 不受影响 |
| 配置：技能目录里的 `config.local.json` | ❌ **丢失** |
| 数据文件（数据包） | ✅ 不受影响（本来就在技能目录外） |

### 数据文件更新

拿到新版 JSON，**直接覆盖配置里指的那个文件**就行——不用改配置、不用重装技能、不用重启
（脚本每次运行都重新读文件，没有任何缓存）。

```bash
# 例：控标库换新版，覆盖掉旧文件即可
copy 新版控标库.json D:\...\tplink_kongbiao_data.json
```

> 数据包里的 `数据/1-控标参数库.json` ~ `5-防火墙规格.json` 同理，覆盖同名文件即可。
> **只有数据包整个挪了位置**，才需要重跑一次 `python 生成配置.py`。

---

## 协作改动与审核

### 提意见：不用会 git，先提 Issue

用着不对劲，**先提 Issue，不用碰代码**：

1. 打开仓库 → `Issues` → `New issue`
2. 把脚本生成的那段 Markdown 粘进去
3. 提交

**脚本会帮你写好这段**——每段交付后它都会提示，你也可以主动跑：

```bash
python deliver_gate.py issue --stage params \
    --title "一句话说清问题" \
    --detail "怎么复现、当时的原始输出是什么" \
    --expect "你期望它怎么做"
```

生成的内容含「问题 / 复现信息 / 具体现象 / 期望行为 / 建议改哪里」，
存到 `~/.workbuddy/tplink-toolkit/feedback/`，同时打在屏幕上方便复制。

> 💡 **为什么要绕这一道**：聊天记录里说一百遍等于零。Issue 有编号、能被搜到、
> 修的时候能在提交里引用（`fixes #12`）——这才叫反馈进了项目。
> 一个开源工具最怕的不是有 bug，是**用的人多、说的人少**。

### 改代码：Pull Request，不是直接推 main

用着用着发现规则不对、想加一条，先分清是哪种改动：

| 你想改 | 改哪 | 影响范围 |
|---|---|---|
| 我自己的口径（定价策略、默认型号） | **配置** `~/.workbuddy/tplink-toolkit.config.json` | 只有你自己 |
| 这一单特殊情况 | 不用改，处理时说明一句即可 | 无 |
| 通用规则写错 / 缺一条 | **仓库源码** → 走 PR（见下） | 所有人（审核合并后） |

仓库是公开的，**除仓库所有者外，其他人对 `main` 没有写权限**——这是有意的，
防止一个人的误改把所有人装到坏版本上。

```bash
# 1. GitHub 网页上点 Fork，把仓库复制到你自己的账号下
git clone https://github.com/<你的账号>/tplink-quote-toolkit.git
cd tplink-quote-toolkit

# 2. 改 skills/ 下的 SKILL.md 或脚本（改 clone 这份，不是 ~/.workbuddy/skills/）

# 3. 提交并推到你自己的 fork
git add -A
git commit -m "改了什么、为什么改"
git push

# 4. 网页上点 Compare & pull request，写清改的动机
```

**审核流程**：仓库所有者在 GitHub 上看 diff → 认可才点 Merge。
**合并前，你的改动只存在你自己的 fork 里，不影响任何人。**

> ⚠️ **不要直接改 `~/.workbuddy/skills/` 下的文件**：那是 `install.py` 装出来的副本，
> 下次重装整个删掉重建，改动会静默消失，而且**你以为改了、其实别人一点没变**。

### 仓库所有者：开启分支保护（一次性设置）

防止误推 main，也防止将来加的协作者绕过审核：

1. 打开 `https://github.com/<用户名>/tplink-quote-toolkit/settings/branches`
2. **Add branch protection rule**（或 Add classic branch protection rule）
3. Branch name pattern 填 `main`
4. 勾选：
   - **Require a pull request before merging**（必须走 PR）
   - **Require approvals** → 1（至少 1 人批准）
   - **Do not allow bypassing the above settings**（连自己也不能绕过）
5. Save

> 单人维护时第 4 步的「Require approvals」可以先把批准人数设为 0，
> 只强制走 PR——这样自己改也得先开个 PR 再合并，留一条可追溯的记录。

---

## 目录结构

```
tplink-quote-toolkit/
├── README.md
├── config.example.json          ← 配置模板（每个字段的含义/留空行为都写在里面，可交给 AI 照着配）
├── install.py                   ← 一键安装
├── 自检.py                       ← 装完 / 改完跑一次，查「能不能用」和「有没有泄露」
├── skills/                      ← WorkBuddy 技能（装到 ~/.workbuddy/skills/）
│   ├── tplink-quote-autofill/   ← 核心：填参数 + 算价
│   ├── tplink-crm-readonly/     ← CRM 只读取价
│   ├── tplink-crm-eol-check/    ← 停产核查
│   ├── tplink-weekly-report/    ← 配单周报
│   └── tplink-presale-bot/      ← 企业微信售前机器人
├── dealer-tool/                 ← 选型检索（命令行）
└── _shared/tpkit_config.py      ← 配置模块的源文件（改动后需同步到各技能）
```

> `_shared/tpkit_config.py` 是配置模块的**源**，各技能里各有一份副本。
> 改了这个文件，记得同步：

```bash
cp _shared/tpkit_config.py skills/tplink-quote-autofill/scripts/
cp _shared/tpkit_config.py skills/tplink-crm-readonly/scripts/
cp _shared/tpkit_config.py skills/tplink-weekly-report/scripts/
cp _shared/tpkit_config.py skills/tplink-crm-eol-check/
cp _shared/tpkit_config.py dealer-tool/
```

---

## 常见问题

**Q：我和对方不在同一个内网，能用吗？**
关键不在"同一个局域网"，而在"能不能访问公司系统"。CRM 域名 `$CRM_BASE`
是公司内部域名，只要在公司网络（任意办事处）或 VPN 内就能通。在家/外网时：
**参数功能照常**（走控标库 + 官网），**取价功能会跳过**（脚本会提示并继续，不会崩）。

**Q：跑起来提示"缺少 CRM 凭据"？**
去配置文件里填 `crm.email` / `crm.password`，或设环境变量 `TP_CRM_EMAIL` / `TP_CRM_PASSWORD`。
只想填参数不要价格，加 `--no-crm`。

**Q：会不会改坏我原来的清单？**
不会。脚本只写**空单元格**，你填过的内容一律跳过；并且运行前会自动备份原文件。

**Q：参数里为什么没有品牌字眼了？**
这是故意的。投标参数带自家品牌名容易被认定排他性条款，脚本会自动中性化。
不想清洗就加 `--no-sanitize`。

**Q：一单到底给几折？**
脚本**不预设**。折扣口径是商务决策，每次由你通过 `--rate-spec` 指定；
不指定就先按"全部取标准价"跑，并提醒你带参数重跑。

**Q：我想验证改造没把逻辑改坏？**

```bash
cd skills/tplink-quote-autofill/scripts
python autofill_quote.py --selftest    # 不联网，秒出结果
python nat_classify.py --selftest
python capacity_check.py --selftest
```

---

## 说明与边界

- 本仓库开源的是**工具与工作流**，数据、账号、客户信息一律不入库。
- 使用前请自行确认符合所在组织的相关规定。
- 官网抓取选择器会随官网改版失效；CRM 接口字段也可能变，遇到大范围"查不到"先查这两处。
- 欢迎提 Issue / PR；改 `_shared/tpkit_config.py` 后请按上面命令同步副本。
