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

**4）重启 WorkBuddy**，然后在对话里直接说：

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

## 目录结构

```
tplink-quote-toolkit/
├── README.md
├── config.example.json          ← 配置模板（每个字段的含义/留空行为都写在里面，可交给 AI 照着配）
├── install.py                   ← 一键安装
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
