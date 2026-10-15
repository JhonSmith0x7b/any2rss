# 生产部署

服务器需要 Docker Engine、Docker Compose 2.30+（支持 `env_file.format: raw`）、Git、Bash 和 `flock`。本项目在 ARM Linux 构建镜像，容器只监听服务器回环地址 `127.0.0.1:8200`，由 Tailscale Funnel 提供 HTTPS。

## 首次安装

由现有服务器管理账号执行：

```sh
sudo install -d -m 700 /opt/any2rss /opt/any2rss/bin /opt/any2rss/data /opt/any2rss/backups /opt/any2rss/releases
sudo git clone https://github.com/JhonSmith0x7b/any2rss.git /opt/any2rss/repository
sudo install -o root -g root -m 755 /opt/any2rss/repository/deploy/deploy.sh /opt/any2rss/bin/deploy.sh
```

创建 root 所有、权限 `0600` 的 `/opt/any2rss/runtime.env`（不提交 Git）：

```dotenv
ANY2RSS_REQUIRE_AUTH=1
ANY2RSS_PUBLIC_URL=https://YOUR-PUBLIC-HOST/any2rss
ANY2RSS_AUTH_USERNAME=GENERATED-USERNAME
ANY2RSS_AUTH_PASSWORD_HASH=scrypt$SALT$HASH
ANY2RSS_AUTH_SECRET=GENERATED-RANDOM-SECRET
OPENROUTER_API_KEY=YOUR-OPENROUTER-API-KEY
DEEPSEEK_API_KEY=YOUR-DEEPSEEK-API-KEY
```

不要给 env 值加引号；Compose 使用 raw 模式保留密码哈希中的 `$`。使用 `any2rss.auth.hash_password()` 生成密码哈希，使用密码管理器或密码学随机数生成用户名、密码与会话密钥。明文登录密码仅保存在本机凭据文件或密码管理器。

迁移现有 SQLite 时，使用 SQLite backup API 创建一致快照，然后放入 `/opt/any2rss/data/any2rss.sqlite3`。不要在进程写入期间仅复制数据库主文件，遗漏 WAL。容器以 root 运行，但已移除所有 Linux capabilities，并禁止获取新权限；数据目录由 root 所有即可。

## 受限部署密钥

为本项目单独生成 SSH 密钥；私钥保存至 GitHub 的 `production` environment secret `DEPLOY_SSH_KEY`。公钥加入部署账号的 `authorized_keys`，只允许调用固定脚本，例如：

```text
restrict,command="sudo -n /opt/any2rss/bin/deploy.sh \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 PUBLIC-KEY any2rss-deploy
```

若部署账号没有既有权限，由管理员通过 `visudo` 添加仅允许该 root 所有脚本的 `NOPASSWD` 规则。禁止为该密钥开放交互 Shell、端口转发或任意 Docker 命令。脚本只接受 `deploy <40 位 SHA>`，且 SHA 必须是公开仓库当前 `origin/main`。

GitHub `production` environment 需要四个 secrets：

- `DEPLOY_HOST`：服务器 SSH 主机名或地址。
- `DEPLOY_USER`：部署账号。
- `DEPLOY_SSH_KEY`：专用私钥。
- `DEPLOY_KNOWN_HOSTS`：通过已有可信连接核实的服务器公钥记录。

首次部署可经管理连接执行 `sudo /opt/any2rss/bin/deploy.sh 'deploy FULL_COMMIT_SHA'`。后续推送 `main` 后自动测试和部署，也可手动运行 workflow。

部署脚本在切换前构建镜像并用 SQLite backup API 备份数据库。新版本健康检查失败会恢复上一个容器版本；为避免丢失最新数据，不自动回滚数据库。首次部署失败则停止新容器。备份在 `/opt/any2rss/backups`，发布源码在 `/opt/any2rss/releases`，需按保留策略定期清理。

`/opt/any2rss/bin/deploy.sh` 是独立安装的安全边界，自动部署不会覆盖它。更改脚本后需管理员审查并重新安装。

## 公网入口

配置 Funnel 将 HTTPS 入口转发到 `http://127.0.0.1:8200`。公网 URL（包括使用的子路径）必须与 `ANY2RSS_PUBLIC_URL` 一致。共享已有 Funnel 入口时应只新增本项目路径，保留其他路由；不要重置整个 Serve/Funnel 配置。

验证未登录访问管理页面进入登录页、未登录 API 返回 `401`、无令牌订阅无法读取，并确认从页面复制的签名 RSS URL 能在不带登录 Cookie 的阅读器中正常订阅。生产 Uvicorn 关闭 access log，避免 RSS 令牌进入访问日志。
