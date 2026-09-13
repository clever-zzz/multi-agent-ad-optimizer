# 03 API 参考

- Base URL：`http://<host>:8000`
- 业务前缀：`/api/v1`（可用 `APP__API_V1_PREFIX` 改）
- 交互式文档：`/docs`（Swagger UI）、`/redoc`。**生产环境（`APP__ENVIRONMENT=production`）自动关闭这两个页面**，但 `/openapi.json` 仍然开放，便于内网生成客户端。
- 传输：全部 JSON，SSE 端点除外。成功响应是 `Content-Type: application/json`；**错误响应是 `Content-Type: application/problem+json`**（RFC 9457）；`/metrics` 是 Prometheus 文本格式（`text/plain`）；`/runs/{run_id}/stream` 是 `text/event-stream`。

---

## 1. 认证

### 1.1 获取 token

```bash
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@adoptimizer.dev","password":"Adm1n!ChangeMe"}'
```

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "rt_...",
  "token_type": "bearer",
  "expires_in": 1800,
  "user": { "id": "usr_...", "email": "...", "role": "admin", "permissions": ["campaign:read", "..."] }
}
```

之后每个请求带：

```
Authorization: Bearer <access_token>
```

### 1.2 token 生命周期

| 项 | 默认 | 配置 |
|---|---|---|
| access token TTL | 30 分钟 | `SECURITY__ACCESS_TOKEN_TTL_MINUTES` |
| refresh token TTL | 7 天 | `SECURITY__REFRESH_TOKEN_TTL_DAYS` |
| 算法 | HS256 | `SECURITY__JWT_ALGORITHM`（HS256/HS512/RS256） |
| issuer / audience | `adoptimizer` / `adoptimizer-api` | `SECURITY__ISSUER` / `SECURITY__AUDIENCE` |

`POST /auth/refresh` **轮换** refresh token：旧的立即失效，返回新的一对。重放已轮换的 token 会被拒。

### 1.3 会话即时撤销

`SECURITY__VERIFY_SESSION_ON_REQUEST=true`（默认）时，每个带 token 的请求都会额外查一次 `refresh_sessions`，确认该会话未被撤销。因此：

- `POST /auth/logout` 后，**尚未过期的 access token 立刻失效**
- 管理员停用账号后，该账号所有在线会话立刻失效
- 管理员改角色后，旧会话被吊销，用户必须重新登录拿到新权限

代价是每请求一次 Redis/DB 查询。详见 [ADR-0003](../adr/0003-session-revocation-on-request.md)。

### 1.4 登录保护

连续失败 `SECURITY__MAX_FAILED_LOGINS`（默认 5）次后账号锁定 `SECURITY__LOCKOUT_SECONDS`（默认 900 秒）。锁定期间即使口令正确也返回 401，且不透露是"口令错"还是"被锁定"。

---

## 2. RBAC 权限矩阵

14 个权限，5 个角色。路由断言**权限**而不是角色，因此角色→权限的映射可以在 `core/security.py::_ROLE_PERMISSIONS` 一处调整。

| 权限 | admin | optimizer | analyst | viewer | ingestor |
|---|:--:|:--:|:--:|:--:|:--:|
| `campaign:read` | ✅ | ✅ | ✅ | ✅ | — |
| `campaign:write` | ✅ | ✅ | — | — | — |
| `run:read` | ✅ | ✅ | ✅ | ✅ | — |
| `run:trigger` | ✅ | ✅ | — | — | — |
| `action:approve` | ✅ | ✅ | — | — | — |
| `action:execute` | ✅ | ✅ | — | — | — |
| `alert:read` | ✅ | ✅ | ✅ | ✅ | — |
| `alert:ack` | ✅ | ✅ | ✅ | — | — |
| `creative:write` | ✅ | ✅ | — | — | — |
| `metrics:read` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `metrics:write` | ✅ | — | — | — | ✅ |
| `system:read` | ✅ | ✅ | ✅ | — | — |
| `user:manage` | ✅ | — | — | — | — |
| `audit:read` | ✅ | — | — | — | — |

`/admin/*` 全部端点用 `require_role(Role.ADMIN)` 直接断言角色（而不仅是权限），因为这些是运维操作。

**角色定位**

- **admin** — 平台负责人。全部权限，含账号管理、审计查看、灌种子、保留期清理。
- **optimizer** — 投放操盘手。能改活动、触发优化、审批并执行动作，但**看不到审计流水、管不了账号、也不能灌指标**。
- **analyst** — 分析师。只读全部业务数据 + 能确认告警，不能改任何东西。
- **viewer** — 观察者。只读，连告警确认都不能做。
- **ingestor** — 采集管道的**机器身份**，只有 `metrics:read` + `metrics:write` 两条。给定时任务或外部推送管道发这个角色的凭据：凭据泄露只能伪造数字，改不了活动、审批不了动作，也就没法把自己伪造的数字批成真实预算变更。它持有的权限比 viewer 还少，但其中一条是写权限——所以它的定位不是"权限最少"，而是"范围最窄"。

> ⚠️ **行为变更**：`metrics:write` 已从 optimizer 收回。原本用 optimizer 账号推送 `POST /ingest/metrics` 的管道会开始收到 `403 permission_denied`；建一个 ingestor 账号并换掉那份凭据即可。用 `adoptimizer scheduler` / `adoptimizer ingest` 走进程内路径的采集不受影响，它根本不经过 HTTP 鉴权。

前端 `frontend/src/stores/auth.ts` 里存着这张矩阵的一份副本，用来隐藏 API 会拒绝的控件。副本会与真身漂移（历史上漂过两次），所以 `backend/tests/unit/test_security.py::TestFrontendMirror` 会解析那个文件并与 `_ROLE_PERMISSIONS` 逐角色比对，不一致就让后端测试变红。

---

## 3. 错误契约

所有错误返回统一结构（RFC 9457 problem document，`Content-Type: application/problem+json`）：

```json
{
  "type": "https://adoptimizer.dev/errors/permission_denied",
  "title": "Permission denied",
  "status": 403,
  "detail": "Requires permission campaign:write",
  "code": "permission_denied"
}
```

客户端应该按 `code` 分支，**不要**解析 `detail` 文案。

| HTTP | `code` | 何时出现 |
|---|---|---|
| 401 | `unauthenticated` | 无 token / token 过期或非法 / 会话已撤销 / 账号锁定或停用 |
| 402 | `budget_exceeded` | LLM 月度预算耗尽且 `LLM__FAIL_OPEN_TO_MOCK=false` |
| 403 | `permission_denied` | 权限或角色不足 |
| 404 | `not_found` | 资源不存在 |
| 409 | `conflict` | 状态冲突（重复 email、乐观锁失败、重复审批/重复执行已终结的动作等） |
| 409 | `approval_required` | 动作未审批就试图执行 |
| 409 | `run_in_progress` | 同一范围内已有运行中的 run |
| 422 | `validation_failed` | 请求体/查询参数校验失败 |
| 429 | `rate_limited` | 触发限流 |
| 502 | `external_service_error` | 广告平台或模型供应商调用失败 |
| 503 | `dependency_unavailable` | 数据库/缓存不可达 |

422 的响应体会额外带 FastAPI 的 `errors` 数组，逐项指出哪个字段错了。

429 会带 `Retry-After` 头（秒）。

---

## 4. 通用约定

### 4.1 分页

列表端点接受 `page`（从 1 开始）与 `page_size`（默认 20，上限 200），返回：

```json
{
  "items": [ ... ],
  "total": 137,
  "page": 1,
  "page_size": 20,
  "pages": 7
}
```

### 4.2 幂等

目前**只有 `POST /api/v1/runs`** 接受 `Idempotency-Key` 请求头：

```bash
curl -X POST http://localhost:8000/api/v1/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Idempotency-Key: 7c9e6679-7425-40de-944b-e07fc1f90ae7' \
  -H 'Content-Type: application/json' \
  -d '{"max_iterations":2}'
```

key 按 **actor** 作用域。同一个 key + 同一个 actor 第二次调用，返回**首次创建的那个 run**（202 + 该 run 的当前状态），不会再派发一次执行。

守卫是 `optimization_runs` 上的唯一索引 `uq_run_idempotency (idempotency_key, requested_by)`。`start_run` 是先查后插，两个并发调用可能都查不到，因此由索引在**插入时**仲裁：输的一方回滚，然后拿回赢家的 run。带 key 但没有 actor 的调用返回 422——`(key, NULL)` 在 SQL 里互不相同，约束会静默失效，所以宁可拒绝。

`background: false` 的重放不会 404。拿不到本地任务的调用方（并发重放、请求落到另一个副本）改为轮询数据库，直到 run 进入终态或超时。

**没有请求指纹校验。** 同一个 key 配不同 body 会返回首次的 run，而不是 409，所以不要跨不同载荷复用同一个 key。`idempotency_records` 表为指纹与响应体重放预留了完整结构（PK、`request_fingerprint`、`response_body`、`expires_at`），但目前没有写入者，详见 [08 限制与路线图](08-limitations-and-roadmap.md) §6.6。

其他会产生副作用的 `POST`（审批 / 执行 / 批量、创意、活动、告警）不读这个头，靠状态机防重复：重复 approve、重复 execute 都返回 409。

### 4.3 请求追踪

每个响应都带 `X-Request-ID`。如果请求里带了就透传，没带就生成。这个 id 会写进日志和 `audit_logs.request_id`，排障时用它把前端报错、后端日志、审计记录串起来。

### 4.4 限流

| 桶 | 默认 | 配置 |
|---|---|---|
| 通用（读） | 300 req/min | `RATE_LIMIT__DEFAULT_REQUESTS_PER_MINUTE` |
| 写操作 | 60 req/min | `RATE_LIMIT__WRITE_REQUESTS_PER_MINUTE` |
| 触发优化 run | 20 次/小时 | `RATE_LIMIT__OPTIMIZE_RUNS_PER_HOUR` |
| 突发 | 60 | `RATE_LIMIT__BURST` |

豁免路径：`/healthz`、`/livez`、`/readyz`、`/metrics`、`/favicon.ico`。

限流按**认证主体**（token 里的 sub）计；匿名请求按客户端 IP 计。超限返回 429 + `Retry-After`。

> 令牌桶状态存在进程内存。多副本部署时实际上限是 `limit × 副本数`。需要全局上限时见 [08 限制](08-limitations-and-roadmap.md)。

---

## 5. 端点全表

### 5.1 系统（免鉴权）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针，永远 200 |
| GET | `/readyz` | 就绪探针，聚合依赖健康；数据库不可达时 503 |
| GET | `/metrics` | Prometheus 文本格式，20 个指标 |
| GET | `/system/info` | 运行时配置摘要（只含可公开字段） |
| GET | `/` | 服务标识（不在 OpenAPI 里） |

`/readyz` 响应：

```json
{
  "status": "ok",
  "version": "1.0.0",
  "environment": "development",
  "uptime_seconds": 1234.5,
  "checks": {
    "database":     { "status": "ok", "dialect": "sqlite" },
    "cache":        { "status": "ok", "backend": "memory" },
    "llm":          { "provider": "mock", "model": "gpt-4o-mini", "status": "mock" },
    "orchestrator": { "mode": "langgraph", "status": "ok" },
    "platforms":    { "mode": "mock", "adapters": {...}, "status": "ok" }
  }
}
```

### 5.2 `/api/v1/auth`

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/auth/login` | 公开 | 换取 token 对 |
| POST | `/auth/refresh` | 持 refresh token | 轮换 refresh token |
| POST | `/auth/logout` | 已登录 | 撤销当前会话，204 |
| GET | `/auth/me` | 已登录 | 当前账号资料 |
| POST | `/auth/change-password` | 已登录 | 改自己口令，撤销其他所有会话，204 |
| POST | `/auth/logout-everywhere` | 已登录 | 撤销自己全部会话，204 |
| POST | `/auth/users` | `admin` 角色 | 新建账号 |
| GET | `/auth/users` | `admin` 角色 | 账号列表 |

### 5.3 `/api/v1/campaigns`

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/campaigns` | `campaign:read` | 列表，支持 status/platform/搜索/分页 |
| POST | `/campaigns` | `campaign:write` | 新建 |
| GET | `/campaigns/{campaign_id}` | `campaign:read` | 详情 |
| PATCH | `/campaigns/{campaign_id}` | `campaign:write` | 局部更新（预算、目标、状态等），写审计 |
| DELETE | `/campaigns/{campaign_id}` | `campaign:write` | 删除，级联删创意与日指标，写审计 |
| GET | `/campaigns/{campaign_id}/creatives` | `campaign:read` | 该活动的创意 |
| POST | `/campaigns/{campaign_id}/creatives` | `creative:write` | 新增创意 |
| PATCH | `/campaigns/{campaign_id}/creatives/{creative_id}` | `creative:write` | 改创意状态（draft/active/paused/rejected） |
| GET | `/campaigns/{campaign_id}/metrics` | `metrics:read` | 该活动的表现快照 |

### 5.4 `/api/v1/creatives`

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/creatives/summary` | `campaign:read` | 按来源（human/agent）与状态计数 |
| GET | `/creatives` | `campaign:read` | 跨活动检索，支持 campaign/status/origin/type 过滤 |

### 5.5 `/api/v1/runs`

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/runs` | `run:trigger` | 触发一次优化闭环，202 + run 对象 |
| GET | `/runs` | `run:read` | 运行列表，支持 status 过滤与分页 |
| GET | `/runs/{run_id}` | `run:read` | 详情：run + 事件时间线 + 动作 + 预算方案 + critic 裁决 |
| GET | `/runs/{run_id}/findings` | `run:read` | 该 run 的 critic 裁决（含被抑制提案与理由） |
| POST | `/runs/{run_id}/cancel` | `run:trigger` | 取消运行中的 run |
| GET | `/runs/{run_id}/stream` | `run:read` | **SSE**：先回放历史事件，再推实时事件 |

`POST /runs` 请求体：

```json
{
  "campaign_ids": ["camp_..."],      // 可选，省略 = 全部活动
  "max_iterations": 2,               // 1..10
  "window_days": 7,                  // 遥测窗口
  "background": true                 // false = 同步等待完成后返回
}
```

SSE 事件类型：`run.started`、`agent.started`、`agent.completed`、`agent.failed`、`run.succeeded`、`run.failed`、`run.cancelled`；流结束时额外发一条 `stream.closed`。每个事件带 `agent`、`payload`、`seq`，`seq` 可用作 `lastSeq` 断点续传。

流的数据来源是**持久化的 `run_events` 表**，进程内事件总线只作为低延迟尾流：每轮先从库里补齐游标之后的事件，再尾随总线；总线静默约 60 秒后回到库并重查 run 状态。因此 API 重启、或请求落到没有执行该 run 的副本上，流依然能正确补齐并正常结束，而不是无限发心跳。

```bash
curl -N http://localhost:8000/api/v1/runs/<RUN_ID>/stream -H "Authorization: Bearer $TOKEN"
```

### 5.6 `/api/v1/actions`

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/actions` | `run:read` | 提案列表，支持 status/type/campaign 过滤；**默认不含 `suppressed`** |
| GET | `/actions/{action_id}` | `run:read` | 单个提案 |
| POST | `/actions/{action_id}/approve` | `action:approve` | 批准，写审计 |
| POST | `/actions/{action_id}/reject` | `action:approve` | 驳回（可带理由），写审计 |
| POST | `/actions/{action_id}/execute` | `action:execute` | 执行已批准的动作，写审计 |
| POST | `/actions/bulk` | `action:approve` + `action:execute` | 批量批准/执行 |

动作类型（与 `ActionType` 枚举一致）：`adjust_budget`、`adjust_bid`、`pause_campaign`、`resume_campaign`、`pause_creative`、`resume_creative`、`refresh_creative`、`start_ab_test`、`stop_ab_test`、`expand_audience`。

其中 `refresh_creative`、`expand_audience`、`stop_ab_test` 是建议型动作：执行后只写本地状态与审计，不会推送到广告平台。

状态机：

```
proposed ──approve──▶ approved ──execute──▶ executed
    │                     │
    └──reject──▶ rejected └──execute(未批准)──▶ 409 approval_required

suppressed ──approve──▶ approved        （运维推翻 critic 的判定）
     │
     └──reject──▶ rejected
```

`suppressed` 是 critic 抑制掉的提案：**标记而非删除**，因此它是一条真实的行，运维可以查看理由并翻案。审批队列（`GET /actions` 不带 status）默认不返回它，需显式 `?status=suppressed` 查询；翻案时审计条目会带 `overruled_critic: true`。批量端点（`/actions/bulk`）只处理 `proposed`，不提供批量翻案。

`SECURITY__REQUIRE_ACTION_APPROVAL=false` 时，`proposed` 可直接 `execute`（**仅用于本地实验**）。

批量端点是**部分成功**语义：响应里逐条给出结果，权限不足会整体返回 403 `permission_denied`。

### 5.7 `/api/v1/alerts`

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/alerts` | `alert:read` | 列表，支持 status/severity/campaign 过滤 |
| GET | `/alerts/summary` | `alert:read` | 按严重度计数 |
| POST | `/alerts/{alert_id}/acknowledge` | `alert:ack` | 确认，写审计 |
| POST | `/alerts/{alert_id}/resolve` | `alert:ack` | 解决，写审计 |

严重度：`info` / `warning` / `critical`。状态：`open` / `acknowledged` / `resolved`。

告警规则（阈值可用 `OPTIMIZATION__*` 调）：CTR 低于 `alert_ctr_floor`、CPA 高于 `alert_cpa_ceiling`、ROAS 低于 `alert_roas_floor`，以及统计异常检测。样本量低于 `min_impressions_for_alerts` 时不告警，避免小样本噪声。同一 `dedup_key` 在窗口内只产生一条。

### 5.8 `/api/v1/analytics`

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/analytics/overview` | `metrics:read` | 头部 KPI + 待办数量 |
| GET | `/analytics/snapshots` | `metrics:read` | 每活动表现快照 |
| GET | `/analytics/timeseries` | `metrics:read` | 日粒度投放趋势 |
| GET | `/analytics/campaigns/{campaign_id}` | `metrics:read` | 单活动下钻 + 创意评分 |
| GET | `/analytics/llm-spend` | `system:read` | 按 provider/model 的模型花费 |
| GET | `/analytics/detect` | `metrics:read` | 只跑异常检测，不做完整优化 |

### 5.9 `/api/v1/ingest`

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/ingest/metrics` | `metrics:write` | 推一批日粒度指标，逐条报告下落 |
| GET | `/ingest/batches` | `metrics:read` | 采集批次台账（含干跑），`?source=` 可过滤 |
| GET | `/ingest/sources` | `metrics:read` | 已注册数据源及其当下可用性 |
| GET | `/ingest/schedule` | `metrics:read` | 定时拉取的**计划**与水位：每个源当下是 due / 已覆盖 / 在补 / 需要人工补数 |

`POST /ingest/metrics` 请求体：

```json
{
  "source": "warehouse.daily",
  "dry_run": false,
  "records": [
    {
      "platform": "google",
      "external_id": "ext_google_1000",
      "stat_date": "2026-09-08",
      "impressions": 12000,
      "clicks": 480,
      "conversions": 31,
      "cost": 912.4
    },
    { "campaign_id": "camp_01J8ZK...", "stat_date": "2026-09-08", "revenue": 4210.0 }
  ]
}
```

响应 **始终是 200**（即使有记录被拒）——批次被处理了，报告说明每条的下落。4xx 意味着**请求本身**不可用，这和"数据脏"是两件事，不该混为一谈：

```json
{
  "batch_id": "ing_01J8ZK...",
  "source": "warehouse.daily",
  "dry_run": false,
  "received": 2,
  "created": 1,
  "updated": 1,
  "rejected_count": 0,
  "unresolved_count": 0,
  "rejected": [],
  "unresolved": [],
  "issues_truncated": false,
  "window_start": "2026-09-08",
  "window_end": "2026-09-08"
}
```

`received == created + updated + rejected_count + unresolved_count`，服务在返回前会断言这一条（`IngestReportOut.reconciles()`）。对不上账的报告是运维最该怀疑的东西，所以它不允许离开这一层。

**校验分两层**，因为两种"错"对值班的人意味着不同的事：

| 层 | 谁拒 | 后果 | 含义 |
|---|---|---|---|
| 结构性 | Pydantic | **422，整批失败** | 生产端坏了：记录不是对象、`stat_date` 不是日期、出现了不认识的字段名 |
| 语义 | `services/ingest.py` | **逐条进 `rejected`/`unresolved`，其余照常落地** | 某一行坏了：负数、未来日期、什么都没测、活动对不上 |

`extra="forbid"` 是刻意的：某列被改名时宁可整批炸掉，也不要静默按 0 写进去——后者会让仪表盘看起来完全正常。

**三个必须知道的语义**

1. **缺列不覆盖。** 每个指标都可选，缺席 ≠ 0。广告平台不知道你的 revenue，电商 webhook 不知道你的 reach；不传（或 `null`）表示"这个源不测这一列"，写入时保留库里已有的值。传 `0` 才是"测了，结果是零"
2. **两种寻址不能矛盾。** 记录可用 `platform`+`external_id`，也可用 `campaign_id`，也可两者都给（数仓导出通常都有）。两者都给时必须指向同一个活动，否则记为 `unresolved`——静默选一个会掩盖生产端的映射漂移
3. **同批次槽位冲突不猜。** 一批里出现两条 `(活动, 创意, 日期)` 相同的记录，**两条都拒**，并在 `rejected` 里互相指名。last-write-wins 是对生产端意图的猜测

`dry_run: true` 不写任何指标行，但**仍然记一条批次台账**（`dry_run: true`），这样"演练过"与"数据源根本没来"可区分。

上限：单批 `MAX_BATCH_RECORDS = 5000` 条（一个事务）；`rejected` / `unresolved` 明细各截断到 200 条并置 `issues_truncated: true`，但**计数始终精确**。

**拉取不走 HTTP。** `GET /ingest/sources` 里 `configured: true` 的源用 CLI 或定时器拉（见 §7），因为一次拉取的耗时取决于平台响应速度，不该挂在请求上。

#### `GET /ingest/schedule`：为什么数字没更新

这是排障时第一个要看的端点。它**只计划、不拉取**，所以仪表盘轮询它不会启动任何工作。响应给出配置、每个已配置源的计划、水位与租约：

```json
{
  "enabled": false,
  "interval_minutes": 360,
  "lookback_days": 3,
  "max_catchup_days": 14,
  "dry_run": false,
  "lease_ttl_seconds": 1800,
  "today": "2026-09-09",
  "sources": [
    {
      "source": "platform",
      "registered": true,
      "configured": true,
      "plan": {
        "start": "2026-08-27",
        "end": "2026-09-09",
        "days": 14,
        "reason": "capped",
        "detail": "the gap starts 2026-08-11 but INGEST__MAX_CATCHUP_DAYS stops the window at 2026-08-27; run adoptimizer ingest --start 2026-08-11 --end 2026-08-26 to backfill the rest",
        "catchup_days": 11,
        "gap_days": 16,
        "covered": false
      },
      "watermark": {
        "window_start": "2026-07-01",
        "window_end": "2026-08-10",
        "batch_id": "ing_01J8ZK...",
        "received": 264,
        "dry_run": false,
        "updated_at": "2026-08-10T06:10:04Z",
        "lag_days": 30
      },
      "lease": {
        "name": "ingest:platform",
        "holder": "",
        "held": false,
        "expires_at": null,
        "last_outcome": "ran",
        "last_batch_id": "ing_01J8ZK...",
        "last_finished_at": "2026-08-10T06:10:04Z",
        "consecutive_failures": 0
      }
    }
  ]
}
```

`plan.reason` 是这个端点存在的全部理由——它回答"为什么"，而不只是"最后一次是几点"：

| `reason` | 含义 | 该做什么 |
|---|---|---|
| `first` | 这个源没有水位。窗口就是名义回溯期，**不会自己发明历史** | 需要更早的数据就显式跑 `adoptimizer ingest --start ... --end ...` |
| `covered` | 已存水位已经覆盖这个窗口，再拉只是重复断言 | 什么都不用做；这一轮会被跳过 |
| `nominal` | 水位与回溯期相接或重叠，重叠部分用来复核平台仍在修订的日子 | 正常状态 |
| `catchup` | 错过过运行，窗口回溯到水位结束的次日（仍在 `INGEST__MAX_CATCHUP_DAYS` 内） | 正常状态，会自愈 |
| `capped` | 缺口比 `INGEST__MAX_CATCHUP_DAYS` 更宽 | **必须人工补数**：`plan.detail` 里就是那条命令，`plan.gap_days` 是还欠几天 |

`lease.held` 说明"此刻是不是有人在拉"；`watermark.lag_days` 是最新已覆盖日与今天的差值，也是 `ingest_lag_days` 这个告警指标的来源。

`registered: false` 只会在 `INGEST__SOURCES` 写错名字时出现——它会被如实报出来，而不是被静默跳过。`registered: true` 但 `configured: false` 表示"装了但没凭据"（例如 mock 模式下的 `platform`），这两种状态必须能区分。

### 5.10 `/api/v1/admin`（全部需要 `admin` 角色）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/system` | 完整运行时配置（含不敏感的安全策略字段） |
| GET | `/admin/health` | 全依赖健康明细 |
| GET | `/admin/audit` | 审计流水，支持 actor/action/resource/时间范围过滤 |
| GET | `/admin/ab-tests` | 实验列表 |
| POST | `/admin/seed` | 灌确定性种子数据集 |
| PATCH | `/admin/users/{user_id}` | 改角色 / 启用停用账号 |
| POST | `/admin/prune` | 执行保留期清理（审计、事件、幂等记录等） |

`PATCH /admin/users/{user_id}`：

```json
{ "role": "analyst", "is_active": false }
```

至少要提供一个字段，否则 422。**安全护栏**：不允许停用或降级系统中最后一个在职 admin，会返回 409 `conflict`。角色变更会吊销该用户全部会话，使其立即以新权限重新登录。所有变更写审计。

---

## 6. curl 速查

```bash
BASE=http://localhost:8000/api/v1

# 登录并保存 token
TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"admin@adoptimizer.dev","password":"Adm1n!ChangeMe"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

AUTH="Authorization: Bearer $TOKEN"

# 看全部活动
curl -s "$BASE/campaigns?page_size=50" -H "$AUTH" | python -m json.tool

# 触发一次优化
curl -s -X POST $BASE/runs -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"max_iterations":2,"window_days":7}' | python -m json.tool

# 待审批动作
curl -s "$BASE/actions?status=proposed" -H "$AUTH" | python -m json.tool

# 批准 + 执行
curl -s -X POST $BASE/actions/<ACTION_ID>/approve -H "$AUTH"
curl -s -X POST $BASE/actions/<ACTION_ID>/execute -H "$AUTH"

# 批量批准
curl -s -X POST $BASE/actions/bulk -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"action_ids":["act_a","act_b"],"operation":"approve"}'

# 告警汇总
curl -s $BASE/alerts/summary -H "$AUTH"

# 审计流水（admin）
curl -s "$BASE/admin/audit?page_size=20" -H "$AUTH" | python -m json.tool

# 灌一批指标（先干跑，看会拒掉什么）
curl -s -X POST "$BASE/ingest/metrics" -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "source": "manual.backfill", "dry_run": true,
  "records": [{"platform":"google","external_id":"ext_google_1000","stat_date":"2026-09-08","cost":912.4}]
}' | python -m json.tool

# 采集台账与数据源可用性
curl -s "$BASE/ingest/batches?page_size=10" -H "$AUTH" | python -m json.tool
curl -s "$BASE/ingest/sources" -H "$AUTH" | python -m json.tool

# 定时拉取的计划与水位（只读，不会启动拉取）
curl -s "$BASE/ingest/schedule" -H "$AUTH" | python -m json.tool

# 健康与指标
curl -s http://localhost:8000/readyz | python -m json.tool
curl -s http://localhost:8000/metrics | head -40
```

---

## 7. CLI

除了 HTTP，所有常规运维操作都有对应命令，避免排障时临时拼 curl：

| 命令 | 说明 |
|---|---|
| `adoptimizer serve --host --port --reload --workers` | 起 API。**生产必须 `--workers 1`**（见架构文档 §4.1） |
| `adoptimizer migrate [--revision] [--offline]` | 应用迁移；`--offline` 只输出 SQL 供评审 |
| `adoptimizer revision --message "..."` | 从 ORM 元数据 autogenerate 迁移 |
| `adoptimizer seed` | 灌确定性种子数据集 |
| `adoptimizer ingest [--source NAME] [--days N 或 --start/--end] [--dry-run] [--file PATH]` | 灌日粒度指标：从已注册数据源拉，或推一个 JSON 文件（完整批次信封或裸记录数组都行）。**非空批次一条都没落地时退出码 1**，可直接挂 cron 告警 |
| `adoptimizer scheduler [--once] [--source NAME]... [--dry-run] [--force] [--interval N]` | 定时拉取指标。`--once` 跑一趟就退出（**CronJob 用的就是这个**），不加则按 `INGEST__INTERVAL_MINUTES` 常驻。**任一次 tick 失败、或拉到了行却一条没落地时退出码 1** |
| `adoptimizer run [--campaign ...] [--max-iterations N] [--window-days N] [--no-wait]` | 跑一轮优化并打印 summary |
| `adoptimizer healthcheck [--url]` | 探活；未就绪时退出码非 0，可直接用于部署脚本 |
| `adoptimizer token --email ...` | 取 access token（口令走隐藏输入），方便 curl |

---

## 8. OpenAPI

```bash
curl -s http://localhost:8000/openapi.json -o openapi.json
```

可用于生成任意语言的客户端。生产环境关闭了 `/docs` 但保留 `/openapi.json`；若连它也要关闭，把 `openapi_url` 也设为 `None`（`app.py::create_app`）。