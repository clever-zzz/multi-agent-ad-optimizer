# LangGraph 入门教程（零基础版）

本教程介绍 **LangGraph** 是什么、核心概念有哪些，并给出**可直接运行**的最小示例；最后说明**在本项目中** LangGraph 如何编排 6 个 Agent 完成广告优化闭环。阅读前建议已读完《AI Agent 基础知识》并装好依赖（见《环境搭建教程》）。

> **版本说明**：第 7 节已与仓库当前代码（`backend/src/adoptimizer/orchestrator/graph.py`）逐行核对一致。早期 demo 的 `python/src/orchestrator/supervisor.py` 已随 demo 一起移除。

---

## 1. LangGraph 是什么

**LangGraph** 是 LangChain 生态里用于构建 **有状态、多步、可循环** Agent 工作流的库。你可以把它理解成：

- 用 **图（Graph）** 表示业务流程：圆点是步骤，箭头是流转方向。
- 每一步是一个函数（或可调用的 Agent），读入**共享状态**，返回**状态更新**。
- 支持 **条件分支**（根据状态走不同路径）和 **循环**（例如优化未达标则再来一轮）。

与「单次调用 LLM」相比，LangGraph 更适合 **Supervisor 模式**、**审批流**、**重试与人工介入** 等工程场景。

官方文档：搜索 `LangGraph documentation` 获取最新版。

---

## 2. 核心概念

### 2.1 StateGraph（状态图）

**StateGraph** 是你创建的「工作流画布」类。你要向它：

- `add_node`：添加节点（每一步做什么）
- `add_edge`：添加普通边（这一步做完必定去下一步）
- `set_entry_point`：指定从哪个节点开始
- `add_conditional_edges`：在某节点后根据函数返回值选路径
- `compile()`：编译成可执行的 `CompiledGraph`，再 `invoke` / `ainvoke` 运行

状态类型常见为 `dict` 或 `TypedDict`。**本项目踩过这个坑，结论是必须用显式 TypedDict**，详见 7.5 节。

### 2.2 Node（节点）

**节点**本质上是一个**函数**，签名类似：

```python
def my_node(state: dict) -> dict:
    # 读取 state["foo"]
    # 做计算或调用 LLM
    return {"bar": 123}   # 返回的键值按 reducer 策略合并进状态
```

节点也可以接收第二个参数 `config`（LangGraph 的 `RunnableConfig`），用来拿运行期上下文 —— 本项目正是这么做的，见 7.4 节。

### 2.3 Edge（边）

**普通边**：`graph.add_edge("a", "b")` 表示 `a` 执行完后**总是**进入 `b`。

### 2.4 State（状态）与 Reducer

**状态**是在整个图执行过程中传递的「共享黑板」：

```python
{
    "campaign_ids": ["c1", "c2"],
    "alerts": [...],
    "iteration": 0,
    "max_iterations": 2,
}
```

**Reducer（归约函数）是最容易被忽略、但最容易出 bug 的地方。** 它决定「节点返回的新值」如何与「通道里已有的值」合并：

- **默认行为**：新值**覆盖**旧值
- **累积行为**：需要显式绑定一个 reducer，例如「列表追加」「字典浅合并」「取最大值」

在 `TypedDict` 里用 `Annotated` 绑定：

```python
class AgentState(TypedDict, total=False):
    alerts: Annotated[list[dict], replace_list]      # 每轮重算，取最新
    optimization_actions: Annotated[list[dict], append_list]   # 跨轮累积
```

**不绑 reducer 的后果**：多轮迭代时，第二轮的结果会**覆盖**第一轮的，历史数据静默丢失。这是本项目早期 demo 的真实 bug，见 7.5 节。

### 2.5 Conditional Edge（条件边）

在某节点结束后调用一个**路由函数**，根据其返回值（字符串）选择下一个节点名或 `END`：

```python
graph.add_conditional_edges(
    "critic",
    route_fn,
    {"continue": "monitor", "end": END},
)
```

`route_fn(state)` 返回 `"continue"` 或 `"end"`。这是实现 **循环优化** 的关键。

