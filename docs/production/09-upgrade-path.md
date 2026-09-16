# 09 · 优化升级路径

> **这份文档与 [08](08-limitations-and-roadmap.md) 的关系**
>
> 08 回答「**有哪些问题**」——它是一份诚实的缺陷清单，按严重度打了 🔴/🟡/🟢。
> 09 回答「**按什么顺序解决、怎么证明解决了**」——它是一份带依赖关系和出口条件的执行路径。
>
> 事项编号沿用 08（P0-1 … P2-22），本文不重新编号，也不重复描述问题本身。08 说"哪里疼"，09 说"先治哪、怎么算治好"。

---

## 1. 排序原则

清单谁都会列，难的是顺序。这份路径按三条原则排，顺序本身就是论证：

### 原则一：先自证，再接触真实世界

在适配器有隐藏 bug 的情况下接上真实广告账号，等于用一个未经校验的写路径去动真金白银。`infra/ads/` 里三个适配器是照着 REST 文档写的，**测试里没有任何一处直接覆盖 HTTP 层**——现有测试用的是 `StubRegistry` + `MockAdsClient`，验证的是"调用被正确分派"，而不是"请求长什么样"。

所以 **S0 必须先于 S1**。补 `httpx.MockTransport` 测试的成本以小时计，而一次错误的 `amountMicros` 换算可能花掉一个月的预算。

### 原则二：先可逆，再不可逆

读路径（拉报表）无害，写路径（改预算、暂停活动）碰钱。所以 **S1 读 → S2 写**，且 S2 内部要求"先只做可逆操作"（暂停/恢复），并且必须**和回滚能力同批交付**——08 §1.2 已指出当前没有平台侧回滚。写通了但撤不回来，比写不通更危险。

### 原则三：先正确，再快，再大

S3（数据管道）和 S4（横向扩展）都属于"变大变快"。在 S0–S2 完成前做这些，只是让一个不可信的系统跑得更快。

---

## 2. 六阶段总览

| 阶段 | 名称 | 目标 | 对应 08 条目 | 出口条件（一句话） | 状态 |
|---|---|---|---|---|---|
| **S0** | 可信基线 | 让"代码对不对"从假设变成已知 | P0-4、P0-5 + 两项新发现 | CI 新增 3 道门禁全绿 | ✅ S0.1 / S0.2 完成；S0.3 / S0.4 待做 |
| **S1** | 读路径联调 | 从真实账号拉到正确数字 | P0-2 | 平台 UI 与 `daily_metrics` 对得上 | ⬜ 待真实账号 |
| **S2** | 写路径与回滚 | 真实账号上写通、且能撤回 | P1-7 | 一次真实写 + 一次真实回滚，审计链完整 | ⬜ 待沙箱账号 |
| **S3** | 数据管道 | ClickHouse 有入口、有量级验证 | P2-17、P1-6 | CH 后端在目标量级 P95 达标 | 🟡 写入路径完成；压测与 prune 待做 |
| **S4** | 横向扩展 | 摆脱单副本单 worker | P2-15、P2-16、P1-8 | `UVICORN_WORKERS=4` 下派发与 SSE 正常 | ⬜ 待做 |
| **S5** | 运维与合规 | 可轮换、可追踪、可举证 | P1-9、P2-20、P2-21 | 密钥轮换不掉线；有端到端 trace | ⬜ 待做 |

**依赖关系**（`→` 表示前者是后者的前置）：

```
S0.1 适配器HTTP测试 ──→ S1 读路径联调 ──→ S2 写路径+回滚
S0.2 CH显式失败 ─────→ S3 数据管道
S0.3 依赖扫描（独立，可随时做）
S0.4 冒烟脚本 ──────→ 为 S1–S5 提供回归手段

S1 + S2 完成 = 「可以投产」（作为人类审批的辅助工具）
S3 ──→ 决定能否启用 DATA_MODE=warehouse
S1–S3 稳定 ──→ S4 才有意义
S5 可与其他阶段并行
```

