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
agents/         6 个 Agent，实现 BaseAgent 模板方法
  ↓ 只能通过 tools/ 触碰外部世界
tools/          能力目录 + 执行器：校验、授权、预算、幂等、干跑、审计
  ↓
domain/         纯业务规则，零 I/O、零框架依赖
repositories/   持久化访问，SQLAlchemy 2 async
  ↓
infra/          引擎与适配器：数据库、缓存、ClickHouse、广告平台、指标数据源
core/           横切关注点：配置、日志、安全、错误、中间件、指标
```

**依赖方向严格单向。** `domain/` 不 import 任何 `infra/`、`api/`、甚至 `sqlalchemy`；这是它能被 100% 单元测试覆盖的前提。`api/` 不 import 任何具体适配器，只通过 `core/deps.py` 拿到注入的 `Container`。`infra/` 同样不 import `schemas/`——指标数据源因此可以脱离 HTTP 层单独构造和测试，转换到线上契约由 `services/ingest.py` 一处负责。

### 各层职责细节

| 模块 | 职责 | 关键文件 |
|---|---|---|
| `core/config.py` | 全部运行时配置的唯一来源，启动即校验；生产环境拒绝弱密钥 | `Settings`, `_enforce_production_hardening` |
| `core/container.py` | 组合根。所有长生命周期协作对象在此构造一次 | `Container`, `build_container` |
| `core/deps.py` | FastAPI 依赖：容器、会话、当前身份、权限断言 | `ClaimsDep`, `SessionDep`, `require`, `require_role` |
| `core/security.py` | Argon2id、JWT 签发校验、角色→权限矩阵 | `TokenService`, `permissions_for` |
| `core/errors.py` | 领域异常 → HTTP problem document 的映射 | `register_exception_handlers` |
| `core/middleware.py` | request_id、安全响应头、令牌桶限流 | `RequestContextMiddleware` 等 |
| `core/metrics.py` | Prometheus 指标定义（18 个） | `REGISTRY` |
| `domain/kpi.py` | CTR/CVR/CPA/ROAS 计算与健康分 | `PerformanceSnapshot`, `health_score` |
| `domain/pricing.py` | eCPM、竞价上限、出价推导 | 全部纯函数 |
| `domain/budget.py` | 预算重分配：CVXPY 优先，贪心 LP 兜底 | `allocate` |
| `domain/anomaly.py` | 阈值 + 统计双重异常检测、告警去重 | `detect`, `deduplicate` |
| `domain/scoring.py` | 创意评分 | `score_creative` |
| `domain/statistics.py` | A/B 显著性检验、样本量估算 | 纯函数 |
| `domain/audience.py` | 受众分段观察 | `analyze` |
| `tools/spec.py` | 工具契约：参数模型、能力声明、结果对象 | `ToolSpec`, `ToolRequest`, `ToolResult` |
| `tools/registry.py` | 能力目录，按 Agent 过滤可见范围 | `ToolRegistry.for_agent` |
| `tools/executor.py` | 唯一出口：校验→授权→预算→幂等→干跑→审计 | `ToolExecutor.call` |
| `tools/platform.py` | 7 个平台工具，一对一映射适配器能力 | `PlatformTools`, `build_platform_tools` |
| `tools/audit.py` | 调用落库（含被拒绝的），失败不拖垮 run | `DatabaseToolAudit` |
| `infra/ingest/base.py` | 数据源协议与记录值对象，零 Pydantic 依赖 | `MetricSource`, `SourceRecord`, `SourceTarget` |
| `infra/ingest/synthetic.py` | 确定性合成数据源（**不是广告数据**，每行都盖 `source="synthetic"`） | `SyntheticMetricSource` |
| `infra/ingest/platform_report.py` | 把适配器 `fetch_report` 的归一化行转成记录；**从不声称 revenue** | `PlatformReportSource` |
| `infra/ingest/registry.py` | 数据源注册表：按名取用、上报可用性 | `MetricSourceRegistry`, `build_metric_sources` |
| `services/ingest.py` | 采集用例：校验→归属→写入→对账，逐条报告下落 | `IngestService` |
| `repositories/ingest.py` | 采集批次台账 | `IngestBatchRepository` |
| `services/scheduling.py` | 定时拉取：日历推窗口、错过策略、租约单飞；`plan_window` 是纯函数 | `IngestScheduler`, `plan_window` |
| `repositories/scheduling.py` | 水位（只放宽不收窄）与租约（一条条件 UPDATE 定胜负） | `WatermarkRepository`, `LeaseRepository` |

---

## 3. Agent 编排

### 3.1 图结构

```
entry ─▶ monitor ─▶ audience ─▶ creative ─▶ bidding ─▶ optimize ─▶ critic
            ▲                                                            │
            │                                                            ▼
            └──────────── "continue" ◀── _route_after_critic
                                                                       │
                                                                       └─ "end" ─▶ END