### 2.6 END

**END** 是 LangGraph 提供的**终止符**，表示图执行结束。

### 2.7 Checkpointer（检查点）

`compile(checkpointer=...)` 可以传入一个检查点存储，让每一步之后的状态被快照下来，从而支持中断恢复、时间旅行调试。最简单的是内存版 `MemorySaver`。

**注意 `MemorySaver` 是进程内的** —— 进程重启，快照就没了。本项目用它，因此有一条明确的已知局限（见 7.6 节）。

---

## 3. 安装和配置

LangGraph 是后端的**核心依赖** —— 注意它列在 `dependencies` 里，不在 `[project.optional-dependencies]`。声明在 `backend/pyproject.toml`：

```toml
dependencies = [
  ...
  "langgraph>=0.2",
  "langchain-core>=0.3",
]

[project.optional-dependencies]
analytics    = ["cvxpy>=1.5"]
clickhouse   = ["clickhouse-connect>=0.7"]
```

安装（在 `backend/` 目录下）：

```bash
pip install -e ".[dev,analytics]"
```

确认版本：

```bash
python -c "import langgraph; from importlib.metadata import version; print(version('langgraph'))"
```

> 当前开发环境实测版本：**langgraph 1.2.11 / langchain-core 1.6.2**。声明的下限是 `>=0.2`，实际装到的是 1.x —— 这个跨度很重要，因为 1.x 对 `StateGraph` 的状态类型要求更严（见 7.5 节）。

下面第 4、6 节的示例**刻意不用 LLM**，保证离线可跑。若要接真实模型，需配置 `backend/.env` 里的 `LLM__PROVIDER` 与 `LLM__API_KEY`。

---

## 4. 最简单的两节点图（完整可运行代码）

保存为 `demo_two_nodes.py`，在装了 `langgraph` 的环境里执行 `python demo_two_nodes.py`：

```python
"""LangGraph 最小示例：两个节点串联，无 LLM，仅演示状态传递。"""

from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph


def last_value(_old: Any, new: Any) -> Any:
    return new if new is not None else _old


class DemoState(TypedDict, total=False):
    message: Annotated[str, last_value]
    step: Annotated[int, last_value]
    summary: Annotated[str, last_value]


def node_load(state: DemoState) -> dict:
    """模拟：加载任务描述。"""
    return {
        "message": state.get("message", "") + " [加载完成]",
        "step": state.get("step", 0) + 1,
    }


def node_summarize(state: DemoState) -> dict:
    """模拟：生成总结。"""
    return {
        "summary": f"步骤计数={state.get('step')}, 内容={state.get('message')}",
        "step": state.get("step", 0) + 1,
    }


def build_graph():
    graph = StateGraph(DemoState)
    graph.add_node("load", node_load)
    graph.add_node("summarize", node_summarize)

    graph.set_entry_point("load")
    graph.add_edge("load", "summarize")
    graph.add_edge("summarize", END)

    return graph.compile()


if __name__ == "__main__":
    app = build_graph()
    result = app.invoke({"message": "Hello", "step": 0})
    print("最终状态:", result)
```

**运行结果**：

```text
最终状态: {'message': 'Hello [加载完成]', 'step': 2, 'summary': '步骤计数=2, 内容=Hello [加载完成]'}
```

**你学到了什么**：

- 用**显式 `TypedDict`** 做状态（不是裸 `dict`，原因见 7.5）
- `set_entry_point` + `add_edge` 形成线性流水线
- `invoke(initial_state)` 一次性跑完（也有流式 API，进阶再用）

---

## 5. Supervisor Pattern 详解

**Supervisor（监督者）模式** 指：有一个**中心编排者**（或一张**中心图**）决定调用哪些「专家」Agent、顺序如何、是否重试。

在 LangGraph 里，Supervisor 常体现为：

1. 多个节点 = 多个专家步骤（监控、受众、创意、竞价、优化、复核）
2. 普通边构成**主流程**
3. 在**最后一个汇总节点**后接 **条件边**：若仍有未解决的问题且未超限 → 回到早期节点；否则 `END`

