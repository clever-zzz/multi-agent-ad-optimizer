# AI Agent 基础知识（零基础版）

本教程用**通俗语言**解释什么是 AI Agent、它由哪些部分组成，以及在**本项目**里 Agent 具体是怎么落地的。不要求你事先会写代码，文末的伪代码仅帮助建立直觉。

> **版本说明**：本文对应仓库当前落地版本（`backend/src/adoptimizer/`）。早期的三语言教学 demo 已移除。
> 想看权威的架构说明，请直接读 [docs/production/02-architecture.md](../production/02-architecture.md)。

---

## 1. 什么是 AI Agent（通俗解释）

你可以把 **AI Agent（智能体）** 想象成一个「会动脑、会动手的小助手」：

- **动脑**：它背后可以接一个大语言模型（类似 ChatGPT），能理解情况、做推理、生成自然语言。
- **动手**：它不仅「说完就算了」，还可以按设计去**调用工具**——查数据库、调广告平台 API、执行一段计算。

和普通「一问一答的聊天机器人」相比，Agent 更强调 **目标导向**：为了完成一个任务，它可能**多步思考**、**多次调用工具**、**记住中间结果**，直到任务结束或达到停止条件。

一句话：**Agent = 明确目标 + 可选的大模型 + 可选记忆 + 可调用的工具 + 执行策略（规划/循环）**。

注意上面「可选」两个字很重要 —— **不接大模型的 Agent 也是 Agent**。本项目就是最好的例子，见第 7 节。

---

## 2. Agent 的四大要素

专业讨论里常提到四个关键词：**LLM、Memory、Tools、Planning**。下面先讲通用含义，再对照本项目的真实做法。

### 2.1 LLM（大语言模型）

**通用作用**：负责「理解和生成自然语言」，以及在一定程度上做推理。

**本项目的做法**：**LLM 是可选的，而且只用在该用的地方。**

6 个 Agent 里只有 2 个真的调 LLM：

| Agent | 调 LLM？ | 干什么 |
|---|---|---|
| monitor | ❌ 否 | 纯规则 + 统计：算指标、查阈值、出告警 |
| audience | ✅ 是 | 生成受众洞察的自然语言摘要 |
| creative | ✅ 是 | 生成广告文案变体（带结构化校验） |
| bidding | ❌ 否 | 纯 `domain.pricing` 数学 |
| optimize | ❌ 否 | 纯规则映射：告警 → 补救动作 |
| critic | ❌ 否 | 纯规则：提案冲突裁决 |

这条取舍在架构文档里写得很明确：

> **LLM 只用在"需要生成或归纳自然语言"的地方，所有涉及金钱的数学都在 `domain/` 里用确定性代码算。** 模型可以影响"怎么解释这个决定"，但不能凭空决定"预算改成多少"。

### 2.2 Memory（记忆）

**通用作用**：让 Agent 记住对话历史、任务中间结果。

**本项目的做法**：三层，各司其职。

| 层 | 载体 | 生命周期 | 存什么 |
|---|---|---|---|
| 单次运行内 | LangGraph **state 通道** | 一轮 run | 指标、告警、提案、裁决（每个通道绑定一个 reducer，见《LangGraph 入门》） |
| 单次运行内 | `MemorySaver` checkpoint | 进程内，**重启即失** | LangGraph 的状态快照 |
| 跨运行 | **数据库**（SQLite / PostgreSQL） | 持久 | 提案（含被 critic 抑制的 `suppressed`）、critic 裁决 `critic_findings`、告警、预算分配、工具调用、事件日志 |

**没有向量数据库、没有长期语义记忆** —— 因为广告优化的决策依据是时序指标和规则阈值，不是"回忆相似案例"。

### 2.3 Tools（工具）

**通用作用**：把想法落到真实世界：查数、改数、发请求。

**本项目的做法**：工具在 `tools/spec.py` 里**声明**（名字、参数 schema、是否只读、是否写操作），由 `tools/executor.py` **统一执行**。执行器负责：

- 参数校验（不合 schema 直接拒绝）
- **幂等键**（同一个键的重复调用返回已记录的结果，不重复执行）
- **dry-run 预演**（写操作可以先"彩排"一遍，看平台会不会拒绝）
- 预算与速率上限
- 每次调用（**包括被拒绝的**）都发一条 `tool.invoked` 事件落库，可审计

当前注册了 7 个工具，其中读类的如 `platform.campaign_report`，写类的如 `platform.pause_campaign`、`platform.set_daily_budget`。

