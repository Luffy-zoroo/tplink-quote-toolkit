# KBS 接口速查（实测 2026-10-23）

平台：蓝凌 Landray KMSS 改造。**站点地址不写死**，由环境变量 `TP_KBS_BASE`
或配置项 `kbs.base` 指定；下文所有路径都相对于该 base。

## 1. 登录

```
GET  /login.jsp                       # 取初始 cookie
POST /j_acegi_security_check          # form-urlencoded
     j_username=<你的账号邮箱>
     j_password=***
     j_redirectto=
```

成功标志：`302` + `Set-Cookie: LtpaToken=...`（Domain 为你的站点域）+ `SESSION`。
失败不会报错，只会重新返回登录页 —— 用"能否拿到 LtpaToken"判断。

## 2. 会话初始化（关键）

```
GET /sys/ftsearch/searchBuilder.do?method=search
GET /sys/portal/page.jsp
```

漏掉这两步，第 3 步的检索接口会返回**结构完整但内容为空**的 JSON
（`queryPage` 为 `{}`、`escaperStr` 为 null），不抛异常、不提示，极容易误判"库里没资料"。

## 3. 全文检索

```
GET /sys/ftsearch/searchBuilder.do
     ?method=search
     &queryString=<关键词>
     &newLUI=true&searchAll=true
     &pageno=1
     &resultType=json
     [&bond=and|or]
     [&searchKnowledge=true&modelName=KmsWikiMain;KmsMultidocKnowledge]
```

请求头建议带：`X-Requested-With: XMLHttpRequest`、`Referer: .../searchBuilder.do?method=search`。

要点：
- **不传 `bond` 时服务端按 `or` 处理**。中文会被拆成单字，`防溺水` → 防/溺/水 三路召回。
  实测同一关键词：`or` 8558 条、`and` 69 条。
- 分页 `rowsize` 默认 10，翻页靠 `pageno`，单次请求不要指望超过 10 条。
- 翻到超出范围时 `queryPage.list` 为空数组，据此判断结束。

### 返回结构（节选）

```
{
  "searchInfo": "共8558 条搜索记录, 1 页, 当前第 856 页",
  "queryPage": {
      "totalrows": 8558,
      "pageno": 1,
      "list": [ { "score": .., "docId": .., "lksFieldsMap": {..} } ]
  },
  "entriesDesign": [ {"modelName": "KmsMultidocKnowledge", "ftSearchCount": 5, "title": "文档知识库"} ],
  "relevantwordList": ["防溺水预警系统", "防溺水监控解决方案", ...]
}
```

`lksFieldsMap` 里每个字段是 `{name, value, ...}`，**取 `.value`**。

| 字段 | 含义 | 备注 |
|---|---|---|
| `subject` | 文档标题 | 常为空，尤其对附件型文档 |
| `fileName` | 附件名 | 标题兜底取这个 |
| `docSubject` | 少数模型有 | 再兜底 |
| `fullText` / `content` / `xmlcontent` | 正文命中片段 | 含 `<font>` 高亮，需 strip |
| `creator` | 创建人 | |
| `createTime` | 创建日期 | `YYYY-MM-DD` |
| `modelName` | 模型全名 | 末段即 `KmsMultidocKnowledge` 等 |
| `docKey` | 文档唯一键 | |
| `linkStr` | 相对地址 | 拼 `BASE` 即文档页；`fdId` 在 query 里 |
| `mimeType` | 附件类型 | |
| `docReadCount` | 浏览量 | |

模型名对照：`KmsMultidocKnowledge` 文档知识库、`KmsWikiMain` 维基、`KmsLearnCourseware` 课件、
`KmComminfoMain` 常用资料、`KmsKemMain` 原子知识、`SysNewsMain` 新闻。

## 4. 文档页与附件

```
GET /kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=<fdId>
```

页面里内联一段 JS：

```js
attachmentObject_attachment.addDoc("文件名.pptx","<附件fdId>",true,"<mime>","<size>","<docId>","79");
```

- 文件名可能是 `\uXXXX` 转义（中文），要 `unicode_escape` 还原。
- 一个文档可能有多个附件（正文页那条通常 0 个附件，附件页那条才有）。

下载：

```
GET /sys/attachment/sys_att_main/sysAttMain.do?method=download&fdId=<附件fdId>
```

返回二进制。实测 11MB / 49MB 的 pptx 都正常，超时给到 90s 以上。

## 5. 已知失败模式

| 现象 | 原因 | 处理 |
|---|---|---|
| 检索返回 `queryPage: {}` | 没做会话初始化 | 补 GET searchBuilder 页 |
| 结果几万条、全是无关 | `bond` 默认 or | 加 `bond=and` |
| 标题整列为空 | 只读了 `subject` | 兜底 `fileName` |
| 附件数为 0 | 用的是正文页的 fdId | 换附件页那条记录的 fdId，或遍历去重后的两条 |
| 下载到 HTML 而非文件 | fdId 传成了文档 fdId | 附件 fdId 是 `addDoc` 的第二个参数 |
| 401/跳登录 | LtpaToken 过期 | 重新 `login()` |

## 6. 实测参考数据（2026-10-23）

- `防溺水`：or 8558 / and 69；去重后 59 份；标题直接命中 4 份（2022 母版方案、2025 来宾市、2025 曲靖市、2022 微信推文）。
- `图门`：and 检索可出平台介绍、纳管第三方、各行业方案；平台能力相关文档集中在 2025-06 之后的版本。
- 好用的图源：来宾市防溺水方案 S09（太阳能+4G+音柱+TUMS 拓扑）、S16（四类报警通道与远程喊话实景）。
