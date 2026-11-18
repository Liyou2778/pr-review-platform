# 本地开发环境（已配置完成）

> 本机没有 Docker（需要管理员 + 重启），因此用**用户态便携版** Postgres / Redis
> 达到同样的架构：真实 Postgres、真实 Redis、真实模型。
> 不需要管理员权限，不注册 Windows 服务，不写注册表。

## 1. 已安装清单

| 组件 | 版本 | 位置 | 状态 |
|---|---|---|---|
| Python | 3.12.2 | `C:\Program Files\Python312` | 系统已有 |
| uv | latest | `%APPDATA%\Python\Python312\Scripts\uv.exe` | 系统已有 |
| 虚拟环境 | Python 3.12 | `.venv\` | ✅ 已建，依赖已装 |
| PostgreSQL | **17.6**（便携版） | `tools\pgsql\` | ✅ 运行中 `127.0.0.1:5432` |
| Redis | **5.0.14.1**（便携版） | `tools\redis\` | ✅ 运行中 `127.0.0.1:6379` |
| open-code-review CLI | **v1.13.11** | `tools\npm\node_modules\...\opencodereview.exe` | ✅ 已装 |
| OCR provider | DeepSeek `deepseek-chat` | `tools\ocrhome\.opencodereview\config.json` | ✅ 已配 |
| Node.js / npm | v24.19.0 / 11.17.0 | 系统 | 系统已有 |

数据库：`prreview`（开发）、`prreview_test`（测试），用户 `prreview`，trust 认证，仅监听 127.0.0.1。

## 2. 日常命令

```powershell
cd pr-review-platform

# 启动 / 停止 / 查看 Postgres + Redis
.\scripts\services.ps1 start
.\scripts\services.ps1 status
.\scripts\services.ps1 stop

# 一键测试（默认 SQLite 快车道）
.\tasks.ps1 test

# 用生产同款 Postgres 跑同一套契约测试
$env:TEST_DATABASE_URL='postgresql+asyncpg://prreview@127.0.0.1:5432/prreview_test'
.\.venv\Scripts\python.exe -m pytest -q

# 起 API / worker（读 .env：Postgres + Redis）
.\tasks.ps1 api        # 终端 A
.\tasks.ps1 worker     # 终端 B
.\tasks.ps1 replay     # 终端 C，造一次评审
```

## 3. 验证记录（2026-11-18 实测）

| 验证项 | 结果 |
|---|---|
| Postgres 连接 | `psql select version()` → PostgreSQL 17.6 |
| Redis 连接 | `redis-cli ping` → `PONG` |
| 契约测试（SQLite） | 9 passed |
| 契约测试（**Postgres**） | 9 passed |
| API `/healthz` | `storage=postgres, queue=redis` |
| 端到端 | webhook/replay → Redis 队列 → worker → findings 落 Postgres |
| 队列故障兜底 | Redis 入队失败的 run 被 worker 的 DB 扫描捞回并完成 |
| 真实模型评审 | OCR + DeepSeek 审 `tools/ocrdemo`，14.2s，1 条 blocker，$0.0119 |
| 跨 run 去重 | 同一 PR 二次评审 `new_findings=0, suppressed=3` |

## 4. 踩过的两个坑（已修复，面试可讲）

1. **`APP_ENV` 没被 pydantic 读到**：字段名是 `env`，只认 `ENV`。表现是测试环境仍走连接池，asyncpg 连接被不同事件循环复用 → `Event loop is closed`。
   修复：`AliasChoices("APP_ENV","ENV")`；测试统一 `NullPool`。
2. **Redis 5 不支持 RESP3**：redis-py 8 默认发 `HELLO 3` → `unknown command HELLO`。
   修复：`protocol=2`；同时 worker 增加 DB 兜底扫描，队列故障不再丢任务。

## 5. OCR CLI 的两个 Windows 坑

1. **npm 11 的 allow-scripts**：postinstall 默认被拦，二进制不会下载。
   → 安装必须加 `--allow-scripts=@alibaba-group/open-code-review`。
2. **配置目录不可写**：CLI 会 `chmod $HOME/.opencodereview/config.json`，在某些 Windows 用户目录下报 `Access is denied`。
   → 把 `USERPROFILE/HOME` 指向项目内 `tools\ocrhome`（已写进 `OCR_HOME` 和适配器）。

## 6. Docker（未安装，可选）

Docker Desktop 需要管理员权限并重启，且会安装 Windows 服务。当前用便携版已覆盖同样能力。
如果之后要跑 `docker compose`：

```powershell
# 管理员 PowerShell
winget install -e --id Docker.DockerDesktop
# 重启后
docker compose up --build
```

`docker-compose.yml` 已写好（postgres + redis + api + 2 个 worker），切换只需改 `.env` 的
`DATABASE_URL` / `QUEUE_BACKEND`，代码不需要改。

## 7. 凭据

- `.env` 已在 `.gitignore` 中；`tools\ocrhome` 也已忽略（内含模型 key）。
- **当前 DeepSeek key 在对话中出现过，建议上线/开源前在控制台轮换一次。**