### 2.4 Planning（规划）

**通用作用**：决定先做哪步、后做哪步，是否循环重试，何时结束。

**本项目的做法**：**没有自由规划，用的是显式的图。**

- 固定流水线：`monitor → audience → creative → bidding → optimize → critic`
- critic 之后一条**条件边**：还有未消化的告警、且迭代预算没用完 → 回到 monitor 再跑一轮；否则结束

不是让 LLM 临场决定"下一步调用谁"。这是有意的：流程对工程师**可读、可测、可复现**。

---

## 3. 单 Agent vs 多 Agent

### 3.1 单 Agent

一个 Agent 包揽所有事：看数据、想策略、调工具、写总结。

- **优点**：结构简单，调试集中。
- **缺点**：角色混杂时提示词容易臃肿；复杂业务难以分工；一个环节出错整条链都受影响。

### 3.2 多 Agent

多个 Agent **各司其职**。本项目的 6 个：

- **monitor（监控）**：拉指标、算 KPI、跑异常检测规则、出告警；还会挑最严重的几条回查广告平台"你同意我的判断吗"
- **audience（受众）**：人群维度分析，输出洞察
- **creative（创意）**：生成素材/文案变体
- **bidding（竞价）**：在约束下算出价建议
- **optimize（优化）**：把上面所有输出**翻译成具体提案**（调预算、调出价、暂停活动、暂停素材、开 A/B 测试），并对每条写操作提案做 dry-run 预演
- **critic（复核）**：提案进人工审批队列**之前**做全局裁决 —— 同一个活动不会同时收到"暂停"和"加预算"，跨迭代重复的同一意图只留一条，平台预检判定为阻塞的直接不进队列。**被抑制的提案不删除**：它以 `suppressed` 状态落库，裁决理由落在 `critic_findings` 表里，运营查得到、也能翻案。唯一一种 critic 不肯替人决定的情况是"同一活动既被提议抬价又被提议砍预算"——两边参照系都成立，它只标记 `opposing_spend_intent` 交人二选一

再由 **编排层（Orchestrator / Supervisor）** 决定调用顺序与循环条件。

- **优点**：边界清晰，便于扩展和测试。
- **缺点**：需要设计**状态传递**和**冲突解决**。

**critic 这个 Agent 存在的理由，正是"冲突解决"这条缺点。** 编排层的 docstring（`backend/src/adoptimizer/orchestrator/graph.py` 开头）把三种方案的取舍写得很直白：

```
- a pipeline cannot loop back after the optimizer finds new anomalies
- a swarm has no global view, so two agents can propose conflicting actions
- the supervisor owns routing, iteration limits and termination in one place
```

第二条就是 critic 的立项理由：**optimize 是"一条规则一个提案"地生成建议的，它天生看不到"同一个活动被两条规则各判了一次"。** 只有拥有全局视图的组件才能做全局裁决。

**本项目是典型的多 Agent + Supervisor 编排**，源码在 `backend/src/adoptimizer/orchestrator/graph.py`。

---

## 4. Agent 工作流程图（与真实代码一致）

```
                    ┌──────────────── "continue" ────────────────┐
                    │                                            │
开始 ─▶ monitor ─▶ audience ─▶ creative ─▶ bidding ─▶ optimize ─▶ critic
  拉指标   受众洞察   生成文案    出价建议    生成提案+预演   冲突裁决 │
  出告警                                                    "end" ─▶ END
```

**条件判断发生在 critic 之后，不是 optimize 之后。** 这个位置很关键，`graph.py` 里的注释说明了原因：

> Routing reads the **post-critic** state, so the loop continues only when **unreconciled anomalies** survive rather than when any rule fired.

即「是否再跑一轮」是在**提案被复核之后**决定的 —— 判断依据是"还有没消化的异常"，而不是"有没有规则触发过"。

终止条件（三选一即结束）：

1. optimize 报告 `is_complete`（迭代到上限 / 没有告警 / 没有可做的动作）
2. `iteration >= max_iterations`
3. `alerts` 为空

---

## 5. 常见 Agent 框架简介

### 5.1 LangGraph（本项目选型）

