# 新闻 Agent

一个可容器化部署的新闻 Agent。服务启动后提供 HTTP 配置页面，登录后可以配置定时任务、新闻源、邮件发送、企业微信发送、收件客户和登录安全。

默认新闻获取链路：

- 优先调用 OpenAI 大模型接口和 Web Search 获取全球热点 TOP10、科技新闻 TOP10
- OpenAI 不可用时，抓取 RSS 候选新闻，并交给开源大模型筛选、排序、生成中英双语摘要
- 开源大模型不可用时，自动回退到 RSS 直接补齐 TOP10

## 启动

```powershell
docker compose up -d --build
```

打开配置页面：

```text
http://localhost:8080
```

首次默认登录：

```text
账号：admin
密码：admin123456
```

登录后请先修改管理员密码。

## 新闻获取方式

页面的“新闻获取方式”支持三层配置。

### OpenAI 新闻获取

```text
使用 OpenAI 大模型获取全球热点和科技新闻 TOP10：勾选
OpenAI API Key：你的 OpenAI API Key
OpenAI 模型：gpt-4.1
OpenAI Base URL：https://api.openai.com/v1
Web Search 工具类型：web_search
```

如果 OpenAI 返回 `insufficient_quota`、API Key 无效或接口不可用，Agent 会自动进入开源大模型兜底。

### 开源大模型兜底

开源模型通常不自带实时联网能力，因此 Agent 会先抓取 RSS 候选新闻，再让开源模型从候选中选出 TOP10、去重、排序、生成中英双语摘要。

Ollama 推荐配置：

```text
OpenAI 不可用时使用开源大模型处理 RSS 候选新闻：勾选
Provider：Ollama
开源模型 Base URL：http://host.docker.internal:11434
开源模型：qwen2.5:7b
候选新闻数：30
开源模型 API Key：留空
```

宿主机准备 Ollama：

```powershell
ollama pull qwen2.5:7b
ollama serve
```

如果使用 vLLM、LM Studio、LocalAI 等 OpenAI-compatible 服务：

```text
Provider：OpenAI-compatible
开源模型 Base URL：http://host.docker.internal:8000/v1
开源模型：你的模型名
开源模型 API Key：按服务要求填写，可为空
```

### RSS 兜底

如果 OpenAI 和开源大模型都不可用，Agent 会直接用 RSS 新闻源补齐邮件，确保任务尽量不中断。

## 页面可配置项

- 新闻获取方式：OpenAI、开源大模型、RSS 兜底
- 定时任务是否启用
- 起始时间，例如 `08:30`
- 相隔时间，单位分钟，例如 `1440` 表示每天一次，`720` 表示每 12 小时一次
- 发送方式：邮件、企业微信，支持同时启用
- 收件客户邮箱，支持逗号或换行分隔多个邮箱
- SMTP 服务器、端口、账号、密码/授权码、发件邮箱
- 企业微信 CorpID、AgentID、应用 Secret、接收用户 UserID
- 全球热点 RSS 新闻源、科技新闻 RSS 新闻源
- 每类新闻条数
- 是否生成中英双语摘要
- 是否在邮件中显示新闻图片
- RSS 无图时是否从原文页面补图
- 是否只生成内容不发送邮件

配置保存到：

```text
./data/config.json
```

Docker Compose 已将该目录挂载到容器内 `/data`，容器重启后配置不会丢失。

## 多客户邮箱

收件邮箱可以逗号分隔：

```text
swh_2018@126.com,customer1@example.com,customer2@example.com
```

也可以换行填写：

```text
swh_2018@126.com
customer1@example.com
customer2@example.com
```

## 邮件内容与图片

邮件正文使用 HTML 卡片格式：

- 每条新闻包含中文标题、中文摘要、英文标题、英文摘要
- 每条新闻包含来源、发布时间、阅读全文链接
- 如果新闻源或模型提供图片，会在新闻卡片里显示图片
- 如果 RSS 没有图片，并启用了“从原文页面补图”，Agent 会尝试读取原文页面的 `og:image` 或 `twitter:image`

部分邮箱客户端可能默认拦截远程图片，需要点击“显示图片”。

## 企业微信发送

在页面的“企业微信”页签中填写：

```text
启用企业微信应用消息发送：勾选
企业微信 CorpID：企业微信后台的企业 ID
企业微信 AgentID：自建应用的 AgentId
应用 Secret：自建应用的 Secret
接收用户 UserID：通讯录里的 UserID，多个用户用逗号或换行分隔
API Base URL：https://qyapi.weixin.qq.com/cgi-bin
```

Agent 会调用企业微信接口获取 `access_token`，然后用应用消息的 `markdown` 类型发送“全球热点 TOP10”和“科技新闻 TOP10”。如果同时启用了邮件和企业微信，两种渠道都会发送。

## 126 邮箱发件说明

如果使用 126 邮箱作为发件邮箱：

- `SMTP 服务器` 通常填 `smtp.126.com`
- `SMTP 端口` 通常填 `465`
- 勾选 `使用 SMTP SSL`
- `SMTP 密码/授权码` 填邮箱后台生成的 SMTP 授权码，不是网页登录密码

## 常见问题

### OpenAI 额度不足

日志出现：

