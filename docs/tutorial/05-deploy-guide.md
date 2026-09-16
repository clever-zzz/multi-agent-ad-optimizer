# 部署与运行指南（零基础版）

本教程带你把项目**真正跑起来**：本地开发、Docker Compose、Kubernetes 三种形态，外加必须知道的运行约束与排查手册。

> **版本说明**：本文对应仓库当前落地版本（`backend/` FastAPI + `frontend/` React）。早期 demo 的 Streamlit 仪表板、Spring Boot 版、Go 版**已随 demo 一起移除**，旧教程里的 `streamlit run` / `mvn spring-boot:run` / `go run ./cmd/server` 全部作废。
>
> 权威部署文档是 [docs/production/04-deployment.md](../production/04-deployment.md)（含镜像构建、CI/CD、上线检查单、升级回滚）。本文是它的零基础入门版，讲清"怎么跑起来"和"为什么会跑不起来"。

---

## 1. 三种部署形态

| 形态 | 依赖 | 适合 | 入口 |
|---|---|---|---|
| **本地开发** | Python 3.12+（Node 只在要前端时） | 学习、开发、评审 | `scripts\dev.ps1` 或 `make dev` |
| **Docker Compose** | Docker + Compose | 单机 / 小团队自托管 | `deploy/compose/docker-compose.yml` |
| **Kubernetes** | 一个 K8s 集群 | 生产 | `deploy/k8s/`（kustomize） |

**默认配置（`LLM__PROVIDER=mock` + SQLite + `DATA_MODE=mock`）不需要 API Key、不需要联网调模型、不需要任何容器或数据库服务。** 这是有意的设计 —— clone 下来就能跑完整闭环。

生产拓扑长这样：

```
                       ┌─────────────┐
   浏览器 ──https──▶   │  Ingress /  │  TLS 终止、HSTS、SSE 长超时、cookie 亲和
                       │   nginx     │
                       └──────┬──────┘
              ┌───────────────┴───────────────┐
              ▼                               ▼
     ┌─────────────────┐            ┌──────────────────┐
     │ frontend (nginx)│            │ backend (uvicorn)│
     │ 静态包 + /api 反代│  ──http──▶ │  单 worker/副本   │
     └─────────────────┘            └───┬──────────┬───┘
                                        ▼          ▼
                                  PostgreSQL     Redis
                                   (19 张表)   (LLM 响应缓存)
                                        ▼
                                  ClickHouse（可选，DATA_MODE=warehouse）
```

> **前端 nginx 同时反代 `/api`**，因此浏览器只看到一个 origin，生产环境不需要开 CORS 通配。`APP__CORS_ALLOW_ORIGINS` 只在前后端分域名部署时才需要真实填写。

---

## 2. 本地开发运行

### 2.1 Windows PowerShell（推荐）

```powershell
cd multi-agent-ad-optimizer

.\scripts\setup.ps1     # 建虚拟环境 + 装依赖 + 复制 .env（前后端都装）
.\scripts\dev.ps1       # 建表 + 灌种子数据 + 同时起 API(:8000) 与 Web(:5173)
```

`scripts\` 下一共 4 个脚本：

| 脚本 | 作用 |
|---|---|
| `setup.ps1` | 一次性环境准备 |
| `dev.ps1` | 迁移 + 种子 + 起前后端 |
| `check.ps1` | 跑质量门禁（可 `-Only backend`、`-Skip ruff-format,...`） |
| `clean.ps1` | 清构建产物、缓存、本地 SQLite |

`dev.ps1` 常用参数：

```powershell
.\scripts\dev.ps1 -SkipMigrate -SkipSeed   # 数据已就绪，只想重启
.\scripts\dev.ps1 -BackendOnly             # 只调 API
.\scripts\dev.ps1 -FrontendOnly            # 只调前端
.\scripts\dev.ps1 -BackendPort 9000        # 换端口（自动同步给 Vite 代理）
```

### 2.2 macOS / Linux / Git Bash

```bash
make install     # = backend-install + frontend-install
make dev         # API :8000 + Web :5173，Ctrl+C 一起停
```

### 2.3 只跑后端（最小验证）

不需要 Node，最快看到 Agent 闭环：

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,analytics]"      # Windows
# .venv/bin/pip install -e ".[dev,analytics]"        # macOS/Linux
cp .env.example .env

.venv/Scripts/adoptimizer migrate
.venv/Scripts/adoptimizer seed
.venv/Scripts/adoptimizer run --max-iterations 2
```

