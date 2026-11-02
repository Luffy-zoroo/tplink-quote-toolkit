---
name: tplink-kbs-search
summary: TP-LINK KBS 知识库检索 + 附件下载 + PPT 页导图——HTTP 直连，无需浏览器（站点地址自填）
description: 当用户要在 TP-LINK KBS 知识库里找方案、找案例、找产品资料、找历史投标材料（"KBS 里查一下""知识库有没有 xx 方案"），或要从方案 PPT 里提取官方拓扑图/界面截图时用本技能。涵盖 ACEGI 登录、全文检索（含「与/或」匹配差异）、附件下载、PPT 指定页导出 PNG、清单 HTML 生成。默认走「与」匹配避免中文拆字噪音，未初始化会话会静默返回空结果，这两点是最容易踩的坑。
agent_created: true
---

# TP-LINK KBS 知识库检索

## 适用场景

- 业主/客户问"有没有 XX 方案"，需要先在知识库里摸底（防溺水、防汛、智慧校园、景区……）。
- 写方案时要找同类项目案例、官方拓扑图、界面上墙截图。
- 投标前找历史材料：控标参数整理思路、第三方纳管方案、项目成功案例。
- 需要把检索结果整理成可交付的清单（客户版/内部版）。

**不适用**：查产品价格与折扣（走 `tplink-crm-readonly`）、查控标参数（走 `tplink-quote-autofill` / 本地控标库）。

## 前置：配站点和账号

**站点地址和凭据都不写死在代码里**（本仓库是公开的），必须自己填。按顺序读，先命中先用：

1. 环境变量 `TP_KBS_BASE` / `TP_KBS_USER` / `TP_KBS_PASSWORD`
2. `~/.workbuddy/tplink-toolkit.config.json`：

```json
{
  "kbs": {
    "base": "https://kbs.<你的域名>",
    "user": "you@example.com",
    "password": "***"
  }
}
```

3. 本技能 `scripts/config.local.json`（同上结构，仅本机用）

> `base` 没配会直接报错并给出填写指引，**不会猜地址**。这和 `crm_base` 的处理方式一致：
> 配置了却读不到，和根本没配，是两回事。

配好后先验证：`python scripts/kbs.py login` → 输出「登录成功」即通。

## 常用命令

```bash
# 1) 检索（默认「与」匹配，翻 3 页）
python scripts/kbs.py search 防溺水 --pages 3

# 2) 限定知识库模型（只看文档知识库+维基）
python scripts/kbs.py search 防溺水 --pages 3 --model "KmsWikiMain;KmsMultidocKnowledge"

# 3) 看某文档有几个附件
python scripts/kbs.py doc 180d9c8fe8a9c7db7ee0d8e48f09f13c

# 4) 下载附件
python scripts/kbs.py att 180d9c8fe8a9c7db7ee0d8e48f09f13c -o ./att

# 5) 一步出清单页（单文件 HTML，可筛选）
python scripts/kbs.py report 防溺水 --pages 6 -o 防溺水资料清单.html
```

`search` 每行输出：`序号 | 日期 | 标题 | 作者 | 模型`，末尾汇总「命中 N 条 / 去重 M 份」。

## 从方案 PPT 里取官方图

客户版方案里，**官方拓扑图和界面上墙截图比自己画的更有说服力**。流程：

1. `kbs.py doc <fdId>` 找到 pptx 附件 → `kbs.py att` 下载；
2. 用 `scripts/slides.ps1` 把需要的页导成 1920×1080 PNG：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/slides.ps1 `
  -Pptx "D:\deck.pptx" -Pages 9,16 -Out "D:\out"
```

> ⚠️ 两个必须注意的点：
> - 直接 `& script.ps1` 会被执行策略拦（本项目机器默认禁止运行脚本），**必须**带 `-ExecutionPolicy Bypass -File`。
> - 脚本文件必须是 **UTF-8 带 BOM**，否则 PowerShell 5.1 按 ANSI 读，中文注释变乱码直接语法报错。本技能里的 `slides.ps1` 已处理，改它时别用无 BOM 保存。

3. 导出图一般带幻灯片页眉（蓝色标题条 + 页码），用 PIL 裁掉再入文档：

```python
from PIL import Image, ImageChops
im = Image.open('s09.png').convert('RGB')
w, h = im.size
im = im.crop((0, 62, w, h))                       # 按实际页眉高度调
bg = Image.new('RGB', im.size, (255, 255, 255))
bb = ImageChops.difference(im, bg).convert('L').point(lambda p: 255 if p > 8 else 0).getbbox()
im.crop(bb).save('fig.png')                       # 顺带去白边
```

4. 源 PPT 里的明显笔误（实测遇到过 `TIUMS` 应为 `TUMS`）**先修再用**，业主版不能带错别字。

## 关键事实与坑（实测 2026-10-23）

| # | 事实 | 说明 |
|---|---|---|
| 1 | 登录是 ACEGI：`POST /j_acegi_security_check`，字段 `j_username` / `j_password` | 成功 302 + `Set-Cookie: LtpaToken`，无验证码 |
| 2 | **登录后必须先 GET 一次检索页**（`searchBuilder.do?method=search` + `portal/page.jsp`） | 不做这步，接口返回**空 JSON 且不报错**，最容易误判"没资料" |
| 3 | 检索接口 `GET /sys/ftsearch/searchBuilder.do?method=search&resultType=json` | 需要 `X-Requested-With` 与 Referer；否则可能拿不到 JSON |
| 4 | 默认 `bond=or`：中文被拆字，噪音放大两个数量级 | 实测 `防溺水`：或匹配 8558 条，与匹配 **69 条**。默认走 and |
| 5 | 标题字段不固定：`subject` 常为空，真标题在 `fileName` | 取值顺序 subject → fileName → docSubject |
| 6 | 摘要/正文片段在 `fullText` / `content` / `xmlcontent`，含 `<font>` 高亮标签 | 必须 strip 标签 |
| 7 | 同一文档有「正文页」和「附件页」两条记录，**fdId 相同** | 按 fdId 去重；实测 69 → 50~59 份 |
| 8 | 附件列表在文档页的 `addDoc("名称","附件fdId",…)` 里，不是接口 | 正则抓，别找 API |
| 9 | 下载地址 `sysAttMain.do?method=download&fdId=<附件fdId>` | 返回二进制，PPT 常见 10~50MB，超时要给足 |
| 10 | 图门 = TUMS，同一套平台两个叫法 | 搜"图门"能出平台介绍、纳管第三方、各行业方案 |

## 输出建议

- 清单类需求 → `report` 出单文件 HTML（自带过滤框、三档分组、关键词高亮），比甩一屏文字好用。
- 客户版材料里引用图片时，图注**别写别的省市/单位真名**，改成"某市水利局"这类脱敏表述。
- 检索到的内部方案若要外发，先确认有没有"仅对内"标记（部分方案标题就写着「仅对内页面」）。

## 维护

- 接口细节、返回字段、失败模式见 `references/api-notes.md`。
- 站点地址**没有默认值**，必须由 `TP_KBS_BASE` 或配置文件的 `kbs.base` 提供。
