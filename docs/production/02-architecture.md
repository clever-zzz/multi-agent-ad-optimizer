# 02 架构

## 1. 设计目标

按重要性排序，后面的取舍都为前面让路：

1. **可审计** — 每一分钱预算的变化都要能回答"谁、什么时候、基于什么数据、为什么"。
2. **可回退** — 任何一个外部依赖（LLM、Redis、ClickHouse、langgraph、CVXPY）挂掉，系统降级而不是崩溃。
3. **可测试** — 业务规则必须是纯函数，能在没有数据库和网络的情况下被单元测试覆盖。
4. **可扩展** — 加一个广告平台、加一个 Agent、换一个模型供应商，都应该是加法而不是改法。
5. **可运维** — 探针、指标、结构化日志、CLI，缺一不可。

---

## 2. 分层

```
api/            HTTP 边界：路由、DTO 校验、依赖注入、状态码
  ↓ 只依赖 services 和 schemas
services/       应用用例：事务边界、权限校验后的编排、审计写入
  ↓ 只依赖 repositories、domain、orchestrator、llm
orchestrator/   LangGraph 图与事件总线
agents/         5 个 Agent，实现 BaseAgent 模板方法
  ↓
domain/         纯业务规则，零 I/O、零框架依赖
repositories/   持久化访问，SQLAlchemy 2 async
  ↓
infra/          引擎与适配器：数据库、缓存、ClickHouse、广告平台
core/           横切关注点：配置、日志、安全、错误、中间件、指标
```

**依赖方向严格单向。** `domain/` 不 import 任何 `infra/`、`api/`、甚至 `sqlalchemy`；这是它能被 100% 单元测试覆盖的前提。`api/` 不 import 任何具体适配器，只通过 `core/deps.py` 拿到注入的 `Container`。

### 各层职责细节

| 模块 | 职责 | 关键文件 |
|---|---|---|
| `core/config.py` | 全部运行时配置的唯一来源，启动即校验；生产环境拒绝弱密钥 | `Settings`, `_enforce_production_hardening` |
| `core/container.py` | 组合根。所有长生命周期协作对象在此构造一次 | `Container`, `build_container` |
| `core/deps.py` | FastAPI 依赖：容器、会话、当前身份、权限断言 | `ClaimsDep`, `SessionDep`, `require`, `require_role` |
| `core/security.py` | Argon2id、JWT 签发校验、角色→权限矩阵 | `TokenService`, `permissions_for` |
| `core/errors.py` | 领域异常 → HTTP problem document 的映射 | `register_exception_handlers` |
| `core/middleware.py` | request_id、安全响应头、令牌桶限流 | `RequestContextMiddleware` 等 |
| `core/metrics.py` | Prometheus 指标定义（17 个） | `REGISTRY` |
| `domain/kpi.py` | CTR/CVR/CPA/ROAS 计算与健康分 | `PerformanceSnapshot`, `health_score` |
| `domain/pricing.py` | eCPM、竞价上限、出价推导 | 全部纯函数 |
| `domain/budget.py` | 预算重分配：CVXPY 优先，贪心 LP 兜底 | `allocate` |
| `domain/anomaly.py` | 阈值 + 统计双重异常检测、告警去重 | `detect`, `deduplicate` |
| `domain/scoring.py` | 创意评分 | `score_creative` |
| `domain/statistics.py` | A/B 显著性检验、样本量估算 | 纯函数 |
| `domain/audience.py` | 受众分段观察 | `analyze` |

---

## 3. Agent 编排

### 3.1 图结构

```
entry ─▶ monitor ─▶ audience ─▶ creative ─▶ bidding ─▶ optimize
            ▲                                                 │
            │                                                 ▼
            └──────────── "continue" ◀── _route_after_optimize
                                                        │
                                                        └─ "end" ─▶ END
```

路由逻辑（`orchestrator/graph.py::_route_after_optimize`）：

```python
if state["is_complete"]:                     return "end"
if state["iteration"] >= state["max_iterations"]: return "end"
return "continue" if state["alerts"] else "end"
```

