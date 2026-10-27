# AI Agent 配置协议 v1

此协议用于配置草稿和发布订阅。发布后由 Web 服务内的调度器执行自定义来源，旧内置示例已停用。
中国作家网与 Book Marks 仅保留配置示例，不会自动创建或更新订阅。

## 用户流程

网页来源：填写入口网址和采集要求 → 复制页面生成的 Agent 提示词 → 让具备浏览器能力的 Agent 实际访问网站并验证至少 3 篇文章 → 粘贴返回 JSON → 结构校验 → 选择插件与输出设置 → 保存本地草稿或导出。

RSS 来源：填写 RSS URL → 选择插件与输出 → 校验/保存/导出。无需生成网页规则。

发布会按配置的条目上限实际抓取一轮，并执行已启用插件；成功后才提供 RSS 地址。当前结构校验只检查类型、必填字段、标准 CSS 语法、字段读取、插件支持情况和上下限，不执行来源请求，也不能证明网站规则有效。

## Agent 返回的来源文档

顶层只允许 `schema_version` 与 `source`。完整机器可读定义通过 `GET /api/config/source-schema` 下载，由服务端 Pydantic 模型生成，提示词和校验共用同一个定义。

```json
{
  "schema_version": 1,
  "source": {
    "type": "rss",
    "url": "https://bookmarks.reviews/feed/"
  }
}
```

网页 `source` 字段：

| 字段 | 规则 |
| --- | --- |
| `type` | 固定 `webpage` |
| `url` | 公开 HTTP(S) 入口，无账号密码、无 fragment |
| `loading` | `method`: `http` 或 `playwright`；`wait_selector` 仅用于 Playwright |
| `list` | `item_selector` 直接选中文章链接；`link_attribute` 固定 `href` |
| `article.title` | 字段读取规则，必须为 `text` |
| `article.body` | 字段读取规则，必须为 `html`，取元素内部 HTML |
| `article.published_at` / `author` | 可选，无可靠规则时为 `null` |
| `pagination` | `mode`: `none` / `next` / `scroll`；`next` 必须有下一页选择器；`scroll` 必须用 Playwright |
| `pagination.max_pages` | 1–20，包含首页/首屏；`none` 固定 1 |
| `timezone` | 有效 IANA 时区，例如 `Asia/Shanghai` |

字段读取规则：

- `selector`：标准 CSS，取首个匹配节点。多节点正文应选中共同的正文容器。
- `exclude_selectors`：仅 `article.body` 支持，最多 30 个标准 CSS 选择器。以正文容器为范围，在副本中删除匹配的后代节点及子树，再读取内部 HTML；不删除容器本身，保留节点前后的正文。省略等于不额外排除。例如 `[".blog-card", "script", "style"]`。
- `read`：`text`（节点纯文本）、`html`（内部 HTML）或 `attribute`（属性值）。
- `attribute`：仅 `read=attribute` 时填写，如 `content`、`datetime`。
- `regex`：可选 Python 正则，恰好一个捕获组；字段读取并 trim 后，取该组值。不是任意执行代码。运行器限制匹配时间和输入大小。
- `date_format`：仅 `published_at` 可填写 Python `strptime` 格式，例如 `%Y年%m月%d日%H:%M`。省略时支持 ISO 8601 或 RFC 2822 日期。没有可提取日期时使用首次采集时间，记录 `published_at_inferred=true`，后续刷新保持该时间。

运行语义约定：文章链接基于列表页解析为绝对 URL，移除 fragment 后去重；只收集 HTTP(S) 链接。翻页遇到无新文章/无下一页提前停止。规则仅处理列表分页，无法自动补齐文章正文分页。正文清理脚本与事件属性后输出。

Agent 成功时只返回原始 JSON，不加 Markdown 围栏、表格或前后说明。网页来源可在 `source.notes` 字符串数组中记录验证样本和局限；这些是 Agent 自述，不是系统验证证据。未确认样本外的分页情况、缺少作者、可排除的推荐卡片不应阻止生成配置。

确实无法访问或必需能力不支持时，仍只返回 JSON：`{"error":{"code":"unsupported_source","message":"具体阻断原因","required_capability":"需要的能力"}}`。它是失败报告，不能导入为来源；页面展示具体原因。