```text
OpenAI API HTTP 429
insufficient_quota
```

说明当前 OpenAI API Key 的额度不足、账单不可用，或套餐不支持继续调用。Agent 会自动切到开源大模型；如果开源大模型也不可用，则回退 RSS。

### 全球热点出现 Google News 占位内容

如果邮件里出现：

```text
该提要不可用。
This feed is not available.
```

新版 Agent 会自动过滤这类占位条目，并追加内置备用 RSS 源补齐 TOP10。也可以在页面的“全球热点 RSS”里使用：

```text
https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans
https://news.google.com/rss/search?q=%E5%85%A8%E7%90%83%20%E7%83%AD%E7%82%B9%20when%3A1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans
https://news.google.com/rss/search?q=international%20news%20when%3A1d&hl=en-US&gl=US&ceid=US:en
```

### 缺少 SMTP 服务器

如果日志或页面状态显示：

```text
缺少邮件配置：SMTP 服务器
```

说明页面里的“SMTP 服务器”还没有填写。使用 126 邮箱发件时通常填：

```text
smtp.126.com
```

## 管理命令

查看日志：

```powershell
docker compose logs -f
```

停止：

```powershell
docker compose down
```

重新构建：

```powershell
docker compose up -d --build
```

## 可选环境变量

现在主要通过 HTTP 页面配置。环境变量只用于首次生成配置文件时的初始值，或用于覆盖服务启动参数。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WEB_PORT` | `8080` | HTTP 配置页面端口 |
| `CONFIG_PATH` | `data/config.json` | 配置文件路径 |
| `ADMIN_USERNAME` | `admin` | 首次启动管理员账号 |
| `ADMIN_PASSWORD` | `admin123456` | 首次启动管理员密码 |
| `USE_OPENAI_NEWS` | `true` | 首次启动是否优先使用 OpenAI 获取新闻 |
| `OPENAI_API_KEY` | 空 | 首次启动 OpenAI API Key |
| `OPENAI_MODEL` | `gpt-4.1` | 首次启动 OpenAI 模型 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI API Base URL |
| `OPENAI_WEB_SEARCH_TOOL` | `web_search` | OpenAI Web Search 工具类型 |
| `USE_OPEN_SOURCE_NEWS` | `true` | OpenAI 不可用时是否启用开源大模型兜底 |
| `OPEN_SOURCE_PROVIDER` | `ollama` | `ollama` 或 `openai_compatible` |
| `OPEN_SOURCE_BASE_URL` | `http://host.docker.internal:11434` | 开源模型服务地址 |
| `OPEN_SOURCE_MODEL` | `qwen2.5:7b` | 开源模型名称 |
| `OPEN_SOURCE_API_KEY` | 空 | 开源模型服务 API Key，可为空 |
| `OPEN_SOURCE_CANDIDATE_COUNT` | `30` | 提供给开源模型筛选的 RSS 候选新闻数 |
| `TZ` | `Asia/Shanghai` | 首次启动时区 |
| `SCHEDULE_TIME` | `08:30` | 首次启动起始时间 |
| `SCHEDULE_INTERVAL_MINUTES` | `1440` | 首次启动相隔时间，单位分钟 |
| `BILINGUAL_EMAIL` | `true` | 首次启动是否生成中英双语摘要 |
| `INCLUDE_IMAGES` | `true` | 首次启动是否在邮件中显示新闻图片 |
| `FETCH_ARTICLE_IMAGES` | `true` | RSS 无图时是否尝试从原文页面补图 |
| `EMAIL_ENABLED` | `true` | 首次启动是否启用邮件发送 |
| `EMAIL_TO` | `swh_2018@126.com` | 首次启动收件邮箱 |
| `SMTP_HOST` | 空 | 首次启动 SMTP 服务器 |
| `SMTP_PORT` | `465` | 首次启动 SMTP 端口 |
| `SMTP_USERNAME` | 空 | 首次启动 SMTP 账号 |
| `SMTP_PASSWORD` | 空 | 首次启动 SMTP 密码/授权码 |
| `SMTP_FROM` | SMTP 账号 | 首次启动发件邮箱 |
| `WECOM_ENABLED` | `false` | 首次启动是否启用企业微信发送 |
| `WECOM_CORP_ID` | 空 | 企业微信 CorpID |
| `WECOM_AGENT_ID` | `0` | 企业微信自建应用 AgentID |
| `WECOM_APP_SECRET` | 空 | 企业微信自建应用 Secret |
| `WECOM_TO_USERS` | 空 | 企业微信接收用户 UserID，多个用逗号分隔 |
| `WECOM_API_BASE_URL` | `https://qyapi.weixin.qq.com/cgi-bin` | 企业微信 API Base URL |
| `RUN_ONCE` | `false` | 启动后只运行一次，不启动页面和调度器 |
| `DRY_RUN` | `false` | 只打印发送内容，不调用邮件或企业微信发送接口 |

## Docker Hub 连接失败

如果构建时出现：

```text
failed to fetch oauth token: Post "https://auth.docker.io/token": dial tcp ... connectex
```

说明 Docker 没有成功连接到 Docker Hub，不是 Agent 代码错误。可以在 Docker Desktop 配置代理，或在 `Settings -> Docker Engine` 中配置可用的 registry mirror。

