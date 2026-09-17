# 04 部署

三种目标拓扑，从单机到 Kubernetes。共同的前提：**先想清楚数据库、密钥和 CORS 域名**，这三样错了系统会拒绝启动（这是设计如此，见 §5）。

---

## 1. 拓扑总览

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

## 2. 单机 / 小团队：Docker Compose

### 2.1 准备

```bash
cd deploy/compose
cp .env.example .env
```

编辑 `.env`，**必填**两项：

```bash
# 生成 JWT 密钥（至少 32 字符）
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

```dotenv
SECURITY__JWT_SECRET=<上面生成的值>
SECURITY__BOOTSTRAP_ADMIN_PASSWORD=<至少 12 字符的强口令>
```

其余保持默认即可跑起开发栈。

### 2.2 开发栈

```bash
docker compose up --build -d
docker compose ps          # 等 postgres/redis/api/frontend 全部 healthy
```

- Web：<http://localhost:8080>
- API：<http://localhost:8000>（`/docs`、`/healthz`、`/readyz`、`/metrics`）

服务清单：

| 服务 | 镜像 | 说明 |
|---|---|---|
| `postgres` | `postgres:16-alpine` | 主库，开启 data checksums，healthcheck 用 `pg_isready` |
| `redis` | `redis:7-alpine` | LLM 响应缓存（限流/事件总线/会话都不走 Redis），AOF 持久化 |
| `api` | 本地构建 `Dockerfile.backend` | 入口脚本自动 `alembic upgrade head`（带重试） |
| `frontend` | 本地构建 `Dockerfile.frontend` | nginx 提供静态包并反代 `/api` |
| `worker` | 同 `api` | **一次性**批量优化任务，`restart: "no"`，profile `worker` |
| `clickhouse` | `clickhouse/clickhouse-server:24.8-alpine` | profile `analytics`，`DATA_MODE=warehouse` 时才需要 |

可选 profile：

```bash
docker compose --profile analytics up -d     # 加 ClickHouse
docker compose run --rm worker               # 手动跑一次批量优化
```

> `worker` 是**一次性 Job**，不是常驻消费者。要定时跑，用宿主 cron 或 k8s CronJob 触发它，不要给它加 `restart: always`。

### 2.3 生产栈（叠加硬化层）

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

`.env` 里额外需要：

```dotenv
IMAGE_TAG=v1.0.0
REGISTRY=registry.example.com/adoptimizer
REDIS_PASSWORD=<强口令>
APP__CORS_ALLOW_ORIGINS=["https://ads.example.com"]
APP__TRUSTED_HOSTS=["ads.example.com"]
APP__ENVIRONMENT=production
API_REPLICAS=2
FRONTEND_REPLICAS=2
POSTGRES_VERSION=16.6
REDIS_VERSION=7.4
```

硬化层做了什么：

| 项 | 效果 |
|---|---|
| `APP__ENVIRONMENT=production` | 触发启动期安全校验（见 §5） |
| 镜像按 tag 拉取，`build: !override null` | 生产不会意外本地构建出未审计的镜像 |
| `ports: !override []`（postgres） | 数据库不再对宿主机暴露 |
| `read_only: true` + `tmpfs` | 容器根文件系统只读 |
| `no-new-privileges:true` | 禁止提权 |
| Redis `--requirepass` + `maxmemory 512mb` + `allkeys-lru` | 有认证、有内存上限、有淘汰策略 |
| `deploy.resources` limits/reservations | 单容器打不满宿主机 |
| `update_config: order: start-first` | 滚动更新时先起新容器再停旧的 |
| `restart_policy` | 失败重试 5 次后放弃，而不是无限重启掩盖问题 |

### 2.4 首次上线后的三件事

```bash
# 1. 确认就绪
curl -sf http://localhost:8000/readyz | python -m json.tool

# 2. 用引导管理员登录，立刻改口令
#    Web → 右上角头像 → 修改密码

# 3. 建真实账号，把引导 admin 停用（注意：系统不允许停用最后一个在职 admin，
#    所以必须先建好替代的 admin 再停用引导账号）

