# QuotaNoa Client

用于 [QuotaNoa-Bot](https://github.com/LeiSureLyYrsc/QuotaNoa-Bot) `Server_Mode` 的独立 Python 客户端。

客户端不依赖 NoneBot，也没有聊天机器人或凭证管理功能，额度重置功能默认关闭（仅可选开启 Codex 官方重置券消费）。它主动通过 WebSocket 连接 Bot 的独立 FastAPI 服务，在收到查询请求后读取本机 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 的额度信息并返回结果。

## 功能

- 主动连接 Bot 的 `Server_Mode`，适合客户端位于 NAT 或家庭网络后的场景
- 查询本机 CLIProxyAPI 的 Claude、Codex、Antigravity、Kimi、xAI 等额度
- 包含绝对时间戳 `reset_at`、订阅到期时间与 Codex 可用重置点数查询
- 支持按平台或账号查询
- 自动心跳、断线重连和指数退避
- 每个客户端使用独立名称和独立连接密钥
- 同一服务器同一时刻不允许多个同名客户端连接
- 不回传 `CPA_MANAGEMENT_KEY`、Access Token 或 `auth_index`
- 默认只读，开关启用时仅允许 Codex 官方重置券消费

## 安全边界

客户端只接受受控协议动作：

```text
quota.query
codex.refresh
```

本机 CLIProxyAPI 管理接口只允许：

```text
GET  /v0/management/auth-files
POST /v0/management/api-call
```

其中 `/api-call` 只能请求代码内置的额度上游地址，并同时校验固定 HTTP 方法（例如 `wham/usage` 仅限 `GET`，`wham/rate-limit-reset-credits/consume` 仅限 `POST`）。服务器不能传入任意 URL、Header 模板、请求方法或管理 API 路径。

此外，Codex 重置功能受客户端本地配置 `CODEX_REFRESH_ENABLED` 控制（默认为 `false` 关闭）。关闭时客户端会直接在本地拒绝 `codex.refresh` 指令，零网络副作用。

客户端没有以下实现：

- `reset-quota`
- 凭证启用、禁用或删除
- OAuth 登录
- 配置读写
- 日志读取或清理
- 通用 HTTP 代理
- Shell 命令执行

## 环境要求

- Python 3.10+
- 可访问本机 CLIProxyAPI 管理接口
- 可通过 WebSocket 访问已启用 `Server_Mode` 的 QuotaNoa-Bot
- 推荐使用 [uv](https://docs.astral.sh/uv/)

## 快速开始

```bash
git clone https://github.com/LeiSureLyYrsc/QuotaNoa-Client.git
cd QuotaNoa-Client
uv sync
```

复制配置文件：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

## Docker Compose

预构建镜像发布到：

```text
ghcr.io/leisurelyyrsc/quotanoa-client:latest
```

先复制并编辑配置：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

容器访问宿主机上的 CLIProxyAPI 时，将 `.env` 中的地址设置为：

```env
CPA_BASE_URL=http://host.docker.internal:8317
```

`SERVER_URL` 必须指向 Bot 的实际 WebSocket 地址；Bot 在另一台机器或域名后时不要使用容器内的 `127.0.0.1`。

拉取并启动：

```bash
docker compose pull
docker compose up -d
```

查看日志：

```bash
docker compose logs -f quotanoa-client
```

更新到最新镜像：

```bash
docker compose pull
docker compose up -d --remove-orphans
```

仓库的 GitHub Actions 会在以下情况自动构建并发布 `linux/amd64`、`linux/arm64` 镜像：

- 推送到 `main`：更新 `latest` 和提交 SHA 标签
- 推送 `v*` 标签：发布对应版本与 SemVer 标签
- 手动运行 `Build and publish Docker image` workflow

GHCR 包首次发布后可能需要在 GitHub Packages 设置中改为公开。若包保持私有，拉取前需登录：

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
```

编辑 `.env`：

```env
CLIENT_NAME=Home
SERVER_URL=wss://cpa.example.com/v1/client/ws
CLIENT_KEY=replace-with-client-specific-key

CPA_BASE_URL=http://127.0.0.1:8317
CPA_MANAGEMENT_KEY=plaintext-management-password
```

启动：

```bash
uv run quotanoa-client
```

也可以使用模块入口：

```bash
uv run python -m cpabot_client
```

## Bot 服务端配置

在 QuotaNoa-Bot 的 `.env.prod` 中启用独立服务器：

```env
SERVER_MODE=true
CLIENT_NAME=Server

CPA_SERVER_HOST=127.0.0.1
CPA_SERVER_PORT=8320
CPA_SERVER_CLIENT_KEYS={"Home":"replace-with-client-specific-key"}
```

客户端的 `CLIENT_NAME` 必须是 `CPA_SERVER_CLIENT_KEYS` 中的键，`CLIENT_KEY` 必须与对应值一致。

Bot 本机默认占用名称 `Server`。远程客户端请改用唯一名称，例如 `Home`、`HK` 或 `Office`。若名称重复，服务器会保留原连接并拒绝新连接。

## 查询命令

连接成功后，可在 Bot 中使用：

```text
/cpa quota Home
/cpa quota antigravity Home
/cpa quota Home antigravity
/cpa quota codex --client Home
/cpa quota --all
/cpa quota codex --all
/cpa quota codex -all
```

说明：

- `/cpa quota Home`：查询 `Home` 客户端全部平台
- `/cpa quota antigravity Home`：查询 `Home` 的 Antigravity
- `/cpa quota --all`：分别查询本机及所有在线客户端
- `/cpa quota codex --all`：分别查询所有客户端的 Codex
- 全客户端结果按客户端分别展示，不会把不同实例的额度混合计算

## 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `CLIENT_NAME` | `Server` | 客户端名称。远程部署必须改成服务器配置中的唯一名称 |
| `SERVER_URL` | `ws://127.0.0.1:8320/v1/client/ws` | Server Mode WebSocket 地址；公网必须使用 `wss://` |
| `CLIENT_KEY` | 空 | 当前客户端名称对应的连接密钥，必填 |
| `CPA_BASE_URL` | `http://127.0.0.1:8317` | 本机 CLIProxyAPI 地址 |
| `CPA_MANAGEMENT_KEY` | 空 | 本机 CLIProxyAPI 管理密钥，必填且不会上传 |
| `CPA_TIMEOUT` | `15` | 管理接口请求超时，单位秒 |
| `CPA_QUOTA_TIMEOUT` | `25` | 单个上游额度请求超时，单位秒 |
| `CPA_QUOTA_CONCURRENCY` | `4` | 同时查询的账号数量 |
| `CPA_QUOTA_CACHE_TTL` | `60` | 本地额度缓存时间，单位秒 |
| `RECONNECT_MIN` | `1` | 断线后的最小重连等待时间 |
| `RECONNECT_MAX` | `30` | 指数退避的最大等待时间 |

## 本地测试

Bot 和客户端位于同一台机器时：

```env
SERVER_URL=ws://127.0.0.1:8320/v1/client/ws
```

运行测试：

```bash
uv run python -m unittest discover -s tests -v
```

构建发行包：

```bash
uv build
```

## 公网部署建议

不要直接把 Uvicorn 的明文 WebSocket 端口暴露到公网。

推荐结构：

```text
Client -- wss:// --> Caddy/Nginx -- ws://127.0.0.1:8320 --> Server_Mode
```

建议：

1. Bot 的 `CPA_SERVER_HOST` 保持为 `127.0.0.1`。
2. 使用 Caddy、Nginx 或 Traefik 提供 TLS，客户端使用 `wss://`。
3. 为每个客户端生成独立的高强度随机密钥，不要共享密钥。
4. 不要把 `CLIENT_KEY` 或 `CPA_MANAGEMENT_KEY` 写进 URL、日志或仓库。
5. 防火墙只开放反向代理的 443 端口。
6. 不要将 CLIProxyAPI 管理端口 `8317` 暴露到公网。
7. 建议在反向代理增加连接频率限制和来源 IP 限制。
8. 密钥泄露后立即替换 Bot 和对应客户端两侧配置。

生成随机密钥示例：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 常见问题

### `未配置 CLIENT_KEY`

确认已经将 `.env.example` 复制为 `.env`，并填写 `CLIENT_KEY`。

### 连接返回 403

检查：

- `CLIENT_NAME` 是否存在于 Bot 的 `CPA_SERVER_CLIENT_KEYS`
- `CLIENT_KEY` 是否与该名称对应
- 是否错误使用了 Bot 本机保留名称 `Server`
- 是否已有同名客户端在线

### 公网连接失败

确认反向代理支持 WebSocket Upgrade，并且客户端使用 `wss://`。Uvicorn 默认建议仅监听本机地址。

### CLIProxyAPI 返回 401/403

检查客户端本机的 `CPA_MANAGEMENT_KEY`。为避免连续错误触发 CLIProxyAPI 的 IP 封禁，客户端会在鉴权失败后暂停请求一段时间。

## 协议兼容性

当前协议版本为 `1`。客户端会拒绝未知操作，并对不支持的协议版本返回错误。Bot 与客户端建议同时升级。