**注意关键路径**：真正决定"能不能投产"的只有 **S0.1 → S1 → S2** 这条链。S3/S4/S5 都是上线之后的事。

---

## 3. 逐阶段展开

### S0 · 可信基线

**目标**：把"现有代码是可信的"从假设变成有证据的结论。这一阶段不写任何新功能。

#### S0.1 平台适配器 HTTP 层测试 ✅ **已完成**

- **为什么是第一步**：这是整条关键路径的起点，且投入产出比最高。
- **动作**：为 `GoogleAdsClient` / `MetaAdsClient` / `TikTokAdsClient` 的每个方法写直测，用 `httpx.MockTransport` 断言**请求路径、请求头、请求体**，并覆盖错误分支（401 / 429 / 500 / 非 JSON 响应）。
- **锚点**：
  - 被测代码 `backend/src/adoptimizer/infra/ads/{google,meta,tiktok}.py`
  - 参考写法 `backend/tests/unit/test_llm_provider.py:44`（已用 `MockTransport` 的先例）
  - `respx>=0.21` **已在 dev 依赖里但零使用**——要么用起来，要么从 `pyproject.toml` 移除，不要留着一个不用又暗示"测过了"的依赖
- **出口条件**：三个适配器每个公开方法至少一条 happy path + 一条错误分支断言；`pytest --cov` 不低于 88% 门槛。
- **风险**：低。纯新增测试，不动生产代码。

**结果**：新增 `backend/tests/unit/test_ads_adapters.py`（37 个用例）。为了让测试走的是**生产同一条代码路径**，给 `PlatformHTTPClient` 和三个适配器加了可注入的 `transport` 参数（默认 `None`，生产行为不变）。

**写测试的过程发现了三个真实缺陷**——都是"照着文档写"必然留下的类型，且任何基于 mock 的测试都抓不到：

| # | 缺陷 | 症状 | 修复 |
|---|---|---|---|
| 1 | **TikTok 写操作完全不带凭据**：`_headers()` 方法定义了但从未传给 HTTP 客户端 | 每一次 `_post` 都会 401 | 构造客户端时传入 `headers=self._headers()` |
| 2 | **TikTok 读操作把 token 放进 query string** | 凭据泄漏进访问日志、代理链路与浏览器历史 | 移除 params 里的 `Access-Token`，统一走 header |
| 3 | **Meta 把 `object_story_spec` 序列化成 Python repr** | httpx 不报错，但发出的是 `{'page_id': 'p'}`（单引号），Meta 以 400 拒绝 | 显式 `json.dumps(spec)` |

第 3 个尤其值得注意：它**不会崩**，只是被平台拒绝，现场表现像"参数写错了"而不是"编码错了"。

#### S0.2 ClickHouse 静默降级改为显式失败 ✅ **已完成**（采用计数而非抛错）

- **问题**：`infra/warehouse.py` 的 `ClickHouseWarehouse._query()` 在客户端不可用时 `return []`，连接失败也只 `logger.warning`。生产表现是**报表全为 0 但没有任何报错**。
- **动作**：区分"查询返回空"与"后端不可用"两种情况。后者应让 `/readyz` 反映出来（`healthcheck()` 已有 `unavailable` 状态，但没有接到就绪探针），并把 `build_warehouse()` 的静默 fallback 改成启动时显式告警。
- **锚点**：`infra/warehouse.py` 的 `_query()` / `healthcheck()` / `build_warehouse()`；`api/v1/system.py` 或 `/readyz` 的装配处。
- **出口条件**：关掉 ClickHouse 后 `/readyz` 返回非 200 或降级标记；日志中不再出现"静默返回空"的路径。
- **风险**：低。但要注意别把 CH 变成硬依赖——fallback 到 SQL 是合理设计，要保留，只是必须**可见**。

**结果（判断被实施过程修正）**：原计划"改成显式失败"，但动手时发现现有测试 `test_warehouse_reads::test_a_driver_failure_degrades_to_an_empty_result` **刻意断言了降级到空结果**，注释写着"仓库故障不能拖垮优化循环"。这个设计意图是对的——读失败降级、写失败抛错，两者职责不同。

