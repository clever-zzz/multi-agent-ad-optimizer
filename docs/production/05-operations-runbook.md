# 05 运维手册

面向值班的人。每一节都从"你会看到什么"开始，到"你该做什么"结束。

---

## 1. 日常巡检

### 1.1 每天（2 分钟）

```bash
# 就绪状态与依赖明细
curl -sf https://ads.example.com/readyz | python -m json.tool

# 有没有卡住的 run
TOKEN=...   # adoptimizer token --email <your-admin> --url https://ads.example.com
curl -s "https://ads.example.com/api/v1/runs?status=running" -H "Authorization: Bearer $TOKEN"

# 未处理告警
curl -s "https://ads.example.com/api/v1/alerts/summary" -H "Authorization: Bearer $TOKEN"

# 待审批动作堆积情况
curl -s "https://ads.example.com/api/v1/actions?status=proposed&page_size=1" -H "Authorization: Bearer $TOKEN"

# 昨天的数据到了吗、落了多少（含干跑批次）
curl -s "https://ads.example.com/api/v1/ingest/batches?page_size=5" -H "Authorization: Bearer $TOKEN"

# 调度本身健康吗：每个源是 due / 已覆盖 / 在补 / 需要人工补数（只读，不会启动拉取）
curl -s "https://ads.example.com/api/v1/ingest/schedule" -H "Authorization: Bearer $TOKEN"
```

判断标准：

| 观察 | 正常 | 需要行动 |
|---|---|---|
| `/readyz` | `status: ok` | 任何 check 为 `unavailable` → §4 |
| `running` 状态的 run | 0–2 个，且 started_at 在 30 分钟内 | 有超过 30 分钟的 → §4.3 |
| `open` 告警 | 与业务波动相符 | `critical` 级别超过 24h 未 ack → §3 |
| `proposed` 动作 | 有人在看 | 持续堆积说明审批流程没人负责 |
| `ingest/batches` 最新一条 | `created_at` 在预期窗口内，`unresolved` / `rejected` 为 0 或可解释 | 没有新批次、或 `unresolved` 持续 > 0 → §4.7 |
| `ingest/schedule` 里每个源的 `plan.reason` | `covered` / `nominal` / `catchup`（错过一次会自愈） | `capped`（`gap_days` > 0，必须人工补数）、`lease.held` 长期为 `true`、或 `registered: false` → §4.7 |

### 1.2 每周

- [ ] 检查 LLM 花费：`GET /api/v1/analytics/llm-spend`，与 `LLM__MONTHLY_BUDGET_USD` 对比；与厂商账单交叉核对一次，偏差大说明 `LLM__PRICING` 的单价该更新了
- [ ] 检查数据库增长：`run_events`、`audit_logs`、`daily_metrics`、`llm_spend`、`ingest_batches` 五张表的行数
- [ ] 跑一次保留期清理：`POST /api/v1/admin/prune?audit_days=365`
- [ ] 确认备份可恢复（不只是"备份成功了"，要真的试过恢复）
- [ ] 检查是否有账号该停用（离职、转岗）

### 1.3 关键 Prometheus 指标

| 指标 | 含义 | 值得告警的条件 |
|---|---|---|
| `http_requests_total{status=~"5.."}` | 服务端错误数 | 5 分钟内 > 10 |
| `http_request_duration_seconds` (p95) | 请求延迟 | p95 > 2s 持续 10 分钟 |
| `active_runs` | 本进程正在执行的 run 数 | 持续 > 5，或长时间不归零 |
| `agent_runs_total{status="failed"}` | Agent 闭环失败次数 | 任意一次都值得看 |
| `agent_step_duration_seconds` | 单个 Agent 步骤耗时 | p95 突增通常意味着 LLM 变慢 |
| `llm_calls_total{outcome="error"}` | 模型调用失败 | 错误率 > 5% |
| `llm_spend_usd_total` | 累计模型花费 | 接近月度预算 |
| `alerts_total{severity="critical"}` | 严重业务告警 | 突增 |
| `actions_total` | 提案动作数 | 突增或长期为 0 都异常 |
| `db_query_duration_seconds` | 数据库查询耗时 | p95 > 200ms |
| `cache_ops_total` | 缓存操作 | 命中率骤降说明 Redis 有问题 |
| `ingest_records_total{outcome="unresolved"}` | 采集到的记录对不上任何活动 | 持续 > 0：活动没导入，或 `external_id` 与平台漂移了 |
| `ingest_records_total{outcome="rejected"}` | 记录本身不合法（负数、未来日期、同批重复） | 突然 > 0 通常意味着生产端改过口径 |
| `ingest_batches_total` | 采集批次数（`mode="dry"` 是演练） | 该来的时间点没来 |
| `ingest_ticks_total{outcome="failed"}` | 定时拉取失败的趟数 | 任意一次都值得看。**`ran` / `skipped` / `lost_lease` 都是健康的**，不要拿这个计数器整体告警 |
| `ingest_lag_days` | 最新已覆盖日与今天的差值 | > `INGEST__LOOKBACK_DAYS + 1` 说明数据在变陈旧。用这个而不是 tick 计数器：窗口被上界卡住时 tick 会一直"成功"，数据却越落越远 |
| `absent(ingest_lag_days)` | 这个源从未被拉过 | 上线后就该有值。缺失通常是 `INGEST__SOURCES` 写错了名字，或 CronJob 根本没起来 |

