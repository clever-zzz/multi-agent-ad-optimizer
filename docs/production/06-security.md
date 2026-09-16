# 06 安全

这个系统能改广告预算，所以它的攻击面本质上是**"谁能花钱、花多少、事后能不能查"**。

---

## 1. 威胁模型

### 1.1 资产

| 资产 | 泄露/篡改后果 |
|---|---|
| 广告平台凭据（Google/Meta/TikTok） | 攻击者可以直接操控真实投放，花钱或窃取受众数据 |
| `SECURITY__JWT_SECRET` | 可伪造任意身份的 token，包括 admin |
| 用户口令哈希 | 离线爆破 |
| 投放数据（花费、ROAS、受众） | 商业机密 |
| 预算/出价配置 | 直接经济损失 |
| 审计日志 | 篡改后无法追责 |

### 1.2 主要威胁与现有缓解

| 威胁 | 缓解 | 位置 |
|---|---|---|
| 凭据暴力破解 | Argon2id + 失败计数 + 账号锁定（5 次 / 15 分钟） | `services/auth.py` |
| token 被盗后长期可用 | access token 30 分钟 + 每请求校验会话 + 登出/停用即时吊销 | `core/deps.py`，[ADR-0003](../adr/0003-session-revocation-on-request.md) |
| refresh token 重放 | 每次刷新**轮换**，旧 token 立即失效 | `services/auth.py` |
| 越权操作 | 14 权限 × 5 角色的 RBAC，路由级强制；前端那份副本由后端测试反向校验 | `core/security.py` |
| 管理员误操作把自己锁死 | 拒绝停用/降级最后一个在职 admin | `services/auth.py` |
| Agent 幻觉导致乱花钱 | 所有动作落 `optimization_actions`，默认需人工审批才执行 | `services/actions.py` |
| 模型供应商不可用/超支 | 重试 + 超时 + 月度预算护栏 + 可配置降级到 mock | `llm/gateway.py` |
| 重复提交造成双倍操作 | `POST /runs` 的 `Idempotency-Key`，由唯一索引 `uq_run_idempotency` 在插入时仲裁并发重放；审批/执行类端点靠状态机（重复 approve / execute 返回 409）。**无请求指纹校验**，见 [08 §6.6](08-limitations-and-roadmap.md) | `services/optimization.py`、`services/actions.py` |
| 无法追责 | 全量审计：actor、before/after、IP、UA、request_id | `services/audit.py` |
| 指标数据投毒（喂假数字，让优化器去乱调真实预算） | `metrics:write` 只给 admin 与专用的 `ingestor` 机器身份（已从 optimizer 收回，泄露的采集凭据动不了活动与动作）；每行盖 `source` + `batch_id`，可回溯到断言它的那个源与那一次采集；批次台账 + 审计条目；即使数字被污染，写平台仍受人工审批门约束 | `services/ingest.py`、`api/v1/ingest.py`、`services/actions.py` |
| XSS / 点击劫持 / MIME 嗅探 | CSP、`X-Frame-Options: DENY`、`nosniff`、Referrer/Permissions-Policy | `core/middleware.py` |
| 中间人 | HSTS（https 请求）、ingress 强制 TLS 重定向 | `core/middleware.py`、`deploy/k8s/ingress.yaml` |
| DoS | 固定窗口限流 + 请求体大小上限 + 请求超时 + gzip 最小尺寸 | `core/middleware.py` |
| 依赖库漏洞 | 生产镜像按 tag 固定、CI 构建校验 | `deploy/`、`.github/workflows/ci.yml` |
| 带病上线 | 生产环境启动即拒绝弱密钥/通配 CORS/SQLite | `core/config.py` |
| 容器逃逸 | 非 root(10001)、只读根文件系统、drop ALL capabilities、no-new-privileges、seccomp RuntimeDefault | `deploy/` |
| 内网横向移动 | NetworkPolicy 默认拒绝，只放行必要路径 | `deploy/k8s/networkpolicy.yaml` |
| 日志泄露敏感信息 | `SecretStr` 不进日志/响应；`log_request_body` 默认 false；`/system/info` 只暴露白名单字段 | `core/config.py` |

### 1.3 明确**未**覆盖的威胁

诚实列出，避免误判风险等级：

