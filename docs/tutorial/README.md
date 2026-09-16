# 入门教程

面向**零基础**读者：不要求你事先会 Python、会 Docker，或者听说过 LangGraph。
每篇都从"这是什么、为什么需要它"讲起，再落到**本仓库当前的真实代码**上。

> 全部 5 篇已对齐 `backend/src/adoptimizer/`（FastAPI + LangGraph，6 个 Agent）与 `frontend/`（React + Vite）。
> 仓库早期的 `python/` `java/` `golang/` 三语言教学 demo 已移除，网上流传的旧截图与旧命令不再适用。

| 教程 | 读完你会知道 | 主要对照的代码 |
|---|---|---|
| [01 环境搭建](01-environment-setup.md) | 把 Python / Node / 依赖 / `.env` / Docker 一次装对，并验证装成功了 | `backend/pyproject.toml`、`backend/.env.example`、`scripts/setup.ps1` |
| [02 Agent 基础](02-agent-basics.md) | AI Agent 的四大要素、单 Agent 与多 Agent 的取舍，以及本项目的 Agent 到底"像不像 Agent" | `agents/*.py`、`tools/`、`llm/` |
| [03 LangGraph 入门](03-langgraph-intro.md) | 图 / 节点 / 边 / 共享状态 / 条件路由，Supervisor 模式，以及本项目怎么建这张图 | `orchestrator/graph.py`、`orchestrator/state.py` |
| [04 ClickHouse 实战](04-clickhouse-guide.md) | MergeTree 与物化视图原理，以及本项目**两条数据读取路径**为什么必须语义一致 | `infra/warehouse.py`、`init-scripts/clickhouse/` |
| [05 部署与运行](05-deploy-guide.md) | 本地 / Compose / Kubernetes 三种形态怎么跑起来，以及"为什么跑不起来"的排查手册 | `deploy/`、`cli.py`、`api/health.py` |

## 阅读顺序建议

- **完全没跑过这个项目** → 01 → 05（先能跑，再懂原理）
- **想搞懂 Agent 与编排** → 02 → 03
- **只关心数据层** → 04
- **要面试** → 02 → 03 → 04，然后转 [../interview/](../interview/README.md)

## 与生产文档的关系

教程讲**为什么和怎么想**，[../production/](../production/README.md) 讲**准确的规格和运维动作**。
两者冲突时以生产文档为准：

| 教程 | 权威版本 |
|---|---|
| 01 环境搭建 | [production/01 快速开始](../production/01-quickstart.md) |
| 02 Agent 基础 · 03 LangGraph | [production/02 架构](../production/02-architecture.md) |
| 04 ClickHouse | [production/02 架构](../production/02-architecture.md) §数据层、[production/04 部署](../production/04-deployment.md) |
| 05 部署与运行 | [production/04 部署](../production/04-deployment.md)、[production/05 运维手册](../production/05-operations-runbook.md) |

架构决策的取舍记录在 [../adr/](../adr/README.md)。