```

路由逻辑（`orchestrator/graph.py::_route_after_critic`）：

```python
if state["is_complete"]:                     return "end"
if state["iteration"] >= state["max_iterations"]: return "end"
return "continue" if state["alerts"] else "end"
```

路由读的是 **critic 之后**的状态，所以只有未被复核掉的告警才会驱动下一轮。

即：**仍有未消化告警、且迭代预算未用尽**时回到 monitor 再跑一轮；否则结束。这让系统能对第一轮动作的效果做二次评估，而不是单趟扫过。

### 3.2 六个 Agent 的实际职责

| Agent | 输入 | 输出（写回 state） | 是否调用 LLM |
|---|---|---|---|
| **monitor** | 活动列表 + 日粒度指标窗口 | `metrics`, `health`, `alerts`, `alert_fingerprints` | 否（纯规则 + 统计） |
| **audience** | `metrics`, 快照 | `audience_observations`, `audience_insights` | 是（洞察摘要） |
| **creative** | `health`, 现有创意, `audience_insights` | `new_creatives` | 是（文案生成，带结构化校验） |
| **bidding** | `metrics`, 目标 CPA/ROAS | `bidding_decisions` | 否（纯 `domain.pricing`） |
| **optimize** | 以上全部 | `budget_allocations`, `optimization_actions`, `iteration` | 否（纯规则映射） |
| **critic** | `optimization_actions` 全集 | `critic_findings` | 否（纯规则） |

**关键取舍：LLM 只用在"需要生成或归纳自然语言"的地方，所有涉及金钱的数学都在 `domain/` 里用确定性代码算。** 模型可以影响"怎么解释这个决定"，但不能凭空决定"预算改成多少"——后者由 CVXPY/贪心 LP 在明确约束下求解。

### 3.3 状态与 reducer

`AgentState` 是显式 `TypedDict`，每个累积通道都绑定了 reducer：

| 通道 | reducer | 语义 |
|---|---|---|
| `new_creatives` | `append_list` | 跨迭代累积，不覆盖 |
| `optimization_actions` | `append_list` | 跨迭代累积 |
| `critic_findings` | `append_list` | 复核裁决累积，含被抑制提案的 id |
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

`langgraph` 未安装或图编译抛异常时，自动切到 `_invoke_sequential`：按同样顺序 await 六个 Agent，用同样的 reducer 合并状态。**业务结果一致**，只是失去 checkpoint 与可视化。`/readyz` 会如实上报当前 mode。

### 3.5 AgentContext

运行期上下文（run_id、配置、LLM 网关、事件总线、快照缓存）通过 langgraph 的 `config["configurable"]` 传递，**不烧进编译好的图**。因此一个图实例服务所有并发 run，Agent 保持无状态、可并发。

上下文里还有两样东西是工具层的前提：`tools`（执行器本身，降级部署里可能是 `None`）和 `campaign_refs`
（内部活动 id → `{platform, external_id, name, status}`）。后者是 Agent 唯一能知道「这个活动在广告平台上
叫什么」的地方，所以它既不需要、也拿不到数据库会话。`context.call_tool(...)` 在工具层缺席或被关闭时返回
`None`：按工具写的 Agent 在降级部署里照样跑得完一轮，而不是整轮失败。

### 3.6 Critic：唯一拥有全局视图的一步

optimizer 是按规则逐条开火的：一个活动同时踩中 CPA 上限和燃烧速度，就会同时收到
`pause_campaign` 和 `adjust_budget`——两个互斥的结果都要人去批。多迭代还会放大这个问题：
`optimization_actions` 是累积通道，同一个 (campaign, creative, type) 意图会每轮重新出现一次，带着新的 id。

critic 在 optimize 之后、路由之前跑，按固定顺序做四件事——先看**能不能执行**，再看重不重复，最后看冲不冲突：

| 规则 | 行为 |
|---|---|
| `duplicate_proposal` | 同一意图跨迭代重复提案，只保留 standing 最高的一条（并列时取最新一轮） |
| `pause_overrides_spend` / `campaign_pause_resume_conflict` / `experiment_on_paused_campaign` | 活动级互斥，standing 高者胜；**平局向“停止花钱”一侧倾斜** |
| `creative_pause_resume_conflict` / `pause_overrides_refresh` | 仅当两条提案指向同一个具体 creative_id 时才算冲突 |
| `unexecutable_proposal` | optimize 的平台预检已经把它挡下（参数非法 / 无权限 / 平台报错），不让人去批一个必然失败的改动 |

预检抑制只看**阻断性**拒绝：`validation_failed`、`permission_denied` 和平台自己报错（`failed`）说明提案本身有问题。
`budget_exhausted` 和「工具层被关闭」是**部署状态**，不是提案的缺陷——护栏没开就吞掉一条本来合理的提案，
等于让护栏掩盖真实工作，所以这两种情况不抑制。预检抑制也永远不会顺手带走一条可行的替代提案：
同一活动上另一条没被挡下的提案照常参与后面的 standing 比较。

**standing 不等于 confidence。** 两个 Agent 报出来的 `confidence` 量的根本不是同一件事：
`recommend_bid` 报的是“有多少投放证据支撑这个出价”，展示量过 5 万就会顶到 0.98；
告警派生的提案报的是“这个异常有多严重”，critical 固定 0.9、warning 固定 0.7。
直接比大小，一个投放数据漂亮的活动就能用 0.98 的调价把 0.9 的 critical 燃烧速度暂停挤掉——
这是真跑出来的 bug，不是假想。所以 `_rank()` 先比 severity（critical 恒胜），
confidence 只在同一 standing 内部做 tie-break，平局再向“停止花钱”一侧倾斜。

关键设计：**抑制是打标记，不是删除**。`optimization_actions` 用 `append_list`，任何节点都不能改写历史；
critic 只往 `critic_findings` 写裁决，真正的过滤发生在 `state.surviving_actions()`——持久化与汇总都走它。
这样一条被抑制的提案永远不会进审批队列，但裁决理由仍然可查，运营可以不同意 critic 并手动恢复。

critic 不调用 LLM、不发明动作、不执行任何东西；完全确定性，同输入同输出。

---

### 3.7 工具层：智能体的能力边界

Agent 不 import 平台客户端、不 import 数据库会话、不发 HTTP 请求。它只能报出一个工具名加一份 JSON 参数，
由 `tools/executor.py` 决定这次调用究竟发生什么。这条间接层不是装饰：它让「能力、权限、预算、幂等、干跑」
五件事在一个地方被强制执行，而不是指望以后每个新 Agent 都记得遵守约定。

**目录**（`tools/platform.py`）：7 个工具，一对一映射 `AdsPlatformClient` 的能力。

| 工具 | 读/写 | 允许调用的 Agent |
|---|---|---|
| `platform.campaign_report` | 读 | 全部（白名单为空即全员可用） |
| `platform.set_daily_budget` | 写 | optimize |
| `platform.pause_campaign` / `platform.resume_campaign` | 写 | optimize |
| `platform.pause_creative` / `platform.resume_creative` | 写 | creative、optimize |
| `platform.create_creative` | 写 | creative |

`GET /api/v1/admin/tools` 直接读注册表生成，所以这份清单不可能和实现漂移。

**执行器的五道关**，顺序固定，只有最后一道会碰到 handler：

| 顺序 | 检查 | 不通过时 |
|---|---|---|
| 1 | 工具存在吗 | `validation_failed` |
| 2 | 这个调用方在允许名单里吗 | `permission_denied` |
| 3 | 本轮 run 的 **Agent** 调用预算用完了吗 | `budget_exhausted` |
| 4 | 幂等键跑过了吗 | `replayed`，直接返回第一次的结果 |
| 5 | 参数合法吗（Pydantic，`extra="forbid"`） | `validation_failed` |

第 3 关里**被拒绝的调用同样计入预算**，否则一个反复传错参数的 Agent 就等于拿到了无限重试。
第 5 关的 `extra="forbid"` 同样关键：模型编造出一个不存在的字段时必须报错，而不是被静默忽略。

第 3 关只统计 **Agent 发起**的调用（`request.agent is not None`）。人工审批后的执行照样被审计、照样计入
`invocations`，但不消耗预算：这个上限约束的是「模型自己能做多少事」，如果让一轮繁忙的预检把运营已经
签字的那次执行挡在门外，护栏本身就成了故障源。

**干跑互锁 —— 真金白银的那道闸。** 写操作在两种情况下不会真正下发：

1. 请求来自 Agent 且 `TOOLS__ALLOW_AGENT_WRITES=false`（默认）。Agent 拿到的是一份预检结果，
   里面写明是哪条护栏拦下的。**模型推理得再自信，也动不了真实账户。**
2. `TOOLS__DRY_RUN=true`。纸面交易模式：连人工批准后的执行也只记录、不下发。接真实账号前先用它跑一遍。

`ToolRequest.agent=None` 表示「人工审批后的执行」，不受第 1 条约束——这是有意的：
真正花钱的决定应该跟在一次人类点击后面，而不是跟在一个置信度后面。

**审计。** 每次调用（包括被拒绝的）都会发一条 `tool.invoked` 事件（进 `run_events`，SSE 时间线可见），
并落一行 `tool_invocations`：`run_id / agent / actor / tool / outcome / read_only / dry_run /
touches_platform / arguments / result / error / duration_ms / idempotency_key`。
其中**被拒绝的行才是最有价值的**：它是「某个 Agent 试图做它不被允许做的事」的持久证据，
运营据此判断护栏设得对不对。审计写入失败只告警、不抛，丢一行记录比拖垮整轮优化更轻。

**谁在真的调用它。** 三类调用方共用同一个执行器，因此共用同一套护栏，区别只在于「谁是发起者」：

| 调用方 | 调什么 | 上限 | 结果落在哪 |
|---|---|---|---|
| monitor | `platform.campaign_report`，对最严重的若干条告警做实时核对 | `MAX_LIVE_CHECKS = 5` | `platform_checks`（带 `served_by`） |
| optimize | 对每条写提案先跑一次预检：`set_daily_budget` / `pause_*` / `resume_*` | `MAX_PREFLIGHTS_PER_ITERATION = 25` | 提案上的 `preflight` 注解 |
| 人工审批后的执行 | 与 Agent 完全相同的工具和执行器，只是 `agent=None` | 不消耗 Agent 预算 | `executions` + `tool_invocations` |

monitor 的实时核对用 `monitor-report:<iteration>:<campaign_id>` 作幂等键，所以同一轮里重复推理不会
重复打平台接口；核对不到目标的告警会记成 `served_by` 为空，而不是被悄悄丢掉。

因为 `TOOLS__ALLOW_AGENT_WRITES=false`，Agent 发起的每一次写都停在预检，于是一次 run 恒满足
`summary.tools.writes == summary.tools.dry_runs`。这条不变量有测试守着——它被打破的方式只有一种：
有人把开关打开了。人工执行是不是真的下发，只由 `TOOLS__DRY_RUN` 决定。**这两个开关是正交的，
不要把它们当成一件事**：前者管「模型能不能自己花钱」，后者管「这套系统现在是不是接了真账号」。

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

18 张表，全部使用**应用层生成的字符串主键**（`core/ids.py::new_id`，带类型前缀如 `camp_`/`run_`/`act_`）。这样 id 可以在 insert 之前生成、可以安全对外暴露、跨库迁移不会撞序列。

| 表 | 用途 | 关键约束 |
|---|---|---|
| `users` | 操作者账号 | `uq` email；role；锁定字段 |
| `refresh_sessions` | 可独立撤销的 refresh token | FK→users `ON DELETE CASCADE` |
| `campaigns` | 广告活动 | `(platform, external_id)` 唯一；`(status, platform)` 复合索引 |
| `creatives` | 创意（人工或 Agent 生成） | FK→campaigns CASCADE；`origin` 区分来源 |
| `daily_metrics` | 日粒度聚合，优化器输入 | `(campaign_id, creative_id, stat_date)` 唯一 **+ 部分唯一索引** `(campaign_id, stat_date) WHERE creative_id IS NULL`；`source` / `batch_id` 记录出处（可空、**不参与**唯一约束） |
| `ingest_batches` | 一次采集尝试的台账，含干跑 | 计数字段必须对得上账；`(source)`、`(created_at)` 索引 |
| `ingest_watermarks` | 一个源**已覆盖**的窗口跨度，用于证明某轮拉取是多余的 | PK = `source`；跨度**只放宽不收窄**；干跑永不推进（见 §5.3） |
| `scheduler_leases` | 定时拉取的单飞锁，兼上一次结果的台账 | PK = `name`（`ingest:<source>`）；认领靠一条带过期条件的 UPDATE；释放以 `holder` 为条件 |
| `optimization_runs` | 一次闭环执行 | status 索引；`(idempotency_key, requested_by)` **唯一索引** `uq_run_idempotency`——并发重放由插入仲裁，不是先查后插 |
| `run_events` | 追加式事件流 | `(run_id, seq)` 唯一，支撑 SSE 回放 |
| `optimization_actions` | Agent 提案，人审批门 | FK→runs `ON DELETE SET NULL`（run 删了动作还在，可审计） |
| `alerts` | 监控告警 | `dedup_key` 索引；`(status, detected_at)` 复合索引 |
| `budget_allocations` | 预算重分配提案 | 记录 `solver`（`cvxpy` / `greedy_lp`） |
| `ab_tests` | 创意实验 | MDE、所需样本量、traffic_split、result JSON |
| `audit_logs` | 不可变审计 | actor/before/after/IP/UA/request_id |
| `idempotency_records` | 为「请求指纹 + 响应体重放」预留，**当前无写入者** | PK = 客户端提供的 key；带 `expires_at`；唯一调用点是 `/admin/prune` 的删除 |
| `llm_spend` | 每次模型调用记账 | provider/model/token/cost/latency/outcome |
| `tool_invocations` | 每次工具调用一行，**被拒的也记** | run_id/agent/actor/tool/outcome/read_only/dry_run；失败不拖垮 run（见 §3.7） |

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

### 5.2 数据入口：出处、缺列与"对不上账就不返回"

优化器的每一个数字都来自 `daily_metrics`，所以"这个数字谁写的"必须可查。表上的两列回答这个问题：

- `source` — 哪个数据源断言的（`synthetic` / `platform` / 推送方自报的名字）
- `batch_id` — 哪一次采集带进来的，可以回查 `ingest_batches` 那一行的计数

两列都**可空且没有 server default**：在采集能力存在之前写入的行、以及演示种子写的所有行，确实没有已知出处，编一个出来就是伪造证据。它们也**不参与** `uq_daily_metric_slot`——一个槽位只有一份真相，谁是最后断言它的那个源属于出处信息，不属于身份。

**缺列不覆盖**是这一层最重要的一条规则，实现在 `MetricRepository.upsert_daily`：

| 传入 | 插入新行 | 更新已有行 |
|---|---|---|
| `None`（未传） | 落库为列默认值 0 | **保留原值** |
| `0` | 0 | 0 |

规则只有一句：**`None` 从不覆盖**。这让测量口径不同的数据源可以共用一张表——广告平台能断言 cost 而不会抹掉电商源写入的 revenue。如果这里把缺席当成 0，接一个真实平台源就会把所有 revenue 清零，优化器随后会针对一个凭空捏造的 ROAS=0 去调预算。

**报告必须对得上账。** `IngestReportOut.reconciles()` 断言 `received == created + updated + rejected_count + unresolved_count`，在返回之前执行。一条被静默丢掉的记录会让仪表盘以一种没人看得见的方式出错，所以这一层的契约是：要么落地，要么出现在 `rejected` / `unresolved` 里并带上索引和原因。

`ingest_batches` 连干跑也记一行，因为"演练过"和"数据源根本没来"必须是两件可区分的事——否则排查"昨天的数据到了吗"时，两种情况看起来一模一样。

### 5.3 定时拉取：窗口来自日历，不来自游标

`daily_metrics` 自己保持新鲜靠的是 `services/scheduling.py`。三条设计决定，都是为了让"错过一次"不变成"永久缺一段"。

**1. 窗口由日历推导，绝不由游标推导。** 由"我上次拉到哪"推出来的窗口会永久继承那条记录里的每一个错误，而且是静默的：相对于上一次，每一次运行都"正确"，所以缺口从不以错误的形式出现，只以数据缺失的形式出现。`ingest_watermarks` 因此只被读来**证明这一轮是多余的**，写下来是为了可观测性——它不是计算依据。`plan_window()` 是纯函数，整套错过策略都能脱离数据库与时钟来断言。

**2. 错过一次由下一次补上，且有明确上界。** 窗口回溯到"已覆盖最后一天的次日"，最多 `INGEST__MAX_CATCHUP_DAYS` 天。超过上界时**不静默收窄**：计划里给出 `gap_days`，`detail` 里直接写出该跑的补数命令（`adoptimizer ingest --start ... --end ...`）。悄悄缩小窗口会让一段历史永久缺失，而且没有任何一条日志提到过它。

`INGEST__LOOKBACK_DAYS` 是另一个方向的余量：广告平台会在事后几天里修订"昨天"，所以只拉今天的调度会以一种没有任何错误上报的方式慢慢变错。重复拉一天在构造上就是安全的——采集按 `(活动, 创意, 日期)` upsert，且缺列不覆盖（§5.2），所以重叠只花一次查询，不会改坏一个值。

**3. 重叠运行由数据库租约仲裁。** `concurrencyPolicy: Forbid` 只在一个 CronJob 自己产生的 Job 之间生效；两个副本各跑一份常驻循环、或循环与手工触发撞上，都需要一把所有人都看得见的锁。`LeaseRepository.acquire` 用**一条带过期条件的 UPDATE** 认领，`rowcount` 就是判决——先查后插的竞态窗口和拉取本身一样宽，而输掉那种竞态的进程不会失败，它会静默地跑第二份。TTL 到期即可被接管，所以持锁的 pod 崩了不会把调度卡死；`record_outcome` 以 `holder` 为条件，于是一个超过 TTL 的慢拉取无法释放接手者的锁。

一趟 tick 分三个事务，各自提交：认领租约（别人在拉取开始前就看得见）→ 拉取 + 写入 + 推水位（**一个窗口绝不会由已回滚的行标记成已覆盖**）→ 记录结果并放锁（这一步在拉取失败时也必须执行）。tick **从不抛异常**：一个源失败会被报成 `failed` 的一趟，因为把异常往外传会让一个没凭据的平台挡住排在它后面的所有源。

节奏归谁：生产上推荐交给 Kubernetes——`deploy/k8s/ingest-cronjob.yaml` 每 6 小时调一次 `adoptimizer scheduler --once`，ConfigMap 里 `INGEST__SCHEDULER_ENABLED=false`。`--once` 刻意**不受**这个开关约束：被进程外的调度器调起正是它的用途，否则就没法在关掉进程内循环的同时保留拉取。没有 Kubernetes 的部署可以反过来——把开关打开，用进程内常驻循环。两条路径写的是同一套水位与租约，所以混用也不会重复灌。

### 5.4 迁移策略

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
              ├─ 流式增量（LLM__STREAM=true 时改走 SSE，片段交给 on_delta）
              ├─ 响应缓存（cache_enabled + TTL，键含 prompt 与参数）
              ├─ 结构化输出校验（llm/structured.py，失败可回退规则生成）
              └─ SpendLedger：每次调用记 token/成本/延迟/结果
                        │
                        ├─ 单价：LLM__PRICING 覆盖 llm/base.py 的 DEFAULT_PRICING
                        └─ 月度预算护栏（LLM__MONTHLY_BUDGET_USD）
                             超预算 → fail_open_to_mock ? 降级 mock : 直接失败
```