即：**仍有未消化告警、且迭代预算未用尽**时回到 monitor 再跑一轮；否则结束。这让系统能对第一轮动作的效果做二次评估，而不是单趟扫过。

### 3.2 五个 Agent 的实际职责

| Agent | 输入 | 输出（写回 state） | 是否调用 LLM |
|---|---|---|---|
| **monitor** | 活动列表 + 日粒度指标窗口 | `metrics`, `health`, `alerts`, `alert_fingerprints` | 否（纯规则 + 统计） |
| **audience** | `metrics`, 快照 | `audience_observations`, `audience_insights` | 是（洞察摘要） |
| **creative** | `health`, 现有创意, `audience_insights` | `new_creatives` | 是（文案生成，带结构化校验） |
| **bidding** | `metrics`, 目标 CPA/ROAS | `bidding_decisions` | 否（纯 `domain.pricing`） |
| **optimize** | 以上全部 | `budget_allocations`, `optimization_actions`, `iteration` | 部分（解释文本） |

**关键取舍：LLM 只用在"需要生成或归纳自然语言"的地方，所有涉及金钱的数学都在 `domain/` 里用确定性代码算。** 模型可以影响"怎么解释这个决定"，但不能凭空决定"预算改成多少"——后者由 CVXPY/贪心 LP 在明确约束下求解。

### 3.3 状态与 reducer

`AgentState` 是显式 `TypedDict`，每个累积通道都绑定了 reducer：

| 通道 | reducer | 语义 |
|---|---|---|
| `new_creatives` | `append_list` | 跨迭代累积，不覆盖 |
| `optimization_actions` | `append_list` | 跨迭代累积 |
| `agent_messages` | `append_list` | 完整轨迹 |
| `alert_fingerprints` | `append_list` | 去重历史 |
| `metrics` / `alerts` / `bidding_decisions` / `budget_allocations` | `replace_list` | 每轮重算，取最新 |
| `health` / `audience_insights` / `daily_budgets` / `usage` | `merge_mapping` | 浅合并 |
| `iteration` | `max_int` | 单调，重放不会回退 |

> 这两点（显式 TypedDict、真正接上 reducer）是原始 demo 的致命 bug 来源：langgraph 1.x 下传裸 `dict` 给 `StateGraph`，每个节点只能看到上一个节点的返回值；reducer 定义了却没接线，多轮迭代会互相覆盖。

### 3.4 降级路径

```python
self._graph = self._compile() if HAS_LANGGRAPH else None
execution_mode = "langgraph" if self._graph else "sequential"
```

`langgraph` 未安装或图编译抛异常时，自动切到 `_invoke_sequential`：按同样顺序 await 五个 Agent，用同样的 reducer 合并状态。**业务结果一致**，只是失去 checkpoint 与可视化。`/readyz` 会如实上报当前 mode。

### 3.5 AgentContext

运行期上下文（run_id、配置、LLM 网关、事件总线、快照缓存）通过 langgraph 的 `config["configurable"]` 传递，**不烧进编译好的图**。因此一个图实例服务所有并发 run，Agent 保持无状态、可并发。

---

## 4. 运行生命周期与并发模型

```
POST /api/v1/runs
  │
  ├─ OptimizationService.start_run
  │    ├─ 校验（活动数上限、频率限制、幂等键）
  │    ├─ 采集输入（快照、现有创意、目标、当前预算）
  │    ├─ 写 optimization_runs 行，status=pending
  │    └─ _dispatch → asyncio.create_task(_execute(...))
  │                                    │
  └─ 立即返回 202 + run 对象            ▼
                            _execute: mark_running
                                      → orchestrator.run(state, ctx)
                                      → 落库 actions/alerts/allocations/events
                                      → 写 summary、token、成本
                                      → mark_finished(status=succeeded|failed|cancelled)
```