- **特点**：用**有向图**描述工作流；节点是函数或 Agent；边表示流转；支持**条件分支**与**循环**；支持 checkpoint 与状态 reducer。
- **适合**：你希望流程**可视化、可控制、可回放**，而不是完全黑盒。
- **本项目**：`OptimizationOrchestrator` 用 LangGraph 编排 6 个 Agent。langgraph 在 `backend/pyproject.toml` 里是**核心依赖**（`dependencies`，不是 extra），但导入仍包在 `try/except ImportError` 里：万一装不上，会回退到一个行为等价的顺序执行器（`_invoke_sequential`），共用同一份 Agent 顺序与同一张 reducer 表，业务结果一致，只是失去 checkpoint 与可视化。`/readyz` 会如实上报当前用的是哪个。**图编译失败不会回退** —— `_compile()` 没有 try/except，编译报错会让进程起不来；这是有意的，图定义写错是 bug，不该被静默降级藏起来。

### 5.2 CrewAI

- **特点**：强调「角色（Role）+ 目标 + 背景故事」式的**团队协作**隐喻，API 偏高层。
- **适合**：快速搭原型、演示多角色对话式协作。

### 5.3 AutoGen（Microsoft）

- **特点**：多 Agent **对话协商**；可与代码执行、工具调用结合。
- **适合**：研究型、对话驱动、需要多轮讨论的自动化任务。

**对比小结**：若你重视**工程可控的流程图**，LangGraph 更贴切；若重视**角色扮演与对话式协作**，可了解 CrewAI / AutoGen。本项目选 LangGraph，因为广告优化要的是**确定性和可审计性**，不是自由对话。

---

## 6. 广告场景中 Agent 能做什么

| 场景 | Agent 做的事 | 本项目由谁负责 |
|---|---|---|
| 监控 | 发现异常指标，生成告警说明 | monitor |
| 诊断 | 结合细分维度解释「为什么」变差 | audience |
| 创意 | 批量生成 A/B 文案，给出测试假设 | creative |
| 竞价 | 在约束下建议出价区间或调整幅度 | bidding |
| 预算 | 在多活动间建议预算再分配 | optimize（`domain.budget.allocate`，可选 CVXPY 凸优化交叉验证） |
| 复核 | 消除互相矛盾的提案 | **critic** |
| 报告 | 把结构化结果转成运营可读的说明 | 各 Agent 的 `agent_messages` + SSE 事件流 |

**注意：线上投放涉及资金与合规。** 本项目的设计立场是明确的 —— `optimize.py` 的文件 docstring 第一句：

> Nothing here executes. The agent only proposes; execution happens in the action service after approval, so **a model hallucination can never change spend on its own**.

**Agent 只产出"提案"，不产出"操作"。** 提案落库时状态是 `proposed`，等人点批准才执行。所以哪怕模型幻觉出一个荒唐建议，最坏结果也只是审批队列里多一条被人拒绝的记录，不会有钱出去。这个开关是 `SECURITY__REQUIRE_ACTION_APPROVAL`，默认 `true`。

---

## 7. 诚实说明：本项目的 Agent 像不像"Agent"？

这一节是给你的**面试护身符**。含糊过去，被追问"你的 Agent 怎么 prompt 的、怎么防止幻觉"就会翻车。

**它不是 LLM 驱动的自主体，而是有明确职责边界的规则化组件，用 LangGraph 编排。** 具体说：

| 常见想象 | 本项目实际 |
|---|---|
| Agent 自己决定下一步做什么 | 固定流水线 + critic 后的条件回路，路由是纯函数 |
| Agent 靠 prompt 产出决策 | 6 个里 4 个完全不调 LLM；涉及金钱的计算全在 `domain/` 纯函数里 |
| Agent 直接操作外部系统 | 只产出提案，执行在人工审批之后 |
| 记忆靠向量库 | 靠 state 通道 + 数据库 |

**反过来，这正是它的优点，而且是可以量化的优点：**

- **可测**：1000+ 个测试用例。确定性代码才测得动 —— 你没法对 prompt 的输出写断言
- **可复现**：同样输入必然同样输出，因此能写回归测试
- **可审计**：每条提案都带 `reason`、`confidence`、`severity`，每次工具调用（含被拒绝的）都有事件落库
- **防幻觉**：模型说什么都改不了钱，因为提案要过 critic 复核 + 人工审批两道关

**面试标准说法**：

> 这个系统里"Agent"指的是**职责边界清晰、通过共享状态协作、由 supervisor 图编排的组件**，不是"能自主决策的 LLM 实体"。我们刻意把 LLM 限制在自然语言生成与归纳上，所有涉及金钱的数学都是确定性代码 —— 这样才有可测性、可复现性和可审计性。多智能体的价值不在于让模型自由发挥，而在于**分工 + 一个拥有全局视图的裁决者**。

---

## 8. 示例：最简单的 Agent（Python 伪代码）

