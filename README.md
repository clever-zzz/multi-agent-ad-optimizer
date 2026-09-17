# Ad Optimizer — 生产级多智能体广告投放优化平台

[![CI](https://github.com/clever-zzz/multi-agent-ad-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/clever-zzz/multi-agent-ad-optimizer/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)](https://react.dev)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2%2B-green)](https://github.com/langchain-ai/langgraph)
[![coverage](https://img.shields.io/badge/coverage-%E2%89%A590%25-success)](backend/pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-purple)](LICENSE)

一个把 **6 个 LLM/规则混合智能体** 编排成闭环的广告投放优化系统：拉取投放数据 → 监控异常 → 分析受众 → 生成创意 → 调整竞价 → 重分配预算 → 冲突复核，产出**可审计的变更提案**，由人审批后才落到广告平台。

后端 FastAPI + SQLAlchemy 2 (async) + LangGraph，前端 React 19 + TanStack Query + Tailwind 4，配套 Docker Compose、Kubernetes (kustomize)、GitHub Actions CI 与完整运维文档。

> **默认零外部依赖即可跑通**：`LLM__PROVIDER=mock` + SQLite + 内置模拟广告平台适配器。不需要 API Key、不需要 Docker、不需要 PostgreSQL。

## 落地状态速览

| 维度 | 现状 |
|---|---|
| 后端 | FastAPI + SQLAlchemy 2 async + Alembic，19 张表，54 个业务端点 + 4 个系统端点 |
| 前端 | React 19 + TypeScript + Vite + Tailwind 4 运营控制台，13 个页面；`package-lock.json` 已提交，安装一律 `npm ci` |
| 编排 | LangGraph Supervisor 图（6 智能体 + 告警迭代回环 + 提案冲突复核）；缺依赖时自动降级为等价顺序执行器 |
| 安全治理 | Argon2id 哈希 + 可撤销 JWT 会话 + 5 角色 RBAC + 全量审计 + 人工审批门 + 首次登录强制改密 |
| 可观测 | `/healthz` `/readyz` `/metrics` + structlog JSON + 全链路 `X-Request-ID` + SSE 运行事件回放 |
| 测试 | 后端 1302（855 unit + 447 integration，分支覆盖率 91.2%，ratchet 下限 90%）+ 前端 109，CI 强制 |
| 部署 | 多阶段非 root 镜像 + compose（dev/prod）+ kustomize（HPA/PDB/NetworkPolicy/Ingress/采集 CronJob） |
| CI | 6 个 job：`backend` / `migrations` / `frontend` / `images` / `manifests` / `workflows` |

---

## 目录

- [30 秒快速开始](#30-秒快速开始)
- [仓库结构](#仓库结构)
- [系统架构](#系统架构)
- [核心设计取舍](#核心设计取舍)
- [功能矩阵](#功能矩阵)
- [API 概览](#api-概览)
- [部署](#部署)
- [开发与质量门禁](#开发与质量门禁)
- [文档索引](#文档索引)
- [已知限制](#已知限制)

---

## 30 秒快速开始

### 方式 A：Windows PowerShell（推荐，无需 make）

```powershell
cd multi-agent-ad-optimizer
.\scripts\setup.ps1 -Proxy http://127.0.0.1:7897   # 需要代理时才加 -Proxy
.\scripts\dev.ps1                                   # 建表 + 灌种子数据 + 同时起前后端
```

浏览器打开 <http://localhost:5173>，用 `admin@adoptimizer.dev` / `Adm1n!ChangeMe` 登录。

> 首次登录会**强制设置个人密码**（bootstrap 账号标记 `must_change_password`，弹窗不可关闭、不可绕过）。这是刻意的治理行为，不是 bug。

### 方式 B：make（macOS / Linux / Git Bash）

```bash
make install     # 建 venv、装后端与前端依赖（前端走 npm ci）
make dev         # API :8000 + Web :5173
```

### 方式 C：不装前端，只跑一次优化闭环

```bash
cd backend
python -m venv .venv && .venv/Scripts/pip install -e ".[dev,analytics]"
.venv/Scripts/adoptimizer run --max-iterations 2      # 打印完整 run summary
.venv/Scripts/adoptimizer serve                        # 再起 API，打开 /docs
```

### 方式 D：Docker Compose（PostgreSQL + Redis + API + Web）

```bash
docker compose -f deploy/compose/docker-compose.yml up --build -d
# Web 控制台 http://localhost:8080   API 文档 http://localhost:8000/docs
```

种子数据集是**确定性**的（固定 RNG seed `20260906`）：8 个广告活动、21 天日粒度指标，其中刻意混入了 CTR 过低、CPA 超标、ROAS 不达标的活动和一个明显劣质的创意，保证第一次跑就能产出告警和待审批动作。

### 接真实广告账户（可选）

**密钥永远不要贴进聊天、issue 或提交里。** 唯一支持的交接方式是写进 `backend\.env`（已被 `.gitignore` 忽略），然后让程序自己核验：

```powershell
cd backend
# 1) 用你自己的编辑器打开 .env，把 GOOGLE_ADS_* / META_* / TIKTOK_* 填在等号右边（不要加引号、不要留空格）
# 2) 切到真实适配器；否则所有平台调用都走 mock，密钥填了也不会被用到
#    DATA_MODE=warehouse
# 3) 纸面交易：连人工批准后的执行也只记录、不下发
#    TOOLS__DRY_RUN=true

.venv\Scripts\adoptimizer creds           # 只打印长度 + SHA-256 前 8 位 + 格式告警，绝不打印值
.venv\Scripts\adoptimizer creds --probe   # 每个平台做一次只读 fetch_report 连通性探测
```

`creds` 的输出可以安全地贴给任何人（包括 AI 助手）：它能证明「密钥被读到了、长度对、没有多余空格或引号」，但不泄露内容。
它还会主动指出三类最常见的坑：值两端有空格、值被引号包住、以及 `DATA_MODE=mock` 导致密钥被读到却不生效。

> `.env` 里的值由 `core/config.py::load_environment()` 在启动时导出到进程环境。广告平台适配器读的是 `os.getenv`，
> 少了这一步它们会永远报告 "not configured"，看起来像密钥错了，实际是密钥从没被读过。

推荐的接入顺序：**测试账户 → `TOOLS__DRY_RUN=true` 纸面跑一轮 → 人工核对动作 → 关掉 dry-run → 小预算真实执行**。

---

## 仓库结构

```
backend/                  生产后端（FastAPI + LangGraph）
  src/adoptimizer/
    core/                 配置、日志、安全(RBAC/JWT/Argon2)、错误、中间件、Prometheus 指标
    domain/               纯业务规则：KPI 计算、竞价定价、预算分配、异常检测、评分、统计检验
    agents/               6 个智能体（monitor / audience / creative / bidding / optimize / critic）
    tools/                工具层：能力目录 + 执行器（校验/授权/预算/幂等/干跑）+ 调用审计落库
    orchestrator/         LangGraph Supervisor 图 + 事件总线（无 LangGraph 时自动降级为顺序执行）
    llm/                  模型网关：重试、超时、结构化输出、按调用记账与预算护栏
    infra/                数据库(SQLAlchemy async)、缓存(Redis/内存)、ClickHouse、广告平台适配器
    repositories/         持久化访问层
    services/             应用用例（认证、活动、优化、动作、审计、分析）
    api/                  路由与 DTO，/healthz /readyz /metrics /system/info 免鉴权
  migrations/             Alembic（异步引擎，SQLite 走 batch mode）
  tests/                  unit + integration（临时 SQLite、mock LLM、内存缓存）
frontend/                 操作台（React 19 + TypeScript + Vite + Tailwind 4）
  src/components/charts/  手写 SVG 图表（TrendChart / Sparkline / Donut / BarList）
  src/pages/              Dashboard / Campaigns / Runs / Actions / Alerts / Creatives / Experiments / Audit / Settings
deploy/
  docker/                 多阶段 Dockerfile（非 root、tini、HEALTHCHECK）+ nginx 模板
  compose/                docker-compose.yml 与 docker-compose.prod.yml（只读根文件系统、副本、资源上限）
  k8s/                    kustomize：Namespace/ConfigMap/Secret 示例/PostgreSQL STS/Redis/迁移 Job/HPA/PDB/Ingress/NetworkPolicy
scripts/                  Windows PowerShell 助手：setup / dev / check / clean
.github/workflows/ci.yml  后端 ruff+mypy+pytest-cov，迁移 alembic upgrade/check/downgrade（Postgres 服务），前端 eslint+tsc+vitest+build，镜像构建与部署清单校验
docs/production/          生产文档：快速开始、架构、API、部署、运维手册、安全、测试、限制、升级路径
docs/adr/                 架构决策记录
docs/tutorial/            入门教程（环境、Agent 基础、LangGraph、ClickHouse、部署）
```

---

## 系统架构

```
                      ┌──────────────────────────────────────────────┐
   React 19 操作台 ──▶│  FastAPI  /api/v1                            │
   (TanStack Query,   │  ├ RequestContextMiddleware  (request_id)    │
    SSE 实时进度)      │  ├ SecurityHeadersMiddleware (HSTS/CSP/...)  │
                      │  ├ RateLimitMiddleware       (固定窗口)       │
                      │  ├ GZip / CORS / TrustedHost                 │
                      │  └ RBAC 依赖注入 (require_permission)        │
                      └───────────────┬──────────────────────────────┘
                                      │ OptimizationService.start_run
                                      ▼
                      ┌──────────────────────────────────────────────┐
                      │  LangGraph Supervisor（状态机 + 条件回环）      │
                      │                                              │
                      │  monitor ─▶ audience ─▶ creative ─▶ bidding   │
                      │     ▲                                │       │
                      │     └────────── optimize ◀───────────┘       │
                      │                 │                            │
                      │      alerts 非空且 iteration < max ─▶ 回环     │
                      └───────────────┬──────────────────────────────┘
                                      │ 事件总线（内存队列 / SSE 回放）
        ┌─────────────────────────────┼─────────────────────────────┐
        ▼                             ▼                             ▼
  PostgreSQL / SQLite          Redis（LLM 响应缓存）      广告平台适配器
  19 张表 + Alembic            可关闭，降级为进程内         mock / Google / Meta / TikTok
                                                              │
                                                        ClickHouse（可选）
                                                        CLICKHOUSE__ENABLED=true
```

**关键不变量**：Agent 之间不直接调用，只通过共享 `AgentState` 通信；所有状态变更都写入 `audit_logs`；所有对外部广告平台的写操作都必须先经过 `optimization_actions` 的人工审批门（`SECURITY__REQUIRE_ACTION_APPROVAL=true`，默认开启）。

详见 [docs/production/02-architecture.md](docs/production/02-architecture.md)。

---

## 核心设计取舍

| 决策 | 取舍 | ADR |
|---|---|---|
| 手写 SVG 图表，不引 recharts | 少 ~120KB gzip、完全可控的暗色主题；代价是自己写坐标轴/tooltip | [ADR-0001](docs/adr/0001-custom-svg-charts.md) |
| 优化 run 用进程内 `asyncio.create_task` 派发 | 零额外基础设施、SSE 延迟极低；代价是**必须 `--workers 1`**，横向扩展要靠多副本 + 会话亲和 | [ADR-0002](docs/adr/0002-in-process-run-dispatch.md) |
| 每个请求校验会话存储 | 登出/停用/改角色**立即**生效，不等 access token 过期；代价是每请求多一次 Redis/DB 查询 | [ADR-0003](docs/adr/0003-session-revocation-on-request.md) |

---

## 功能矩阵

**编排与智能体**
- LangGraph Supervisor 图；`langgraph` 未安装或编译失败时自动降级为等价的顺序执行器，业务结果一致
- 6 个智能体：异常监控、受众分析、创意生成、竞价优化、预算重分配、冲突复核
- critic 在人工审批前复核全部提案：同一活动不会同时收到“暂停”与“加预算”，跨迭代重复的同一意图只保留一条；平台预检判定为阻塞的提案（`unexecutable_proposal`）直接不进审批队列，免得运营去批一个注定失败的动作；裁决先比告警严重度、再比置信度（两者量的不是同一件事）
- critic **标记而不删除**：裁决落 `critic_findings` 表，被抑制的提案以 `suppressed` 状态留在 `optimization_actions` 里。运维可以 `GET /runs/{id}/findings` 查理由，也可以 approve 翻案（审计带 `overruled_critic: true`）
- 唯一不由 critic 裁决的冲突是**反向花费意图**（同一活动同时被提议抬价与砍预算）：两者各自的参照系都成立，critic 不替运营选边，而是产出 `opposing_spend_intent` 裁决把两条都留在队列里、附上必须由人决定的理由
- 迭代回环：仍有告警且未超 `max_iterations` 时自动再跑一轮
- 预算分配优先走 CVXPY 凸优化，不可用时回退到贪心 LP，并在结果里标注 `solver`

**工具层（智能体的能力边界）**
- 7 个平台工具（1 读 + 6 写），一对一映射适配器能力；`GET /api/v1/admin/tools` 直接读注册表生成，文档不会和实现漂移
- 每个工具带 Pydantic 生成的 JSON Schema + 智能体白名单：monitor 只能读，只有 optimize 能改预算/暂停活动，只有 creative 能发新创意
- 执行器是唯一出口，按固定顺序过五道关：工具是否存在 → 调用方是否有权 → 本轮 run 调用预算 → 幂等键是否跑过 → 参数是否合法（`extra="forbid"`，编造字段直接报错）
- **写操作对智能体永远是干跑**（`TOOLS__ALLOW_AGENT_WRITES=false`）：智能体只拿到预检结果，真正下发到广告平台的只有人工审批后的执行
- `TOOLS__DRY_RUN=true` 是纸面交易模式：连人工批准的执行也只记录不下发，接真实账户前先用它验证
- 每次调用（含被拒绝的）都发 `tool.invoked` 事件并落 `tool_invocations` 表；`GET /api/v1/admin/tools/invocations` 可查
- **智能体真的在调用它**：monitor 对最严重的告警拉实时报表做核对（每轮 ≤ 5 次，幂等键去重，结论写进 `platform_checks`）；optimize 对每条写提案先跑一次平台预检（每轮 ≤ 25 次，结论作为注解写回提案）
- **人工执行走同一个执行器**：审批后的执行只是把 `agent` 置空，因此绕过“智能体不得写”却仍受 `TOOLS__DRY_RUN` 约束；它照常被审计、计入 `invocations`，但不消耗智能体调用预算
- 于是一次 run 恒满足 `summary.tools.writes == summary.tools.dry_runs`——这条不变量有测试守着，被打破只可能是有人打开了 `TOOLS__ALLOW_AGENT_WRITES`

**数据接入（采集框架 + 定时调度）**
- `POST /api/v1/ingest/metrics` 是推入口：仓库导出、商务 webhook、补数脚本都能灌日粒度指标，响应逐条交代每一行的下落
- **缺列不等于 0**：记录里没出现的度量不会覆盖已存值，只有被显式断言的数字才落地——这是“平台没报”和“平台报了 0”的区别
- **归属绝不靠猜**：内部 id 与平台坐标（`platform` + `external_id`）同时给出时必须一致；指向两个不同活动、或指向不存在的活动，一律进 `unresolved`，而不是写到一个看起来合理的地方
- 同一批次里抢同一个（活动, 创意, 日期）槽位的记录**双双拒绝**，让修正落在数据源而不是工单里
- 报告必须对账：`received == created + updated + rejected + unresolved`，返回前断言；对不上账的报告不允许离开这一层
- 干跑（`dry_run`）同样记一条批次台账，“彩排过”和“数据从没来过”因此可以区分
- 两个已注册拉取源：`synthetic`（确定性 RNG，没有广告账户也能跑）与 `platform`（复用现有适配器，mock 模式下诚实地报 `configured: false`）；拉取走 CLI 与定时器，不挂在 HTTP 请求上
- **到点自己拉**：`adoptimizer scheduler --once`（Kubernetes CronJob 用的就是这个，清单在 `deploy/k8s/ingest-cronjob.yaml`）或不加 `--once` 的进程内常驻循环，二选一，由 `INGEST__SCHEDULER_ENABLED` 决定
- **窗口由日历推导，不由游标推导**：错过一次由下一次补上（回溯到已覆盖最后一天的次日，上界 `INGEST__MAX_CATCHUP_DAYS`）。超过上界**不静默收窄**——报 `gap_days` 并在 `detail` 里给出该跑的补数命令。由游标推导的调度会把一次错误继承成永久缺口，而且每次都"相对上次正确"，没人看得见
- **重叠运行由数据库租约仲裁**：一条带过期条件的 UPDATE，`rowcount` 就是判决；先查后插的竞态窗口和拉取本身一样宽，输掉它的进程不会失败，会静默跑第二份。TTL 到期即可接管，所以持锁的 pod 崩了不会把调度卡死
- `ingest_watermarks` **只放宽不收窄**（一次窄拉取不会抹掉一次宽补数的记忆），且**干跑永不推进水位**——否则彩排会让真实运行跳过它只假装覆盖过的窗口
- `GET /api/v1/ingest/schedule` 只计划不拉取，`plan.reason`（`first`/`covered`/`nominal`/`catchup`/`capped`）直接回答"为什么数字没更新"，轮询它不会启动任何工作
- 每条落地的指标都带 `source` / `batch_id` 溯源列，处置结果计入 `ingest_records_total` / `ingest_batches_total`；调度另有 `ingest_ticks_total{source,outcome}` 与 `ingest_lag_days{source}`（陈旧告警挂在后者上：窗口被上界卡住时 tick 会一直"成功"，数据却越落越远）
- **推送入口有自己的身份**：`POST /ingest/metrics` 要 `metrics:write`，而这个权限只发给 admin 与专用的 `ingestor` 角色（已从 optimizer 收回）。采集凭据泄露只能伪造数字，改不了活动、审批不了动作
- 框架已通、**真实平台数据尚未接通**，见 [08 §3.5](docs/production/08-limitations-and-roadmap.md)

**安全与合规**
- Argon2id 口令哈希（可调 time/memory/parallelism），JWT access + 可撤销 refresh 会话
- 5 角色 RBAC（admin / optimizer / analyst / viewer / ingestor）映射到 14 个细粒度权限；`ingestor` 是给采集管道的机器身份，只有 `metrics:read` + `metrics:write`，前端那份矩阵副本由后端测试反向校验
- 登录失败计数 + 锁定；改密码/改角色/停用账号会吊销会话；**最后一个在职 admin 不允许被降级或停用**
- bootstrap 账号标记 `must_change_password`，首次登录强制改密且弹窗不可关闭
- 全量审计日志（actor、前后值、IP、UA、request_id）；写操作支持 `Idempotency-Key`
- 生产环境启动即校验：弱密钥、通配 CORS、SQLite 一律拒绝启动
- 安全响应头（CSP、HSTS、X-Content-Type-Options、Referrer-Policy、Frame-Options）

**可观测性**
- `/healthz`（存活）、`/readyz`（依赖聚合，不可用时 503）、`/metrics`（Prometheus）
- structlog JSON 结构化日志 + 全链路 `X-Request-ID`
- 每次 LLM 调用记账（token/成本/延迟/结果），月度预算护栏：超限直接抛 `BudgetExceededError`（HTTP 402），**不受 `LLM__FAIL_OPEN_TO_MOCK` 影响**——那个开关只管供应商调用重试打满后是否退到 mock
- 运行事件落库 `run_events`，SSE 可回放，断线重连不丢进度

**前端操作台**
- 登录/登出、改密、账号管理（改角色、停用）
- Dashboard 总览 KPI、活动列表与详情、创意库、优化运行列表与详情（SSE 实时时间线）
- 动作审批台（单条 + 批量 approve/reject/execute）、告警中心（ack/resolve）、A/B 实验、审计流水、系统设置
- 手写 SVG 图表、错误边界、乐观更新、请求去重、分页与骨架屏

---

## API 概览

业务端点前缀 `/api/v1`，共 **54** 个；另有 **4** 个免鉴权系统端点挂在根路径（`/healthz` `/readyz` `/metrics` `/system/info`）。错误响应统一为 RFC 9457 风格的 problem document：

```json
{
  "type": "https://adoptimizer.dev/errors/permission_denied",
  "title": "Permission denied",
  "status": 403,
  "detail": "Requires permission campaign:write",
  "code": "permission_denied"
}
```

| 分组 | 端点数 | 说明 |
|---|---|---|
| `/auth` | 8 | 登录、刷新（轮换）、登出、改密、全端登出、账号增查（admin） |
| `/campaigns` | 9 | 活动 CRUD、创意子资源、活动指标快照 |
| `/creatives` | 2 | 跨活动创意检索与汇总 |
| `/runs` | 5 | 触发运行、列表、详情、取消、SSE 流 |
| `/actions` | 6 | 提案列表/详情、审批、驳回、执行、批量 |
| `/alerts` | 4 | 列表、按严重度汇总、确认、解决 |
| `/analytics` | 6 | 总览、快照、时序、活动下钻、LLM 花费、即时异常检测 |
| `/ingest` | 4 | **推日粒度指标**（逐条报告下落）、采集批次台账、已注册数据源可用性、**定时拉取的计划与水位**（只读） |
| `/admin` | 9 | 运行时配置、依赖健康、**工具能力目录**、**工具调用审计**、审计、实验、灌种子、改用户、保留期清理 |
| 系统 | 4 | `/healthz` `/readyz` `/metrics` `/system/info`（根路径） |

完整端点表、权限矩阵与调用示例见 [docs/production/03-api-reference.md](docs/production/03-api-reference.md)。交互式文档：启动后访问 <http://localhost:8000/docs>。

---

## 部署

| 目标 | 入口 | 文档 |
|---|---|---|
| 本地开发 | `scripts/dev.ps1` 或 `make dev` | [quickstart](docs/production/01-quickstart.md) |
| 单机 / 小团队 | `deploy/compose/docker-compose.yml` | [deployment](docs/production/04-deployment.md) |
| 生产 | `deploy/compose/docker-compose.prod.yml` | [deployment](docs/production/04-deployment.md) |
| Kubernetes | `kubectl apply -k deploy/k8s` | [deployment](docs/production/04-deployment.md) |

生产清单默认启用：镜像 tag 固定、只读根文件系统 + tmpfs、非 root UID 10001、资源 requests/limits、HPA + PDB、NetworkPolicy、Ingress 上的 SSE 长连接超时。

上线前必读：[operations-runbook](docs/production/05-operations-runbook.md) 与 [security](docs/production/06-security.md)。

---

## 开发与质量门禁

```bash
.\scripts\check.ps1        # Windows：ruff format/lint → mypy → alembic upgrade/check/downgrade → pytest --cov → eslint → tsc → vitest → build
make check                 # 同上，CI 顺序一致
```

| 门禁 | 工具 | 阈值 / 当前规模 |
|---|---|---|
| 格式 | `ruff format` | 必须无 diff |
| Lint | `ruff check` | 0 error（E/W/F/I/N/UP/B/A/C4/SIM/TCH/RUF/S/PTH/DTZ/ASYNC/RET/ARG） |
| 类型 | `mypy --strict` / `tsc` | 后端 0 error；前端 `strict` + `noUnusedLocals` |
| 迁移 | `alembic upgrade head / check / downgrade base` | 可升级、可回滚，且 `alembic check` 无 autogenerate 漂移 |
| 后端测试 | `pytest --cov` | 1302 个（855 unit + 447 integration）；分支覆盖率 91.2%，ratchet 下限 **90%**，失败即 CI 红 |
| 前端测试 | `vitest` | 109 个；`eslint --max-warnings 0` 同时强制 0 warning |
| 依赖安装 | `npm ci` | lockfile 已提交，CI 与 `setup.ps1` 一律走 `npm ci`，漂移即失败 |

CI 还会构建两个容器镜像，并校验 compose 与 kustomize 清单可渲染。见 [.github/workflows/ci.yml](.github/workflows/ci.yml)（6 个 job：`backend`、`migrations`、`frontend`、`images`、`manifests`、`workflows`）。

---

## 文档索引

**生产文档** — [docs/production/](docs/production/README.md)
- [01 快速开始](docs/production/01-quickstart.md) · [02 架构](docs/production/02-architecture.md) · [03 API 参考](docs/production/03-api-reference.md)
- [04 部署](docs/production/04-deployment.md) · [05 运维手册](docs/production/05-operations-runbook.md) · [06 安全](docs/production/06-security.md)
- [07 测试与 CI](docs/production/07-testing-and-ci.md) · [08 限制与路线图](docs/production/08-limitations-and-roadmap.md) · [09 优化升级路径](docs/production/09-upgrade-path.md)

**决策记录** — [docs/adr/](docs/adr/README.md)

**入门教程**（零基础，已对齐当前落地版本）— [docs/tutorial/](docs/tutorial/README.md)
- [01 环境搭建](docs/tutorial/01-environment-setup.md) · [02 Agent 基础](docs/tutorial/02-agent-basics.md) · [03 LangGraph 入门](docs/tutorial/03-langgraph-intro.md)
- [04 ClickHouse 实战](docs/tutorial/04-clickhouse-guide.md) · [05 部署与运行](docs/tutorial/05-deploy-guide.md)

---

## 已知限制

诚实地列出来，避免把它当成能直接接管真实预算的系统：

- **广告平台适配器默认是 mock**。`google/meta/tiktok` 适配器有真实的请求/签名/分页骨架，但**未经真实账号联调**，投产前必须逐个平台做沙箱验证。接入步骤与核验命令见上文[接真实广告账户](#接真实广告账户可选)。
- **进程内 run 派发** 要求 `uvicorn --workers 1`；横向扩展靠多副本 + 会话亲和，或改造为 arq/Celery 队列（`worker` extra 已预留）。
- **EventBus 是进程内队列**，跨副本的 SSE 订阅需要换成 Redis Pub/Sub。
- **LLM 默认 mock**。切到真实模型后，创意生成与受众洞察的质量取决于 prompt 与模型，需要自建评测集。
- **智能体的写操作永远停在预检**。工具层已经接进决策路径（monitor 拉实时报表核对告警、optimize 对每条写提案做平台预检、人工执行也走同一个执行器），但 `TOOLS__ALLOW_AGENT_WRITES=false` 意味着模型自己一次都动不了真实账户——这是刻意设计，不是待补的缺口。代价是预检只能暴露参数、权限和状态层面的问题；真实平台的配额、竞价冲突要到人工执行那一刻才知道。
- **LLM 目前不在决策路径上**。全仓只有 2 处模型调用：`creative` 生成文案（失败降级为模板）、`audience` 产出叙述性假设（当前不被下游消费）。所有涉及金额的决定都由 `domain/` 的确定性代码做出，所以换掉 mock provider 不会改变动作集合。这是刻意取舍，但不该被误读为“模型在做决策”。
- **ClickHouse 路径**（`DATA_MODE=warehouse`）的写入侧已就绪：`infra/analytics/` 提供 `AnalyticsSink`，`adoptimizer warehouse sync` 把运营库的日报镜像进仓库（`ReplacingMergeTree`，可安全重跑）。注意平台 API 给的是**日报而非事件流**，所以当前主路径是聚合表 `campaign_daily_metrics`（`CLICKHOUSE__METRICS_SOURCE` 的默认值就是 `daily`，与写入侧对齐；`events` 需显式开启，且因为暂无生产写入者会在启动时打 `clickhouse_events_source_has_no_writer` 告警而不是静默读空），`ad_events` 留给真实事件流。读侧对该表统一加 `FINAL`：`ReplacingMergeTree` 的替换只发生在后台 merge，而 sync 是文档支持重跑的 30 天窗口镜像，不收敛版本就会把同一次投放按重放次数累加——ROAS 因分子分母同比放大而看起来正常，所以不会有任何告警。**未经真实数据量验证**。读路径连不上库时降级到空结果（刻意设计，避免拖垮优化循环），但会计入 `warehouse_reads_total{outcome="degraded"}` 而不再无声。见 [09 §S3](docs/production/09-upgrade-path.md)。
- 完整清单与缓解方案见 [docs/production/08-limitations-and-roadmap.md](docs/production/08-limitations-and-roadmap.md)；**按什么顺序解决、每步怎么验收**见 [09 优化升级路径](docs/production/09-upgrade-path.md)。

---

## License

MIT — 见 [LICENSE](LICENSE)。