- **派发始终发生**；请求体里的 `background` 只决定调用方是否等待完成（同步模式用于 CLI 和测试）。
- 事件写入两处：`run_events` 表（持久、可回放）+ `EventBus` 内存队列（低延迟 SSE）。
- `GET /runs/{id}/stream` 是 SSE：先回放已落库的历史事件，再订阅实时队列，客户端断线重连不丢进度。
- 进程启动时 `reap_stale_runs` 把上次异常退出遗留的 `running` 状态 run 标记为 `failed`，避免僵尸。

### 4.1 为什么必须 `--workers 1`

`_tasks` 字典与 `EventBus` 订阅者都是**进程内**的。多 worker 时：

- run 在 worker A 执行，SSE 请求落到 worker B → B 的 EventBus 没有这个 run 的订阅者，只能回放历史事件后空转。
- `wait_for(run_id)` 在另一个进程里找不到 task。

横向扩展的正确做法是**多副本 + 会话亲和**（每个副本 `--workers 1`），或把派发换成共享队列。详见 [ADR-0002](../adr/0002-in-process-run-dispatch.md) 与 [08 限制](08-limitations-and-roadmap.md)。

---

## 5. 数据模型

14 张表，全部使用**应用层生成的字符串主键**（`core/ids.py::new_id`，带类型前缀如 `camp_`/`run_`/`act_`）。这样 id 可以在 insert 之前生成、可以安全对外暴露、跨库迁移不会撞序列。

| 表 | 用途 | 关键约束 |
|---|---|---|
| `users` | 操作者账号 | `uq` email；role；锁定字段 |
| `refresh_sessions` | 可独立撤销的 refresh token | FK→users `ON DELETE CASCADE` |
| `campaigns` | 广告活动 | `(platform, external_id)` 唯一；`(status, platform)` 复合索引 |
| `creatives` | 创意（人工或 Agent 生成） | FK→campaigns CASCADE；`origin` 区分来源 |
| `daily_metrics` | 日粒度聚合，优化器输入 | `(campaign_id, creative_id, stat_date)` 唯一 **+ 部分唯一索引** `(campaign_id, stat_date) WHERE creative_id IS NULL` |
| `optimization_runs` | 一次闭环执行 | status 索引；idempotency_key 索引 |
| `run_events` | 追加式事件流 | `(run_id, seq)` 唯一，支撑 SSE 回放 |
| `optimization_actions` | Agent 提案，人审批门 | FK→runs `ON DELETE SET NULL`（run 删了动作还在，可审计） |
| `alerts` | 监控告警 | `dedup_key` 索引；`(status, detected_at)` 复合索引 |
| `budget_allocations` | 预算重分配提案 | 记录 `solver`（`cvxpy` / `greedy_lp`） |
| `ab_tests` | 创意实验 | MDE、所需样本量、traffic_split、result JSON |
| `audit_logs` | 不可变审计 | actor/before/after/IP/UA/request_id |
| `idempotency_records` | 写操作幂等 | PK = 客户端提供的 key；带 `expires_at` |
| `llm_spend` | 每次模型调用记账 | provider/model/token/cost/latency/outcome |

### 5.1 `daily_metrics` 的双粒度：写入去重与读取合并

`creative_id` 可为 NULL（表示"活动级"聚合行）。SQL 标准下 NULL 互不相等，所以 `(campaign_id, creative_id, stat_date)` 的唯一约束**保护不了活动级槽位**：并发 upsert 会插入重复行，而所有快照查询都用 `SUM()`，重复行会导致花费和收入被重复计算。

因此额外建了一个部分唯一索引：

```sql
CREATE UNIQUE INDEX uq_daily_metric_campaign_slot
  ON daily_metrics (campaign_id, stat_date)
  WHERE creative_id IS NULL;
```

PostgreSQL 与 SQLite 都支持部分索引，ORM 侧通过 `postgresql_where` / `sqlite_where` 同时声明。