# 4. 有外部管道要推 POST /ingest/metrics 的话，给它建一个 ingestor 账号。
#    这个角色只有 metrics:read + metrics:write：凭据泄露只能伪造数字，
#    改不了活动、审批不了动作。别拿某个人的 optimizer/admin 账号去跑管道。
```

---

## 3. Kubernetes

### 3.1 清单构成

```
deploy/k8s/
  namespace.yaml         adoptimizer 命名空间
  configmap.yaml         非敏感配置（APP__/DATABASE__/REDIS__/SECURITY__ 策略等）
  secret.example.yaml    敏感配置模板 —— 故意不在 kustomization 里
  postgres.yaml          StatefulSet + PVC + Service
  redis.yaml             Deployment + Service
  migrate-job.yaml       CronJob（也用于部署流水线里手动创建 Job）
  ingest-cronjob.yaml    CronJob：每 6 小时跑一趟 `adoptimizer scheduler --once`
  backend.yaml           Deployment + Service + HPA + PDB
  frontend.yaml          Deployment + Service
  ingress.yaml           两个 Ingress：常规 API(60s) 与 SSE(3600s + cookie 亲和)
  networkpolicy.yaml     默认拒绝 + 明确放行
  kustomization.yaml     组装以上（不含 secret.example.yaml）
```

### 3.2 上线步骤

```bash
# 1. 命名空间
kubectl apply -k deploy/k8s        # 首次会因缺 Secret 而让 Pod 起不来，这是预期的

# 2. 带外创建 Secret（绝不要把占位符提交进仓库）
kubectl -n adoptimizer create secret generic adoptimizer-secrets \
  --from-literal=SECURITY__JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  --from-literal=SECURITY__BOOTSTRAP_ADMIN_PASSWORD='<至少12字符的强口令>' \
  --from-literal=POSTGRES_PASSWORD='<强口令>' \
  --from-literal=DATABASE__URL='postgresql+asyncpg://adoptimizer:<强口令>@postgres:5432/adoptimizer'

# 3. 固定镜像版本
cd deploy/k8s
kustomize edit set image adoptimizer/backend=registry.example.com/adoptimizer/backend:v1.0.0
kustomize edit set image adoptimizer/frontend=registry.example.com/adoptimizer/frontend:v1.0.0
cd ../..

# 4. 先跑迁移，再滚动 API
kubectl -n adoptimizer create job adoptimizer-migrate-$(date +%s) \
  --from=cronjob/adoptimizer-migrate
kubectl -n adoptimizer wait --for=condition=complete job -l app.kubernetes.io/component=migrate --timeout=300s

kubectl apply -k deploy/k8s
kubectl -n adoptimizer rollout status deploy/backend
kubectl -n adoptimizer rollout status deploy/frontend

# 5. 立刻拉一趟指标，不必等下一个整点（第一次上线建议做）
kubectl -n adoptimizer create job adoptimizer-ingest-$(date +%s) \
  --from=cronjob/adoptimizer-ingest
kubectl -n adoptimizer wait --for=condition=complete job \
  -l app.kubernetes.io/component=ingest --timeout=600s
