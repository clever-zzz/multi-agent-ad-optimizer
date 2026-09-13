# 08 限制与路线图

这一页的目的是让你**准确地**知道这个系统能做什么、不能做什么。一个把限制藏起来的"生产级"项目，比一个诚实的半成品危险得多——因为它能改广告预算。

严重度：🔴 会阻碍真实投产 / 🟡 可用但有明确代价 / 🟢 可接受的取舍

---

## 1. 广告平台集成

### 🔴 1.1 真实平台适配器未经联调

`infra/ads/google.py`、`meta.py`、`tiktok.py` 有完整的请求构造、认证头、分页与错误映射骨架，但**没有在真实账号上跑通过一次**。`DATA_MODE=mock`（默认）走的是 `mock.py`。

**影响**：直接把 `DATA_MODE` 切到真实平台几乎肯定会失败，失败方式包括认证方式不对、字段名不匹配、分页游标语义不同、速率限制处理缺失、沙箱与生产端点差异。

**投产前必须做**（按平台逐个来，不要一次性全上）：

1. 申请开发者账号与沙箱/测试广告账户
2. 只接**读**路径：拉活动、拉日报表。对比平台 UI 的数字，误差要在可解释范围内
3. 接**写**路径，但先只支持"暂停"这类可逆操作，并且：
   - 先在测试账户上跑
   - 保留 `SECURITY__REQUIRE_ACTION_APPROVAL=true`
   - 记录 `external_reference`，确认平台侧确实变了
4. 加上平台侧的速率限制与配额处理（当前的限流是**对我们自己 API** 的，不是对平台 API 的）
5. 处理部分失败：批量动作里第 3 条失败时，前 2 条已经生效了，需要能报告并回滚

**代码上要补的**：真实的 OAuth 刷新流程与凭据加密存储（当前凭据从环境变量读明文）、平台 webhook/回调、请求幂等（平台侧的，不是我们的 `Idempotency-Key`）。

**联调时必须一起验的两条写路径**：

- 创意状态是对称的两条：`PAUSE_CREATIVE` → `pause_creative`、`RESUME_CREATIVE` → `resume_creative`。四个适配器（mock / google / meta / tiktok）都实现了，分派关系有测试锁定——`RESUME_CREATIVE` 曾被错接成 `pause_creative`，在 mock 环境下完全看不出来。切真实平台时两条都要各跑一次，确认平台侧状态确实变了。
- 预算改动推给平台的值与写进本地台账的值是**同一个** `round(v, 2)` 之后的数字，避免出现"库里 500.0、平台上 499.99"的对不上账。验证时直接比对 `before_value` / `after_value` 与平台 UI。

### 🟡 1.2 执行动作没有平台侧的回滚

`optimization_actions.before_value` 记录了变更前的值，因此**人工**可以照着改回去。但没有 `POST /actions/{id}/rollback` 这样的自动回滚。

真实场景里"改回去"往往不是把数字设回原值那么简单（例如暂停后再恢复，广告平台的学习期会重置）。这属于业务决策，不适合自动化。

---

## 2. 编排与并发

### 🟡 2.1 单副本单 worker

见 [ADR-0002](../adr/0002-in-process-run-dispatch.md)。

| 约束 | 后果 | 缓解 |
|---|---|---|
| `UVICORN_WORKERS` 必须为 1 | 单副本吞吐受限于一个事件循环 | 加副本 |
| run 在执行它的进程里 | 副本崩溃 = 该 run 丢失 | 启动时 `reap_stale_runs` 标记为 failed，需人工重跑 |
| SSE 实时尾流是进程内的 | 请求落到别的副本时，实时性退化为约 60s 批量补齐 | Ingress cookie 亲和（清单已配） |
| 令牌桶在进程内 | 全局限流上限 = `limit × 副本数` | 换成 Redis 后端 |
| 无跨进程并发上限 | 无法限制"全系统同时最多 N 个 run" | 只有按主体的 `OPTIMIZE_RUNS_PER_HOUR` |