`run` 会打印完整 run summary，包括 `actions` / `actions_proposed` / `actions_suppressed` / `critic_findings` / `budget_adjustments` 等字段 —— 一眼能看到六个 Agent 干了多少活、critic 拦下了多少。

### 2.4 登录

打开 <http://localhost:5173>（Compose 部署是 <http://localhost:8080>）：

```
邮箱：admin@adoptimizer.dev
口令：Adm1n!ChangeMe
```

> 这两个值来自 `backend\.env` 的 `SECURITY__BOOTSTRAP_ADMIN_*`。**第一次登录后立刻在「设置 → 修改密码」里改掉。**

---

## 3. CLI 命令全表

后端装完会有一个 `adoptimizer` 可执行文件（`backend\.venv\Scripts\adoptimizer.exe`）：

| 命令 | 作用 | 常用参数 |
|---|---|---|
| `serve` | 起 API 服务 | `--host` `--port` `--reload` `--workers` |
| `migrate` | 应用数据库迁移 | `--revision`（默认 `head`）`--offline`（只出 SQL） |
| `revision` | 从 ORM 模型自动生成迁移 | `--message "..."` |
| `seed` | 灌种子数据（8 个活动 + 21 天指标 + 管理员账号） | `--force`（已有活动也灌） |
| `run` | 直接跑一轮优化并打印 summary | `--max-iterations` |
| `ingest` | 拉一趟指标 | `--source` `--days` `--start` `--end` `--dry-run` `--payload-file` |
| `scheduler` | 采集调度器 | `--once`（跑一次就退，适合 cron） |
| `healthcheck` | 探活 | `--url` |
| `token` | 取一个访问令牌（调 API 用） | |
| `creds` | 检查三个广告平台的凭据齐不齐、格式对不对（**绝不打印值**） | `--probe`（每个平台做一次只读连通性探测）、`--external-id` |
| `warehouse status` | 报当前配置的数仓能不能接写入（sink 是 `null` 就等于写进空气） | 无参数；**不可写时退出码非 0**，可以直接当部署脚本的前置检查 |
| `warehouse sync` | 把日指标从主库镜像进数仓。幂等：目标表是按 (campaign, creative, date) 的 ReplacingMergeTree，重跑一个窗口是就地纠正而不是插重复行 | `--days`（默认 30）`--campaign`（只镜像一个活动）`--dry-run`（只报窗口里有多少行，不写） |

> **`creds` 这个命令很好用，但要说准它做什么。** 它只报**广告平台凭据**（Google / Meta / TikTok），不是整份配置；对每个密钥它输出的是 `present` / `length` / `sha256_8` / `warnings`，**从来不打印值本身**——`_mask()` 这个名字有点误导，它做的是"指纹"而不是"打码"。
> 所以它的用途是：怀疑"我改的 `.env` 到底被读到没有、有没有多打一个空格"时跑一次，看 `length` 和 `warnings`（它会主动报"首尾有空白"和"值被引号包起来了"这两个最常见的认证失败原因）。
> 输出可以安全地贴给任何人。想看**整份**生效配置用 `GET /api/v1/system/info`（只含可公开字段）。

`make` 里对应的快捷目标：

```bash
make serve / migrate / revision / seed / run-once
make lint / types / test / build / check       # check = CI 的全部门禁
make up / down / logs / ps / images / deploy-k8s
make verify-migrations                          # 在一次性库上 upgrade→check→downgrade→upgrade
```

---

## 4. Docker Compose 部署

### 4.1 准备

```bash
cd deploy/compose
cp .env.example .env
```

`.env` 里有**两个必填项**，不填 compose 会直接拒绝启动（`${VAR:?message}` 语法）：

```bash
SECURITY__JWT_SECRET=              # 至少 32 字符
SECURITY__BOOTSTRAP_ADMIN_PASSWORD= # 至少 12 字符，且不能是弱口令黑名单里的词
```

生成 JWT 密钥：

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

### 4.2 启动开发栈

```bash
docker compose up --build -d
docker compose ps          # 等到全部 healthy
```

- Web 控制台：<http://localhost:8080>
- API 文档：<http://localhost:8000/docs>

服务清单：

