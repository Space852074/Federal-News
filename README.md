# 美国政府新闻聚合器

本地运行、最近 5 天过滤、来源健康检查、中文字段、去重与 Notion 导出。

GitHub 仓库：<https://github.com/Space852074/Federal-News>

## 启动

```powershell
python app.py
```

浏览器打开 <http://127.0.0.1:8765>。首次启动会读取 `data/news.json`；点击“立即刷新”会抓取 `sources.json` 中启用的官方来源。

## 翻译

抓取器会保留英文原文，并生成 `title_zh` / `summary_zh` 字段。若设置 `OPENAI_API_KEY`，刷新时可通过 `--translate` 使用 OpenAI 兼容接口翻译；也可在导出的 JSON 中补齐中文后重新载入。

```powershell
$env:OPENAI_API_KEY='...'
python scraper.py --translate
```

## Notion 字段

数据库建议使用以下列：

- `Title`：标题（title）
- `State`：状态（select/status）
- `SortDate`：发布日期（date）
- `txt`：中文摘要（rich_text）
- `link`：原文链接（url）
- `UniqueKey`：去重键（rich_text）
- `created time`：Notion 自动创建时间（created_time）

本地可下载 `notion-import.csv` 直接导入。若要由 Codex 写入现有数据库，只需提供目标 Notion 数据库链接，并确保当前 Notion 连接能访问该页面。

## 使用 Notion API 自动写入

1. 在 <https://www.notion.so/profile/integrations> 创建内部集成，开启读取、插入和更新内容权限。
2. 在目标数据库右上角选择 `••• → Connections → Add connection`，添加刚创建的集成。
3. 仅在当前 PowerShell 会话设置令牌，不要把令牌粘贴进代码：

```powershell
$env:NOTION_API_KEY='ntn_你的令牌'
$env:NOTION_DATABASE_ID='3b893e41daef805e8d84e4ea188657ac'
python notion_sync.py
```

同步器使用 Notion API `2026-03-11`，会自动取得数据源结构、读取现有 `UniqueKey`，只创建缺失记录。网页中的“同步 Notion”按钮同样可用，但启动网页服务的进程必须已经继承 `NOTION_API_KEY` 环境变量。

也可以直接点击网页中的“同步 Notion”，在本地弹窗粘贴令牌。令牌只会传给本机 `127.0.0.1` 服务并放入同步子进程的环境内存，不会写入项目文件或浏览器存储。

## GitHub Actions 云端定时运行

工作流 `.github/workflows/federal-news.yml` 每天按台北时间运行三次：

- `00:20`
- `06:20`
- `12:20`

每次依次执行最近 5 天抓取、中文翻译、去重和 Notion 同步。云端任务不依赖本机开机。

在 GitHub 仓库 `Settings → Secrets and variables → Actions` 中配置：

- `NOTION_API_KEY`：Notion 内部集成令牌。
- `NOTION_DATABASE_ID`：目标数据库 ID。
- `NOTION_DATA_SOURCE_ID`：可选；数据库只有一个数据源时留空。

真实令牌、本地 `data` 运行结果、`vendor` 依赖和浏览器测试目录均由 `.gitignore` 排除，不会上传。
