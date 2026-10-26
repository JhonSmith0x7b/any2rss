# any2rss

将网页或 RSS 转为持久化 RSS 2.0，支持通过 AI Agent 生成网页提取规则，以及可选 LLM 翻译。

## 运行

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/uvicorn any2rss.app:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/>，首页列出自己添加的订阅；点击新建打开配置，点击详情查看该源的配置、进度和最近文章。

- 网页来源：输入网址和需求，复制提示词交给可访问网页的 Agent，粘贴返回 JSON。支持 HTTP、Playwright、列表分页及正文排除规则。
- RSS 来源：填入 RSS 2.0 地址即可。
- 可选 AI 翻译：默认 Qwen 3.7 Flash（OpenRouter，`qwen/qwen3.7-flash`），在本机 `.env` 设置 `OPENROUTER_API_KEY`；也可选择 DeepSeek Flash（`deepseek-flash`），使用 `DEEPSEEK_API_KEY`。语言支持简体中文、英语和日语，密钥不写入订阅 JSON。默认仅翻译标题，摘要与正文保留原文；后台可开启“翻译正文”。旧配置未指定该开关时同样只翻译标题，历史成品随后续正常更新替换。
- 发布按真实条目上限抓取并执行插件，每篇成功后立即保存并合并到 `/feeds/{id}.xml`；每 3 秒更新页面进度。定时更新由 Web 服务调度，须保持服务运行。
- 详情中可编辑或删除。编辑保留 RSS 地址，部分成功时保留旧文章并合并新文章；新配置全部完成后切换为新内容。删除停止调度并关闭 RSS 入口，本机历史记录保留，ID 暂不重复使用。
- 后台最多同时处理 2 个订阅；翻译显示文章、批次、重试与最近响应时间。格式错误会在请求预算内拆小批次恢复，单篇失败后继续处理其余文章并补试一次；仍有失败则显示“部分成功”，成功文章已发布；失败文章、超时未开始的链接和待翻译原文持久化，下轮按订阅更新间隔重试，即使文章已从列表页消失也会重试。
- SQLite 默认位于 `data/any2rss.sqlite3`，可用 `ANY2RSS_DB` 更改。翻译缓存持久化，订阅读取不调用模型。

三个早期内置示例已停用，不再列出、提供 RSS 或后台采集。旧历史数据及解析代码保留；不需要启动旧 `any2rss.worker`。

## Docker

```sh
docker compose up -d --build
```

Compose 只运行 Web 服务，共享 `data/` 并从 `.env` 读取翻译密钥，默认绑定本机 8000 端口。本地默认不启用登录；公网部署必须启用账号认证。

## 验证

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

测试使用临时数据库。旧示例的回归测试显式启用归档兼容路径，不代表运行服务仍提供内置订阅。

协议见 [配置规范](docs/configuration.md)。`GET /api/subscriptions` 获取列表，`GET /api/subscriptions/{id}` 获取详情；`PUT` 修改、`DELETE` 删除；`POST /api/config/publish` 发布。

## 公网部署

生产部署说明见 [deploy/README.md](deploy/README.md)。生产模式强制登录，RSS 地址使用独立的只读签名令牌，可直接交给 RSS 阅读器；令牌相当于该订阅的阅读权限，请勿公开分享。页面和 API 需要登录，健康检查 `/healthz` 不包含订阅信息。

GitHub Actions 在 `main` 分支测试通过后，通过受限 SSH 密钥部署到现有服务器。密钥、登录凭据、数据库和翻译 API Key 均不进入仓库。