**读取侧同样不能想当然。** 同一天可能同时存着两种粒度：`creative_id IS NULL` 的活动级汇总行，和若干条按创意拆分的明细行——而明细之和**就是**那条汇总行。`SqlAggregateWarehouse` 要是直接对整表 `SUM()`，每个 KPI 都会翻倍，偏偏 Agent 打分用的正是这些数字。

所以聚合读取按 `(campaign_id, stat_date)` **槽位**合并：

- 活动级汇总行**优先**，它是这个槽位的权威值
- 只有当某个槽位**完全没有**汇总行时，才用该槽位的明细行兜底

按槽位而不是按活动合并，是为了让混合 portfolio 也精确：存了汇总行的活动贡献汇总值，只有明细的活动照样贡献它的投放量，而不是读成 0、然后从优化里悄悄消失。

ClickHouse 后端没有这个问题——它读的是原始事件流 `ad_events`，只有一种粒度。

### 5.2 迁移策略

- `migrations/env.py` 使用**异步引擎**（`async_engine_from_config` + `run_sync`），因为项目只装异步驱动（aiosqlite / asyncpg）。要求第二个同步驱动（如 psycopg2）意味着两套代码路径操作同一个 schema。
- SQLite 下开启 `render_as_batch=True`，让同一份迁移脚本在两种后端都能跑（SQLite 无法原地 ALTER 多数约束）。
- `compare_type=True` + `compare_server_default=True`，autogenerate 能发现类型漂移。
- 约束名走 `infra/db/base.py` 的显式命名约定，保证跨后端稳定、可逆。
- SQLite 开发库首次启动会自动 `create_all()` **并写入 `alembic_version` 戳**，因此后续 `adoptimizer migrate` 是 no-op 而不是 "table already exists"。PostgreSQL 永远只由迁移管理。

---

## 6. LLM 网关

```
Agent ──▶ LLMGateway ──▶ provider (mock | openai | azure_openai | openai_compatible)
              │
              ├─ tenacity 重试（指数退避，仅对可重试错误）
              ├─ 并发信号量（LLM__CONCURRENCY）
              ├─ 超时（LLM__TIMEOUT_SECONDS）
              ├─ 响应缓存（cache_enabled + TTL，键含 prompt 与参数）
              ├─ 结构化输出校验（llm/structured.py，失败可回退规则生成）
              └─ SpendLedger：每次调用记 token/成本/延迟/结果
                        │
                        └─ 月度预算护栏（LLM__MONTHLY_BUDGET_USD）
                             超预算 → fail_open_to_mock ? 降级 mock : 直接失败
```

`mock` provider 是**确定性**的：同样的输入永远得到同样的输出，不联网、不花钱。这是整个测试套件能跑得快且稳定的原因。

---

## 7. 横切关注点

### 7.1 中间件栈（外→内）

```
CORSMiddleware            # 最外层，预检请求不该被后面任何一层拦
  TrustedHostMiddleware   # 仅当 trusted_hosts != ["*"] 时挂载
  GZipMiddleware          # minimum_size=1024
  RateLimitMiddleware     # 令牌桶；/healthz /readyz /metrics /system/info 豁免
  SecurityHeadersMiddleware
  RequestContextMiddleware # 生成/透传 X-Request-ID，绑定到日志上下文
    └─ 路由
```

> FastAPI/Starlette 的 `add_middleware` 是**后加的在外层**，所以代码里的添加顺序与请求实际经过的顺序相反。

安全响应头（`setdefault`，允许上游覆盖）：

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Permissions-Policy: geolocation=(), microphone=()
Content-Security-Policy: default-src 'self'; img-src 'self' data:;
  style-src 'self' 'unsafe-inline'; script-src 'self';
  connect-src 'self'; frame-ancestors 'none'