**优点**：

- 流程对工程师**可读、可测、可复现**
- 比「完全由 LLM 自由决定下一步」更**稳定**
- 路由、迭代上限、终止条件**集中在一处**

**缺点**：

- 图复杂后要注意**状态体积**和**无限循环**（务必设 `max_iterations` 与 `recursion_limit`）

本项目 `graph.py` 文件开头的 docstring 把三种编排方案的取舍写成了三行，值得直接引用：

```
- a pipeline cannot loop back after the optimizer finds new anomalies
- a swarm has no global view, so two agents can propose conflicting actions
- the supervisor owns routing, iteration limits and termination in one place
```

翻译：
- **纯流水线**不行 —— 优化器发现新异常后没法回头再跑一轮
- **纯蜂群（swarm）**不行 —— 没有全局视图，两个 Agent 会提出互相矛盾的动作
- **supervisor** 把路由、迭代上限、终止条件收在一处

**第二条正是本项目额外增加 critic 节点的理由。**

---

## 6. 条件路由和循环示例（扩展）

保存为 `demo_loop.py`：

```python
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph


def append_log(existing: list | None, incoming: list | None) -> list:
    """累积 reducer：日志跨轮追加，不覆盖。"""
    return [*list(existing or []), *list(incoming or [])]


def take_max(existing: int | None, incoming: int | None) -> int:
    """单调 reducer：计数只增不减。"""
    return max(existing or 0, incoming or 0)


class LoopState(TypedDict, total=False):
    n: Annotated[int, take_max]
    log: Annotated[list[str], append_log]


def work(state: LoopState) -> dict:
    n = state.get("n", 0) + 1
    return {"n": n, "log": [f"第{n}次工作"]}


def should_retry(state: LoopState) -> str:
    return "again" if state.get("n", 0) < 3 else "stop"


def build_loop_graph():
    g = StateGraph(LoopState)
    g.add_node("work", work)
    g.set_entry_point("work")
    g.add_conditional_edges("work", should_retry, {"again": "work", "stop": END})
    return g.compile()


if __name__ == "__main__":
    print(build_loop_graph().invoke({"n": 0, "log": []}))
```

输出：

```text
{'n': 3, 'log': ['第1次工作', '第2次工作', '第3次工作']}
```

**注意 `log` 有三条**。如果去掉 `Annotated[..., append_log]`，每轮都会覆盖上一轮，最后只剩 `['第3次工作']` —— 这就是 reducer 没接线的后果。真实项目里「再次进入」通常是回到**监控**或**第一个 Agent**，而不是空转回自己。

---

## 7. 在本项目中 LangGraph 是怎么用的

本节与 `backend/src/adoptimizer/orchestrator/graph.py`（308 行）逐行核对一致。

### 7.1 状态字典里有什么

`AgentState` 定义在 `backend/src/adoptimizer/orchestrator/state.py`，是一个显式 `TypedDict`，共 22 个通道。其中 **16 个累积/合并通道各自绑定了 reducer**；剩下 6 个是标量输入与标志位（`run_id`、`campaign_ids`、`window_days`、`max_iterations`、`current_agent`、`is_complete`），后值覆盖即正确，不需要 reducer：

| 通道 | reducer | 语义 |
|---|---|---|
| `new_creatives` | `append_list` | 跨迭代累积，不覆盖 |
| `optimization_actions` | `append_list` | 跨迭代累积（所以第 2 轮会看到第 1 轮的提案） |
| `critic_findings` | `append_list` | 复核裁决累积，含被抑制提案的 id |
| `agent_messages` | `append_list` | 完整轨迹 |
| `tool_preflights` | `append_list` | 预演记录累积 |
| `metrics` / `alerts` / `bidding_decisions` / `budget_allocations` | `replace_list` | 每轮重算，取最新 |
| `platform_checks` / `audience_observations` | `replace_list` | 同上 |
| `health` / `audience_insights` / `daily_budgets` / `usage` | `merge_mapping` | 字典浅合并 |
| `iteration` | `max_int` | 单调递增，**重放不会让进度回退** |

