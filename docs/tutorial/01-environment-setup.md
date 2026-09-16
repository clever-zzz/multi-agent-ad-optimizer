# 环境搭建教程（零基础版）

本教程面向**完全没有编程经验**的读者，一步步带你把本项目所需的开发环境装好。

> **版本说明**：本文对应仓库当前的落地版本 —— `backend/`（FastAPI + LangGraph）与 `frontend/`（React + Vite）。
> 仓库早期的三语言教学 demo（`python/`、`java/`、`golang/`，即 Streamlit / Spring Boot / goroutine 版本）**已经移除**，网上流传的旧截图与旧命令不再适用。
> 想要完整的运行路径与验证步骤，权威文档是 [docs/production/01-quickstart.md](../production/01-quickstart.md)；本文是它的零基础展开版。

---

## 1. 你需要准备什么

- 一台能上网的电脑（Windows / macOS / Linux 均可）。
- 大约 **3～5GB** 可用磁盘空间（Python 虚拟环境 + `node_modules`）。
- 安装软件时的**管理员权限**。
- 耐心：第一次装环境出问题很正常。

**好消息**：默认配置下（`LLM__PROVIDER=mock` + SQLite + `DATA_MODE=mock`）**不需要任何 API Key、不需要联网调用大模型、不需要 Docker、不需要装数据库**。装好 Python 和 Node 就能跑通完整闭环。

---

## 2. Python 3.12 及以上

### 2.1 先确认是否已安装

打开终端（Windows 用 PowerShell，macOS/Linux 用 Terminal）：

```bash
python --version      # Windows
python3 --version     # macOS / Linux
```

显示 `Python 3.12.x` 或更高即可跳过安装。

> **为什么是 3.12 而不是 3.11**：`backend/pyproject.toml` 里写死了 `requires-python = ">=3.12"`。低于这个版本 `pip install` 会直接拒绝。

### 2.2 Windows

1. 访问 <https://www.python.org/downloads/> 下载 **Python 3.12+** 安装包。
2. 运行安装程序时**务必勾选**「Add python.exe to PATH」，否则命令行找不到 `python`。
3. **关闭并重开** PowerShell，再执行 `python --version` 验证。

也可以用微软商店版或 `winget install Python.Python.3.12`。

### 2.3 macOS

```bash
brew install python@3.12
```

或从 python.org 下载官方安装包。

### 2.4 Linux（Ubuntu / Debian）

```bash
sudo apt update
sudo apt install python3.12 python3.12-venv python3-pip
python3.12 --version
```

---

## 3. Node.js 20.11 及以上

前端控制台（React + Vite）需要 Node。**只在你要跑 Web 界面时才需要**；只想验证 Agent 闭环可以跳过本节，走第 4.3 节的「只跑后端」。

### 3.1 安装