```

> `secret.example.yaml` **故意没有**列进 `kustomization.yaml`。把占位凭据 apply 进集群，要么被生产硬化校验直接拒绝启动，要么更糟——用一个已经公开在 Git 里的密钥启动集群。

### 3.3 关键配置说明

**backend Deployment**

| 配置 | 值 | 原因 |
|---|---|---|
| `replicas` | 2 | 每副本单 uvicorn worker；横向扩展靠副本而不是 worker |
| `strategy` | `maxSurge: 1, maxUnavailable: 0` | 滚动期间不降容量 |
| `terminationGracePeriodSeconds` | 60 | 给正在执行的 run 留收尾时间 |
| `preStop: sleep 10` | | 让 Pod 先从 Service endpoints 摘除，再停 uvicorn，避免切断在途请求 |
| `runAsNonRoot` / `runAsUser: 10001` | | 镜像里就是这个 UID |
| `readOnlyRootFilesystem: true` | | 只有 `/tmp` 与 `/var/lib/adoptimizer` 可写（emptyDir） |
| `capabilities.drop: [ALL]` | | 不需要任何 Linux capability |
| `startupProbe` `/healthz` | 30 × 5s | 冷启动（含迁移）慢时不会被 liveness 打死 |
| `readinessProbe` `/readyz` | 10s 周期 | 数据库不可达即摘流 |
| `livenessProbe` `/healthz` | 20s 周期 | 只看进程是否还活着，**不看依赖** |
| `prometheus.io/*` 注解 | | 供 Prometheus 自动发现抓取 |

**HPA**：CPU 70% / 内存 80%，2–8 副本，缩容稳定窗口 300s。

> ⚠️ HPA 扩容出新副本后，**正在执行的 run 不会迁移过去**（见 [ADR-0002](../adr/0002-in-process-run-dispatch.md)）。SSE 路由上的 cookie 亲和能让客户端保持在原副本，因此实时时间线不受影响；新副本只承接新请求。

**PDB**：`minAvailable: 1`，保证节点维护时不会全部下线。

**migrate CronJob**：每周日 03:00 兜底跑一次（`concurrencyPolicy: Forbid`），真正的迁移应在部署流水线里显式触发。`RUN_MIGRATIONS=false` 写在 ConfigMap 里，因此 API Pod **不会**自己跑迁移——多副本同时 ALTER TABLE 是要出事的。

**ingest CronJob**：每 6 小时的第 10 分钟跑一趟 `adoptimizer scheduler --once`。`timeZone: Etc/UTC` 是必须的——窗口按日历日算，触发器就得和它同一个钟，否则各节点按本地时区解释 cron 表达式，一天里会漂出好几个不同的"6 点"。关键三项：

| 配置 | 值 | 原因 |
|---|---|---|
| `concurrencyPolicy` | `Forbid` | 慢拉取不会在自己身后排队；数据库租约是第二道保险，覆盖跨来源的重叠（常驻循环撞上手工触发） |
| `startingDeadlineSeconds` | `3600` | k8s 侧的错过策略：错过超过一小时就不补跑，因为晚跑的窗口和下一趟算出来的是同一个 |
| `backoffLimit` | `2` | 重试两次后退出码非 0，由 `ingest_ticks_total{outcome="failed"}` 叫人；无限重试会把一个坏掉的数据源藏成一个永远不结束的 Job |

Pod 标签沿用 `app.kubernetes.io/name: backend`，所以已有的 `backend` NetworkPolicy 直接覆盖它：出站到 postgres / redis / DNS / 443，入站不接受任何连接。

> 用了 CronJob，就把 ConfigMap 里的 `INGEST__SCHEDULER_ENABLED` 设为 `false`（清单里已经是这个值）。否则每个 API 副本还会各起一份进程内循环：租约能防止重复写入，但会白花 N 份算力，而且节奏会随某个副本重启而丢掉。`--once` **不受**这个开关约束——被进程外调度器调起正是它的用途。没有 Kubernetes 的部署可以反过来，把开关打开用常驻循环；两条路径写的是同一套水位与租约，混用也不会重复灌。

**NetworkPolicy**：默认拒绝入站，只放行 ingress→frontend、frontend→backend、backend→postgres/redis。

**Ingress**：拆成两个资源。`adoptimizer-sse` 承载 `/api/v1/runs`，`proxy-buffering: off` + 3600s 读超时 + cookie 亲和；`adoptimizer` 承载其余路径，60s 超时。nginx 会按最长前缀匹配，所以拆分是安全的。

### 3.4 需要按环境改的地方

`configmap.yaml` 里这些是示例值，**必须改**：

```yaml
APP__CORS_ALLOW_ORIGINS: '["https://ads.example.com"]'   # 换成你的域名
APP__TRUSTED_HOSTS: '["ads.example.com"]'                # 换成你的域名
DATABASE__URL: "postgresql+asyncpg://adoptimizer@postgres:5432/adoptimizer"
```

`ingress.yaml` 里的 `ads.example.com` 与 `adoptimizer-tls` 同样要换。

如果要接外部托管的 PostgreSQL/Redis（RDS、ElastiCache 等），删掉 `postgres.yaml` / `redis.yaml`，把连接串放进 Secret，并在 `networkpolicy.yaml` 里放行对应的 egress。

---

## 4. 环境变量总表

后端配置的完整清单与逐项说明在 [`backend/.env.example`](../../backend/.env.example)。Compose 栈的变量在 [`deploy/compose/.env.example`](../../deploy/compose/.env.example)。

生产环境**必须显式设置**的：

| 变量 | 说明 |
|---|---|
| `APP__ENVIRONMENT` | `production`。触发启动期硬化校验，并关闭 `/docs` 与 `/redoc` |
| `SECURITY__JWT_SECRET` | ≥32 字符的唯一随机值。泄露 = 任何人都能伪造任意身份的 token |
| `SECURITY__BOOTSTRAP_ADMIN_PASSWORD` | ≥12 字符。仅在空库首次启动时用 |
| `DATABASE__URL` | 必须是 `postgresql+asyncpg://...` |
| `APP__CORS_ALLOW_ORIGINS` | 明确的 https origin 列表，不能含 `*` |
| `APP__TRUSTED_HOSTS` | 明确的主机名列表 |
| `REDIS__URL` | 带口令 |
| `LLM__PROVIDER` / `LLM__API_KEY` | 用真实模型时必填；`mock` 时留空 |
| `LLM__MONTHLY_BUDGET_USD` | 成本护栏，按实际预算设 |
| `LLM__PRICING` | JSON，按模型给出 USD/百万 token 的 `[prompt, completion]`。内置表是国际标价，区域计费不同（中国区 DashScope 按 CNY）时必须覆盖，否则预算护栏按错单价扣减。**留空 = 不覆盖**（`.env.example` 就是空值）；只接受空字符串或合法 JSON，`LLM__PRICING` 填纯空白会在 JSON 解码阶段直接报错 |
| `OBSERVABILITY__LOG_LEVEL` | `INFO`。排障时临时调 `DEBUG`，注意 DEBUG 会打印更多上下文 |
| `INGEST__SOURCES` | JSON 数组，**只放真实数据源**；默认值 `["synthetic"]` 是给演示环境的 |
| `INGEST__SCHEDULER_ENABLED` | 用 CronJob 就设 `false`（见 §3.3），靠进程内常驻循环才设 `true` |
| `INGEST__LOOKBACK_DAYS` / `INGEST__MAX_CATCHUP_DAYS` | 前者是平台修订昨日数字的重叠余量，后者是错过运行能自愈的上界。必须 `catchup >= lookback`，否则启动即报错 |
| `INGEST__LEASE_TTL_SECONDS` | 必须大于一次完整拉取的耗时，否则慢拉取会被下一趟中途接管。默认 1800，与 CronJob 的 `activeDeadlineSeconds` 对齐 |

**绝对不要**在生产改的：

| 变量 | 默认 | 原因 |
|---|---|---|
| `SECURITY__REQUIRE_ACTION_APPROVAL` | `true` | 关掉等于让 Agent 无人审批直接改预算 |
| `SECURITY__VERIFY_SESSION_ON_REQUEST` | `true` | 关掉后登出/停用不再即时生效，见 [ADR-0003](../adr/0003-session-revocation-on-request.md) |
| `UVICORN_WORKERS` | `1` | >1 会让 run 与它的 SSE 订阅者分离，见 [ADR-0002](../adr/0002-in-process-run-dispatch.md) |
| `INGEST__SOURCES` | `["synthetic"]` | **别把 `synthetic` 放进生产调度。**它是种子化 RNG 生成的数字，定时拉它等于凭空制造一段没有任何平台报告过的指标历史——而这恰恰是调度器要防止的失败模式 |
| `INGEST__DRY_RUN` | `false` | 生产上设 `true` 会让每一趟都"成功"却什么都不写，水位也不推进，于是数据永远停在上线那天。演练完就关掉 |

---

## 5. 启动期硬化校验

`APP__ENVIRONMENT` 为 `staging` 或 `production` 时，`Settings._enforce_production_hardening` 会在**构造配置对象时**抛错，进程根本起不来。检查项：

| 检查 | 条件 |
|---|---|
| JWT 密钥 | 不在弱值黑名单（`changeme`/`secret`/`insecure`/…）且长度 ≥32 |
| 引导管理员口令 | 不在弱值黑名单且长度 ≥12 |
| CORS | `APP__CORS_ALLOW_ORIGINS` 不含 `*` |
| 数据库 | `DATABASE__URL` 不是 SQLite |

错误信息会把所有不达标项一次性列全，不用一个一个试：

```
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  Insecure production configuration: SECURITY__JWT_SECRET must be a unique value
  of at least 32 characters; DATABASE__URL must point at PostgreSQL in production
```

另外 `LLM__PROVIDER` 为 `openai` / `azure_openai` / `openai_compatible` 时，`LLM__API_KEY` 为空也会启动失败。想不填 Key 就用 `LLM__PROVIDER=mock`。

> **这是有意的。** 一个用默认密钥启动的生产实例，等于把"改广告预算"的权限公开在网上。宁可起不来，也不要带病上线。

---

## 6. 镜像构建

两个 Dockerfile 都以**仓库根**为构建上下文：

```bash
docker build -f deploy/docker/Dockerfile.backend  -t registry.example.com/adoptimizer/backend:v1.0.0  .
docker build -f deploy/docker/Dockerfile.frontend -t registry.example.com/adoptimizer/frontend:v1.0.0 .
docker push registry.example.com/adoptimizer/backend:v1.0.0
docker push registry.example.com/adoptimizer/frontend:v1.0.0
```

**后端镜像**（多阶段）：

- Stage 1 装 `build-essential` + `libffi-dev`，编译 argon2-cffi / asyncpg / numpy 的 wheel，装 `[postgres,analytics,worker]` extras 进 `/opt/venv`
- Stage 2 只拷贝 venv + `alembic.ini` + `migrations`，基础镜像回到干净的 `python:3.12-slim`
- `tini` 作 PID 1 回收僵尸进程；`curl` 仅供 HEALTHCHECK
- 非 root（UID/GID 10001），`/var/lib/adoptimizer` 归属该用户
- `HEALTHCHECK` 只探 `/healthz`（存活）；就绪交给编排器判 `/readyz`，因为它依赖数据库可达
- `ENTRYPOINT` 是 `backend-entrypoint.sh`：`RUN_MIGRATIONS=true` 时先 `alembic upgrade head`，带重试（默认 30 次 × 2s）覆盖 "postgres 还没就绪" 的启动竞态

**前端镜像**：

- Stage 1 `node:22-alpine`，优先 `pnpm --frozen-lockfile`，其次 `npm ci`，都没有才 `npm install`。仓库里有 `package-lock.json`，所以实际走的是 `npm ci` 分支：镜像里的依赖树与 CI 完全一致
- `VITE_*` 在构建期内联，所以 API 前缀是 build arg（默认 `/api/v1`，相对路径 → 同源）
- Stage 2 `nginx:1.27-alpine`，把 `nginx-default.conf.template` 放进 `templates/`，由镜像自带的 entrypoint 用 `envsubst` 渲染 → **后端地址是运行时设置，不是构建期**
- nginx 职责：静态包、`/api` 反代、SSE 不缓冲、客户端路由回退到 `index.html`、`/assets/` 一年 immutable 缓存、`index.html` 永不缓存

nginx 运行时变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `API_UPSTREAM` | `api:8000` | 后端 host:port |
| `LISTEN_PORT` | `8080` | nginx 监听端口 |
| `SSE_READ_TIMEOUT` | `3600s` | run 流可以静默多久 |

---

## 7. CI/CD

`.github/workflows/ci.yml` 已经覆盖：

| 阶段 | 内容 |
|---|---|
| backend | `ruff format --check` → `ruff check` → `mypy src`（strict）→ `pytest --cov`（覆盖率 <90% 即失败） |
| frontend | `eslint --max-warnings 0` → `tsc` → `vitest run` → `vite build` |
| images | push 时构建两个镜像（验证 Dockerfile 可用） |
| manifests | `docker compose config` 与 `kustomize build` 渲染校验 |

推荐的发布流水线：

```
PR ──▶ CI 全绿 ──▶ 人工 review
                └─▶ 构建并推送带 tag 的镜像
main ─▶ 部署到 staging ─▶ 冒烟测试 ─▶ 部署到 production
                                        ├─ 1. create job from cronjob/adoptimizer-migrate
                                        ├─ 2. kubectl apply -k deploy/k8s
                                        ├─ 3. rollout status
                                        └─ 4. 冒烟：/readyz、登录、触发一次 run、审批一个动作
```

详见 [07 测试与 CI](07-testing-and-ci.md)。

---

## 8. 上线检查单

复制这份清单，逐项打勾。

**配置**
- [ ] `APP__ENVIRONMENT=production`
- [ ] `SECURITY__JWT_SECRET` 是新生成的 ≥32 字符随机值，且不在 Git 里
- [ ] `SECURITY__BOOTSTRAP_ADMIN_PASSWORD` ≥12 字符，且不在 Git 里
- [ ] `DATABASE__URL` 指向 PostgreSQL，口令不是 `adoptimizer`
- [ ] `REDIS__URL` 带口令
- [ ] `APP__CORS_ALLOW_ORIGINS` / `APP__TRUSTED_HOSTS` 是真实域名
- [ ] `LLM__MONTHLY_BUDGET_USD` 按实际预算设置
- [ ] `LLM__PRICING` 覆盖了实际使用的模型，启动日志里没有 `llm_model_has_no_pricing_entry`
- [ ] `SECURITY__REQUIRE_ACTION_APPROVAL=true`
- [ ] `SECURITY__VERIFY_SESSION_ON_REQUEST=true`
- [ ] 单副本单 worker（`UVICORN_WORKERS=1`）

**数据库**
- [ ] `alembic upgrade head` 成功，`alembic check` 报告无漂移
- [ ] PostgreSQL 有备份策略（见 [05 运维手册](05-operations-runbook.md)）
- [ ] 数据库端口不对公网暴露

**网络与 TLS**
- [ ] 全站 https，`ssl-redirect` 与 `force-ssl-redirect` 开启
- [ ] HSTS 生效（响应头里有 `Strict-Transport-Security`）
- [ ] SSE 路径 `proxy-buffering: off` 且读超时 ≥ 一次 run 的最长耗时
- [ ] NetworkPolicy 已应用

**可观测性**
- [ ] Prometheus 抓取 `/metrics`，能看到 `http_requests_total` 等指标
- [ ] 日志采集器收到 JSON 日志，能按 `request_id` 检索
- [ ] 告警规则已配（见运维手册 §3）
- [ ] `ingest_ticks_total` 与 `ingest_lag_days` 有 series（`ingest_lag_days` 完全缺失 = 这个源从未被拉过，用 `absent()` 报）

**验收**
- [ ] `/healthz` 200，`/readyz` 200 且所有 check 为 ok
- [ ] 引导管理员能登录，**并已改口令**
- [ ] 已创建至少一个非引导的 admin 账号
- [ ] `INGEST__SOURCES` 里没有 `synthetic`，且 `INGEST__SCHEDULER_ENABLED=false`（节奏归 CronJob）
- [ ] 有外部推送管道时，它用的是 `ingestor` 账号而不是某个人的 optimizer / admin 账号（`audit_logs.actor_role` 可验证）
- [ ] 手动跑一趟 `kubectl create job --from=cronjob/adoptimizer-ingest` 成功；`GET /api/v1/ingest/schedule` 里 `plan.reason` 变成 `covered`、`lease.held` 为 `false`
- [ ] 触发一次 run 能跑完，SSE 时间线实时更新
- [ ] 审批并执行一个动作成功，审计流水里有对应记录
- [ ] 用 viewer 账号调写接口返回 403
- [ ] `/docs` 与 `/redoc` 在生产已关闭（404）

**回滚准备**
- [ ] 上一个可用镜像 tag 已记录
- [ ] 迁移是否可 `downgrade` 已确认（不可逆迁移要有单独预案）
- [ ] 数据库快照/备份时间点已记录

---

## 9. 升级与回滚

**升级**

```bash
# Compose（compose 文件在 deploy/compose/，本节的命令都在仓库根目录执行）
IMAGE_TAG=v1.1.0 docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml up -d
docker compose -f deploy/compose/docker-compose.yml logs -f api   # 确认迁移与启动日志

# Kubernetes
kubectl -n adoptimizer create job adoptimizer-migrate-$(date +%s) --from=cronjob/adoptimizer-migrate
kubectl -n adoptimizer wait --for=condition=complete job -l app.kubernetes.io/component=migrate --timeout=300s
cd deploy/k8s && kustomize edit set image adoptimizer/backend=...:v1.1.0 && cd ../..
kubectl apply -k deploy/k8s
kubectl -n adoptimizer rollout status deploy/backend
```

**回滚**

```bash
# Kubernetes
kubectl -n adoptimizer rollout undo deploy/backend
kubectl -n adoptimizer rollout status deploy/backend

# Compose
IMAGE_TAG=v1.0.0 docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml up -d
```

⚠️ **代码可以回滚，schema 不一定能。** 回滚前先确认这次发布引入的迁移是否 `downgrade()` 可逆：

```bash
docker compose exec api alembic downgrade -1 --sql    # 只看 SQL，不执行
```

如果迁移不可逆（加非空列、删列、数据转换），正确做法是**向前修**而不是回滚代码：保留新 schema，只回滚应用镜像，前提是旧版本代码能与新 schema 共存。因此发布纪律要求：**迁移必须向后兼容一个版本**（先加列、双写、再删列，分两次发布）。

详细故障处置见 [05 运维手册](05-operations-runbook.md)。