| 威胁 | 现状 |
|---|---|
| MFA / SSO | 未实现。企业部署应放在反向代理或身份提供方（见 §7） |
| CSRF | 未做专门防护。当前设计用 `Authorization: Bearer` 而非 Cookie 承载凭据，浏览器不会自动附带，因此经典 CSRF 不成立；**如果改成 Cookie 会话就必须补 SameSite + CSRF token** |
| 细粒度资源级授权 | 只有角色→权限，没有"某个用户只能管某几个活动"。多租户需要额外设计 |
| 数据加密（静态） | 依赖存储层（PostgreSQL 卷加密 / RDS 加密），应用层不做字段级加密 |
| 广告平台凭据的密钥轮换 | 凭据从环境变量读取，轮换 = 改配置 + 重启；未接入 Vault 类系统 |
| 速率限制的全局一致性 | 固定窗口计数在进程内，多副本下上限是 `limit × 副本数` |
| 审计日志的防篡改 | `audit_logs` 表可被有数据库权限的人修改；未做哈希链或 WORM 存储 |

---

## 2. 认证

### 2.1 口令

- **Argon2id**，参数可调：`argon2_time_cost=3`、`argon2_memory_cost_kib=65536`(64 MiB)、`argon2_parallelism=2`
- 最小长度 `password_min_length=10`
- 哈希串本身带参数，因此**未来提高成本因子后，旧哈希仍可验证**，可在下次登录时透明重算
- 登录失败与账号锁定返回同样的 401，不区分"口令错"和"账号锁定"，避免账号枚举

测试环境用 `argon2_time_cost=1, memory_cost=8192`，否则测试套件会慢到没法用（见 [07 测试](07-testing-and-ci.md)）。

### 2.2 Token

```
access token   JWT (HS256)，载荷含 sub / role / permissions / sid / iss / aud / exp / iat / jti
refresh token  落库为 refresh_sessions 行，可独立撤销
```

- `iss` 与 `aud` 参与校验，防止跨服务 token 混用
- `exp` / `iat` 校验，容忍少量时钟偏移
- 每次 `/auth/refresh` **轮换**：旧 refresh token 标记 `revoked_at`，返回新的一对

### 2.3 会话校验（每请求）

`core/deps.py::get_current_claims`：

```
1. 解析 Authorization: Bearer
2. 验签 + iss + aud + exp
3. SECURITY__VERIFY_SESSION_ON_REQUEST=true
   → 用 token 里的 sid 查 refresh_sessions，必须存在、未撤销、未过期
4. 加载用户，必须 is_active
5. 返回 TokenClaims
```

第 3 步是这个系统的安全基石。理由与代价见 [ADR-0003](../adr/0003-session-revocation-on-request.md)。

### 2.4 触发会话吊销的操作

| 操作 | 吊销范围 |
|---|---|
| `POST /auth/logout` | 当前会话 |
| `POST /auth/logout-everywhere` | 该用户全部会话 |
| `POST /auth/change-password` | 除当前会话外的全部会话 |
| `PATCH /admin/users/{id}` 改 `role` | 该用户全部会话 |
| `PATCH /admin/users/{id}` 设 `is_active=false` | 该用户全部会话 |

改角色时吊销会话是必要的：旧 token 里带着旧权限，不吊销就等于降权不生效。

---

## 3. 授权

### 3.1 权限矩阵