`mock` provider 是**确定性**的：同样的输入永远得到同样的输出，不联网、不花钱。这是整个测试套件能跑得快且稳定的原因。

`LLM__STREAM=true` 时网关改走 SSE，把每个文本片段交给调用方传入的 `on_delta`。片段是展示用的糖，**`CompletionResult.text` 才是权威结果**，因此三条语义必须记住：缓存命中把整段文本作为**一个**片段补发；重试只在**第一次**尝试转发片段（半截流已经渲染过，再灌一次就是重复内容）；降级到 mock 的结果**不发**任何片段。打开它最常见的理由不是首字延迟，而是有些厂商的思考模型（Qwen3 thinking）只接受流式调用；副作用是 `LLM__TIMEOUT_SECONDS` 从“整次生成的上限”变成“单个分片的读超时”。当前只有 `agents/creative.py` 与 `agents/audience.py` 两个调用点具备传 handler 的位置，事件总线与前端还没有消费增量——见 `llm/base.py` 里 `DeltaHandler` 的说明。

成本单价（USD / 百万 token）来自 `llm/base.py` 的 `DEFAULT_PRICING`，可被 `LLM__PRICING` 按模型覆盖，格式是一个 JSON 对象、每个值是 `[prompt, completion]`，例如 `{"qwen-plus": [0.113, 0.282]}`。内置表只是兜底，**生产上应当把它当成不可信的默认值**：厂商会调价，区域计费口径也不同——`dashscope.aliyuncs.com` 属中国区、按 CNY 计价，约为国际 USD 标价的三分之一，照标价记账会让花费虚高到 3 倍以上。而这个数字正是 `LLM__MONTHLY_BUDGET_USD` 的扣减依据与 `GET /api/v1/analytics/llm-spend` 的全部来源，所以单价错了不是"报表不准"，而是预算护栏会在错误的时间点触发。没被列出价格的模型走 `default` 行，`build_gateway()` 会在启动时打一条 `llm_model_has_no_pricing_entry` 警告——这是唯一能发现该问题的时机，因为运行期一切照常。显式写 `0` 是有效的（免费额度、赠送 token），不会被当成缺项回退到 `default`。

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