| 服务 | profile | 说明 |
|---|---|---|
| `postgres` | 常驻 | PostgreSQL 16 |
| `redis` | 常驻 | Redis 7 |
| `api` | 常驻 | 后端（uvicorn，**单 worker**） |
| `frontend` | 常驻 | nginx 托管静态包 + 反代 `/api` |
| `worker` | `worker` | 后台 arq worker |
| `ingest` | `ingest` | 定时采集，建议 `run --rm` 一次性跑 |
| `clickhouse` | `analytics` | 分析仓库，配合 `DATA_MODE=warehouse` |

按需加 profile：

```bash
docker compose --profile worker up -d              # 后台 worker
docker compose --profile analytics up -d           # ClickHouse
docker compose --profile ingest run --rm ingest    # 跑一次采集
```

### 4.3 生产栈（叠加硬化层）

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```

`docker-compose.prod.yml` 是一个 **overlay**，叠加上去会：固定镜像 tag（禁止 `latest`）、只读根文件系统、设副本数与资源上限、强制 `APP__ENVIRONMENT=production`、不再对外发布数据库端口。

生产模式下还需要在 `.env` 里补：

```bash
IMAGE_TAG=v1.0.0
REGISTRY=registry.example.com/adoptimizer
APP__CORS_ALLOW_ORIGINS=["https://ads.example.com"]   # 明确 https 域名，不能通配
APP__TRUSTED_HOSTS=["ads.example.com"]
API_REPLICAS=2
FRONTEND_REPLICAS=2
```

### 4.4 首次上线后的四件事

```bash
# 1. 确认就绪
curl -s http://localhost:8000/readyz | python -m json.tool

# 2. 用引导管理员登录，立刻改口令
#    Web → 右上角头像 → 修改密码

# 3. 建真实账号，再把引导 admin 停用
#    注意：系统不允许停用最后一个在职 admin，所以必须先建好替代 admin 再停用

# 4. 如果有外部管道要推 POST /ingest/metrics，给它单独建一个 ingestor 账号
#    这个角色只有 metrics:read + metrics:write：凭据泄露只能伪造数字，
#    改不了活动、审批不了动作。别拿某个人的 optimizer/admin 账号去跑管道。
```

### 4.5 停止与清数据

```bash
docker compose down        # 保留数据卷
docker compose down -v     # 连数据一起清（慎用）
```

---

## 5. Kubernetes 部署

### 5.1 清单构成

`deploy/k8s/` 下 12 个清单，由 `kustomization.yaml` 组织：

| 清单 | 作用 |
|---|---|
| `namespace.yaml` | 命名空间 |
| `configmap.yaml` | 非敏感配置 |
| `secret.example.yaml` | **模板**，真实 Secret 必须带外创建，绝不提交进仓库 |
| `postgres.yaml` | 数据库（生产建议改用云托管实例） |
| `redis.yaml` | LLM 响应缓存 |
| `backend.yaml` | API Deployment + Service |
| `frontend.yaml` | 前端 Deployment + Service |
| `migrate-job.yaml` | 迁移 Job，**必须在滚动 API 之前跑完** |
| `ingest-cronjob.yaml` | 定时采集 CronJob |
| `ingress.yaml` | Ingress：TLS、HSTS、SSE 长超时、会话亲和 |
| `networkpolicy.yaml` | 网络策略 |

### 5.2 上线步骤（顺序很重要）

```bash
# 1. 命名空间
kubectl apply -f deploy/k8s/namespace.yaml

# 2. 带外创建 Secret（绝不要把占位符提交进仓库）
kubectl -n adoptimizer create secret generic adoptimizer-secrets --from-env-file=prod.env

# 3. 固定镜像版本
cd deploy/k8s && kustomize edit set image <registry>/adoptimizer-backend:<tag>

# 4. 先跑迁移，再滚动 API
kubectl apply -k deploy/k8s
kubectl -n adoptimizer wait --for=condition=complete job/adoptimizer-migrate --timeout=300s