建议的 Grafana 面板分五行：**流量与延迟** / **Agent 与 run** / **模型与成本** / **数据入口** / **依赖（DB、缓存）**。

---

## 2. 常见运维操作

所有操作都有 CLI 或 API，不需要手写 SQL。

```bash
# 探活（部署脚本里用；未就绪时退出码非 0）
adoptimizer healthcheck --url https://ads.example.com

# 应用迁移
adoptimizer migrate
adoptimizer migrate --offline          # 只输出 SQL，先给人看

# 生成迁移
adoptimizer revision --message "add xxx column"

# 灌种子数据（仅演示/测试环境）
adoptimizer seed

# 灌指标数据：先干跑看会拒掉什么，再真写
adoptimizer ingest --source synthetic --days 7 --dry-run
adoptimizer ingest --source synthetic --days 7
adoptimizer ingest --source platform --start 2026-09-01 --end 2026-09-07   # 需 DATA_MODE=warehouse
adoptimizer ingest --file ./backfill.json                                  # 回数仓导出/历史补数

# 看采集台账与数据源可用性
curl -s ".../api/v1/ingest/batches?page_size=20" -H "Authorization: Bearer $TOKEN"
curl -s ".../api/v1/ingest/sources" -H "Authorization: Bearer $TOKEN"

# 跑一次优化
adoptimizer run --max-iterations 2 --window-days 7
adoptimizer run --campaign camp_xxx --campaign camp_yyy

# 取 token
adoptimizer token --email ops@example.com --url https://ads.example.com
```

**保留期清理**

```bash
curl -X POST "https://ads.example.com/api/v1/admin/prune?audit_days=365" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

返回 `{"audit_logs_removed": N, "idempotency_keys_removed": M}`。`audit_days` 范围 30–3650。

> ⚠️ `prune` **不清理 `run_events`**。这张表增长最快（每个 run 几十到几百行），目前没有自动回收。见 §5 与 [08 限制](08-limitations-and-roadmap.md)。

建议用 k8s CronJob 或宿主 cron 每周执行一次，并把返回值记进日志。

---

## 3. 告警响应

系统里有**两类**告警，不要混淆：

| 类型 | 来源 | 在哪看 | 谁处理 |
|---|---|---|---|
| **业务告警**（`alerts` 表） | monitor Agent 检测到的投放异常 | Web「告警」页 / `GET /alerts` | 投放操盘手 |
| **系统告警** | Prometheus/日志规则 | 你的告警平台 | 值班工程师 |

### 3.1 业务告警

严重度与含义：

| severity | 触发条件（默认阈值） | 响应时限 |
|---|---|---|
| `critical` | ROAS < `alert_roas_floor`(1.0)，或 CPA > `alert_cpa_ceiling`(200) 且花费显著 | 当天 |
| `warning` | CTR < `alert_ctr_floor`(0.005) | 3 个工作日 |
| `info` | 统计异常检测命中，但幅度未达阈值 | 按需 |

样本量低于 `min_impressions_for_alerts`(100) 时不产生告警，避免小样本噪声。

处置流程：

```
1. 在「告警」页点开告警，看 context 里的实际观测值与阈值
2. 点【确认】(acknowledge) —— 表示"有人在看"，停止升级
3. 判断：
   a) 是投放本身的问题 → 去「动作」页审批 Agent 提的对应提案，或手动改活动
   b) 是阈值不合适 → 调 OPTIMIZATION__ALERT_* 配置（需要重启）
   c) 是数据问题 → 检查 daily_metrics 是否缺数/重复