正文排除已接入通用采集器。保存本地草稿不会执行；点击“发布订阅”才提交服务端任务。

## 页面导出的完整文档

在来源文档之外增加：

```json
{
  "plugins": [
    {
      "name": "llm_translate",
      "enabled": true,
      "model": "qwen/qwen3.7-flash",
      "target_language": "zh-CN",
      "translate_body": false
    }
  ],
  "output": {
    "id": "bookmarks-draft",
    "title": "Book Marks · 简体中文",
    "interval_minutes": 60,
    "item_limit": 50
  }
}
```

以上是完整文档的附加字段片段，需要与 `schema_version` 和 `source` 合并。
`plugins=[]` 表示不处理。当前只支持一个 `llm_translate` 插件、Qwen 3.7 Flash（默认，OpenRouter）或 DeepSeek V4.1 Flash，以及 `zh-CN` / `en` / `ja` 三种目标语言。`translate_body` 默认为 `false`，翻译标题及正文纯文本的前 500 个 Unicode 字符（不足 500 字时取全部）。译文预览作为 RSS 摘要，并在 HTML 转义后放到正文开头，后面保留完整原文；设为 `true` 时翻译标题、摘要和全文。旧配置缺少该字段时也使用预览模式，可在后台通过“翻译全文”开关切换。两种模式的翻译缓存隔离。数组形式为之后按顺序添加插件留出结构。

密钥、Cookie、脚本都不能放进配置。模型密钥继续使用服务器环境配置。未知字段、未知插件、错误类型被拒绝，不静默忽略。

`POST /api/config/validate` 返回规范化配置，`scope=structure_only`。不保存服务器任务、不发布新 RSS，也不改变现有后台采集。`output` 中间隔/条目范围应用于新发布的自定义订阅，不改变内置来源。

## 发布与更新

`POST /api/config/publish` 接收完整配置，结构验证通过后持久化任务，返回 202 和 `status_url`。`GET /api/publications/{id}` 查看抓取/插件进度与错误；成功后 `feed_url` 为 `/feeds/{id}.xml`。`GET /api/publications` 列出服务端订阅。

首次发布和定时更新均最多处理 `item_limit` 篇（实际数量受来源条目数及列表分页设置影响），启用翻译时会真实调用模型；后续按 `interval_minutes` 更新。每篇文章完成插件处理后保存并合并到 RSS；部分失败时保留成功文章，待重试链接和原文持久化后按更新间隔重试。RSS 读取不会触发抓取或模型。没有提取到条目不会发布空订阅。

同标识不能覆盖内置或已成功发布的订阅；初次失败可修正配置后用原标识重新提交。进程重启后恢复未完成任务，插件缓存持久化。Web 服务须保持运行，自定义订阅调度与内置 worker 独立。当前是本机使用，无账号权限系统；发布不等于公网部署。

发布状态中的 `progress` 报告本轮阶段、阶段内完成条数、总数及最后进度时间。发现链接时总数未知，前端显示不定进度；抓取和翻译进度分别计算，不代表已发布。`saved_articles` 表示本轮已保存条数，`pending_articles` 表示待重试条数；`count` 是服务端已保存的成品数，RSS 随成功文章陆续更新。页面自动轮询，无需反复打开 RSS 判断任务是否工作。

## 机核完整正文

机核公开文章可设置 `source.article.body.adapter: "gcores"`。列表仍通过 HTML 获取；正文通过该站公开的 `/gapi/v1/articles/{id}` 接口按 Draft.js 文本块顺序生成 HTML，展开普通图片、GIF 和图集，保留图注与文字链接。无需滚动，也不依赖图集轮播产生的克隆节点。

标题、作者、发布时间仍由原有 CSS 字段规则提取。正文 `selector` 保留网页实际正文容器，`exclude_selectors` 应用于适配器生成的 HTML。其他网站省略 `adapter`。不支持付费或受权限限制的正文；未知正文类型会报错，避免将缺失内容误报为成功。

可参考 [机核来源配置](../examples/gcores-source.json)。该文件仅含来源；插件与输出设置仍由页面配置。