# 5. 第一次上线建议立刻拉一趟指标，不必等下一个整点
kubectl -n adoptimizer create job --from=cronjob/adoptimizer-ingest manual-ingest
```

### 5.3 最小概念清单（零基础）

- **Pod**：一个或多个容器的最小调度单位
- **Deployment**：管理 Pod 副本数与滚动升级
- **Service**：给一组 Pod 一个稳定的内部地址
- **Ingress**：集群的 HTTP(S) 入口，管域名、TLS、路由
- **ConfigMap / Secret**：配置与敏感信息，注入成环境变量或文件
- **Job / CronJob**：一次性任务 / 定时任务
- **kustomize**：用 overlay 组合清单，`kubectl apply -k` 直接支持

### 5.4 生产注意点（广告类系统特有）

1. **审批门必须开着**：`SECURITY__REQUIRE_ACTION_APPROVAL=true`。关掉等于让 Agent 自动改真实广告预算。
2. **Ingress 要给 SSE 配长超时**：`GET /runs/{id}/stream` 是长连接，默认 60s 代理超时会把运行中的时间线掐断。
3. **会话亲和**：见下一节，这是本项目的硬约束。
4. **迁移先行**：`migrate-job` 必须在新版本 API 滚动之前 `complete`，否则新代码会撞上旧表结构。
5. **引导管理员要停用**：但注意系统不允许停用最后一个在职 admin。

---

## 6. ⚠️ 一个必须知道的硬约束：后端只能单 worker

这是本项目最重要的部署约束，写在 [ADR-0002](../adr/0002-in-process-run-dispatch.md) 里。

### 6.1 约束是什么

**`uvicorn --workers` 必须为 1，K8s 里也要靠会话亲和保证同一个 run 的请求落到同一个副本。**

`Dockerfile.backend` 里 `UVICORN_WORKERS=1`，compose 与 k8s 清单同样保持单 worker。

### 6.2 为什么

`POST /api/v1/runs` 触发一次可能持续几十秒到几分钟的多智能体闭环。它的执行方式是**进程内 `asyncio.create_task` 派发**：

```
POST /runs ──▶ 写 optimization_runs 行（status=pending）并 commit
           └─▶ asyncio.create_task(execute_run(...))    # 进程内派发
           ◀── 立即返回 202 + run 对象
```

同时，实时进度靠**进程内的 EventBus**（内存队列）推送。所以：

> `--workers 2` 会让一半的 SSE 请求落到**没有该 run 订阅者**的进程上。

### 6.3 缓解措施（已经做了的）

SSE 端点不是只读内存队列，它**以持久化的 `run_events` 表为权威源**：

```
GET /runs/{id}/stream 循环：
  ① 从 run_events 表补齐 cursor 之后的事件
  ② 若 run 已是终态 → 结束流
  ③ 否则尾随 EventBus；静默约 60s 后回到 ①
```

因此落到"错误"副本的请求**不会丢数据、不会挂死**，只是实时性退化成"每 60 秒批量补一次"。

生产上要进一步保证实时性，在 Ingress 上开会话亲和：

```yaml
nginx.ingress.kubernetes.io/affinity: cookie
```

### 6.4 这个选择的取舍

**正面**：零额外基础设施（clone → pip install → serve 就能跑完整闭环）；SSE 延迟极低；run 记录先落库再执行，进程崩溃时留下可被 `reap_stale_runs` 回收的持久记录；API 重启后重连依然能补齐完整时间线。

**负面（必须正视）**：必须单 worker；横向扩展要靠多副本 + 亲和；**进程崩溃 = 正在跑的 run 丢失**，只能靠 reaper 标记 failed 后重跑，没有 at-least-once 重试语义；没有跨进程的并发上限。

**通往外部任务队列的路已经铺好**：`pyproject.toml` 预留了 `worker = ["arq>=0.26"]` extra，compose 有 `worker` profile，`execute_run` 的签名被刻意设计成"可以在任何进程里调用"（自己开 session、自己建 context、不依赖请求态）。切换到 arq 只需要改派发点、加一个消费模块、把 EventBus 换成 Redis Pub/Sub。

---

## 7. 启动期硬化校验：为什么它会拒绝启动

`APP__ENVIRONMENT` 为 `staging` 或 `production` 时，`Settings._enforce_production_hardening` 会在**构造配置对象时**抛错，进程根本起不来：

| 检查 | 条件 |
|---|---|
| JWT 密钥 | 不在弱值黑名单（`changeme`/`secret`/`insecure`/…）且长度 ≥32 |
| 引导管理员口令 | 不在弱值黑名单且长度 ≥12 |
| CORS | `APP__CORS_ALLOW_ORIGINS` 不含 `*` |
| 数据库 | `DATABASE__URL` 不是 SQLite |

另外 `LLM__PROVIDER` 为 `openai` / `azure_openai` / `openai_compatible` 时，`LLM__API_KEY` 为空也会启动失败。想不填 Key 就用 `LLM__PROVIDER=mock`。

错误信息会把所有不达标项**一次性列全**，不用一个一个试：

```
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  Insecure production configuration: SECURITY__JWT_SECRET must be a unique value
  of at least 32 characters; DATABASE__URL must point at PostgreSQL in production
