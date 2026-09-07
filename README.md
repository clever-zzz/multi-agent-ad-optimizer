# Ad Optimizer — 生产级多智能体广告投放优化平台

[![CI](https://github.com/clever-zzz/multi-agent-ad-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/clever-zzz/multi-agent-ad-optimizer/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)](https://react.dev)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2%2B-green)](https://github.com/langchain-ai/langgraph)
[![coverage](https://img.shields.io/badge/coverage-%E2%89%A578%25-success)](backend/pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-purple)](LICENSE)

一个把 **5 个 LLM/规则混合智能体** 编排成闭环的广告投放优化系统：拉取投放数据 → 监控异常 → 分析受众 → 生成创意 → 调整竞价 → 重分配预算，产出**可审计的变更提案**，由人审批后才落到广告平台。

后端 FastAPI + SQLAlchemy 2 (async) + LangGraph，前端 React 19 + TanStack Query + Tailwind 4，配套 Docker Compose、Kubernetes (kustomize)、GitHub Actions CI 与完整运维文档。

> **默认零外部依赖即可跑通**：`LLM__PROVIDER=mock` + SQLite + 内置模拟广告平台适配器。不需要 API Key、不需要 Docker、不需要 PostgreSQL。

## 落地状态速览

| 维度 | 现状 |
|---|---|
| 后端 | FastAPI + SQLAlchemy 2 async + Alembic，14 张表，47 个业务端点 + 4 个系统端点 |
| 前端 | React 19 + TypeScript + Vite + Tailwind 4 运营控制台，9 个页面；`package-lock.json` 已提交，安装一律 `npm ci` |
| 编排 | LangGraph Supervisor 图（5 智能体 + 告警迭代回环）；缺依赖时自动降级为等价顺序执行器 |
| 安全治理 | Argon2id 哈希 + 可撤销 JWT 会话 + 4 角色 RBAC + 全量审计 + 人工审批门 + 首次登录强制改密 |
| 可观测 | `/healthz` `/readyz` `/metrics` + structlog JSON + 全链路 `X-Request-ID` + SSE 运行事件回放 |
| 测试 | 后端 591（332 unit + 259 integration，覆盖率 80.81%，ratchet 下限 78%）+ 前端 99，CI 强制 |
| 部署 | 多阶段非 root 镜像 + compose（dev/prod）+ kustomize（HPA/PDB/NetworkPolicy/Ingress） |
| CI | 5 个 job：`backend` / `migrations` / `frontend` / `images` / `manifests` |
| 遗留 demo | `python/` `java/` `golang/` 仅作教学参考，不在生产路径上 |

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

---

## 仓库结构

```
backend/                  生产后端（FastAPI + LangGraph）
  src/adoptimizer/
    core/                 配置、日志、安全(RBAC/JWT/Argon2)、错误、中间件、Prometheus 指标
    domain/               纯业务规则：KPI 计算、竞价定价、预算分配、异常检测、评分、统计检验
    agents/               5 个智能体（monitor / audience / creative / bidding / optimize）
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
docs/production/          生产文档：快速开始、架构、API、部署、运维手册、安全、测试、限制
docs/adr/                 架构决策记录
docs/interview/           历史面试材料（八股、STAR、简历模板、旧版 README）
docs/tutorial/            入门教程（环境、Agent 基础、LangGraph、ClickHouse、部署）
python/ java/ golang/     原始 demo 实现（Streamlit / Spring Boot / goroutine），仅作教学参考，不参与生产部署
```

---

## 系统架构

```
                      ┌──────────────────────────────────────────────┐
   React 19 操作台 ──▶│  FastAPI  /api/v1                            │
   (TanStack Query,   │  ├ RequestContextMiddleware  (request_id)    │
    SSE 实时进度)      │  ├ SecurityHeadersMiddleware (HSTS/CSP/...)  │
                      │  ├ RateLimitMiddleware       (令牌桶)         │
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
  PostgreSQL / SQLite          Redis（缓存 + 限流）        广告平台适配器
  14 张表 + Alembic            可关闭，降级为进程内         mock / Google / Meta / TikTok
                                                              │
                                                        ClickHouse（可选）
                                                        DATA_MODE=warehouse
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
- 5 个智能体：异常监控、受众分析、创意生成、竞价优化、预算重分配
- 迭代回环：仍有告警且未超 `max_iterations` 时自动再跑一轮
- 预算分配优先走 CVXPY 凸优化，不可用时回退到贪心 LP，并在结果里标注 `solver`

**安全与合规**
- Argon2id 口令哈希（可调 time/memory/parallelism），JWT access + 可撤销 refresh 会话
- 4 角色 RBAC（admin / optimizer / analyst / viewer）映射到 11 个细粒度权限
- 登录失败计数 + 锁定；改密码/改角色/停用账号会吊销会话；**最后一个在职 admin 不允许被降级或停用**
- bootstrap 账号标记 `must_change_password`，首次登录强制改密且弹窗不可关闭
- 全量审计日志（actor、前后值、IP、UA、request_id）；写操作支持 `Idempotency-Key`
- 生产环境启动即校验：弱密钥、通配 CORS、SQLite 一律拒绝启动
- 安全响应头（CSP、HSTS、X-Content-Type-Options、Referrer-Policy、Frame-Options）

**可观测性**
- `/healthz`（存活）、`/readyz`（依赖聚合，不可用时 503）、`/metrics`（Prometheus）
- structlog JSON 结构化日志 + 全链路 `X-Request-ID`
- 每次 LLM 调用记账（token/成本/延迟/结果），月度预算护栏，超预算按配置降级到 mock
- 运行事件落库 `run_events`，SSE 可回放，断线重连不丢进度

**前端操作台**
- 登录/登出、改密、账号管理（改角色、停用）
- Dashboard 总览 KPI、活动列表与详情、创意库、优化运行列表与详情（SSE 实时时间线）
- 动作审批台（单条 + 批量 approve/reject/execute）、告警中心（ack/resolve）、A/B 实验、审计流水、系统设置
- 手写 SVG 图表、错误边界、乐观更新、请求去重、分页与骨架屏

---

## API 概览

业务端点前缀 `/api/v1`，共 **47** 个；另有 **4** 个免鉴权系统端点挂在根路径（`/healthz` `/readyz` `/metrics` `/system/info`）。错误响应统一为 RFC 9457 风格的 problem document：

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
| `/admin` | 7 | 运行时配置、依赖健康、审计、实验、灌种子、改用户、保留期清理 |
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
| 后端测试 | `pytest --cov` | 591 个（332 unit + 259 integration）；分支覆盖率 80.81%，ratchet 下限 **78%**，失败即 CI 红 |
| 前端测试 | `vitest` | 99 个；`eslint --max-warnings 0` 同时强制 0 warning |
| 依赖安装 | `npm ci` | lockfile 已提交，CI 与 `setup.ps1` 一律走 `npm ci`，漂移即失败 |

CI 还会构建两个容器镜像，并校验 compose 与 kustomize 清单可渲染。见 [.github/workflows/ci.yml](.github/workflows/ci.yml)（5 个 job：`backend`、`migrations`、`frontend`、`images`、`manifests`）。

---

## 文档索引

**生产文档** — [docs/production/](docs/production/)
- [01 快速开始](docs/production/01-quickstart.md) · [02 架构](docs/production/02-architecture.md) · [03 API 参考](docs/production/03-api-reference.md)
- [04 部署](docs/production/04-deployment.md) · [05 运维手册](docs/production/05-operations-runbook.md) · [06 安全](docs/production/06-security.md)
- [07 测试与 CI](docs/production/07-testing-and-ci.md) · [08 限制与路线图](docs/production/08-limitations-and-roadmap.md)

**决策记录** — [docs/adr/](docs/adr/)

**历史与教学材料**（不影响生产路径）
- [旧版 README（面试向）](docs/interview/legacy-readme.md) · [面试问答](docs/interview/qa-collection.md) · [八股](docs/interview/baguwen.md) · [STAR 话术](docs/interview/star-method.md) · [简历模板](docs/interview/resume-template.md)
- [入门教程](docs/tutorial/) · [三语言代码讲解](docs/code-walkthrough/) · [早期架构稿](docs/architecture.md) · [原始规划](docs/plan.md)

---

## 已知限制

诚实地列出来，避免把它当成能直接接管真实预算的系统：

- **广告平台适配器默认是 mock**。`google/meta/tiktok` 适配器有真实的请求/签名/分页骨架，但**未经真实账号联调**，投产前必须逐个平台做沙箱验证。
- **进程内 run 派发** 要求 `uvicorn --workers 1`；横向扩展靠多副本 + 会话亲和，或改造为 arq/Celery 队列（`worker` extra 已预留）。
- **EventBus 是进程内队列**，跨副本的 SSE 订阅需要换成 Redis Pub/Sub。
- **LLM 默认 mock**。切到真实模型后，创意生成与受众洞察的质量取决于 prompt 与模型，需要自建评测集。
- **ClickHouse 路径**（`DATA_MODE=warehouse`）有 schema 与查询实现，但没有真实数据量的压测数据。
- 完整清单与缓解方案见 [docs/production/08-limitations-and-roadmap.md](docs/production/08-limitations-and-roadmap.md)。

---

## License

MIT — 见 [LICENSE](LICENSE)。