> 16 个通道里 13 个由 Agent 写；`daily_budgets` / `audience_observations` / `usage` 由服务层或编排器注入，Agent 走 `AgentContext` 读。这份"谁不写"的名单显式登记在 `state.py::UNWIRED_BY_AGENTS`，有测试守着——见 §7.5 的坑 3。

四个 reducer 的实现都在 `state.py` 顶部，每个只有几行：

```python
def replace_list(old, new):   return list(new) if new is not None else (old or [])
def append_list(old, new):    return [*list(old or []), *list(new)] if new is not None else list(old or [])
def merge_mapping(old, new):  return {**(old or {}), **(new or {})}
def max_int(old, new):        return max(old or 0, new or 0)
```

**为什么 `iteration` 要用 `max_int` 而不是默认覆盖**：如果某个节点被重放（checkpoint 恢复、重试），一个较旧的 `iteration` 值会把进度**倒退**，可能导致死循环。取最大值保证单调。

### 7.2 图的拓扑

```
entry ─▶ monitor ─▶ audience ─▶ creative ─▶ bidding ─▶ optimize ─▶ critic
                                                                     │
              ┌────────────── "continue" ────────────────────────────┤
              ▼                                                       │
           monitor                                             "end" ─▶ END
```

建图代码（`graph.py` 的 `_compile()`）：

```python
graph = StateGraph(AgentState)

for agent_name, agent in self._ordered_agents():
    graph.add_node(agent_name.value, self._node_for(agent))

graph.set_entry_point(AgentName.MONITOR.value)
graph.add_edge(AgentName.MONITOR.value,  AgentName.AUDIENCE.value)
graph.add_edge(AgentName.AUDIENCE.value, AgentName.CREATIVE.value)
graph.add_edge(AgentName.CREATIVE.value, AgentName.BIDDING.value)
graph.add_edge(AgentName.BIDDING.value,  AgentName.OPTIMIZE.value)
graph.add_edge(AgentName.OPTIMIZE.value, AgentName.CRITIC.value)
graph.add_conditional_edges(
    AgentName.CRITIC.value,
    self._route_after_critic,
    {"continue": AgentName.MONITOR.value, "end": END},
)

return graph.compile(checkpointer=self._checkpointer)
```

**条件边从 `critic` 出发，不是从 `optimize` 出发。** 这不是细节，是语义。`_route_after_critic` 的 docstring：

> Routing reads the **post-critic** state, so the loop continues only when **unreconciled anomalies** survive rather than when any rule fired.

即「是否再跑一轮」是在**提案被复核之后**决定的，判断依据是"还有没消化的异常"，而不是"有没有规则触发过"。

路由函数本身只有 6 行：

```python
@staticmethod
def _route_after_critic(state: AgentState) -> Literal["continue", "end"]:
    if state.get("is_complete"):
        return "end"
    iteration = int(state.get("iteration", 0) or 0)
    if iteration >= int(state.get("max_iterations", 3) or 3):
        return "end"
    return "continue" if state.get("alerts") else "end"
```

三道闸门：Agent 自己说完了 / 迭代预算用尽 / 没有告警了。

调用时还会设 `recursion_limit`：

```python
config = {
    "configurable": {"thread_id": context.run_id, CONTEXT_CONFIG_KEY: context},
    "recursion_limit": 6 * int(state.get("max_iterations", 3) or 3) + 8,
}
```

**`6` 就是 Agent 的数量** —— 每轮最多 6 个节点，加 8 的余量。这是防止 LangGraph 自身递归保护误杀正常长流程，同时保证异常情况下一定会停。

### 7.3 六个 Agent 的注册顺序

```python
def _ordered_agents(self) -> list[tuple[AgentName, BaseAgent]]:
    return [
        (AgentName.MONITOR,  self.monitor),
        (AgentName.AUDIENCE, self.audience),
        (AgentName.CREATIVE, self.creative),
        (AgentName.BIDDING,  self.bidding),
        (AgentName.OPTIMIZE, self.optimize),
        (AgentName.CRITIC,   self.critic),
    ]
```