```

> **这是有意的。** 一个用默认密钥启动的生产实例，等于把"改广告预算"的权限公开在网上。宁可起不来，也不要带病上线。

---

## 8. 验证它真的在工作

跑完上面任一路径后，按顺序确认这几件事（完整版见 [01 快速开始](../production/01-quickstart.md) 的「验证它真的在工作」一节）：

**1. 依赖健康**

```bash
curl -s http://localhost:8000/readyz | python -m json.tool
```

期望 `status: ok`，且：
- `checks.database.status == "ok"`
- `checks.llm.provider == "mock"`（或你配的真实 provider）
- `checks.orchestrator.mode` 为 `langgraph`（装了 LangGraph）或 `sequential`（未装，**等价降级**，业务结果一致）

**2. 种子数据到位**

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@adoptimizer.dev","password":"Adm1n!ChangeMe"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s "http://localhost:8000/api/v1/campaigns?page_size=50" -H "Authorization: Bearer $TOKEN" \
  | python -c 'import sys,json;d=json.load(sys.stdin);print(d["total"],"campaigns")'
```

期望输出 `8 campaigns`。

**3. 优化 run 能跑完**

```bash
curl -s -X POST http://localhost:8000/api/v1/runs \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"max_iterations":2,"window_days":7}'
```

记下返回的 `id`，几秒后：

```bash
curl -s http://localhost:8000/api/v1/runs/<RUN_ID> -H "Authorization: Bearer $TOKEN" \
  | python -c 'import sys,json;d=json.load(sys.stdin);print(d["run"]["status"], d["run"]["summary"]["action_counts"])'
```

期望 `status == "succeeded"` 且 `action_counts` 非空。

**4. 实时事件流**

```bash
curl -N http://localhost:8000/api/v1/runs/<RUN_ID>/stream -H "Authorization: Bearer $TOKEN"
```

应该看到 `run.started` → 每对 `agent.started` / `agent.completed` → 中间的 `tool.invoked` → `run.succeeded`。断掉重连（带 `?lastSeq=N`）应该能补齐而不重复。

**5. 审批门生效**

`SECURITY__REQUIRE_ACTION_APPROVAL=true` 时，未审批的动作直接执行必须被拒：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://localhost:8000/api/v1/actions/<ACTION_ID>/execute \
  -H "Authorization: Bearer $TOKEN"
