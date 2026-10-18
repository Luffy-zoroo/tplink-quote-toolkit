# 控标库字段与型号分布

## 文件位置

| 路径 | 说明 |
|---|---|
| `<控标数据目录>\tplink_kongbiao_data.json` | 权威源，657 型号 / 约 7884 条参数 / 约 543 个加分项 |
| `tplink-search.html`（各会话目录） | 自包含单文件检索页，数据内嵌 `BASE_DATA`，双击即用 |
| `index.html` | GitHub Pages 部署版（密码 kongbiao2026） |

> ⚠️ 通过网页 UI「添加产品」的内容只存在**浏览器 localStorage**，不在这个 JSON 里。
> 要合并需用户导出 JSON。

## 结构

```json
[
  {
    "category": "防火墙",
    "product_name": "",
    "model": "TL-NFW8600E",
    "params_raw": "1、...\n2、...",
    "param_items": [
      {"text": "1、支持...", "is_bonus": false},
      {"text": "★2、支持...", "is_bonus": true}
    ]
  }
]
```

- `param_items[].is_bonus` 为 `true` 即**加分项**（原文带 ★ 或 ▲ 标记），投标时是拉开差距的关键。
- `params_raw` 是整段原文，`param_items` 是拆条结果。填写时用 `param_items` 逐条拼回，保留编号。

## 查询示例

```python
import json
d = json.load(open(r'<控标数据目录>\tplink_kongbiao_data.json', encoding='utf-8'))

# 按型号
[x for x in d if x['model'] == 'TL-NFW8600E']

# 按类别取所有加分项型号
[x['model'] for x in d if x['category'] == 'AP'
 and any(p['is_bonus'] for p in x['param_items'])]

# 全文搜
[x['model'] for x in d if '算法商城' in x['params_raw']]
```

## 型号分布（部分大类）

| category | 数量 | category | 数量 |
|---|---|---|---|
| 枪机 | 29 | 框架路由器 | 6 |
| 考勤门禁 | 28 | 数据中心交换机 | 5 |
| 吸顶AP | 23 | 工业级路由器 | 5 |
| 面板AP | 21 | 停车道闸 | 18 |
| 半球 | 18 | 人行道闸 | 16 |
| IPC | 18 | 电动车充电桩 | 11 |
| 硬盘录像机 | 17 | 防火墙 | 13 |
| 网线 | 17 | 上网行为管理 | 7 |
| 工业以太网交换机 | 41 | TUMS管理一体机 | 8 |

## 命名不规范的坑

- 部分型号带后缀，如 `TL-VS7500（含TL-NVDU7606S业务板卡）`、`TL-NS650-24GPE4T-RPS （其余-RPS机型可以以此为模板修改）`
  → 精确匹配必须先做，失败再用**前缀/包含**匹配。
- 有些型号完全不在库里（如 `TL-NVD6004C`、`TL-NIPC5484EP-4`、`TL-VE46U-X` 早期版本）
  → 走官网兜底。

## 品牌敏感词统计（用于评估清洗范围）

扫描全库命中次数：

| 字眼 | 命中条数 |
|---|---|
| 云平台 | 189 |
| TP-LINK / Tp-Link / tp-link | 52 |
| TUMS / tums | 36 |
| 商云 | 21 |
| 算法商城 | 15 |
| TP-SRC / SRC码控 | 4 |
| 商云APP | 3 |

→ 说明「参数里混着品牌字眼」是**普遍现象**，每次填参数都必须过一遍清洗。