**这一个方法同时被两条执行路径使用** —— LangGraph 建图时用它注册节点，顺序执行器回退时用它决定 await 顺序。所以两条路径的 Agent 顺序**不可能**不一致。

### 7.4 AgentContext 走 config，不烧进图

节点不是直接挂 `agent.run`，而是包了一层：

```python
CONTEXT_CONFIG_KEY = "__agent_context"

def _node_for(self, agent: BaseAgent) -> Any:
    async def _node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        configurable = config.get("configurable") or {}
        context = configurable.get(CONTEXT_CONFIG_KEY)
        if context is None:
            raise RuntimeError("Agent context was not supplied in the graph config")
        await context.raise_if_cancelled()
        return await agent.execute(state, context)

    _node.__name__ = str(agent.name.value)
    return _node
```

**为什么这么绕？** 因为 `AgentContext` 是**每次运行独有的**（含 `run_id`、配置、LLM 网关、事件总线、快照缓存）。如果把它烧进编译好的图，那么**一个图实例只能服务一次运行**。走 config 传，就能让**一个编译好的图服务所有并发 run**，Agent 保持无状态、可并发。

顺带一个容易踩的坑，代码里有注释专门说明：

> `RunnableConfig` must be a real runtime import: LangGraph reads the parameter annotation to decide whether a node accepts the config bag, and a node whose `config` is typed as anything else is invoked with the state alone.

即 `from langchain_core.runnables import RunnableConfig` **不能**放在 `if TYPE_CHECKING:` 里。LangGraph 会在运行时读取参数注解来判断要不要传 config；写成字符串注解或 `Any`，节点就只会收到 state，`config` 变成 `None`，然后你的 `context` 永远取不到。

`await context.raise_if_cancelled()` 让取消请求能在**节点边界**立刻生效，不用等整轮跑完。

### 7.5 三个真实的坑（两个在旧 demo，第三个是审出来的）

`state.py` 的文件 docstring 直接记下了前两个：

> Two properties matter for correctness here and both were broken in the demo:
>
> 1. The schema must be an explicit TypedDict. Passing a bare `dict` to StateGraph on **langgraph 1.x** gives every node only the previous node's return value, so downstream agents silently see an empty state.
> 2. Accumulating channels need real reducers. The demo defined merge helpers and **never wired them up**, so multi-iteration runs overwrote earlier findings.

**坑 1：裸 `dict` 传给 `StateGraph`。** 在 langgraph 0.x 下能凑合工作，升到 1.x 后每个节点只能看到**上一个节点的返回值**，看不到累积的共享状态。症状是下游 Agent 静默拿到空 state —— **不报错，只是结果为空**，非常难查。

**坑 2：reducer 定义了但没接线。** 旧 demo 写好了 `append_list` 之类的合并函数，却没有用 `Annotated` 绑到通道上。结果多轮迭代时后面的轮次覆盖前面的，第一轮的发现全部丢失。

**坑 3：通道声明了但没人写。** 比前两个更隐蔽 —— 通道**带着 reducer、有 `Annotated` 注解、在 `initial_state()` 里初始化成 `[]`**，一切看起来都对，但全仓没有任何代码往里面写过值，于是它永远读到空。表现像 Agent 的 bug，实际是缺一根线。

这个坑有两个实例，都在 2026-09-16 审出：

- `alert_fingerprints`：文档声称它保存"跨轮告警去重历史"，实际**全仓零读写**，已删除。它也不该存在 —— `_route_after_critic` 正是靠 `alerts` 非空来决定要不要再跑一轮，在内存里跨轮抑制告警会让 run 在第一轮就退出。跨轮去重落在库层（`AlertRepository.upsert_from_detection` 按 `dedup_key` 刷新而非插入）。
- `daily_budgets` / `audience_observations`：确实没被 Agent 写，但这是**有意的** —— 它们由服务层注入，Agent 走 `AgentContext` 读。这两个保留，并登记进 `state.py::UNWIRED_BY_AGENTS`。