```

期望 `409`（Conflict，未审批）。审批后重试期望 `200`。

**6. RBAC 生效**

用 viewer 角色账号（`viewer@adoptimizer.dev` / `V1ewer!Test2026`）调写接口，期望 `403` 且 body 是 problem document。

**7. 前端联通**

登录后确认：Dashboard 有 KPI 卡片和趋势图、「运行」页能看到刚才那次 run 并有实时时间线、「动作」页有待审批提案且能批准→执行、「告警」页能 ack / resolve、「审计」页（admin）能看到你刚才每一步操作。

---

## 9. 常见部署问题排查

### 9.1 `ModuleNotFoundError: No module named 'adoptimizer'`

包是以 `pip install -e` 装进 `backend\.venv` 的。确认用的是**虚拟环境里的**解释器，且工作目录是 `backend\`。

### 9.2 服务起不来，报 `Insecure production configuration`

见第 7 节。这是硬化校验，不是 bug。本地开发把 `APP__ENVIRONMENT` 保持 `development`。

### 9.3 Docker 端口冲突

改 `deploy/compose/.env` 里的 `API_PORT` / `FRONTEND_PORT`；本地开发用 `.\scripts\dev.ps1 -BackendPort 9000`。

### 9.4 ClickHouse 连接被拒

先看后端日志是哪一条：

| 日志 | 含义 | 处理 |
|---|---|---|
| `clickhouse_connected` | 连上了 | 正常 |
| `clickhouse_unavailable` | 驱动没装 | `pip install -e ".[clickhouse]"` |
| `clickhouse_enabled_but_unreachable_falling_back_to_sql` | 配了但连不上 | 检查 `analytics` profile 是否起了、`CLICKHOUSE__HOST` 是否对 |

**注意**：第三种情况下系统会**自动降级到 SQL 聚合路径并继续正常工作**，不会崩。所以"功能正常"不代表"ClickHouse 真的在用"，要看日志或 `/readyz`。

### 9.5 SSE 时间线卡住不动 / 中途断掉

- **本地 `--reload` 模式**：改代码会重启进程，进行中的 run 会丢。这是开发模式的正常现象。
- **多副本部署**：见第 6 节，需要会话亲和，否则退化为约 60 秒批量补齐。
- **走了 nginx/Ingress**：SSE 需要长超时与关闭缓冲（后端已发 `X-Accel-Buffering: no`，但代理侧也要配 `proxy_read_timeout`）。

### 9.6 LLM 调用超时或 401

- 401 → `LLM__API_KEY` 无效或没配。不打算接真实模型就用 `LLM__PROVIDER=mock`。
- 超时 → 调 `LLM__TIMEOUT_SECONDS`。注意开了 `LLM__STREAM=true` 后，这个值的语义从"整次生成的上限"变成"**单个分片**的读超时"。
- 有月度预算护栏：`LLM__MONTHLY_BUDGET_USD`，超了会拒绝调用。

### 9.7 跑测试报 `PermissionError: [WinError 5]`

系统临时目录权限问题，指定一个自定义临时目录：

```bash
pytest -p no:cacheprovider --basetemp=.pytest_tmp_local
```

### 9.8 迁移相关

```bash
make verify-migrations    # 在一次性库上 upgrade head → check → downgrade base → upgrade head
```

`alembic check` 失败说明 ORM 模型与迁移脚本不一致，需要 `make revision m="..."` 补一个迁移。

---

## 10. 一键对照表

| 目标 | 命令 |
|---|---|
| 装环境（Windows） | `.\scripts\setup.ps1` |
| 装环境（其他） | `make install` |
| 起前后端 | `.\scripts\dev.ps1` / `make dev` |
| 只起 API | `cd backend && .venv\Scripts\adoptimizer serve --port 8000` |
| 建表 | `adoptimizer migrate` / `make migrate` |
| 灌种子数据 | `adoptimizer seed` / `make seed` |
| 跑一轮优化 | `adoptimizer run --max-iterations 2` / `make run-once` |
| 拉一趟指标 | `adoptimizer ingest --days 7` |
| 查平台凭据齐不齐 | `adoptimizer creds`（只出指纹，不出值） |
| 探活 | `adoptimizer healthcheck` / `curl /readyz` |
| API 文档 | <http://localhost:8000/docs> |
| 质量门禁全套 | `make check` |
| Compose 起停 | `make up` / `make down` |
| K8s 部署 | `kubectl apply -k deploy/k8s` / `make deploy-k8s` |
| 清理 | `.\scripts\clean.ps1` / `make clean` |

---

## 11. 小结

- **三种形态**：本地开发（零外部依赖）→ Docker Compose（单机/小团队）→ Kubernetes（生产）。
- **默认配置刻意做到零依赖**：mock LLM + SQLite + mock 数据，clone 下来就能跑完整六 Agent 闭环。
- **一个硬约束**：后端单 worker，横向扩展靠多副本 + 会话亲和；持久化的 `run_events` 回放是兜底，保证不丢数据但实时性会退化。理由与逃生路线见 [ADR-0002](../adr/0002-in-process-run-dispatch.md)。
- **生产环境会拒绝带病启动**：弱密钥、通配 CORS、SQLite 一律启动失败，这是设计如此。
- **审批门是资金安全的最后一道闸**：`SECURITY__REQUIRE_ACTION_APPROVAL` 默认 `true`，别关。

延伸阅读：

- [docs/production/01-quickstart.md](../production/01-quickstart.md) —— 完整运行路径与逐步验证
- [docs/production/04-deployment.md](../production/04-deployment.md) —— **部署权威文档**：镜像构建、CI/CD、上线检查单、升级回滚
- [docs/production/05-operations-runbook.md](../production/05-operations-runbook.md) —— 上线后怎么运维
- [docs/production/06-security.md](../production/06-security.md) —— 安全模型
- [docs/adr/](../adr/) —— 三条架构决策记录