4. 处理完点【解决】(resolve)
```

`dedup_key` 保证同一问题在窗口内只产生一条告警，不会刷屏。如果你看到同一个 `dedup_key` 反复出现在不同时间，说明问题一直没解决，而不是告警系统坏了。

### 3.2 系统告警

见 §4 的故障处置。

---

## 4. 故障处置

### 4.1 `/readyz` 返回 503

先看 body 里哪个 check 不是 ok：

**`database: unavailable`**

```bash
# Compose
docker compose ps postgres
docker compose logs --tail=100 postgres
docker compose exec postgres pg_isready -U adoptimizer -d adoptimizer

# Kubernetes
kubectl -n adoptimizer get pods -l app.kubernetes.io/name=postgres
kubectl -n adoptimizer logs -l app.kubernetes.io/name=postgres --tail=100
kubectl -n adoptimizer exec postgres-0 -- pg_isready
```

常见原因与处置：

| 原因 | 症状 | 处置 |
|---|---|---|
| 连接池耗尽 | `FATAL: sorry, too many clients` | 调低 `DATABASE__POOL_SIZE`×副本数，或提高 PostgreSQL `max_connections` |
| 磁盘满 | `could not write to disk` | 清理/扩容；WAL 堆积见 §5 |
| 密码/连接串错 | `password authentication failed` | 核对 Secret 里的 `DATABASE__URL` |
| Pod 未就绪 | `Pending` / `CrashLoopBackOff` | `kubectl describe pod` 看事件 |

> 数据库不可达时，API 会持续 503 并被摘出负载均衡。**不要重启 API**，问题不在它。

**`cache: unavailable`**

Redis 挂了。影响：缓存失效、限流退化为进程内实现、会话校验变慢。

```bash
docker compose logs --tail=50 redis
kubectl -n adoptimizer logs -l app.kubernetes.io/name=redis --tail=50
```

**API 本身不会因为 Redis 挂掉而不可用**（`build_cache` 会降级为进程内缓存），但 `/readyz` 会如实上报。如果你的编排器把 readyz 失败当作重启依据，Redis 抖动会导致无意义的重启——这时可以把 Redis 从 readiness 判定里摘掉，只保留数据库。

**`llm: mock` 而你配置的是 openai**

说明发生了降级。检查 `LLM__FAIL_OPEN_TO_MOCK`：为 `true` 时模型供应商出错会自动降级到 mock（run 仍能跑完，但创意质量下降）；日志里会有对应 warning。查 `llm_calls_total{outcome="error"}` 与 `/analytics/llm-spend`。

**`orchestrator.mode: sequential` 而你期望 langgraph**

`langgraph` 没装上或图编译失败。功能等价，但没有 checkpoint。检查镜像是否装了该依赖（后端镜像装的是 `[postgres,analytics,worker]` extras；`langgraph` 在核心依赖里，正常应该有）。看启动日志里的 `container_ready` 事件。

### 4.2 API 反复重启

```bash
kubectl -n adoptimizer logs deploy/backend --previous
docker compose logs --tail=200 api
```

按启动日志里的关键字定位：

| 日志关键字 | 含义 | 处置 |
|---|---|---|
| `Insecure production configuration: ...` | 生产硬化校验失败 | 按提示修配置。这是设计行为，不要绕过 |
| `LLM__API_KEY is required when ...` | 配了真实 provider 但没给 Key | 补 Key，或改 `LLM__PROVIDER=mock` |
| `[entrypoint] migrations failed after 30 attempts` | 迁移一直连不上库或报错 | 先看 postgres 是否健康，再单独跑 `alembic upgrade head` 看真实错误 |
| `(psycopg/asyncpg) UndefinedTableError` | 迁移没跑 | 确认 `RUN_MIGRATIONS` 或 migrate Job |
| `address already in use` | 端口冲突 | 改 `API_PORT` |
| `llm_model_has_no_pricing_entry` | 该模型不在内置价目表里，花费按 `default` 行估算 | 不阻塞启动，但预算护栏与 `/analytics/llm-spend` 会偏。设 `LLM__PRICING` 给出真实单价 |

### 4.3 run 卡在 `running` 不动

进程启动时的 `reap_stale_runs` 会把**上次异常退出**遗留的非终态 run 标记为 failed（阈值 30 分钟）。所以：

```bash
# 1. 看它到底跑了多久
curl -s ".../api/v1/runs/<RUN_ID>" -H "Authorization: Bearer $TOKEN" | python -m json.tool