**已经做对的**：SSE 以持久化的 `run_events` 为权威源，所以重启后重连能补齐完整时间线；协作式取消让跨副本的 cancel 也能生效；`mark_finished` 拒绝覆盖已终态的 run。

### 🟢 2.2 没有常驻 worker

`pyproject.toml` 有 `worker = ["arq>=0.26"]` extra，compose 有 `worker` profile，但那个 service 是**一次性 Job**（跑一轮优化后退出），不是消费队列的常驻进程。

`OptimizationService.execute_run` 的签名已经设计成"可在任意进程调用"（自开 session、自建 context、不依赖请求态），所以迁移到 arq 的改动面是可控的：`_dispatch` 改成入队 + 新增 `worker/` 模块 + `EventBus` 换 Redis Pub/Sub。

### 🟢 2.3 LangGraph checkpoint 只在内存

`MemorySaver` 意味着进程重启后无法从断点续跑。对当前的短闭环（几十秒）影响很小；如果将来单次要跑几十分钟，需要换成持久化 checkpointer。

---

## 3. 数据与分析

### 🔴 3.1 ClickHouse 路径未经真实数据量验证

`infra/warehouse.py` 与 `init-scripts/clickhouse/` 提供了 schema 与查询实现，`DATA_MODE=warehouse` 可以切换。查询构造、绑定参数、以及连不上库时的降级路径都有测试覆盖（用一个假驱动，见 `tests/integration/test_warehouse.py`），但**没有任何真实数据量下的压测数据**。

未验证的点：高基数维度下的查询延迟、`audience_observations` 的真实数据来源与口径、物化视图是否需要、以及从广告平台到 ClickHouse 的**采集管道仍然不存在**（当前假设数据已经在库里）。

到**运营库**（`daily_metrics`）的采集入口已经落地，见 §3.5，但它写的是 PostgreSQL / SQLite 这一侧，**不写 ClickHouse**——两条路径的数据量级差着几个数量级，运营库入口跑得通不代表仓库路径跑得通。

**投产前**：采集方案已经有了可挂载的框架（`MetricSource` 协议 + `POST /ingest/metrics`），剩下的是把真实平台接上去，再做数据量评估。

### 🟡 3.2 `run_events` 没有保留期策略

这张表增长最快，而 `POST /admin/prune` 只清理 `audit_logs` 与 `idempotency_records`。

同样没有自动回收的：`llm_spend`、过期的 `refresh_sessions`。