三种坑的共同点是**静默失败**：不抛异常、不报错，只是结果不对。前两个靠 `state.py` 的 docstring 警示，第三个靠测试钉死：

```python
def test_every_reducer_channel_is_wired_or_declared_unwired() -> None:
    missing = set(_REDUCERS) - _channels_written_by_agents() - set(UNWIRED_BY_AGENTS)
    assert not missing, "no agent writes these, so they read as empty forever: " + ...
```

`_channels_written_by_agents()` 用 `ast` 静态扫描 `agents/*.py` 里的字典字面量键与下标赋值 —— 用静态扫描而不是跑一次 run，因为一次 run 只会走到它的数据恰好覆盖的分支，而这正是"没人写的通道"能在全绿测试里活下来的原因。

### 7.6 可选依赖与回退

```python
try:
    from langchain_core.runnables import RunnableConfig
    from langgraph.graph import END, StateGraph
    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False

try:
    from langgraph.checkpoint.memory import MemorySaver
    HAS_CHECKPOINTER = True
except ImportError:
    HAS_CHECKPOINTER = False
```

`langgraph` 没装（`HAS_LANGGRAPH` 为假）时，`self._graph` 为 `None`，自动切到 `_invoke_sequential`：

> **注意这里没有"图编译失败也降级"这回事。** `self._graph = self._compile() if HAS_LANGGRAPH else None`
> ——`_compile()` 外面**没有** try/except，所以图定义写错时编译异常会一路抛出去、进程根本起不来。
> 这是有意的：langgraph 在 `pyproject.toml` 里是**核心依赖**（不是 extra），编译失败是 bug，
> 静默降级成顺序执行器只会把它藏起来。降级路径针对的是"依赖装不上"，不是"代码写错了"。

```python
async def _invoke_sequential(self, state, context):
    current = dict(state)
    max_iterations = int(current.get("max_iterations", 3) or 3)

    for _ in range(max_iterations):
        for _agent_name, agent in self._ordered_agents():
            await context.raise_if_cancelled()
            update = await agent.execute(current, context)
            current = _apply_update(current, update)     # ← 用同一张 reducer 表合并

        if current.get("is_complete"):
            break
        if not current.get("alerts"):
            break
    return current
```

`_apply_update` 查的是模块级的 `_REDUCERS` 字典。这张表**不是手抄的**：它由 `_reducers_from_state()` 用 `get_type_hints(AgentState, include_extras=True)` 从 `Annotated` 元数据里自动派生，只挑出其中是普通函数的那一项（`list[dict[str, Any]]` 这种 typing 构造同样 callable，所以判据要比 `callable` 更窄）。派生结果是 16 条，与 `AgentState` 上绑定的 reducer 一一对应，两条路径**在构造上**就不可能对同一个通道有不同的合并语义。

手抄版本曾经付出过代价：它漏登记了 `usage`（该用 `merge_mapping`，实际退化成后值覆盖），而 `_apply_update` 对未登记的 key 静默走后值覆盖，所以这个 bug **只在降级路径上暴露**。现在有黄金表测试守着（`tests/unit/test_orchestrator_fallback.py`），第一次跑就把它抓了出来。

**业务结果一致**，只是失去 checkpoint 与可视化。`execution_mode` 属性会上报当前用的是哪个，`/readyz` 如实暴露：

```python
@property
def execution_mode(self) -> str:
    return "langgraph" if self._graph is not None else "sequential"
```

> **仍然存在的局限**：checkpointer 用的是 `MemorySaver`，**进程内存**，重启即失，所以一次 run 无法从断点续跑。内存占用倒是有界的——run 收尾时会调 `forget_run(run_id)` → `adelete_thread` 把该 thread 的快照丢掉，否则它会随进程存活期单调增长（全仓没有任何续跑路径，留着毫无用处）。