# 2. 看事件时间线停在哪个 Agent
#    Web「运行详情」页，或 run 详情里的 events 数组

# 3a. 如果是本进程在跑 → 取消
curl -X POST ".../api/v1/runs/<RUN_ID>/cancel" -H "Authorization: Bearer $TOKEN"

# 3b. 如果执行它的进程已经没了 → 重启 API，reaper 会回收
kubectl -n adoptimizer rollout restart deploy/backend
```

取消是**协作式**的：在每个 Agent 步骤之间检查一次库里的状态，因此最坏情况要等当前 Agent 跑完（通常几秒到几十秒，取决于 LLM 延迟）。`mark_finished` 拒绝把已终态的 run 改写为 succeeded，所以取消不会被后到的结果覆盖。

单个 Agent 步骤耗时异常，通常是 LLM 慢：查 `agent_step_duration_seconds` 按 agent 分组，以及 `llm_latency_seconds`。

### 4.4 SSE 时间线不动

按顺序排查：

1. **代理缓冲**。nginx/ingress 必须对该路径 `proxy_buffering off`。症状：run 结束后所有事件一次性涌出。
2. **读超时太短**。后端每 15s 发一次 keep-alive 注释行，所以 60s 超时够用；如果你的链路超时 <15s，流会被反复切断。
3. **落到了别的副本**。多副本且没有会话亲和时，实时尾流失效，但流会**每约 60s 从 `run_events` 表补齐一次**并正常结束，不会永久挂起。要真·实时就在 ingress 上开 cookie 亲和（清单里已配）。
4. **浏览器 EventSource 断线**。前端用 `lastSeq` 续传，正常情况下会自动恢复。看浏览器 Network 面板里该请求是否 200 且 `content-type: text/event-stream`。

### 4.5 401/403 突然变多

| 症状 | 可能原因 |
|---|---|
| 全员 401 | `SECURITY__JWT_SECRET` 变了（重启后从环境重新读取），或 Redis/会话存储不可达导致会话校验失败 |
| 某个人 401 | 会话被吊销：改过密码、被停用、角色被改。重新登录即可 |
| 某人 403 | 角色被改了，或该端点需要的权限他本来就没有。对照 [03 API 参考](03-api-reference.md) §2 的权限矩阵 |
| 采集管道突然全部 403 | 它用的是 optimizer 账号。`metrics:write` 已从 optimizer 收回，只有 admin 与 `ingestor` 能写；建一个 ingestor 账号换凭据即可 |
| 登录后立刻 401 | 系统时钟漂移导致 `exp` 判定错误。检查 NTP |

### 4.6 429 变多

限流命中。先确认是真流量还是客户端重试风暴：

```bash
curl -s ".../api/v1/admin/audit?page_size=50" -H "Authorization: Bearer $ADMIN_TOKEN"
```

看 `request_id` 与 IP 分布。确认是正常增长后按需调：

```dotenv
RATE_LIMIT__DEFAULT_REQUESTS_PER_MINUTE=600
RATE_LIMIT__WRITE_REQUESTS_PER_MINUTE=120
RATE_LIMIT__OPTIMIZE_RUNS_PER_HOUR=40
```

> 令牌桶状态在**进程内**。N 个副本的实际全局上限是 `limit × N`。反过来，如果某个用户被限流而你算不出为什么，先确认请求是不是分散到了多个副本。

### 4.7 数据没进来，或进来了没落地

症状通常是"仪表盘看着不对"而不是报错，所以按顺序排除，别一上来就怀疑优化器。

**第一步永远是 `GET /ingest/schedule`。** 它一次给出配置、每个源的计划与理由、水位、以及谁正持有锁——"为什么数字没更新"的绝大多数答案都在 `plan.reason` 和 `plan.detail` 里，不需要翻日志。它**只计划不拉取**，所以随便轮询。

```bash
# 1. 计划与水位：reason 说明为什么是这个窗口，detail 里通常直接写着该跑什么
curl -s ".../api/v1/ingest/schedule" -H "Authorization: Bearer $TOKEN" | python -m json.tool

