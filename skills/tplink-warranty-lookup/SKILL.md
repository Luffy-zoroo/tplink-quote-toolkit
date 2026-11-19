---
name: tplink-warranty-lookup
description: 查 TP-LINK 产品保修期（保换/保修几年）。当用户问"XX型号保修几年""质保多久""查保修期""给清单补保修期""两年还是三年保修""非TP-LINK产品判定"时使用。数据源是官网售后政策页 https://service.tp-link.com.cn/feedback/consult 的前端数据文件 consultData.js（结构化 JSON，5000+ 型号），不是抓渲染页。
---

# TP-LINK 保修期查询

## 数据源（关键）

保修页 `https://service.tp-link.com.cn/feedback/consult` 是 SPA，页面上的搜索框只是**前端过滤**，真实数据全部来自：

```
https://service.tp-link.com.cn/static/js/consultData.js
```

结构：`var consultTable = {"updatetime":"2026-11-18","tablename":"...","products":[{"name":"交换机","detail":[{"type":"三层网管交换机","model":[["TL-NS650-48GP4F",...]],"time":["三年保修"]}]}]}`

- **`model` 是嵌套数组**（list of list of str），必须递归拍平；只按一层遍历会**静默查不到**（曾踩过）。
- `time` 是数组，正常只有一个元素：`一年保换` / `一年保修` / `两年保修` / `三年保修` / `五年保修` / `不保修`。
- 保修标签**按子类（detail.type）整体挂载**，同子类下所有型号共享同一保修期。
- 页面明示：本政策 2025-01-01 起执行；之前购买的产品按原保修卡。

## 用法

脚本：`scripts/warranty.py`（纯标准库，无需第三方依赖）

> **解释器**：查单个型号用任意 `python` 即可；**批量填 xlsx 需要 openpyxl**。
> 若默认 python 没装，用 WorkBuddy 自带的虚拟环境：
> `~/.workbuddy/binaries/python/envs/default/Scripts/python.exe`（Win）/ `.../bin/python`（mac），
> 或直接 `pip install openpyxl`。

```bash
# 单型号 / 多型号
python scripts/warranty.py TL-NS650-48GP4F
python scripts/warranty.py TL-NS650-48GP4F TL-SG1008D TL-SG2210

# 模糊找型号（给系列名前缀，列出全部变体）
python scripts/warranty.py --like NS650

# 强制刷新官网数据（默认缓存 7 天内复用）
python scripts/warranty.py --refresh TL-NS650-48GP4F

# 批量填 xlsx 保修期列（改前自动 .bak 备份）
python scripts/warranty.py --xlsx "D:\path\清单.xlsx" --sheet 清单二 --model-col G --warranty-col M
#   表头行默认自动跳过（型号列含 型号/名称/产品/设备/材料/品名/规格 即跳过）；表头写别字就用 --header-row 1
#   先 --dry-run 预览一遍再落盘
```

默认输出人类可读表格（含大类/子类/型号/保修期/数据更新日期）。加 `--json` 输出机器可读结果。

## 批量填表规则

1. **先 dump 表头确认列位**，不要凭默认值猜（清单模板每次都不同，列位错会静默全错）。
2. 单元格多型号：按 `\n , ， 、 /` 拆分，去掉 `*3` 这类数量后缀、全角空格，逐个查。
3. 查不到 ≠ 非 TP-LINK：`YLS*` 是 TP-LINK 域联子品牌（三年保修），`TL-` 才是型号前缀的常见形态，但**先看有没有同前缀命中**，再判"未收录"。
4. 写值只写保修标签原文（如 `三年保修`），不要自己改写成"3年"。

## 判读注意

- 塑壳/SOHO 交换机常见 `一年保换`（换新），机架式商用/三层交换机多为 `三年保修`。
- 光模块、线缆、配件多为 `一年保修` 或 `不保修`。**实测（2026-11-18 数据）**：`TL-EC6-305` 等综合布线「网线」子类整体 `不保修`，`TL-TC862-3` 电缆也不保修；报清单时这类要提前跟销售打招呼，别承诺质保。
- 若型号不在表内，先 `--like` 查同系列，判断是写法差异（后缀 `-G`/`-RPS`/颜色词/套装）还是真未收录。
- 回答时带上数据日期（`updatetime`），并说明"以官网当前政策为准，2025-01-01 前购买按原保修卡"。