所以改为**保持降级行为、让降级可见**：新增 `warehouse_reads_total{backend,outcome}`，outcome 区分 `ok` / `empty` / `degraded`。一个稳定的 `degraded` 速率就是"报表正在静默地读另一个数据源"的告警信号。这比抛错更贴合原设计，也不会让一次仓库抖动中断优化循环。

#### S0.3 依赖漏洞扫描进 CI（= P0-4）

- **动作**：`backend` job 加 `pip-audit`，`frontend` job 加 `npm audit --audit-level=high`。高危 CVE 阻塞合并。
- **锚点**：`.github/workflows/ci.yml` 的 `backend`（第 22 行起）与 `frontend`（第 158 行起）job。
- **出口条件**：两个 job 中出现扫描步骤，且当前依赖树通过（或把已知问题显式加入豁免清单并写理由）。
- **风险**：低，但首次接入可能扫出一批存量问题，需要决定"修"还是"记录豁免"。

#### S0.4 生产冒烟脚本（= P0-5）

- **动作**：把 [07 §6](07-testing-and-ci.md#6-手工端到端验收清单) 里可自动化的部分写成一个脚本：起服务 → 登录 → 建活动 → 触发 run → 轮询到终态 → 审批 → 执行 → 断言审计行存在。
- **锚点**：`scripts/`（现有 `setup.ps1` / `dev.ps1` / `check.ps1` / `clean.ps1`，缺一个 `smoke.ps1` 或 `smoke.py`）。
- **出口条件**：脚本可在干净环境一键跑通，退出码可靠。
- **风险**：低。这是 S1–S5 的回归基础设施，值得先做。

**S0 出口条件**：CI 新增 3 道门禁（适配器测试 / 依赖扫描 / 冒烟脚本）全绿，且 `ruff` + `mypy --strict` + `pytest`（≥88%）+ `alembic check` 全部保持通过。

---

### S1 · 读路径联调

**目标**：从真实广告账号拉到正确的活动与日报表数据。

- **动作**：
  1. 用**已经写好**的 `adoptimizer creds --probe`（`cli.py:522`）做只读探测——它明确只做 read-only report 调用，是安全的第一次接触。
  2. 配置 `DATA_MODE=warehouse` + 平台凭据，跑 `adoptimizer ingest --source platform`。
  3. 逐日对账：把 `daily_metrics` 的数字与平台 UI 对齐。
- **锚点**：`cli.py` 的 `creds` 命令与 `_probe_platforms()`；`infra/ingest/platform_report.py`；`services/scheduling.py` 的水位与日历窗口逻辑。
- **出口条件**（= P0-2 的交付标准）：至少一个平台，连续 7 天的 impressions / clicks / cost 与平台 UI 一致；差异有解释（归因窗口、时区、延迟）。
- **真实瓶颈不是代码**：平台侧的 **developer token / 应用审核** 可能需要数天到数周。**这件事应该今天就启动申请**，不要等 S0 做完——它是整条路径上唯一一个不受你控制的串行等待。
- **风险**：中。`platform_report.py` 刻意不报 revenue（ad network 不知道你的收入），对账时不要误判为 bug——这是设计。

---

### S2 · 写路径与回滚

**目标**：在真实账号上写通，并且能撤回。

- **动作**：
  1. **先在沙箱/测试账号**上，用 `TOOLS__DRY_RUN=false` + `TOOLS__ALLOW_AGENT_WRITES=false` 走通一次预算调整——注意这个组合的含义：**智能体写仍然干跑，只有人工审批后的 `_push()` 才真写**（`services/actions.py:332` 传 `agent=None` 是这条通道的钥匙）。
  2. 验证 `external_reference` 有值、审计行含 `dry_run: false`。
  3. **同批交付回滚**：为每个可逆动作（pause/resume/budget）提供撤销路径。当前 08 §1.2 明确"无平台侧回滚"，这是 S2 的核心缺口，不是可选项。
  4. 确认前端审批界面正确显示 `dry_run` 状态——`_execution_from_tool()` 已经保证干跑不会被伪装成"平台已接受"，UI 要如实呈现。
- **锚点**：`services/actions.py` 的 `execute()` / `_push()` / `_execution_from_tool()`；`tools/executor.py` 的 `_should_dry_run()`。
- **出口条件**（= P1-7 的交付标准）：测试账户上暂停 → 恢复 → 预算调整各成功一次，每次都有 `external_reference`；人为制造一次平台错误，动作落 `failed` 且 `error_message` 可读；一次回滚验证通过。
- **风险**：**高**。这是整个系统唯一会动真钱的地方。建议：先在 `DRY_RUN=true` 下把全链路走顺，再开真写；真写时用极小额度（比如日预算 1 元）。

---

### S3 · 数据管道

**目标**：让 ClickHouse 从"只有读没有写"变成"有入口、有量级验证"。

- **动作**：
  1. **补 `ad_events` 写入路径**——建表 SQL 在 `init-scripts/clickhouse/01_create_tables.sql`，但全仓库**没有任何代码往这张表写**。这是 08 §3.1 没点透的关键：不是"没压测"，是"没数据"。
  2. 在目标量级（建议先定 1 亿行）做真实压测，记录 P95 延迟。
  3. 扩展 `/admin/prune`（= P1-6）：目前只清 `audit_logs` + `idempotency_keys`，需要纳入 `run_events` / `llm_spend` / 过期 `refresh_sessions` / `critic_findings`。**注意** `critic_findings` 的 `ON DELETE SET NULL` 会留孤儿行，prune 时要一并处理。
- **锚点**：`infra/warehouse.py`；`init-scripts/clickhouse/`；`api/v1/admin.py:214` 的 `prune` 端点；`repositories/audit.py` 的 `prune` 先例。
- **出口条件**：CH 后端在目标量级下 P95 达标；`/admin/prune` 覆盖 6 张表且有测试。
- **风险**：中。写入路径涉及数据一致性，要复用已有的幂等与水位机制，不要另起一套。

**结果（写入路径已完成，压测与 prune 待做）**：

实施时发现了一个比"没写入代码"更根本的问题：**平台 API 给的是日报，不是事件流**。要把日报灌进 `ad_events`（事件表），只能为每次曝光合成一行事件，那既会造出海量虚假行，也要为每行编造一个成本分摊。所以补的不是一条管道，而是**两条**：

| 表 | 用途 | 现状 |
|---|---|---|
| `ad_events` | 真实事件流（`Enum8` 维度，含人口细分） | 写入器已就绪，等事件流接入 |
| `campaign_daily_metrics` | 日报聚合（`String` 维度，**新增**） | **今天就能灌**，是主路径 |

新增组件：

- `infra/analytics/models.py` — `AdEvent` / `DailyMetricRow` / `WriteReport`，以及**列类型规范化**。这是"即接即用"的关键：`ad_events` 的 `Enum8` 列会让 ClickHouse 拒绝整批插入，且报错只提列名不提行号。所以不规范的值被**拒绝并计入原因**（`platform_not_modelled` / `device_not_modelled` …），而不是被静默改成默认值——`mock` 平台被刻意列为不可存储，因为一行 mock 数据出现在分析表里，污染的正是这张表存在的意义。
- `infra/analytics/sink.py` — `AnalyticsSink` 协议 + `ClickHouseSink`（分批插入、显式列名、失败抛错）+ `NullSink`（每行都报 skipped，杜绝"灌数成功但仓库是空的"）+ `build_sink()` 工厂。
- `services/warehouse_sync.py` — `WarehouseSyncService`：从主库 `daily_metrics` 读 → 写仓库。**用镜像而不是双写**：主库是事务性的、权威的，仓库是最终一致的分析副本；在同一个事务里写两边，会让仓库故障要么阻塞采集、要么逼采集回滚，而这份数据仓库随时能从主库重新推导。
- `init-scripts/clickhouse/02_daily_metrics.sql` — 新表，`ReplacingMergeTree` 按 (campaign, creative, date) 去重，**这是重跑安全的前提**，也是部分失败后操作员敢直接重跑的原因。
- CLI：`adoptimizer warehouse status`（可用性预检）与 `adoptimizer warehouse sync [--days N] [--campaign ID] [--dry-run]`。
- 读侧对该表统一加 `FINAL`（`ClickHouseWarehouse._relation`）：`ReplacingMergeTree` 只在后台 merge 时替换旧版本，而 sync 支持重跑，不收敛就会把重放留下的多个版本一起 `sum()` 进去。
- 读侧：`ClickHouseWarehouse` 新增 `CLICKHOUSE__METRICS_SOURCE=events|daily`（**默认 `daily`**，即与写入侧对齐的那一张表；`events` 目前没有生产写入者，显式选中时 `build_warehouse` 会打 `clickhouse_events_source_has_no_writer` 告警而不是静默读空。默认值曾经选 `events`，理由是"不让已有部署的读取行为在脚下改变"，但没有东西可保留：一张没有写入者的表永远回答空，而刻意的降级又会把这件事藏起来）。`daily` 模式下 `audience_observations` 如实回退到 campaign 维度，而不是编造看不见的人口细分。

**仍未完成**：真实数据量压测（需要真实数据）、`/admin/prune` 扩展（= P1-6）。

---

### S4 · 横向扩展

**目标**：摆脱 `UVICORN_WORKERS=1` 与 cookie 亲和的约束。

- **动作**：
  1. **arq worker**（= P2-15）：`pyproject.toml` 里 `worker = ["arq>=0.26"]` 已预留但未接。把 `services/optimization.py:188` 的 `_dispatch()`（内部 `asyncio.create_task`，第 198 行）换成投递队列。
  2. **EventBus 换 Redis Pub/Sub**（= P2-16）：Redis 已是运行时依赖，但 EventBus 仍在进程内。这是水平扩展后第一个坏的东西。
  3. **限流与会话校验换 Redis 后端**（= P1-8）。
  4. **checkpointer 落持久化**：当前 `MemorySaver`。08 §2.3 的辩解成立（丢的是续跑能力，不是可审计性——`run_events` 才是权威源），但生产重启后应该能续跑。
- **锚点**：`services/optimization.py` 的 `_dispatch()` / `wait_for()` / `_poll_until_terminal()`；`deploy/k8s/backend.yaml` 的 `UVICORN_WORKERS=1` 注释（ADR-0002）；`deploy/k8s/redis.yaml`。
- **出口条件**：`UVICORN_WORKERS=4` + 2 副本下，run 派发、SSE 流、跨副本取消全部正常；`wait_for()` 在非持有副本上走轮询分支不退化。
- **风险**：中高。这是对核心执行路径的改造，S0.4 的冒烟脚本是安全网。

---

### S5 · 运维与合规

**目标**：可轮换、可追踪、可举证。可与前面阶段并行。

| 项 | 对应 08 | 动作锚点 | 出口条件 |
|---|---|---|---|
| JWT `kid` 多密钥轮换 | P1-9 | `core/security.py` 的 `TokenService._encode()` / `decode()` | 轮换期间新旧 token 并存有效，无人掉线 |
| OTLP 分布式追踪 | P2-21 | 现有 `X-Request-ID` 是全链路追踪的起点 | 一次 run 的跨组件 trace 可在 Jaeger 中看到 |
| 审计哈希链 | P2-20 | `services/audit.py` + `AuditLog` 模型 | 任一历史行被篡改可被检测 |
| Prometheus 告警 + Grafana | P1-12 | 新建 `deploy/observability/` | 规则文件与 dashboard JSON 入库 |
| 创意评测集 | P1-11 | `agents/creative.py` 的 prompt | 换 prompt/模型前后有可比分数 |
| Playwright E2E | P1-10 | `frontend/` | 登录→run→审批→执行进 CI |

**注意 S5 里最该先做的是 JWT 轮换**——它是唯一一个"出事时你无法补救"的项（单密钥意味着想换密钥就得全员重新登录）。

---

## 4. 如果只有两周

如果时间盒是两周，砍掉一切非关键路径，只做这条线：

| 天 | 做什么 | 产出 |
|---|---|---|
| D1 | **今天**就提交平台 developer token / 应用审核申请 | 解除唯一的串行外部等待 |
| D1–D3 | S0.1 适配器 HTTP 层测试 | 写路径可信 |
| D3–D4 | S0.2 CH 显式失败 + S0.3 依赖扫描 | 消除静默故障 + 补基本卫生 |
| D4–D5 | S0.4 冒烟脚本 | 后续所有阶段的回归手段 |
| D6–D8 | S1 读路径 probe + 对账（等审核通过） | 数字对得上 |
| D9–D12 | S2 写路径（DRY_RUN 全链路 → 真写极小额度） | 一次真实写 + 回滚 |
| D13–D14 | 缓冲 + 补文档 | — |

**两周后你得到的不是一个"更大"的系统，而是一个"能证明自己是对的"系统。** 这才是可以交付给真实预算的前提。

---

## 5. 反模式（不要做什么）

这些和 08 §「明确不做」一脉相承，但在"优化升级"的语境下值得再强调一次：

| 反模式 | 为什么是错的 |
|---|---|
| 跳过 S0.1 直接接真实账号 | 用未验证的写路径动真钱。这是最贵的捷径 |
| 先做 S4 横向扩展 | 让不可信的系统跑得更快，只是更快地产生不可信的结果 |
| 把 S2 的"回滚"推迟到 S3 之后 | 写通了但撤不回来，比写不通更危险 |
| 为了过覆盖率门槛写无断言测试 | 88% 是下限不是目标。S0.1 的价值在断言内容，不在行数 |
| 让 LLM 参与预算数字决策 | 08 已列为"明确不做"。金钱的数学必须是确定性的 |
| 把 SQLite 当生产选项调优 | 生产硬化校验会拒绝它，别在这上面花时间 |

---

## 6. 面试叙事映射

这套路径的每一步都能转成一段可讲的技术故事——**因为它的每一步都是"发现了什么问题 → 为什么这样排序 → 怎么证明修好了"**：

| 阶段 | 可讲的点 |
|---|---|
| **S0.1** | "我审计自己的项目时发现，三个平台适配器有 1000+ 行代码，但测试用的是 Stub，从来没断言过真实请求长什么样。这是我给自己找出的最大盲区。" |
| **S0.2** | "`_query()` 在后端不可用时 `return []`，生产表现是报表全 0 且无告警。**静默失败比崩溃更危险**——崩溃至少会被人发现。" |
| **S1/S2** | "我把'读路径'和'写路径'分开联调，因为读无害、写碰钱。写路径我坚持和回滚同批交付——08 里我自己写了'无平台侧回滚'，那就不能装作没看见。" |
| **三道闸的咬合** | "`_should_dry_run()` 判断 `from_agent`，而人工审批后的 `_push()` 显式传 `agent=None`。**这是两处代码刻意咬合出的一条通道**，不是靠约定或文档。" |
| **S4** | "我知道 `UVICORN_WORKERS=1` 是个约束，但我没有假装它不是。ADR-0002 记录了原因，`pyproject.toml` 里 arq extra 是预留的接口。**承认约束并留下演进接口，比掩盖它更专业。**" |
| **整体** | "我给自己的项目写了 08 那份限制清单，把 🔴 标在真实平台未联调上。**能准确说出自己系统的边界在哪，比声称它没有边界更可信。**" |

---

## 7. 一句话

**这条路径的排序逻辑只有一句：先让系统能证明自己是对的，再让它接触真实世界，最后才让它变快变大。**

任何试图跳过前两步直接做第三步的优化，都只是在放大一个尚未验证的假设。