**20 个 Prometheus 指标**，覆盖六个层面：

| 层面 | 指标 |
|---|---|
| HTTP | `http_requests_total`, `http_request_duration_seconds` |
| Agent | `agent_runs_total`, `agent_run_duration_seconds`, `agent_step_total`, `agent_step_duration_seconds`, `active_runs`, `queue_depth` |
| LLM | `llm_calls_total`, `llm_latency_seconds`, `llm_tokens_total`, `llm_spend_usd_total` |
| 业务/基础设施 | `alerts_total`, `actions_total`, `cache_ops_total`, `db_query_duration_seconds` |
| 数据入口 | `ingest_records_total{source,outcome}`, `ingest_batches_total{source,mode}` |
| 数据调度 | `ingest_ticks_total{source,outcome}`, `ingest_lag_days{source}` |

`ingest_records_total` 的 `outcome` 是固定八个值（`created`/`updated`/`rejected`/`unresolved` 各带一个 `dry_` 前缀），干跑不会被算成真实写入。`source` 由 `schemas/ingest.py::SOURCE_PATTERN` 约束成小写短名（与 `core/config.py::SOURCE_NAME_PATTERN` 是同一个常量），因此它作为标签不会把序列基数撑开。

`ingest_ticks_total` 的 `outcome` 四个值里，`ran` / `skipped` / `lost_lease` **都是健康的**（拉了、已覆盖、别人正在拉），只有 `failed` 该叫人。数据陈旧因此不靠这个计数器告警，而靠 `ingest_lag_days`：窗口被上界卡住时 tick 会一直成功，数据却越落越远。**完全没有序列**表示这个源从未被拉过，用 `absent()` 报。

