# 架构决策记录（ADR）

记录那些"换一个选择也能跑，但换了之后系统性质会变"的决定。格式参考 Michael Nygard 的 ADR，状态只有三种：`Accepted`（生效中）、`Superseded`（被后续 ADR 取代）、`Deprecated`（不再适用）。

| 编号 | 标题 | 状态 |
|---|---|---|
| [0001](0001-custom-svg-charts.md) | 前端图表用手写 SVG，不引 recharts | Accepted |
| [0002](0002-in-process-run-dispatch.md) | 优化 run 用进程内 asyncio 任务派发 | Accepted |
| [0003](0003-session-revocation-on-request.md) | 每个请求校验会话存储 | Accepted |

## 新增 ADR

复制现有文件，编号递增，写清 **Context（为什么现在要决定）→ Decision（决定了什么）→ Consequences（好的和坏的都要写）**。坏的后果写不出来，说明还没想清楚。