# 2. 最近的采集尝试。没有新批次 = 调度没跑
curl -s ".../api/v1/ingest/batches?page_size=10" -H "Authorization: Bearer $TOKEN"

# 3. 数据源当下能不能用（configured=false 的源拉不动）
curl -s ".../api/v1/ingest/sources" -H "Authorization: Bearer $TOKEN"

# 4. 手动补一趟（--once 不受 INGEST__SCHEDULER_ENABLED 约束，这正是 CronJob 用的形式）
adoptimizer scheduler --once --source platform

# 5. 干跑一遍，报告会逐条说明哪些被拒、为什么
adoptimizer ingest --source platform --days 3 --dry-run
```

`plan.reason` 直接给出处置动作：

| `plan.reason` | 含义 | 处置 |
|---|---|---|
| `covered` | 已覆盖，本轮会跳过 | 数据没问题，去别处找原因 |
| `nominal` / `catchup` | 正常，或正在补一次错过的运行 | 不用管；`catchup` 会在下一趟回到 `nominal` |
| `capped` 且 `gap_days > 0` | 缺口比 `INGEST__MAX_CATCHUP_DAYS` 宽，**调度器刻意不猜** | 跑 `plan.detail` 里那条 `adoptimizer ingest --start ... --end ...`。补完水位会自动放宽，之后的 tick 恢复正常 |
| `first` | 这个源从没有水位 | 上线后第一次是正常的；之后一直是 `first` 说明每趟都在失败，看 `lease.last_outcome` 与 `last_message` |

`lease` 那一段回答"是不是有人在拉、上一次拉得怎么样"：`held: true` 且长时间不变，说明有一趟卡住了（或持有者崩了但 TTL 还没到，`INGEST__LEASE_TTL_SECONDS` 之后会被接管）；`consecutive_failures` 持续增长就是同一个源在反复失败，`last_message` 里有原因。`registered: false` 只有一种可能：`INGEST__SOURCES` 里的名字写错了。

| 台账上的现象 | 含义 | 处置 |
|---|---|---|
| 没有新批次 | 调度没跑，或跑了但失败了 | 看 cron / CronJob 的退出码与日志；`kubectl -n adoptimizer get jobs -l app.kubernetes.io/component=ingest` 与 `ingest_ticks_total` 能区分"没触发"和"触发了但失败" |
| `ingest/schedule` 显示 `capped` | 缺口超过 `INGEST__MAX_CATCHUP_DAYS`，调度器拒绝自己猜 | 按 `plan.detail` 补数（见上表） |
| `lease.held` 长期为 `true` | 有一趟还在拉，或持有者崩了 | 等 `INGEST__LEASE_TTL_SECONDS`；到期会被自动接管，不需要人工删行 |
| `received: 0` | 源连通但没有行 | `platform` 源在 `DATA_MODE=mock` 或凭据不全时**报错而不是返回 0**；真返回 0 说明平台侧这个窗口没有数据，或活动的 `external_id` 与平台不一致 |
| `unresolved` 持续 > 0 | 记录指的活动库里没有 | 报告里每条都带 `identity`。补导入活动，或 `PATCH /campaigns/{id}` 补 `external_id` |
| `rejected` 里是 `same campaign/creative/day` | 生产端在一批里对同一天断言了两次 | 修生产端。服务端刻意不猜哪条对 |
| `rejected` 里是 `negative` / `ahead of today` | 生产端口径或时钟有问题 | 同上。**不要**靠放宽服务端校验让它过去 |
| 计数都对但数字仍然不对 | 某个源覆盖了它不该覆盖的列 | 查 `daily_metrics.source` / `batch_id` 定位是谁写的。`upsert_daily` 的契约是**缺列不覆盖**，所以真出现覆盖说明那个源传了值 |

> ⚠️ `adoptimizer ingest` 与 `adoptimizer scheduler --once` 都在**非空批次一条都没落地**时退出码 1，部分脏数据退出码 0；`scheduler` 另外在**任一趟 tick 失败**时退出码 1。挂 cron 时按退出码告警即可；不要因为偶尔非 0 就把整条采集停掉——那会把"一行脏数据"放大成"完全没有数据"。
>
> `scheduler --once` 的退出码把"某个源拉不到"和"拉到了但对不上账"合并成一个信号，具体是哪一个看它打印的 JSON：`ticks[].outcome == "failed"` 是前者（`error` 里有原因），`received > 0` 而 `created == updated == 0` 是后者（通常是 `external_id` 与平台漂移了）。

---

## 5. 容量与数据增长

### 5.1 增长最快的表

| 表 | 增长驱动 | 当前是否有回收 |
|---|---|---|
| `run_events` | 每次 run × 每 Agent × 每迭代（几十到几百行） | ❌ **无** |
| `llm_spend` | 每次 LLM 调用一行 | ❌ 无 |
| `audit_logs` | 每次状态变更一行 | ✅ `POST /admin/prune?audit_days=N` |
| `idempotency_records` | 每次带幂等键的写请求 | ✅ 同上（按 `expires_at`） |
| `daily_metrics` | 活动数 × 创意数 × 天数 | 业务数据，通常保留 |
| `refresh_sessions` | 每次登录一行 | 过期后仍可查，无自动清理 |
| `ingest_batches` | 每次采集尝试一行（干跑也记） | ❌ 无。行很小（十几个整数），但和 `run_events` 一样需要保留期，见 [08 §3.2](08-limitations-and-roadmap.md) |
| `ingest_watermarks` | 每个数据源**一行**，原地更新 | ✅ 不增长（上界 = `INGEST__SOURCES` 的长度） |
| `scheduler_leases` | 每个 `ingest:<source>` **一行**，原地更新；释放时清空而**不删除**，为的是留住"上次是谁跑的、结果如何" | ✅ 不增长 |

监控它们：

```sql
SELECT relname AS table, n_live_tup AS rows
FROM pg_stat_user_tables
ORDER BY n_live_tup DESC;