日志用 structlog，JSON 输出，`X-Request-ID` 与 `run_id` 通过 contextvar 绑定到每条记录，可以按请求或按 run 完整串联。

### 7.3 探针

| 端点 | 语义 | 失败行为 |
|---|---|---|
| `/healthz` | 存活。进程能响应即 200 | 永不因依赖故障返回非 200（否则会被编排器无意义地重启） |
| `/readyz` | 就绪。聚合 database/cache/llm/orchestrator/platforms/ingest/tools | 数据库不可达 → 503，从负载均衡摘除 |
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
  ├─ hooks/use*.ts       10 个文件 / 44 个导出 hook，每个资源一组（useCampaigns/useRuns/useActions/useAlerts/useAdmin/...）
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

**批准之后紧接着的执行**，才是真正碰平台的那一步，也是工具层唯一会被人工流量走到的地方：

```
POST /api/v1/actions/{id}/execute
  → ActionService.execute_action
      状态机校验：已 executed/rejected ⇒ 409；require_approval 下必须 approved
      ToolRequest(agent=None, actor=操作者, idempotency_key="action:"+action_id)
        → ToolExecutor  五道关：存在 / 白名单 / 预算（人工流量豁免）/ 幂等 / 参数
            → 干跑互锁：agent=None 绕过「Agent 不得写」，仍受 TOOLS__DRY_RUN 约束
            → PlatformRegistry → AdsPlatformClient.update_budget / pause_campaign / ...
        ← ToolResult(outcome, dry_run, data)
      → 写 tool_invocations + audit_logs（after 里带 dry_run 与 external_reference）
      → 成功置 executed（记下 external_reference），异常置 failed（记下 error_message）
```

注意 `agent=None` 这一个字段的差别：同一份代码、同一套护栏，只因为发起者是人，写操作才被放行。
如果 `TOOLS__ENABLED=false`，执行会退回到直接调 adapter 的老路径——这条兜底路径也有测试守着，
保证关掉工具层不会让审批功能整个失效。