- **Windows / macOS**：访问 <https://nodejs.org/> 下载 **LTS 版本**（20.x 或更高）安装。
- **macOS 用 Homebrew**：`brew install node@20`
- **Linux**：建议用 [nvm](https://github.com/nvm-sh/nvm)：`nvm install 20`

### 3.2 验证

```bash
node --version    # 应为 v20.11 或更高
npm --version     # 应为 10 或更高
```

---

## 4. 安装项目依赖（三条路径，选一条）

三条路径做的事一样，区别只是操作系统和「要不要前端」。

### 4.1 路径 A：Windows PowerShell（推荐）

```powershell
cd multi-agent-ad-optimizer

# 建虚拟环境 + 装依赖 + 复制 .env（前后端都装）
.\scripts\setup.ps1

# 国内网络需要代理时：
.\scripts\setup.ps1 -Proxy http://127.0.0.1:7897
```

`setup.ps1` 会依次做 5 件事：

1. 在 `backend\.venv` 建虚拟环境
2. `pip install -e ".[dev,analytics]"`
3. 复制 `backend\.env.example` → `backend\.env`
4. 在 `frontend\` 执行 `npm ci`
5. 复制 `frontend\.env.example` → `frontend\.env`

装完后启动：

```powershell
.\scripts\dev.ps1     # 建表 + 灌种子数据 + 同时起 API(:8000) 和 Web(:5173)
```

常用参数：

```powershell
.\scripts\dev.ps1 -SkipMigrate -SkipSeed   # 数据已就绪，只想重启
.\scripts\dev.ps1 -BackendOnly             # 只调 API
.\scripts\dev.ps1 -FrontendOnly            # 只调前端
.\scripts\dev.ps1 -BackendPort 9000        # 换端口（自动同步给 Vite 代理）
```

打开 <http://localhost:5173>，用下面账号登录：

```
邮箱：admin@adoptimizer.dev
口令：Adm1n!ChangeMe
```

> 这两个值来自 `backend\.env` 的 `SECURITY__BOOTSTRAP_ADMIN_*`。**第一次登录后立刻在「设置 → 修改密码」里改掉。**

### 4.2 路径 B：make（macOS / Linux / Git Bash）

```bash
make install     # = backend-install + frontend-install
make dev         # API :8000 + Web :5173，Ctrl+C 一起停
```

其他常用目标（`make help` 看全部）：

```bash
make migrate       # 应用数据库迁移
make seed          # 灌种子数据（8 个活动 + 21 天指标 + 管理员账号）
make run-once      # 不开服务，直接从 CLI 跑一轮优化并打印 summary
make check         # 跑完 CI 的全部门禁（lint + types + test + build）
make up / down     # Docker Compose 起停
```

> Windows 上 `make dev` 与 `make clean` 会转调 `scripts\dev.ps1` / `scripts\clean.ps1`。

### 4.3 路径 C：只跑后端（最小验证）

想先确认 Agent 闭环本身能工作、完全不碰前端：

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,analytics]"      # Windows
# .venv/bin/pip install -e ".[dev,analytics]"        # macOS/Linux
cp .env.example .env

.venv/Scripts/adoptimizer migrate    # 建表
.venv/Scripts/adoptimizer seed       # 灌种子数据
.venv/Scripts/adoptimizer run --max-iterations 2    # 跑一轮完整优化
```

`run` 会打印完整的 run summary（迭代次数、提案数、被 critic 抑制数、预算变动等），是最快看到系统全貌的方式。

### 4.4 `pip install -e ".[dev,analytics]"` 里装了什么

| extras | 内容 | 作用 |
|---|---|---|
| 核心 | `fastapi` `uvicorn` `pydantic` `pydantic-settings` `sqlalchemy[asyncio]` `typer` `langgraph>=0.2` `langchain-core>=0.3` | Web 框架、配置、ORM、CLI、Agent 编排 |
| `dev` | `pytest` `ruff` `mypy` `alembic` 等 | 测试、格式化、静态检查、迁移 |
| `analytics` | `cvxpy>=1.5` | 预算分配的凸优化求解器 |
| `clickhouse` | `clickhouse-connect>=0.7` | **可选**，只有 `CLICKHOUSE__ENABLED=true` 才需要 |
| `openai` | `langchain-openai>=0.2` | **可选**，只有接真实 LLM 才需要 |

安装报错最常见的原因是**网络**或**Python 版本过低**。国内可加镜像：

```bash
pip install -e ".[dev,analytics]" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 5. `.env` 配置说明

仓库里有**三个** `.env.example`，管的事完全不同，别搞混：

| 文件 | 管什么 | 什么时候要动 |
|---|---|---|
| `backend/.env.example` | **应用本身的配置**（约 11KB，几十个变量） | 本地开发主要改这个 |
| `deploy/compose/.env.example` | Docker Compose 栈变量（端口、镜像、Postgres 口令） | 只在走容器部署时 |
| `frontend/.env.example` | 前端 API 地址（2 行） | 一般不用改 |

`backend/.env.example` 的变量名是**双下划线嵌套格式**，因为后端用 `pydantic-settings` 把 `SECTION__KEY` 映射成 `settings.section.key`：

```
APP__ENVIRONMENT=development        →  settings.app.environment
DATABASE__URL=sqlite+aiosqlite:///./adoptimizer.db
CLICKHOUSE__ENABLED=false           →  settings.clickhouse.enabled
LLM__PROVIDER=mock                  →  settings.llm.provider
```

### 5.1 新手最少要关心什么

| 变量 | 默认值 | 说明 |
|---|---|---|
| `APP__ENVIRONMENT` | `development` | 改成 `production` 会触发严格校验：弱密钥、通配 CORS、SQLite 一律**拒绝启动** |
| `DATABASE__URL` | `sqlite+aiosqlite:///./adoptimizer.db` | 本地开发够用，不用装数据库 |
| `LLM__PROVIDER` | `mock` | `mock` 不需要 API Key。真实模型可选 `openai` / `azure_openai` / `openai_compatible` |
| `DATA_MODE` | `mock` | `mock` = 所有平台调用走 mock 适配器；`warehouse` = 用真实适配器，并解锁 `platform` 采集源。**它不负责切 ClickHouse** —— 那是 `CLICKHOUSE__ENABLED=true`，两个要一起设 |
| `CLICKHOUSE__ENABLED` | `false` | 关掉时系统走 SQL 聚合路径，完全不需要 ClickHouse |
| `REDIS__ENABLED` | `true` | 连不上会自动降级，不会导致启动失败 |
| `SECURITY__JWT_SECRET` | 一个明显的开发占位值 | **上线前必须换**，生成方式见下 |
| `SECURITY__REQUIRE_ACTION_APPROVAL` | `true` | 提案是否需要人工批准才会执行。**别关** |

生成一个合格的 JWT 密钥：

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

> ⚠️ **注意**：后端读的是**它自己工作目录下**的 `.env`（`backend/src/adoptimizer/core/config.py` 里 `env_file=".env"`）。也就是说，用 `cd backend` 起的进程读的是 `backend/.env`。仓库根目录**不再有** `.env`，早期 demo 的那份（`RUN_MODE` / `DASHBOARD_PORT` / `OPENAI_API_KEY` 那套扁平变量名）已随 demo 一起移除，**现在的后端不认那些变量名**。

---

## 6. Docker（可选）

**本地开发不需要 Docker。** 只有下面两种情况才用得上：

1. 想跑完整的容器栈（Postgres + Redis + API + Web）
2. 想启用 ClickHouse 分析仓库（`DATA_MODE=warehouse`）

### 6.1 启动完整栈

```bash
cd deploy/compose
cp .env.example .env      # 填两个必填项：SECURITY__JWT_SECRET、SECURITY__BOOTSTRAP_ADMIN_PASSWORD
docker compose up --build -d
docker compose ps         # 等到全部 healthy
```

Web 控制台：<http://localhost:8080>，API 文档：<http://localhost:8000/docs>

### 6.2 按需加 profile

```bash
docker compose --profile worker up -d        # 加后台 arq worker
docker compose --profile analytics up -d     # 加 ClickHouse
docker compose --profile ingest run --rm ingest   # 跑一次定时采集
```

`analytics` profile 启动的 ClickHouse 会挂载 `init-scripts/clickhouse/01_create_tables.sql` 自动建表（表结构见《ClickHouse 实战教程》）。

### 6.3 停止

```bash
docker compose down        # 保留数据卷
docker compose down -v     # 连数据一起清（慎用）
```

### 6.4 Docker Desktop 安装

- **Windows / macOS**：<https://www.docker.com/products/docker-desktop/>
- **Windows 额外要求**：BIOS 里开启虚拟化（Intel VT-x / AMD-V），并安装 WSL2
- **Linux**：`sudo apt install docker.io docker-compose-plugin`

验证：`docker compose version`

---

## 7. 验证安装是否成功

按顺序检查，全部通过即可认为环境 OK。

### 7.1 版本

```bash
python --version      # ≥ 3.12
node --version        # ≥ 20.11（只跑后端可跳过）
```

### 7.2 后端依赖与 CLI

```bash
cd backend
.venv/Scripts/adoptimizer --help     # Windows
# .venv/bin/adoptimizer --help       # macOS/Linux
```

能列出 `serve` / `migrate` / `seed` / `run` / `ingest` / `scheduler` / `healthcheck` / `token` / `creds` 等子命令就说明装对了。

### 7.3 跑通一轮优化

```bash
.venv/Scripts/adoptimizer migrate
.venv/Scripts/adoptimizer seed
.venv/Scripts/adoptimizer run --max-iterations 2
```

看到 `"status": "succeeded"` 且 summary 里有 `actions` / `actions_suppressed` / `critic_findings` 字段，就说明**六个 Agent 的闭环真的跑起来了**。

### 7.4 起服务并访问 API 文档

```bash
.venv/Scripts/adoptimizer serve --host 127.0.0.1 --port 8000
```

浏览器打开 <http://localhost:8000/docs>（Swagger UI）与 <http://localhost:8000/readyz>。

`/readyz` 会如实上报当前的**执行模式**：`langgraph`（装了 LangGraph）或 `sequential`（回退顺序执行器）。两种模式业务结果一致。

### 7.5（可选）ClickHouse 是否响应

只有启用了 `analytics` profile 才需要查：

```bash
curl "http://localhost:8123/ping"        # 应返回 Ok.
```

---

## 8. 常见问题 FAQ

### Q1：`pip install` 很慢或超时

用国内镜像（见 4.4 节的 `-i` 参数），或给 pip 配全局镜像源。公司网络下也可以走 `scripts\setup.ps1 -Proxy`。

### Q2：Docker 启动失败，提示虚拟化未开启（Windows）

BIOS/UEFI 里开启 **Intel VT-x** 或 **AMD-V**；Windows 上还需安装 WSL2 并让 Docker Desktop 使用 WSL2 后端。

### Q3：端口被占用

- API 端口：`.\scripts\dev.ps1 -BackendPort 9000`，或 `adoptimizer serve --port 9000`
- 容器端口：改 `deploy/compose/.env` 里的 `API_PORT` / `FRONTEND_PORT`

### Q4：`ModuleNotFoundError: No module named 'adoptimizer'`

包是以 `pip install -e` 方式装进 `backend\.venv` 的。确认：
1. 用的是**虚拟环境里的**解释器（`.venv\Scripts\python.exe`），不是系统 Python
2. 执行目录是 `backend\`

### Q5：启动报「JWT secret 太弱」或「SQLite 不允许」

这是 `APP__ENVIRONMENT=production` 的严格校验在起作用，**是有意的安全设计**。本地开发把 `APP__ENVIRONMENT` 保持 `development`；真要上生产就换一个 48 字节的随机密钥并改用 PostgreSQL。

### Q6：没有 OpenAI Key 能学本项目吗

**完全可以。** `LLM__PROVIDER=mock` 时走内置的模拟网关。而且这个项目里 6 个 Agent 只有 2 个（audience、creative）会调 LLM，**所有涉及金钱的计算都是确定性代码**，接不接真实模型都不影响预算分配、异常检测、提案复核的结果。

### Q7：跑测试时报 `PermissionError: [WinError 5]`

系统临时目录权限问题。加上自定义临时目录即可：

```bash
pytest -p no:cacheprovider --basetemp=.pytest_tmp_local
```

---

## 9. 小结

你已完成：**Python 3.12+**、**Node 20.11+（可选）**、**虚拟环境与依赖安装**、**`.env` 配置**、**（可选）Docker 栈** 与 **基本验证**。

下一步建议按顺序阅读：

1. 《AI Agent 基础知识》—— 理解这个项目的 6 个 Agent 分别在干什么
2. 《LangGraph 入门教程》—— 理解它们是怎么被编排起来的
3. [docs/production/01-quickstart.md](../production/01-quickstart.md) —— 完整的运行路径与接口验证
4. [docs/production/02-architecture.md](../production/02-architecture.md) —— **架构权威文档**，分层职责、编排细节、事件流、并发模型

如有报错，请把**完整错误信息**、操作系统版本、Python/Node 版本一并记录，便于排查。