SELECT pg_size_pretty(pg_total_relation_size('run_events')) AS run_events;
```

**临时缓解**（在实现自动回收之前）：对已终态且超过保留期的 run 批量删事件。因为 `run_events.run_id` 有 `ON DELETE CASCADE`，删 `optimization_runs` 行会连带清掉事件——但那也会删掉 run 本身的记录，**不要在生产这么做**。正确做法是只删事件：

```sql
-- 先 SELECT 确认范围，再 DELETE。务必在事务里，分批执行。
BEGIN;
DELETE FROM run_events
WHERE run_id IN (
  SELECT id FROM optimization_runs
  WHERE finished_at < now() - interval '90 days'
    AND status IN ('succeeded','failed','cancelled')
);
COMMIT;
```

这条语句是**手工运维动作**，不属于应用逻辑；执行前请确认审计合规要求允许删除运行事件。路线图里已列入自动保留期策略，见 [08 限制](08-limitations-and-roadmap.md)。

### 5.2 扩容信号

| 信号 | 说明 | 动作 |
|---|---|---|
| `http_request_duration_seconds` p95 上升，但 `db_query_duration_seconds` 平稳 | 应用层 CPU 饱和 | 加副本 |
| `db_query_duration_seconds` p95 > 200ms | 数据库成为瓶颈 | 查缺失索引、连接池、PG 资源 |
| `agent_step_duration_seconds` 中 `creative`/`audience` 偏高 | LLM 延迟 | 调 `LLM__CONCURRENCY`、`LLM__TIMEOUT_SECONDS`，或换更快的模型；只接受流式调用的思考模型（Qwen3 thinking）需要 `LLM__STREAM=true` |
| `active_runs` 长期接近 `OPTIMIZE_RUNS_PER_HOUR` 上限 | 优化任务排队 | 加副本（注意 ADR-0002 的亲和性要求） |
| Redis 内存接近 `maxmemory` | 缓存被 LRU 淘汰，命中率下降 | 扩 Redis 或调低 `REDIS__CACHE_TTL_SECONDS` |

---

## 6. 备份与恢复

### 6.1 备份范围

| 数据 | 备份方式 | RPO 建议 |
|---|---|---|
| PostgreSQL | `pg_dump` 逻辑备份 + WAL 归档做 PITR | 逻辑备份每日；WAL 连续 |
| Redis | **可以不备份**（缓存、限流、会话都可重建；用户会掉登录） | — |
| ClickHouse（若启用） | 事件明细，通常可从上游重新灌 | 按需 |
| 配置/Secret | 你的密钥管理系统 | 与变更同步 |

### 6.2 备份

```bash
# Compose
docker compose exec -T postgres \
  pg_dump -U adoptimizer -d adoptimizer -Fc \
  > "adoptimizer-$(date -u +%Y%m%dT%H%M%SZ).dump"

