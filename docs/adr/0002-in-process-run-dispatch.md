# ADR-0002：优化 run 用进程内 asyncio 任务派发

- 状态：Accepted
- 日期：2026-09-06
- 相关：`backend/src/adoptimizer/services/optimization.py`、`backend/src/adoptimizer/orchestrator/events.py`、`deploy/docker/Dockerfile.backend`

## Context

`POST /api/v1/runs` 要触发一次可能持续几十秒到几分钟的多智能体闭环。需要决定它在哪里执行：

1. **请求内同步执行** — 最简单，但 HTTP 连接会被长时间占用，网关/浏览器超时风险高。
2. **进程内 `asyncio.create_task`** — API 进程自己起后台任务。
3. **外部任务队列（arq / Celery / Dramatiq）** — 独立 worker 进程消费。

同时要满足：前端需要**实时**看到每个 Agent 的开始/结束与中间产物（SSE），并且断线重连不能丢事件。

约束条件：

- 项目要能"clone 下来就能跑"，不能强依赖一个必须先起好的 broker。
- 已有 Redis 作为 **LLM 响应缓存**后端，但它是**可选**的（`REDIS__ENABLED=false` 时降级为进程内缓存）。限流与事件总线本来就不走 Redis。
- 运维人力有限，多一个 worker 部署单元就多一份监控、日志、扩缩容配置。

## Decision

采用**方案 2**，并把持久化与实时推送拆开：

```
POST /runs ──▶ 写 optimization_runs 行（status=pending）并 commit
           └─▶ asyncio.create_task(execute_run(...))   # 进程内派发
           ◀── 立即返回 202 + run 对象

execute_run ──▶ mark_running
            ──▶ orchestrator.run(state, context)
                    每个事件 publish 到 EventBus
                        ├─▶ DatabaseEventSink → run_events 表（持久，权威）
                        └─▶ 进程内订阅队列（低延迟尾流）
            ──▶ mark_finished(status, summary, usage)

GET /runs/{id}/stream（SSE）
   循环：① 从 run_events 表补齐 cursor 之后的事件
        ② 若 run 已是终态 → 结束流
        ③ 否则尾随 EventBus；静默约 60s 后回到 ①
```

三个配套决定：

1. **派发始终发生**。请求体的 `background` 只决定调用方是否 `await wait_for(...)`，不决定是否执行。（早期实现里 `background=false` 会跳过派发，导致同步模式必然 `NotFoundError`。）
2. **容器镜像固定单 worker**。`Dockerfile.backend` 里 `UVICORN_WORKERS=1` 并在注释里说明原因；compose 与 k8s 清单同样保持单 worker。
3. **协作式取消**。`AgentContext.cancellation_check` 在每个 Agent 步骤之间查一次库里的 run 状态；一旦发现已被置为终态就抛 `RunCancelled`，`mark_finished` 也拒绝把已终态的 run 改写成 succeeded。这让"从另一个副本发起的取消"也能生效。

## Consequences

**正面**

- 零额外基础设施。`clone → pip install → adoptimizer serve` 就能跑完整闭环，这对评审、教学、单机部署都是决定性的。
- SSE 延迟极低（进程内队列，没有 broker 往返）。
- run 记录先落库再执行，进程崩溃时留下可被 `reap_stale_runs` 回收的持久记录，不会凭空消失。回收不只在启动时发生：lifespan 里还挂着 `run_reaper_loop`（默认 10 分钟一扫），所以某个副本崩了、别的副本还活着时，卡住的 run 也会被标记 failed，不用等下一次重启。
- 因为 SSE 以 `run_events` 表为权威源，**API 重启后重连依然能补齐完整时间线**。

**负面（必须正视）**

- **必须单 worker。** `--workers 2` 会让一半的 SSE 请求落到没有该 run 订阅者的进程上。虽然有了持久回放兜底（能补齐已落库事件、并在 run 终态后正常结束），但实时性会退化成"每 60 秒批量补一次"。
- **横向扩展要靠多副本**，且实时流体验依赖请求落到执行该 run 的副本。缓解手段：
  - 在 Ingress/负载均衡上开启会话亲和（`nginx.ingress.kubernetes.io/affinity: cookie`）
  - 或者接受"准实时"（≤60s 延迟）的持久回放
- **进程崩溃 = 正在跑的 run 丢失**，只能靠 reaper 标记为 failed 后重跑。没有 at-least-once 重试语义。reaper 的判据是"最后一次事件的时间"而不是创建时间，所以它不会误杀跑得慢但仍在推进的 run。
- **没有跨进程的并发上限**。`RATE_LIMIT__OPTIMIZE_RUNS_PER_HOUR` 是按主体的频率限制，不是全局在跑数量限制；`active_runs` 指标也只反映本进程。

**通往方案 3 的路已经铺好**

`pyproject.toml` 预留了 `worker = ["arq>=0.26"]` extra，`deploy/compose/docker-compose.yml` 有 `worker` profile，`execute_run` 的签名被刻意设计成"可以在任何进程里调用"（自己开 session、自己建 context、不依赖请求态）。切换到 arq 只需要：

1. `_dispatch` 改成 `await redis.enqueue_job("execute_run", run_id, ...)`
2. 加一个 `worker/` 模块消费该任务
3. `EventBus` 换成 Redis Pub/Sub 后端

`mark_finished` 的终态保护与协作式取消在那之后依然有用。

## 复审触发条件

- 需要 SLA 级别的"run 必达"（不能因进程重启丢失）
- 单副本吞吐成为瓶颈，且会话亲和不可用
- 需要跨 run 的全局并发控制或优先级队列