> **但 critic 的裁决已经单独落库了。** `critic_findings` 表存下每一条裁决（`kind`、`reason`、被抑制提案的原文 `suppressed_actions`、以及 `escalate` 标记）；被抑制的提案本身也没有消失，它以 `ActionStatus.SUPPRESSED` 留在 `optimization_actions` 里 —— **标记而非删除**。运维可以：
> - `GET /api/v1/runs/{run_id}/findings` 查看该 run 的全部裁决与理由；
> - 对 `suppressed` 的提案执行 `POST /actions/{id}/approve` **翻案**，审计条目带 `overruled_critic: true`。
>
> 分工是刻意的：**图状态不持久，图产出的决策持久**。持久化 checkpointer 会带来存储与清理成本，而真正需要被追问的是决策，不是中间状态。

### 7.7 建议阅读源码的顺序

1. `backend/src/adoptimizer/orchestrator/state.py` —— 状态通道 + 4 个 reducer + `surviving_actions()`（`graph.py::_reducers_from_state()` 会把它们派生成降级路径用的表）
2. `backend/src/adoptimizer/orchestrator/graph.py` —— `_ordered_agents` → `_node_for` → `_compile` → `_route_after_critic` → `_invoke_sequential`
3. `backend/src/adoptimizer/agents/base.py` —— `BaseAgent.execute()` 模板方法与 `AgentContext`
4. 具体 Agent：`monitor.py` → `optimize.py` → `critic.py`（复杂度递增）

---

## 8. 调试小贴士

- **看事件流，不要靠 print。** 每个 Agent 的进出都会发 `agent.started` / `agent.completed` 事件，payload 里带 `duration_ms`、`iteration`、`updated_keys`（这一步改了哪些通道）、`summary`、`messages`。起服务后开 `GET /api/v1/runs/{run_id}/stream`（SSE）就能实时看到。
- **看 `updated_keys`。** 如果某个 Agent 该写的通道没出现在它的 `updated_keys` 里，说明它根本没返回那个键。
- **循环不停止时**，检查三件事：`alerts` 是否被清空、`iteration` 是否递增（`max_int` reducer 是否接线）、`max_iterations` 是否合理。
- **下游 Agent 拿到空 state** → 高度怀疑 7.5 节的坑 1（状态类型不是显式 TypedDict）。
- **多轮迭代丢历史** → 高度怀疑 7.5 节的坑 2（累积通道的 reducer 没接线）。
- **节点里 `config` 是 None** → 检查 `RunnableConfig` 是不是被放进了 `TYPE_CHECKING` 块（7.4 节）。
- 版本升级后若 API 变更，以官方迁移文档为准。本项目声明 `langgraph>=0.2` 但实测跑在 1.2.x 上，跨大版本时尤其注意。

---

## 9. 小结

| 概念 | 本项目对应 |
|---|---|
| StateGraph | `StateGraph(AgentState)`，显式 TypedDict |
| Node | 6 个 Agent，经 `_node_for()` 包装成 `(state, config)` 签名 |
| Edge | `monitor → audience → creative → bidding → optimize → critic` |
| Conditional Edge | `_route_after_critic`，从 **critic** 出发回到 monitor 或 END |
| State | `AgentState`，22 个通道；16 个累积通道绑定 reducer，6 个标量通道后值覆盖 |
| Reducer | `replace_list` / `append_list` / `merge_mapping` / `max_int` |
| Checkpointer | `MemorySaver`（进程内，可选依赖） |
| 终止保护 | `max_iterations` + `recursion_limit = 6 × max_iterations + 8` |
| 降级 | `_invoke_sequential`，共用同一张（从 `AgentState` 自动派生的）reducer 表，业务结果一致，有等价性测试 |

- **LangGraph 用 StateGraph + 节点 + 边 + 条件边 + reducer** 表达有状态、可循环的 Agent 流程。
- **Supervisor Pattern** 在本项目中体现为 **固定六节点流水线 + critic 后的条件回路**。
- **reducer 是正确性的关键**，不是可选装饰 —— 本项目早期 demo 就因为没接线而在多轮迭代下丢数据。
- 本文提供了**无 LLM** 的可运行最小示例，你可以在此基础上逐步接入真实模型与工具调用。

下一篇建议阅读：《ClickHouse 实战教程》（数据侧）或《部署与运行指南》（跑起整个项目）。