临时处置见 [05 运维手册 §5](05-operations-runbook.md#5-容量与数据增长)。路线图里 P1。

### 🟢 3.3 金额用 Float

`daily_metrics.cost`、`campaigns.daily_budget` 等是 `Float`，为了与广告平台 API 的数值口径一致，并在领域边界统一 round。

对于"展示与决策"够用；如果要做**对账/财务**，必须换成 `Numeric`。这是有意取舍，不是疏忽。

### 🟢 3.4 无多租户

数据模型里没有 `tenant_id`，RBAC 也没有资源级授权（"某用户只能管某几个活动"）。单团队使用没问题；要做 SaaS 需要重新设计。

### 🟡 3.5 数据入口只有框架，没有真实数据

`services/ingest.py` + `infra/ingest/` + `POST /api/v1/ingest/metrics` 构成一个完整的采集入口：两层校验、身份归属解析、缺列不覆盖、逐条报告下落、批次台账、出处列（`daily_metrics.source` / `batch_id`）、审计与 Prometheus 指标，全都在，也都有测试。

**调度也在**：`services/scheduling.py` + `GET /api/v1/ingest/schedule` + `adoptimizer scheduler` + `deploy/k8s/ingest-cronjob.yaml`。窗口由日历推导而不是由游标推导，错过一次由下一次补上（有 `INGEST__MAX_CATCHUP_DAYS` 上界，超过就报 `gap_days` 并给出补数命令而不是静默收窄），重叠运行由数据库租约仲裁，水位只放宽不收窄，干跑永不推进水位。设计理由见 [02 §5.3](02-architecture.md#53-定时拉取窗口来自日历不来自游标)。

**缺的是数据本身，不是管道。**

**但注册在案的数据源只有两个，而且没有一个接的是真实平台数据：**

| 源 | 是什么 | 何时 `configured` |
|---|---|---|
| `synthetic` | 种子化 RNG 生成的数字。**不是广告数据**，每行都盖 `source="synthetic"`，出处列本身就是标签 | 永远（不依赖任何凭据） |
| `platform` | 把已有适配器 `fetch_report` 的归一化行转成 `SourceRecord` | `DATA_MODE=warehouse` 且至少一个适配器凭据齐全 |

`platform` 源是**真代码而不是桩**：三个适配器都已经把响应归一化成 `{date, impressions, clicks, conversions, cost}` 同一个形状，转换逻辑有测试覆盖（含 TikTok 返回 `"2026-09-01 00:00:00"` 这种时间戳形态）。它没被验证的部分是**真实账户上的端到端联调**，也就是 P0-2 那件事：没有真账号就跑不到那条路径。而 mock 适配器按构造返回空行，所以 `platform` 源在 mock 模式下**报错而不是返回 0**——"拉取失败"与"没有数据"必须是两件可区分的事，否则一条坏掉的管道看起来是健康的。

仍然缺的：

- **没有重试与退避。** 一次拉取里某个活动失败会抛出整批（这是刻意的：不制造部分成功的假象）。调度层的"重试"就是下一趟 tick，间隔固定为 `INGEST__INTERVAL_MINUTES`；没有指数退避，也不会针对 429 单独收窄节奏——对一个已经在限流你的平台更用力地重试，是把限流变成停服
- **调度本身没有被真实平台验证过。** 窗口算术、水位、租约、错过策略都有测试；但"一个真实平台在一次拉取里要多久、会不会超时、能不能扛住 6 小时一次的节奏"要等 P0-2 的真账号才知道。`INGEST__LEASE_TTL_SECONDS`（1800）与 CronJob 的 `activeDeadlineSeconds`（1800）都是按估计给的，联调后应当按实测收紧
- **推送侧没有幂等键。** 同一批推两次，第二次全部记为 `updated`；没有 `Idempotency-Key`，也没有基于内容的去重（同 §6.6）
- **`revenue` 只能靠推送。** 平台源永远不声称 revenue（它真的不知道），所以 ROAS 相关的决策依赖你自己把收入数据推进来。这是设计，不是缺陷——但意味着**只接平台源的系统算不出真实 ROAS**
- **ClickHouse 那一侧没有入口。** 见 §3.1

---

## 4. LLM

### 🟡 4.1 默认 mock，真实模型质量未评测

`LLM__PROVIDER=mock` 是确定性的模板生成器。切到真实模型后：

- 创意文案质量取决于 prompt 与模型，**当前没有评测集**，无法回答"换了模型是变好还是变坏"
- 受众洞察是自然语言摘要，没有事实性校验
- prompt injection 的风险面变大（活动名、创意文案都会进 prompt）

**缓解现状**：涉及金钱的数学全部在 `domain/` 里用确定性代码算，LLM 不能凭空决定预算数字；结构化输出经 Pydantic 校验，不合法则回退规则生成器；所有动作都要人工审批。

**投产前建议**：建一个 20–50 条的创意评测集（人工打分），对 prompt 与模型改动做回归；在把用户可控文本拼进 prompt 的位置加分隔与长度限制。

### 🟢 4.2 预算护栏是月度累计，不是实时

`llm_spend` 表按调用记账，`LLM__MONTHLY_BUDGET_USD` 超限后按 `fail_open_to_mock` 决定降级还是失败。多副本下累计是准的（都写同一个库），但判定不是原子的，极端并发下可能略微超出。

**单价同样是近似值**：记账用的是 `llm/base.py` 的 `DEFAULT_PRICING`（USD 国际标价）而不是厂商回传的账单金额，因此阶梯计价、缓存命中折扣、区域计价（中国区按 CNY）都不会自动反映出来。用 `LLM__PRICING` 把实际单价写死是唯一可靠的办法，并定期与厂商账单对一次。没写就按 `default` 行估算，启动日志会给一条 `llm_model_has_no_pricing_entry` 警告。

### 🟢 4.3 只支持 OpenAI 兼容协议

`openai` / `azure_openai` / `openai_compatible` 三种 provider。要接 Anthropic、Bedrock、本地 vLLM 之外的其他供应商需要新增 provider 实现（`llm/base.py` 的协议已经抽象好了，加一个实现类即可）。

---

## 5. 前端

### 🟢 5.1 图表是自研的

见 [ADR-0001](../adr/0001-custom-svg-charts.md)。四种图表够当前使用，但：

- 无障碍只做到 `role="img"` + `<title>`，键盘用户拿不到 tooltip
- 没有框选缩放、联动筛选这类交互
- 新增图表类型的成本比引库高

### 🟢 5.2 无国际化

界面文案硬编码中文/英文混排。要多语言需要引入 i18n 框架并把文案抽出来。

### 🟡 5.3 前端测试覆盖偏薄

6 个测试文件，集中在纯函数（格式化、图表几何、口令策略）、HTTP 客户端与认证状态机。**没有**整页渲染测试、路由测试、E2E 测试。

`SettingsPage` 的账号管理弹窗、`RunDetailPage` 的 SSE 时间线这类交互，目前只靠 [07 §6 的手工清单](07-testing-and-ci.md#6-手工端到端验收清单)覆盖。

### 🟢 5.4 lockfile 已提交，构建可复现（已解决）

`frontend/package-lock.json`（lockfileVersion 3，429 个包，全部解析自 registry.npmjs.org）已在仓库里。CI 的 `setup-node` 打开了 `cache: npm` + `cache-dependency-path`，安装步骤只有 `npm ci`；`Dockerfile.frontend` 同样优先 `npm ci`。

`npm install` 回退分支**已删除**。lockfile 与 `package.json` 漂移时构建直接失败，而不是悄悄解析出另一棵依赖树——这正是回退分支的危险之处：它会让"本地能跑、CI 也能跑、但两边跑的不是同一套依赖"变成常态。

**维护约定**：改依赖就把 lockfile 一起提交。

仍然没做的：lockfile 只保证**可复现**，不保证**没有已知 CVE**。`npm audit` / `pip-audit` 一类的依赖漏洞扫描还没进 CI，见下面路线图 P0 第 4 项与 [07 §5.2](07-testing-and-ci.md#52-还没接上但应该接的)。

---

## 6. 安全

### 🟡 6.1 无 MFA / SSO

只有邮箱 + 口令。企业部署的正确做法是在反向代理或身份提供方那一层接 OIDC/SAML，本系统只保留授权与审计。当前代码没有对接点，需要新增一个"外部身份 → 本地用户"的映射与 token 交换流程。

### 🟡 6.2 JWT 密钥轮换会导致全员掉线

只支持单密钥，没有 `kid` + 多密钥并存验签。轮换 = 所有 access token 立即失效。

### 🟢 6.3 审计日志非防篡改

`audit_logs` 表可被拥有数据库写权限的人修改。要做到真正不可篡改需要哈希链（每行包含前一行的哈希）或写到 WORM 存储/独立审计服务。当前定位是"操作留痕与追责线索"，不是"法务级证据"。

### 🟢 6.4 限流不防分布式暴力破解

令牌桶按认证主体计，匿名请求按 IP 计。有代理池的攻击者可以绕过 IP 维度。账号锁定（5 次 / 15 分钟）是主要防线，配合 ingress 层的 WAF/IP 白名单更稳。

### 🟡 6.5 「全设备登出」后端已就绪，UI 没有入口

`POST /api/v1/auth/logout-everywhere` 会吊销调用者名下所有 refresh session，接口与权限都已实现并有测试覆盖，但前端目前只在顶栏提供单设备登出。账号可能已泄露时，操作者没有一键踢掉所有会话的按钮。

补法很小：在「设置 → 账号」里加一个确认对话框调用该端点，成功后清空本地 token。列在这里是因为在补上之前，这个能力只能通过 API 直接调用。

### 🟡 6.6 `Idempotency-Key` 只覆盖 `POST /runs`，且没有请求指纹

真正生效的守卫是 `optimization_runs` 上的唯一索引 `uq_run_idempotency (idempotency_key, requested_by)`。`start_run` 是先查后插，两个并发调用可能都查不到，所以由索引在**插入时**仲裁：输的一方捕获 `IntegrityError`、回滚、再查一次，拿回赢家的 run，不会派发第二次执行。带 key 但没有 actor 的调用被 422 拒绝——`(key, NULL)` 在 SQL 里互不相同，约束会静默失效，而这种「看起来有保护其实没有」正是最坏的情况。

配套修掉的：`background: false` 的重放过去会 404。`wait_for` 只认自己派发过的任务，而重放的调用方（并发对手、另一个副本）手里没有任务，于是抛 `NotFoundError`，为一个明明存在的 run 返回 404。现在拿不到本地任务就轮询数据库到终态。

仍然缺的：

- **只有 `POST /runs` 读这个头。** 审批 / 执行 / 批量、创意、活动、告警等写端点都不读，它们靠状态机防重复（重复 approve / execute 返回 409）——够用，但语义与 `Idempotency-Key` 不一致，客户端无从预期
- **没有请求指纹校验。** 同一个 key 配不同 body 返回首次的 run，而不是 409。调用方跨不同载荷复用 key 时会静默拿到错误的结果
- `idempotency_records` 表结构完整（PK = key、`request_fingerprint`、`response_body`、`expires_at`），`IdempotencyRepository` 也有 `get` / `record` / `prune_expired`，但**没有任何写入者**——唯一的调用点是 `/admin/prune` 里的删除。这与 `upsert_daily`、`_accumulate_usage` 是同一类失效：设施建好了，没人接线

补齐路径：一个幂等中间件，按 `(key, actor)` 命中 `idempotency_records`，指纹不符返回 409，命中则重放 `response_body` 与状态码，然后挂到所有产生副作用的 `POST` 上。接完之后 `/admin/prune` 那条删除路径才终于有东西可删。

完整威胁模型见 [06 安全](06-security.md)。

### 🟢 6.7 独立的采集身份（已解决）

`Role.INGESTOR` 已落地，只持有 `metrics:read` + `metrics:write` 两条权限，`metrics:write` 同时从 **optimizer** 上收回。现在能写指标的只有 admin 与 ingestor。

之前的状态是：`POST /api/v1/ingest/metrics` 用 `metrics:write` 授权，而这个权限挂在 admin 与 optimizer 两个角色上。给 optimizer 的理由当时是成立的——一个 optimizer token 本来就带 `campaign:write` 与 `action:execute`，所以这不是新的提权，而它换来的是采集管道能跑在一个非 admin 身份下。但那句话说的是**角色**，问题出在**凭据**上：被复制进 pipeline runner、被塞进 Git 的 secret store、被打印进十几份日志的，是那份 token。凭据泄露时，攻击者拿到的是一个能改活动、能审批并执行动作的身份，而"执行动作"是真的会去动广告平台的。拆出来之后，泄露的采集凭据只能伪造数字，没法把自己伪造的数字批成真实预算变更。

路由断言的是**权限**而不是角色，映射集中在 `core/security.py::_ROLE_PERMISSIONS` 一处，所以这次改动没有碰任何端点代码。

**升级注意**：这是行为变更。原本用 optimizer 账号推送指标的管道会开始收到 `403`，需要建一个 ingestor 账号换凭据。走 `adoptimizer scheduler` / `adoptimizer ingest` 进程内路径的采集不受影响——那条路不经过 HTTP 鉴权。

追溯能力本来就在，现在能追到正确的身份上了：`daily_metrics.source` / `batch_id` 回答"这个数字是谁写进来的"，`ingest_batches` 回答"这批灌了什么、拒了什么、是不是演练"，`audit_logs` 里的 `metrics.ingested` 带 `actor_role`，回答"谁在什么时候按的按钮"。

仍然没做的：ingestor 是**全局**身份，不能限定"只能灌某几个活动"或"只能灌某个 source"。要做资源级隔离得等 P2 第 19 项（多租户 + 资源级授权）。

---

## 7. 工程

### 🟡 7.1 迁移与 `create_all` 双路径

SQLite 开发库首次启动会自动 `create_all()` 并写入 `alembic_version` 戳，之后 `adoptimizer migrate` 是 no-op。这解决了"先起服务再跑迁移会撞表"的问题，但代价是**存在两条建表路径**。

如果 ORM 模型与迁移脚本漂移，SQLite 开发环境不会报错（它走 `create_all`），而 PostgreSQL 生产环境会。

**防线（已落地）**：CI 的 `migrations` job 用 PostgreSQL service container 跑 `alembic upgrade head → alembic check → alembic downgrade base → alembic upgrade head`，autogenerate 一旦检测到漂移就让 CI 变红；`backend` job 另外用一次性 SQLite 验证同一条链路可升级、可回滚。本地等价命令是 `.\scripts\check.ps1`（`migrations` 门禁）或 `make verify-migrations`，两者都指向临时库，不会碰你配置里的数据库。

### 🟢 7.2 遗留代码仍在仓库里

`python/`、`java/`、`golang/` 是原始 demo 实现（Streamlit / Spring Boot / goroutine），`docs/interview/`、`docs/tutorial/`、`docs/code-walkthrough/` 是配套的教学与面试材料。

它们**不参与生产部署**，也不被 CI 覆盖。保留是因为有教学价值。如果你要把这个仓库当作严肃的生产项目对外，建议：

- 把它们移到 `legacy/` 目录下，或
- 拆成独立仓库，或
- 至少在根 README 里保持现在的明确标注（已做）

### 🟢 7.3 没有 CHANGELOG 与语义化版本流程

`version = "1.0.0"` 写死在 `pyproject.toml` 与 `package.json` 里，没有自动化的版本管理与变更记录。

### 🟢 7.4 无 APM / 分布式追踪

`OBSERVABILITY__TRACING_ENABLED` 与 `otlp_endpoint` 配置项已预留，但**没有实现** OTLP 导出。当前只有结构化日志（带 `request_id` / `run_id`）与 Prometheus 指标。

单服务架构下够用；如果拆出 worker 服务，跨进程追踪会变成必需品。

---

## 8. 路线图

按"不做就不能投产"到"锦上添花"排序。

### P0 — 投产阻塞项

| # | 事项 | 交付标准 |
|---|---|---|
| 1 | ~~提交 `frontend/package-lock.json`，CI 切到 `npm ci` + npm 缓存~~ ✅ **已完成** | lockfile 已入库，`setup-node` 开了 npm 缓存，CI 与 `Dockerfile.frontend` 都只走 `npm ci`，`npm install` 回退分支已删除 |
| 2 | 至少一个广告平台的**读**路径真实联调 | 从真实账户拉到的活动与日报表数字，与平台 UI 对得上。拉取路径已存在（`adoptimizer ingest --source platform`，见 §3.5），缺的是真账号与对账 |
| 3 | ~~CI 增加 `alembic upgrade head && alembic check`~~ ✅ **已完成** | `.github/workflows/ci.yml` 的 `migrations` job（PostgreSQL service container）已落地，`backend` job 另有 SQLite 可逆性验证 |
| 4 | 依赖漏洞扫描（`pip-audit` + `npm audit`）进 CI | 高危 CVE 阻塞合并 |
| 5 | 生产环境冒烟脚本化 | [07 §6](07-testing-and-ci.md#6-手工端到端验收清单) 里可自动化的部分变成一个脚本 |

### P1 — 上线后一个月内

| # | 事项 | 交付标准 |
|---|---|---|
| 6 | `run_events` / `llm_spend` / 过期 `refresh_sessions` 的保留期策略，并入 `/admin/prune` | 表大小可控，有配置项，有测试 |
| 7 | 至少一个广告平台的**写**路径联调（先只做可逆操作） | 在测试账户上执行暂停/恢复成功，`external_reference` 有值，失败可报告 |
| 8 | 限流与会话校验换 Redis 后端 | 多副本下限流全局一致 |
| 9 | 密钥轮换支持（JWT `kid` + 多密钥并存） | 轮换不导致全员掉线 |
| 10 | Playwright E2E：登录 → 触发 run → 看时间线 → 审批 → 执行 | 进 CI |
| 11 | 创意质量评测集与回归流程 | 换 prompt/模型前后有可比的分数 |
| 12 | Prometheus 告警规则文件 + Grafana dashboard JSON 入库 | `deploy/observability/` |
| 13 | ~~指标采集的定时调度（CronJob 清单 + 常驻循环 + 明确的错过策略）~~ ✅ **已完成** | `deploy/k8s/ingest-cronjob.yaml`（`--once`，`Forbid`，`startingDeadlineSeconds`）+ `adoptimizer scheduler` 常驻循环 + `INGEST__*` 七个配置项。窗口由日历推导，错过一次由下一次补上（上界外报 `gap_days` 并给出补数命令），重叠由数据库租约仲裁，水位只放宽；`ingest_ticks_total` / `ingest_lag_days` 与 `GET /ingest/schedule` 负责看得见 |
| 14 | ~~独立的 `ingestor` 角色，采集任务用它而不是 optimizer~~ ✅ **已完成** | 见 §6.7。`Role.INGESTOR` 只持有 `metrics:read` + `metrics:write`，`metrics:write` 已从 optimizer 收回；采集凭据被偷时炸不到活动与动作。**注意这是行为变更**，用 optimizer 账号推送的管道需要换凭据 |

### P2 — 规模化时再做

| # | 事项 | 触发条件 |
|---|---|---|
| 15 | 迁移到 arq/Celery 队列 + 常驻 worker | 需要 run 必达 SLA，或单副本吞吐成瓶颈 |
| 16 | EventBus 换 Redis Pub/Sub | 多副本且 cookie 亲和不可用 |
| 17 | ClickHouse 采集管道 + 真实数据量压测 | 决定启用 `DATA_MODE=warehouse` |
| 18 | SSO/OIDC 集成 | 企业身份体系要求 |
| 19 | 多租户 + 资源级授权 | 要服务多个团队/客户 |
| 20 | 审计日志哈希链或外发到 WORM 存储 | 合规要求法务级证据 |
| 21 | OTLP 分布式追踪 | 服务拆分后 |
| 22 | 金额改 `Numeric` + 财务对账口径 | 需要与财务系统对账 |

### 明确不做

| 事项 | 原因 |
|---|---|
| 自动执行未经审批的动作 | 这是设计原则，不是待办。`SECURITY__REQUIRE_ACTION_APPROVAL` 的默认值应该是 `true`，并且不应该有"方便起见"的例外 |
| 用 LLM 直接决定预算数字 | 金钱相关的数学必须是可解释、可复现的确定性代码。模型可以解释决定，不能凭空决定 |
| 把 SQLite 支持成生产选项 | 生产硬化校验会拒绝它。SQLite 只是开发便利 |
| 自研图表库 | 见 [ADR-0001](../adr/0001-custom-svg-charts.md) 的复审触发条件；没触发之前不重做 |

---

## 9. 一句话总结

**这个系统现在可以做的事**：在 mock 数据或你自己灌进来的数据上，稳定地跑通"采集 → 监控 → 分析 → 生成创意 → 调竞价 → 分预算 → 产出可审批提案 → 人工审批 → 执行 → 全程审计"的闭环，并且有企业级的认证、授权、审计、限流、探针、指标、容器化与 CI。数据入口（`POST /ingest/metrics` + `adoptimizer ingest`）是真接口而不是摆设：每一行要么落地、要么带着原因出现在报告里，落地的那部分带出处（`source` / `batch_id`）。

**它现在还不能做的事**：直接接管你在 Google/Meta/TikTok 上的真实预算。那需要先完成 P0-2 与 P1-7 的平台联调，并且在你自己的沙箱账户上验证过。同样地，**采集入口还没有真实数据源**——`platform` 源的转换逻辑写好了也有测试，但没有真账号就跑不到那条路径，日常能用的只有明确标注为合成数据的 `synthetic`（见 §3.5）。