# Kubernetes
kubectl -n adoptimizer exec postgres-0 -- \
  pg_dump -U adoptimizer -d adoptimizer -Fc > backup.dump
```

`-Fc` 是自定义格式，支持并行恢复与选择性恢复。

### 6.3 恢复

```bash
# 1. 停 API，避免恢复过程中写入
kubectl -n adoptimizer scale deploy/backend --replicas=0

# 2. 恢复
pg_restore -U adoptimizer -d adoptimizer --clean --if-exists --no-owner adoptimizer-YYYYMMDD.dump

# 3. 确认 alembic 版本与代码匹配
docker compose exec api alembic current

# 4. 起回来
kubectl -n adoptimizer scale deploy/backend --replicas=2
curl -sf https://ads.example.com/readyz
```

**恢复后必查**：

- `alembic current` 的 revision 与当前代码的 head 一致（不一致就先 `migrate` 或回滚代码）
- 引导管理员账号还在，且能登录
- 随便挑一个 run，`GET /runs/{id}` 的事件时间线完整

### 6.4 演练

**没演练过的备份等于没有备份。** 每季度做一次：

1. 从生产备份恢复到隔离环境
2. 用 `adoptimizer healthcheck` 与一次 `adoptimizer run` 验证可用
3. 记录恢复耗时（这就是你的真实 RTO）

---

## 7. 发布与回滚

完整步骤在 [04 部署 §9](04-deployment.md#9-升级与回滚)。运维视角的要点：

**发布顺序永远是：迁移 → 应用。**

- 迁移先跑，且必须**向后兼容当前在跑的应用版本**（只加可空列、加表、加索引；不删不改语义）
- 应用滚动更新期间，新旧版本会同时在线（`maxSurge: 1, maxUnavailable: 0`），所以 schema 必须同时兼容两者
- 删除类的 schema 变更放到**下一次**发布

**回滚判断**：

| 情况 | 动作 |
|---|---|
| 新版本启动即崩（配置/依赖问题） | 立刻 `rollout undo`，风险低 |
| 新版本能起但行为异常 | 先判断是否 schema 相关；不是就 `rollout undo` |
| 迁移已执行且不可逆 | **不要回滚代码**，向前修。前提是旧代码能与新 schema 共存 |
| 数据被写坏 | 停止写入 → 评估 PITR 恢复点 → 走 §6.3 |

回滚后确认：

```bash
kubectl -n adoptimizer rollout status deploy/backend
curl -sf https://ads.example.com/readyz | python -m json.tool
# 触发一次 run 验证闭环仍然可用
```

---

## 8. 需要人工介入的场景清单

系统设计上尽量自动化，但这些**故意**留给人：

| 场景 | 为什么不能自动 |
|---|---|
| 审批 Agent 提出的预算/出价/暂停动作 | 花的是真钱。`SECURITY__REQUIRE_ACTION_APPROVAL=true` 是硬性要求 |
| 创建/停用账号、改角色 | `user:manage` 仅 admin；且有"最后一个 admin"护栏 |
| 给采集管道签发凭据 | 应该发 `ingestor` 角色（只有 `metrics:read` + `metrics:write`），不是某个人的 optimizer/admin 账号——泄露时炸不到活动与动作，审计里也追得到是管道灌的 |
| 执行保留期清理 | 删数据不可逆，且合规要求因组织而异 |
| 调整 `OPTIMIZATION__*` 业务阈值 | 这是业务判断，不是技术判断 |
| 从 mock 切到真实广告平台 | 需要逐平台沙箱验证，见 [08 限制](08-limitations-and-roadmap.md) |
| 恢复备份 | 会丢数据，必须有人决定恢复点 |

---

## 9. 联系信息与升级路径

按你的组织填写：

| 层级 | 负责 | 联系方式 | 响应时限 |
|---|---|---|---|
| L1 值班 | | | 15 分钟 |
| L2 后端 | | | 1 小时 |
| L3 架构 | | | 4 小时 |
| 业务方（投放） | | | — |

升级判据：`critical` 业务告警 2 小时无人 ack、`/readyz` 持续 503 超过 10 分钟、疑似密钥泄露（立刻走 [06 安全](06-security.md) §6 的应急流程）。