下面不是可运行程序，只帮助理解「循环：模型思考 → 调工具 → 再思考」这个经典 ReAct 形态。

```python
# ========== 伪代码：极简 ReAct 风格 Agent ==========

def simple_agent(user_goal, max_steps=5):
    memory = []  # 记忆：每步的思考与观察
    for step in range(max_steps):
        # 1) LLM 根据「目标 + 记忆」决定下一步
        plan = llm.chat(
            system="你是一个广告助手，可调用工具。",
            messages=memory + [{"role": "user", "content": user_goal}],
        )

        # 2) 若模型决定结束，直接返回
        if plan.action == "FINISH":
            return plan.final_answer

        # 3) 若模型要调工具
        if plan.action == "TOOL":
            observation = call_tool(plan.tool_name, plan.tool_args)
            memory.append({"thought": plan.thought, "observation": observation})
            continue

        # 4) 其它情况：保守结束，避免死循环
        return plan.text

    return "达到最大步数，未完成。"
```

**解读**：`memory` 对应 **Memory**，`llm.chat` 对应 **LLM**，`call_tool` 对应 **Tools**，`for step in range(max_steps)` 对应最简单的 **Planning / 停止条件**。

**但请注意：本项目不是这个形态。** 这里的"下一步做什么"由 LLM 临场决定；本项目的下一步由图的边决定。上面这段伪代码的价值是让你理解**为什么**很多 Agent 系统难以测试和复现 —— 而本项目选择了另一条路（见第 7 节）。

本项目真实的 Agent 骨架长这样（简化自 `backend/src/adoptimizer/agents/base.py`）：

```python
class BaseAgent(ABC):
    name: AgentName

    async def execute(self, state, context):
        """模板方法：埋点、计时、指标、日志、异常事件都在这里统一处理。"""
        await context.bus.publish(context.run_id, "agent.started", agent=self.name.value, ...)
        try:
            update = await self.run(state, context)      # ← 子类只实现这一个方法
        except Exception:
            await context.bus.publish(context.run_id, "agent.failed", ...)
            raise                                        # 业务组件失败必须炸出来
        await context.bus.publish(context.run_id, "agent.completed", ...)
        return update

    @abstractmethod
    async def run(self, state, context) -> dict:
        """读 state，算，返回状态更新。"""
```

**新增一个 Agent 只需要实现 `run()`** —— 事件埋点、Prometheus 指标、结构化日志、异常处理全部由基类自动提供。

---

## 9. 与本项目如何衔接

读完本文后，建议按这个顺序看源码：

1. `backend/src/adoptimizer/orchestrator/state.py` —— 状态通道有哪些、每个通道绑定什么 reducer
2. `backend/src/adoptimizer/orchestrator/graph.py` —— 6 个节点、固定边、`_route_after_critic` 条件边、顺序执行器回退
3. `backend/src/adoptimizer/agents/base.py` —— `BaseAgent` 模板方法与 `AgentContext`
4. 任意一个具体 Agent，建议顺序：`monitor.py`（最简单）→ `optimize.py`（提案生成 + 预演）→ `critic.py`（冲突裁决，最复杂）
5. `backend/src/adoptimizer/domain/` —— 所有涉及金钱的纯函数：`anomaly.py`、`budget.py`、`pricing.py`、`scoring.py`、`kpi.py`

配套阅读：《LangGraph 入门教程》（编排细节）、[docs/production/02-architecture.md](../production/02-architecture.md)（架构权威文档）。

---

## 10. 小结

- **Agent** 是为完成目标而使用**工具**与**规划**的程序结构；LLM 是常见但**非必需**的部件。
- **四大要素**在本项目的落地：LLM（仅 2/6 个 Agent 用）、Memory（state 通道 + 数据库，无向量库）、Tools（声明式 spec + 统一执行器 + dry-run 预演 + 全量审计）、Planning（显式图，非自由规划）。
- **多 Agent** 通过 **Supervisor** 编排；本项目的 supervisor 额外拥有一个 **critic** 节点做全局冲突裁决 —— 这是"多 Agent 必然产生矛盾提案"这个缺点的直接应对。
- **LangGraph** 适合用图表达流水线与循环；本项目另外保留了一条行为等价的顺序执行器，作为**导入失败时的退路**（langgraph 本身是核心依赖，不是 extra）。
- 广告领域涉及资金，**Agent 输出必须是"建议"而非"自动生效"**。本项目在代码层面强制了这一点：Agent 只提案，执行在人工审批之后。

下一篇：《LangGraph 入门教程》。