Cross-Origin-Opener-Policy: same-origin
Strict-Transport-Security: max-age=31536000; includeSubDomains   # 仅 https
```

### 7.2 可观测性

**17 个 Prometheus 指标**，覆盖四个层面：

| 层面 | 指标 |
|---|---|
| HTTP | `http_requests_total`, `http_request_duration_seconds` |
| Agent | `agent_runs_total`, `agent_run_duration_seconds`, `agent_step_total`, `agent_step_duration_seconds`, `active_runs`, `queue_depth` |
| LLM | `llm_calls_total`, `llm_latency_seconds`, `llm_tokens_total`, `llm_spend_usd_total` |
| 业务/基础设施 | `alerts_total`, `actions_total`, `cache_ops_total`, `db_query_duration_seconds` |

日志用 structlog，JSON 输出，`X-Request-ID` 与 `run_id` 通过 contextvar 绑定到每条记录，可以按请求或按 run 完整串联。

### 7.3 探针

| 端点 | 语义 | 失败行为 |
|---|---|---|
| `/healthz` | 存活。进程能响应即 200 | 永不因依赖故障返回非 200（否则会被编排器无意义地重启） |
| `/readyz` | 就绪。聚合 database/cache/llm/orchestrator/platforms | 数据库不可达 → 503，从负载均衡摘除 |
| `/metrics` | Prometheus 抓取 | — |
| `/system/info` | 运行时配置（只暴露安全字段） | — |

四个端点全部免鉴权，且在限流豁免名单里。

---

## 8. 前端架构

```
React 19 + TypeScript (strict) + Vite 6 + Tailwind 4
  │
  ├─ lib/api.ts          唯一的 HTTP 出口：base URL、token 注入、401→refresh 重试、problem document 解析
  ├─ lib/sse.ts          EventSource 封装，断线重连
  ├─ lib/queryKeys.ts    集中管理 TanStack Query 缓存键，避免手写字符串
  ├─ lib/queryClient.ts  全局 QueryClient（重试、失效策略）
  ├─ stores/auth.ts      zustand：token、当前用户、权限判定
  ├─ stores/toast.ts     zustand：全局提示
  ├─ hooks/use*.ts       12 个 hook，每个资源一组（useCampaigns/useRuns/useActions/useAlerts/useAdmin/...）
  ├─ components/ui/      无业务语义的基础组件（Button/Modal/Table/Badge/...）
  ├─ components/charts/  手写 SVG：TrendChart / Sparkline / Donut / BarList + chartUtils
  ├─ components/layout/  AppLayout / Sidebar / Topbar
  └─ pages/              13 个页面，路由级懒加载
```

**数据流约定**：组件不直接调 `api.ts`，一律通过 hook → TanStack Query。写操作用 mutation + `invalidateQueries`，审批/执行类操作走乐观更新 + 失败回滚。

**权限**：`ProtectedRoute` 检查登录态；页面内按 `permissions` 数组决定按钮是否渲染。这只是 UX，**真正的强制在后端**（`require_permission`）。

图表选择手写 SVG 而非引入图表库，理由见 [ADR-0001](../adr/0001-custom-svg-charts.md)。

---

## 9. 一次完整请求的时序（以"审批并执行一个动作"为例）

```
Web「动作」页点【批准】
  → useApproveAction.mutate({actionId})
    → POST /api/v1/actions/{id}/approve   [Authorization: Bearer <access>]
      RequestContextMiddleware   生成 request_id，绑定日志上下文
      SecurityHeadersMiddleware  准备响应头
      RateLimitMiddleware        写操作桶（60/min）扣减
      CORSMiddleware             放行同源
      get_current_claims          解 JWT → 校验 iss/aud/exp
                                 → SECURITY__VERIFY_SESSION_ON_REQUEST=true
                                   ⇒ 查 refresh_sessions 确认会话未撤销
      require(ACTION_APPROVE)     角色→权限矩阵断言，不足则 403 problem doc
      ActionService.approve       状态机校验（proposed → approved）
                                 → 写 audit_logs（actor/before/after/ip/ua/request_id）
      session.commit()
    ← 200 OptimizationActionOut
  → queryClient.invalidateQueries(actions.list)
  → toast.success
```

任何一步失败都会：回滚事务、返回统一 problem document、带上同一个 `X-Request-ID`，日志里能用这个 id 串起完整链路。