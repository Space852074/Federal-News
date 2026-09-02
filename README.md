# Federal News

美国联邦机构新闻聚合器：抓取最近 5 天新闻、翻译标题与正文、增量写入 Notion。

GitHub：<https://github.com/Space852074/Federal-News>

## 本地启动

```powershell
python app.py
```

浏览器打开 <http://127.0.0.1:8765>。

## 增量同步流程

云端任务严格按以下顺序执行：

1. 抓取各机构新闻列表，并翻译标题。
2. 查询 Notion 的 `UniqueKey`，只新建 Notion 中不存在的新闻，先写入部门、日期、中文标题和网址。
3. 将本轮成功新建的键保存到 `data/notion-new-keys.json`。
4. 只读取这些新新闻的详情页，用标题和正文检查监控关键词。
5. 只有命中关键词的新闻才翻译正文，并将中文正文回填到 Notion 的 `内容` 字段；普通新闻不翻译正文，内容栏保持为空。
6. 不会为 Notion 已有记录重新读取详情页或抓取正文。

例外：如果已有记录的标题带 `🔴` 或 `🟡`、但 `内容` 仍为空，后续任务会继续补抓正文。司法部页面遇到 Akamai 验证页时，程序改用 DOJ 官方 JSON API 获取正文。

监控关键词会检查英文标题和正文：

- `china`、`chinese`：中文标题前加 `🔴`
- `Virus`、`COVID-19`、`vaccine`、`Fentanyl`：中文标题前加 `🟡`
- 同时命中两组：标题前加 `🔴🟡`

## Notion 字段

- `部门`：title
- `日期`：date
- `标题`：rich_text
- `网址`：url
- `内容`：rich_text
- `UniqueKey`：rich_text
- `创建时间`：created_time

GitHub 仓库需要配置以下 Actions Secrets：

- `NOTION_API_KEY`
- `NOTION_DATABASE_ID`
- `NOTION_DATA_SOURCE_ID`（可选；数据库只有一个数据源时可不设置）
- `AZURE_TRANSLATOR_KEY`（Azure Translator F0 的密钥）
- `AZURE_TRANSLATOR_REGION`（区域或多服务资源必填，例如 `eastasia`；单服务
  `global` 资源可不设置）

翻译统一使用 Azure Translator。可选的仓库 Actions Variable
`AZURE_TRANSLATOR_ENDPOINT` 可用于自定义端点；不设置时使用
`https://api.cognitive.microsofttranslator.com`。如果 Azure 配置缺失或调用失败，
任务会明确失败，不会把英文原文误写成“已翻译”。

## GitHub Actions 定时

以台北时间每日运行：

- `06:00`
- `11:50`
- `12:00`
- `18:00`

GitHub Actions 的定时任务可能因平台排队而延迟几分钟启动。
