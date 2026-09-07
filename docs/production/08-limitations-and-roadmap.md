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

未验证的点：高基数维度下的查询延迟、`audience_observations` 的真实数据来源与口径、物化视图是否需要、以及从广告平台到 ClickHouse 的**采集管道根本还不存在**（当前假设数据已经在库里）。

**投产前**：先明确采集方案（平台 API 轮询 / 平台导出 / 第三方 ETL），再做数据量评估。

### 🟡 3.2 `run_events` 没有保留期策略

这张表增长最快，而 `POST /admin/prune` 只清理 `audit_logs` 与 `idempotency_records`。

同样没有自动回收的：`llm_spend`、过期的 `refresh_sessions`。

临时处置见 [05 运维手册 §5](05-operations-runbook.md#5-容量与数据增长)。路线图里 P1。

### 🟢 3.3 金额用 Float

`daily_metrics.cost`、`campaigns.daily_budget` 等是 `Float`，为了与广告平台 API 的数值口径一致，并在领域边界统一 round。

对于"展示与决策"够用；如果要做**对账/财务**，必须换成 `Numeric`。这是有意取舍，不是疏忽。

### 🟢 3.4 无多租户

数据模型里没有 `tenant_id`，RBAC 也没有资源级授权（"某用户只能管某几个活动"）。单团队使用没问题；要做 SaaS 需要重新设计。

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

完整威胁模型见 [06 安全](06-security.md)。

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
| 2 | 至少一个广告平台的**读**路径真实联调 | 从真实账户拉到的活动与日报表数字，与平台 UI 对得上 |
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

### P2 — 规模化时再做

| # | 事项 | 触发条件 |
|---|---|---|
| 13 | 迁移到 arq/Celery 队列 + 常驻 worker | 需要 run 必达 SLA，或单副本吞吐成瓶颈 |
| 14 | EventBus 换 Redis Pub/Sub | 多副本且 cookie 亲和不可用 |
| 15 | ClickHouse 采集管道 + 真实数据量压测 | 决定启用 `DATA_MODE=warehouse` |
| 16 | SSO/OIDC 集成 | 企业身份体系要求 |
| 17 | 多租户 + 资源级授权 | 要服务多个团队/客户 |
| 18 | 审计日志哈希链或外发到 WORM 存储 | 合规要求法务级证据 |
| 19 | OTLP 分布式追踪 | 服务拆分后 |
| 20 | 金额改 `Numeric` + 财务对账口径 | 需要与财务系统对账 |

### 明确不做

| 事项 | 原因 |
|---|---|
| 自动执行未经审批的动作 | 这是设计原则，不是待办。`SECURITY__REQUIRE_ACTION_APPROVAL` 的默认值应该是 `true`，并且不应该有"方便起见"的例外 |
| 用 LLM 直接决定预算数字 | 金钱相关的数学必须是可解释、可复现的确定性代码。模型可以解释决定，不能凭空决定 |
| 把 SQLite 支持成生产选项 | 生产硬化校验会拒绝它。SQLite 只是开发便利 |
| 自研图表库 | 见 [ADR-0001](../adr/0001-custom-svg-charts.md) 的复审触发条件；没触发之前不重做 |

---

## 9. 一句话总结

**这个系统现在可以做的事**：在 mock 数据或你自己灌进来的数据上，稳定地跑通"监控 → 分析 → 生成创意 → 调竞价 → 分预算 → 产出可审批提案 → 人工审批 → 执行 → 全程审计"的闭环，并且有企业级的认证、授权、审计、限流、探针、指标、容器化与 CI。

**它现在还不能做的事**：直接接管你在 Google/Meta/TikTok 上的真实预算。那需要先完成 P0-2 与 P1-7 的平台联调，并且在你自己的沙箱账户上验证过。