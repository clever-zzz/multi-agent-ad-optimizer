# ADR-0003：每个请求校验会话存储

- 状态：Accepted
- 日期：2026-09-06
- 相关：`backend/src/adoptimizer/core/deps.py`、`backend/src/adoptimizer/services/auth.py`、`SECURITY__VERIFY_SESSION_ON_REQUEST`

## Context

系统用无状态 JWT 作为 access token（默认 30 分钟），refresh token 落库为 `refresh_sessions` 行、可单独撤销。

无状态 JWT 的经典问题：**签出去就无法收回**。于是这些操作在 access token 过期前都是无效的：

- 用户点"登出"后，token 仍然能用最多 30 分钟
- 管理员停用某个离职员工的账号，该员工仍能继续操作最多 30 分钟
- 管理员把某人的角色从 admin 降为 viewer，旧 token 里的权限依然生效
- 用户改密码（通常意味着"其他设备都该下线"）

对一个能改广告预算的系统来说，"离职员工还能操作 30 分钟"是不可接受的。

候选方案：

1. **缩短 access token TTL**（例如 5 分钟）— 缩小窗口但没消除，且增加 refresh 频率。
2. **token 黑名单** — 只在登出时写入，但停用账号/改角色时需要枚举该用户所有未过期 token，做不到。
3. **每个请求校验会话存储** — 白名单而非黑名单。
4. **完全有状态的 session cookie** — 放弃 JWT，改动面太大且不利于 CLI/脚本调用。

## Decision

采用**方案 3**，由 `SECURITY__VERIFY_SESSION_ON_REQUEST`（默认 `true`）控制。

`core/deps.py::get_current_claims` 在验完 JWT 签名、`iss`、`aud`、`exp` 之后，额外做一次：

```python
session = await RefreshSessionRepository.get_valid(session_id)   # 未撤销且未过期
if session is None:
    raise AuthenticationError(...)
```

配套的写路径全部主动吊销会话：

| 操作 | 效果 |
|---|---|
| `POST /auth/logout` | 撤销当前会话 |
| `POST /auth/logout-everywhere` | 撤销该用户全部会话 |
| `POST /auth/change-password` | 撤销除当前会话外的全部会话 |
| `PATCH /admin/users/{id}` 改角色 | 撤销该用户全部会话（强制以新权限重新登录） |
| `PATCH /admin/users/{id}` 停用 | 撤销该用户全部会话 |

还有一条独立的安全护栏：`AuthService.set_active` / `set_role` 拒绝停用或降级**系统中最后一个在职 admin**，返回 409。否则一次误操作就能把整个平台锁死在无人可管理的价格。

## Consequences

**正面**

- 登出、停用、改角色、改密码**立即生效**，不等 token 过期。这是能通过企业安全评审的前提。
- 白名单语义：默认拒绝。新增一种"应该下线"的场景时，只要吊销会话就自动生效，不需要在黑名单逻辑里补代码。
- 与"最后一个 admin 护栏"一起，把两类高危误操作（越权残留、把自己锁死）都堵住了。

**负面**

- **每个认证请求多一次存储查询。** 用 Redis 时约 0.2–0.5ms；落到 PostgreSQL 时约 1–2ms。对一个 QPS 不高的内部操作台可以接受，对高并发公开 API 不行。
- JWT 不再是真正"无状态"的。水平扩展时所有副本必须能访问同一个会话存储（本来 refresh 也需要，所以没有引入新依赖）。
- 会话存储成为可用性依赖：它挂了，所有认证请求都会 401。`/readyz` 会反映这一点。

**逃生阀**

`SECURITY__VERIFY_SESSION_ON_REQUEST=false` 可以关掉校验，退化为纯无状态 JWT（撤销只在 refresh 时生效）。仅建议在只读、低风险、且延迟敏感的部署里使用，并在关闭时同步把 `SECURITY__ACCESS_TOKEN_TTL_MINUTES` 调到 5 以下。生产 compose 覆盖层与 k8s ConfigMap 都显式把它设为 `"true"`，避免有人无意中关掉。

## 复审触发条件

- API QPS 上到会话存储查询成为可测量的瓶颈（届时优先考虑 Redis + 短 TTL 缓存，而不是直接关掉校验）
- 引入服务间调用的 machine-to-machine token（那类 token 没有"会话"，需要单独的撤销机制）