见 [03 API 参考 §2](03-api-reference.md#2-rbac-权限矩阵)。

设计原则：**路由断言权限，不断言角色**。

```python
dependencies=[require(Permission.CAMPAIGN_WRITE)]
```

而不是

```python
dependencies=[require_role(Role.ADMIN, Role.OPTIMIZER)]
```

这样调整角色→权限映射只需要改 `core/security.py::_ROLE_PERMISSIONS` 一处，不必翻遍所有路由。`/admin/*` 是例外：那些是运维操作，用 `require_role(Role.ADMIN)` 直接断言角色更明确。

前端 `stores/auth.ts` 里有一份矩阵副本（用来隐藏 API 会拒绝的控件）。副本必然漂移，所以 `tests/unit/test_security.py::TestFrontendMirror` 直接解析那个 TS 文件，逐角色比对权限集合，并校验 `types.ts` 里的 `Role` / `Permission` 联合类型与后端枚举一致——漂移在后端 CI 就红，不用等操作者在页面上撞 403。

### 3.2 最后一个 admin 护栏

`AuthService.set_active` 与 `set_role` 在执行前统计在职 admin 数量。如果目标是最后一个，返回 `409 ConflictError`。

这挡住的是一类真实事故：管理员在整理账号时把自己停用/降级，然后整个平台没有任何人能管账号。

### 3.3 前端权限只是 UX

`ProtectedRoute` 与按 `permissions` 数组隐藏按钮，**都不是安全边界**。真正的强制在后端每个路由的 `require(...)`。前端隐藏按钮只是为了不让用户点了才看到 403。

---

## 4. 输入与输出安全

| 面 | 措施 |
|---|---|
| 请求体 | Pydantic 模型全量校验；`max_request_body_bytes=2 MiB` 上限；`email-validator` 校验邮箱 |
| 查询参数 | `Annotated[..., Query(ge=..., le=...)]` 显式约束；分页 `page_size` 上限 200 |
| SQL | 全部通过 SQLAlchemy 参数化，无字符串拼接 SQL |
| 路径 | 无用户可控的文件路径操作 |
| 命令 | 无 `subprocess` 调用用户输入 |
| 响应 | DTO（`schemas/`）与 ORM 模型分离，不会意外泄露 `hashed_password` 等字段 |
| 错误 | 统一 problem document，`detail` 不暴露堆栈、SQL、内部路径 |
| LLM 输出 | 结构化输出经 Pydantic 校验；不合法则回退确定性规则生成器，**不会把模型的自由文本直接写库或执行** |
| 指标入口 | `extra="forbid"`：未知列名整批 422，某列被改名时不会静默按 0 写入；`source` 受正则约束（小写短名），因为它同时是 Prometheus 标签，自由文本会撑开序列基数；单批 5000 条上限；`rejected` / `unresolved` 明细截断到 200 条但计数精确 |
| SSRF | 广告平台适配器的 base URL 来自配置，不接受用户输入 |

关于 LLM 的一个关键设计：**Agent 只能"提案"，不能"执行"**。

```
Agent ──▶ optimization_actions（status=proposed）
                    │
             人工审批（action:approve）
                    │
             执行（action:execute）──▶ 广告平台适配器
```

`SECURITY__REQUIRE_ACTION_APPROVAL=true` 时，未审批的动作执行返回 409 `approval_required`。这意味着即使 prompt injection 让模型输出了"把所有预算调到 100000"，它也只会变成一条等人审批的提案。

同一个门也挡住了 critic 抑制掉的提案：它们以 `status=suppressed` 落库，审批队列默认不返回，
需要 `action:approve` 权限的人显式 `?status=suppressed` 查出来再 approve 才能翻案。
翻案**必须留痕**——审计条目带 `overruled_critic: true`，所以"谁在什么时候推翻了机器的判断"是可查的。
批量端点只处理 `proposed`，不提供批量翻案：绕过 critic 应当是一次有意识的单条决定，而不是一次 `for` 循环。

---

## 5. 密钥管理

### 5.1 需要什么

| 密钥 | 用途 | 轮换影响 |
|---|---|---|
| `SECURITY__JWT_SECRET` | 签发/校验 JWT | 轮换后所有现存 access token 失效，用户需重新登录 |
| `SECURITY__BOOTSTRAP_ADMIN_PASSWORD` | 空库首次启动建 admin | 只在第一次生效，之后可改可删 |
| `POSTGRES_PASSWORD` / `DATABASE__URL` | 数据库 | 需与应用同步更新 |
| `REDIS_PASSWORD` | LLM 响应缓存 | 同上 |
| `LLM__API_KEY` | 模型供应商 | 无用户影响 |
| `GOOGLE_ADS_*` / `META_*` / `TIKTOK_*` | 广告平台 | 无用户影响 |

### 5.2 规则

1. **一律走环境变量/Secret，绝不进 Git。** `.gitignore` 已排除 `.env`、`*.env.local`；`deploy/k8s/secret.example.yaml` 故意不在 `kustomization.yaml` 里。
2. **`SECURITY__JWT_SECRET` 用 `secrets.token_urlsafe(48)` 生成**，每个环境一个，不复用。
3. **生产硬化校验兜底**：弱密钥 + `APP__ENVIRONMENT=production` = 拒绝启动。
4. **轮换 JWT 密钥的正确姿势**（避免全员掉线）：
   - 目前只支持单密钥，轮换会导致所有 access token 立刻失效
   - 选择低峰期，提前通知；用户重新登录即可（refresh 也会失效）
   - 如果需要无损轮换，得先实现密钥 kid + 多密钥并存验签，这在路线图里
5. **轮换数据库口令**：先改 PostgreSQL，再改 Secret，再滚动重启 API。中间会有短暂连接失败，`/readyz` 会反映出来。
6. **`SecretStr` 类型**保证密钥不会出现在 `repr()`、日志或错误信息里。`/admin/system` 与 `/system/info` 只返回 `Settings.public_dict()` 的白名单字段。

### 5.3 检查有没有泄露

```bash
# 仓库里不该有任何真实密钥
git log --all -p -S 'SECURITY__JWT_SECRET=' -- . | grep -v 'dev-only-insecure' | head

# 工作区不该有未忽略的 .env
git status --ignored --short | grep -E '\.env$'
```

---

## 6. 应急响应

### 6.1 怀疑 JWT 密钥泄露

```bash
# 1. 立刻生成新密钥并更新 Secret
python -c "import secrets;print(secrets.token_urlsafe(48))"

# 2. 滚动重启，使所有旧 token 失效
kubectl -n adoptimizer rollout restart deploy/backend

# 3. 吊销所有会话（数据库层）—— 让 refresh token 也全部失效
#    通过 API：每个受影响用户调 /auth/logout-everywhere
#    或直接：UPDATE refresh_sessions SET revoked_at = now() WHERE revoked_at IS NULL;

# 4. 通知所有用户重新登录

# 5. 查审计日志，评估泄露窗口内发生了什么
curl -s ".../api/v1/admin/audit?page_size=200" -H "Authorization: Bearer $NEW_ADMIN_TOKEN"
```

### 6.2 怀疑某个账号被盗

```bash
# 停用（会立即吊销其所有会话）
curl -X PATCH ".../api/v1/admin/users/<USER_ID>" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"is_active": false}'

# 查这个账号做过什么
curl -s ".../api/v1/admin/audit?actor_id=<USER_ID>&page_size=200" -H "Authorization: Bearer $ADMIN_TOKEN"

# 重点看 resource_type=optimization_action 的 approve/execute 记录
```

### 6.3 Agent 做了错误的预算调整

```bash
# 1. 找到相关 run
curl -s ".../api/v1/runs?page_size=20" -H "Authorization: Bearer $TOKEN"

# 2. 看它提了哪些动作、谁批的、执行了没有
curl -s ".../api/v1/runs/<RUN_ID>" -H "Authorization: Bearer $TOKEN"

# 3. 已执行的动作需要人工在广告平台侧回滚
#    before_value / after_value 字段记录了变更前的值，照着改回去
# 4. 未执行的动作直接 reject
curl -X POST ".../api/v1/actions/<ACTION_ID>/reject" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"reason":"rolled back after incident"}'
```

> `optimization_actions.before_value` 存在的意义就是这个：它让"改回去"有据可依，而不是靠记忆。

### 6.4 临时止血

需要立刻停止所有自动化操作时：

```dotenv
SECURITY__REQUIRE_ACTION_APPROVAL=true    # 确认是 true
RATE_LIMIT__OPTIMIZE_RUNS_PER_HOUR=1      # 掐住触发频率
LLM__PROVIDER=mock                        # 停止真实模型调用与花费
```

或者直接把 `/runs` 的触发权限收回（临时把所有 optimizer 降为 analyst——注意别把最后一个 admin 也降了）。

---

## 7. 加固清单

### 7.1 部署前必做

- [ ] `SECURITY__JWT_SECRET` 为新生成的随机值，≥32 字符
- [ ] `SECURITY__BOOTSTRAP_ADMIN_PASSWORD` ≥12 字符，且首次登录后已改
- [ ] `DATABASE__URL` 指向 PostgreSQL，口令非默认
- [ ] `REDIS__URL` 带口令
- [ ] `APP__ENVIRONMENT=production`（会关闭 `/docs`、`/redoc` 并触发硬化校验）
- [ ] `APP__CORS_ALLOW_ORIGINS` 是明确的 https origin，无 `*`
- [ ] `APP__TRUSTED_HOSTS` 是明确主机名
- [ ] 全站 TLS，HSTS 生效
- [ ] `SECURITY__REQUIRE_ACTION_APPROVAL=true`
- [ ] `SECURITY__VERIFY_SESSION_ON_REQUEST=true`
- [ ] 数据库端口不对公网暴露
- [ ] NetworkPolicy 已应用
- [ ] 已创建至少一个非引导的 admin，并停用了引导账号

### 7.2 建议做

- [ ] 在 ingress 层加 WAF 或至少 IP 白名单（内部操作台通常不需要公网可达）
- [ ] 接 SSO/OIDC：把认证委托给企业身份提供方，本系统只保留授权与审计
- [ ] 日志与审计导出到独立的、应用账号无写权限的存储
- [ ] `LLM__FAIL_OPEN_TO_MOCK=false` + 明确的失败告警（如果你要求"必须用真实模型的结果"）
- [ ] 定期依赖漏洞扫描（`pip-audit`、`npm audit`、镜像扫描）纳入 CI
- [ ] 数据库静态加密（RDS/卷加密）
- [ ] 备份加密 + 恢复演练

### 7.3 渗透测试关注点

给外部测试人员的提示，按预期产出排序：

| 方向 | 具体测试 |
|---|---|
| 授权 | 用 viewer token 打全部写端点；用 optimizer token 打 `/admin/*` 与 `POST /ingest/metrics`；用 ingestor token 打 `/campaigns`、`/runs`、`/actions/*`（应全部 403）；改 JWT 里的 `role`/`permissions` 字段（应被签名拦住） |
| 会话 | 登出后继续用旧 access token；停用账号后用旧 token；改角色后用旧 token |
| 审批门 | 未审批直接 `POST /actions/{id}/execute`；重复执行同一动作；approve 后再 reject |
| 幂等 | 同一 `Idempotency-Key` 配不同 body（当前返回首次的 run，不是 409——这是已知缺口）。「并发同 key」已自动化：`test_concurrent_replays_create_exactly_one_run` |
| 注入 | 活动名/创意文案/审计过滤参数里的 SQL 与 XSS 载荷（后者要看前端是否转义） |
| 限流 | 登录爆破；`/runs` 触发频率；限流是否按主体而非按 IP |
| LLM | 在创意文案、活动名里塞 prompt injection，看能否让 Agent 提出异常动作（预期：只能产生提案，且被审批门拦住） |
| 信息泄露 | 错误响应是否带堆栈；`/system/info` 是否泄露密钥；生产 `/docs` 是否关闭 |
| SSE | 未授权订阅他人 run；`lastSeq` 传负数/超大值 |
| 竞态 | 并发审批同一动作；并发触发多个 run；并发写同一 `daily_metrics` 槽位 |

---

## 8. 合规与审计

### 8.1 审计记录内容

每次状态变更写 `audit_logs`：

| 字段 | 内容 |
|---|---|
| `actor_id` / `actor_email` / `actor_role` | 谁（角色也记，因为角色会变） |
| `action` | 做了什么（`campaign.updated`、`action.approved`、`user.updated` 等） |
| `resource_type` / `resource_id` | 对什么 |
| `before` / `after` | 变更前后（JSON） |
| `ip_address` / `user_agent` | 从哪 |
| `request_id` | 与日志串联 |
| `created_at` | 何时 |

查询：`GET /api/v1/admin/audit`，支持按 actor、action、resource、时间范围过滤。仅 `admin` 角色可访问（`audit:read` 权限 + `require_role(ADMIN)`）。

### 8.2 保留期

```bash
POST /api/v1/admin/prune?audit_days=365     # audit_days 范围 30–3650
```

同时清理过期的 `idempotency_records`。

> 合规要求通常规定审计日志的**最短**保留期。`audit_days` 的下限设为 30 是为了防止误操作把审计清空；如果你的合规要求是 7 年，就设 2555 并确认磁盘容量。
>
> 注意 `run_events` 目前不受 prune 覆盖，见 [05 运维手册 §5](05-operations-runbook.md#5-容量与数据增长)。

### 8.3 数据主体权利

系统存储的个人数据只有操作者账号（email、姓名、口令哈希、登录 IP/UA）。投放数据与受众数据是聚合的业务指标，`target_audience` 是描述性文本而非个人数据。

如果接了真实广告平台并回传受众级数据，需要重新做数据分类与 DPIA。

---

## 9. 相关文档

- [ADR-0003 每请求校验会话](../adr/0003-session-revocation-on-request.md)
- [ADR-0002 进程内派发与单 worker 约束](../adr/0002-in-process-run-dispatch.md)
- [03 API 参考 §2 权限矩阵](03-api-reference.md#2-rbac-权限矩阵)
- [04 部署 §5 启动期硬化校验](04-deployment.md#5-启动期硬化校验)
- [08 限制与路线图](08-limitations-and-roadmap.md)