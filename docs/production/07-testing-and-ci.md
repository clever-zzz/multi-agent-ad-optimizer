# 07 测试与 CI

---

## 1. 测试策略

```
              ┌────────────────────────┐
              │  端到端（浏览器 + 真实栈）│   手工，见 §6
              ├────────────────────────┤
              │  集成测试 435 个          │   httpx ASGITransport + 临时 SQLite
              ├────────────────────────┤   全链路：中间件 → RBAC → service → repo → DB
              │  单元测试 838 个          │   纯函数，零 I/O
              ├────────────────────────┤
              │  前端单测（vitest+jsdom）  │   lib/ 与 components/ 的纯逻辑
              └────────────────────────┘
```

**核心原则：整个后端测试套件是 hermetic 的。**

- 每个测试一个临时 SQLite 文件（`tmp_path`）
- `LLM__PROVIDER=mock`（确定性、离线、免费）
- `DATA_MODE=mock`（模拟广告平台适配器）
- `REDIS__ENABLED=false`（进程内缓存）
- `CLICKHOUSE__ENABLED=false`
- `RATE_LIMIT__ENABLED=false`（避免测试之间互相限流）
- Argon2 参数降到 `time_cost=1, memory_cost=8192`（否则每个认证测试都要付 64 MiB × 3 轮的代价）

结果：**不需要 Docker、不需要网络、不需要任何外部服务**，`pytest` 直接跑完 1273 个测试（838 unit + 435 integration）。

---

## 2. 后端测试

### 2.1 布局

```
backend/tests/
  conftest.py                       fixture：settings / app / client / admin_headers / make_user / snapshot_factory
  unit/                             纯业务规则，不碰 HTTP 也不碰数据库，838 个
    test_statistics.py        (25)   A/B 显著性检验、样本量估算
    test_kpi.py               (32)   CTR/CVR/CPA/ROAS、健康分
    test_pricing.py           (25)   eCPM、竞价上限、出价推导
    test_budget.py            (35)   预算重分配（贪心路径 + 凸求解器）
    test_anomaly.py           (29)   阈值 + 统计异常检测、去重
    test_scoring.py           (17)   创意评分
    test_monitor.py           (3)   监控 Agent 读上轮烧钱率：只收 burn_rate 裁决、首轮无上轮、通道被重放也不会炸
    test_ingest.py            (63)   采集框架：synthetic 源确定性、平台报表源、记录契约、语义规则、活动归属、同批槽位冲突
    test_scheduling.py        (40)   定时拉取的窗口算术：首次/已覆盖/名义/补数/触顶五条分支、补数上界与 `gap_days`、租约与水位派生字段、结果契约
    test_state.py             (26)   AgentState reducer 语义
    test_orchestrator_fallback.py (8) 降级执行器：reducer 黄金表锁定合并语义、两条路径产出同一个 run、累积通道真的在累积；外加"通道声明了但没人写"的静态扫描守护
    test_metrics_wiring.py    (3)    AST 解析 `core/metrics.py` 的声明集、扫全包找发点，断言没有"声明了却从不导出"的指标；并钉住指标名清单
    test_tools.py             (57)   工具规格/注册表/执行器：拒绝路径、干跑互锁、幂等、可观测性、平台工具
    test_optimize_tools.py    (25)   optimize 写提案预检：谁会被预检、被拒怎么办、预检预算、消息汇总
    test_critic.py            (55)   冲突复核：暂停 vs 加预算、生命周期/创意冲突、跨迭代去重、严重度优先、不可执行抑制
    test_security.py          (48)   Argon2、JWT、角色→权限矩阵（含 ingestor）；并反向解析前端 stores/auth.ts 与 lib/types.ts，那份矩阵副本一漂移就红
    test_config.py            (68)   配置校验，含生产硬化拒绝路径、`INGEST__*` 七个键的环境解析、数据源命名规则，以及"每个设置项都有示例 / 每个示例都有读取方 / 每个设置项都真的被读"三向守护
    test_logging.py           (18)   结构化日志：handler 跟随进程当下的 stdout（含已关闭与无控制台两条分支）、tty 判定、重复配置只留一个 handler、contextvar 注入
    test_llm_provider.py      (59)   OpenAI 兼容 provider：端点、鉴权、失败映射、SSE 流式增量（httpx MockTransport）
    test_llm_gateway.py       (59)   网关：预算护栏、重试退避、缓存、降级、并发信号量、指标、流式片段转发语义、按 run 归集的用量记账
    test_ads_adapters.py      (37)   三家广告适配器的 HTTP 层直测（`httpx.MockTransport`）：端点、凭据放置、分页、错误映射
    test_analytics_sink.py    (27)   数仓写入侧：行模型、`ReplacingMergeTree` 语义、`NullSink` 把每行报为 skipped
    test_cli_warehouse.py     (7)    `warehouse status` / `warehouse sync` 两条命令
    test_cli.py               (72)   typer CLI 每条命令（副作用用桩替换），含 `adoptimizer ingest` 与 `adoptimizer scheduler`（`--once` / `--force` / `--dry-run` / 开关语义 / 退出码）
  integration/                      走完整 HTTP 栈，435 个
    test_health.py            (25)   探针、安全响应头、限流
    test_auth_api.py          (42)   登录/刷新/登出/改密/锁定/轮换/最后一个 admin 护栏
    test_rbac_api.py          (25)   端点 × 角色权限矩阵（5 个角色全展开，参数化成大量用例）
    test_campaigns_api.py     (36)   活动与创意 CRUD、校验、审计
    test_ingest_api.py        (36)   推入口端到端：鉴权、逐条下落报告、溯源落列、干跑、批次台账、数据源状态、拉取服务、采集身份进审计
    test_scheduler_api.py     (39)   定时拉取端到端：`GET /ingest/schedule` 鉴权与只读性、首次→已覆盖、错过自愈、触顶报 `gap_days`、水位只放宽、干跑不推进、租约单飞（含插入竞争与两次重叠 tick）、常驻循环可停
    test_optimization_flow.py (47)   完整闭环：触发 → 事件 → 动作 → 审批门 → 批量 → 幂等（含并发重放）→ 告警 → 分析 → 用量与账本对账
    test_action_execution.py  (65)   执行器每条 _apply 分支、审批门、批量批准、参数解析、经工具层的人工执行
    test_tool_layer.py        (28)   端到端：能力目录/审计端点、run summary 带工具层、护栏可翻转、审计落库
    test_run_reaper.py        (6)    陈旧 run 的回收：按最后事件时间判定、不覆盖终态
    test_run_stream.py        (20)   SSE 事件流：序号有序、断线重连回放、心跳与回库 resync
    test_warehouse.py         (54)   ClickHouse 读路径：降级为空结果、`daily` 源槽位塌缩、人口维度并集、campaign 过滤
    test_warehouse_sync.py    (12)   运营库 → 仓库的镜像同步：可安全重跑、`NullSink` 语义
    test_run_stream.py        (20)   SSE 回放/续传/终止、取消、RBAC
    test_run_reaper.py        (6)   reaper 判据与收尾：还在出事件不误杀、静默超窗才回收、排队 run 回落 created_at、新鲜 run 放过、终态不重开、回收时关流
    test_warehouse.py         (34)   SQL 与 ClickHouse 两个后端（假驱动）：查询构造、绑定参数、降级
```

### 2.2 fixture 设计

```python
settings   → make_settings("sqlite+aiosqlite:///<tmp_path>/adoptimizer-test.db")
app        → create_app(settings) + 进入 lifespan_context（建容器、建表、灌 admin）
client     → httpx.AsyncClient(transport=ASGITransport(app))   # 不开真实 socket
admin_headers → 登录引导管理员
make_user(role) → 建账号并返回其 headers，用于 RBAC 测试
snapshot_factory → 造 PerformanceSnapshot，省掉重复样板
```

`app` fixture 走的是**真实的 lifespan**，所以容器构造、SQLite 建表、admin 种子、`reap_stale_runs` 全都被覆盖到了。这是集成测试有价值的关键——它测的是真的启动路径，不是一个拼装出来的假 app。

`client` 用 `ASGITransport`，因此中间件栈（request_id、安全头、限流、CORS）也都在链路里。

### 2.3 运行

```bash
cd backend
.venv/Scripts/pytest                                  # Windows
.venv/bin/pytest                                      # macOS/Linux

pytest -q                                             # 简短输出
pytest --cov                                          # 带覆盖率（<88% 失败）
pytest tests/unit -q                                  # 只跑单元测试
pytest tests/integration/test_optimization_flow.py -q # 只跑一个文件
pytest -k "last_admin" -q                             # 按名字筛
pytest -x --lf                                        # 遇到第一个失败就停，只跑上次失败的
pytest --maxfail=3 -n auto                            # 装了 pytest-xdist 可并行
```

标记（`--strict-markers`，写错名字会直接报错）：

| 标记 | 含义 |
|---|---|
| `integration` | 需要外部服务（postgres/redis/clickhouse）。默认套件里**没有**用到，预留给未来的真实依赖测试 |
| `slow` | 长耗时 |

```bash
pytest -m "not integration and not slow"
```

### 2.4 覆盖率

`pyproject.toml`：

```toml
[tool.coverage.run]
branch = true
source = ["src/adoptimizer"]
concurrency = ["thread", "greenlet"]
omit = ["*/migrations/*"]

[tool.coverage.report]
fail_under = 88
show_missing = true
```

**分支覆盖**，不是行覆盖。88% 是**棘轮式的下限而非目标**——`core/security.py` 是 **100%**、`domain/` 九个文件全在 89%–100%，三家广告适配器也已经补到 92%–95%（用 `httpx.MockTransport` 直测，不再依赖真实凭据）；真正把总数拉低的是需要外部服务、或难以伪造时序的那几个文件，清单在下面。当前实测 **91.07%**，门禁设在 88%，余量 **3.07 个点**。一个新的大模块如果完全没测试就会把 CI 弄红，而这正是它该做的事。实测值往上爬超过 3 个点时，就把 `fail_under` 跟着提上去，别让覆盖率悄悄回落——工具层那一轮从 78% 提到 80%，采集框架那一轮从 80% 提到 85%，调度这一轮从 85% 提到 88%。三轮新增的模块都是 **100%**：采集的 `services/ingest.py`、`schemas/ingest.py`、`api/v1/ingest.py`、`repositories/ingest.py` 与 `infra/ingest/` 全部 6 个文件，调度的 `schemas/scheduling.py`、`services/scheduling.py`、`repositories/scheduling.py` 3 个文件；这一轮顺带把 `core/logging.py`（live-stdout handler）从 95.2% 补到了 **100%**。身份隔离那一轮没有新增源文件——`Role.INGESTOR` 与它的权限映射都落在既有的 `domain/enums.py` 和 `core/security.py` 里，两个文件仍是 **100%**，所以实测总量没动，门禁也就不用再抬。**数仓写入侧这一轮**加的是 `infra/analytics/`（`AnalyticsSink` + 行模型）、`services/warehouse_sync.py`、`warehouse status` / `warehouse sync` 两个 CLI 命令与 ClickHouse 侧的 `campaign_daily_metrics` 表，同时把三家广告适配器的 HTTP 层直测补齐（`tests/unit/test_ads_adapters.py`，37 用例）：测试数从 1144 走到 **1258**，实测从 89.30% 走到 **91.07%**。新增模块本身是 `services/warehouse_sync.py` 与 `infra/analytics/__init__.py` **100%**、`infra/analytics/models.py` **97.8%**、`infra/analytics/sink.py` **96.8%**、`infra/warehouse.py` **98.4%**。余量因此到 **3.07 个点**，正好越过上面那条“超过约 3 个点就抬门禁”的线——**下一步该把 `fail_under` 提到 90**；仓库此刻仍留在 88，这笔待办记在 `backend/pyproject.toml` 的注释里。

critic 持久化这一轮加了一张表、一个端点、三个可配阈值，实测从 88.54% 走到 88.80%：`agents/critic.py` 与 `core/config.py` 都是 **100%**，但 0.8 个点的余量没到抬门禁的门槛（约 3 点），所以 `fail_under` 留在 88。

局限清理这一轮没有新增源文件，改的是既有实现的正确性（checkpoint 回收、reaper 判据与周期任务、reducer 表自动派生、实时进度心跳），但顺手把一个"该测而没测"的缺口补上了：`orchestrator/graph.py` 从 **61.6%** 走到 **86.1%**。实测总量因此到 **89.30%**（1144 个测试），1.3 个点的余量仍然不到抬门禁的门槛，`fail_under` 继续留在 88。

**当前最大的几个缺口**（照着补最划算）：`infra/cache.py` **34.2%**（Redis 分支要真服务，内存实现之外的路径基本没测）、`repositories/audit.py` **49.4%**、`orchestrator/events.py` **57.1%**——未覆盖的是订阅者 fan-out（含队列满时丢最旧）与带心跳上限的活流生成器，要测它们得伪造背压和跨副本时序，属于"难测"而不是"忘了测"。

`orchestrator/graph.py` 已经从 **61.6%** 补到 **86.1%**：`_invoke_sequential`（降级顺序执行器）是"langgraph 挂了系统还能跑"这条承诺的唯一实现，以前整段未测，现在有 `tests/unit/test_orchestrator_fallback.py` 守着——一张 reducer 黄金表锁定每个通道的合并语义，外加"两条执行路径必须产出同一个 run"的等价性断言。这张表第一次跑就抓到一个真 bug：手抄的 `_REDUCERS` 漏登记了 `usage` 通道（该用 `merge_mapping`，实际退化成后值覆盖），而且只在降级路径暴露。表本身现在由 `_reducers_from_state()` 从 `AgentState` 的注解自动派生，手抄版本已经删掉了。

`infra/ads/{google,meta,tiktok}.py` 与 `infra/ads/http_client.py` 曾经被归为“真的测不到：需要真实凭据与真实网络”（当时 52.8% / 48.8% / 53.3% / 37.5%）。**这个理由已经不成立了**：`tests/unit/test_ads_adapters.py`（37 用例）用 `httpx.MockTransport` 断言每个适配器的请求路径、请求头与请求体，并覆盖 401 / 429 / 非 JSON 等错误分支，四个文件现在是 **92.66% / 93.10% / 94.74% / 95.38%**——这也是总覆盖率从 89.30% 走到 91.07% 的主要来源。真正剩下的“要真服务才测得到”只有 `infra/cache.py` 的 Redis 分支。

> ⚠️ **`concurrency = ["thread", "greenlet"]` 不是可选项，删掉它覆盖率会凭空掉 3 个点。**
>
> SQLAlchemy 的 asyncio 适配层用 `greenlet_spawn` 跑同步 DBAPI，驱动又在工作线程里把结果递回来。这两处切换会把 coverage 的 tracer 甩掉，而且**不会报错**——它只是把明明执行过的行记成未覆盖。本项目每一个仓储调用都要跨这条边界，所以漏记的量很大：同一套测试，配好这一项之前测出来是 84.60%，配好之后是 87.99%（当时 935 个测试；后来 1258 个测试时测出 91.07%）；`services/ingest.py` 从“347-369 未覆盖”变成 100%。
>
> 症状很好认：**同一个函数里前半段有覆盖、后半段整块没有，而测试断言的恰恰是“没覆盖”那段产出的字符串**（比如报告里的 reason 文案对得上、`return report` 却显示未执行）。看到这个先查 `concurrency`，别去补根本不缺的测试。

看哪些行没覆盖：

```bash
pytest --cov --cov-report=term-missing
pytest --cov --cov-report=html && start htmlcov/index.html    # Windows
```

### 2.5 写新测试

**改 `domain/` 里的业务规则** → 加单元测试。这些是纯函数，测试应该只断言输入输出：

```python
def test_bid_never_exceeds_the_target_cpa_cap() -> None:
    snapshot = snapshot_factory("camp_1", impressions=10_000, clicks=100, conversions=5)
    decision = recommend_bid(snapshot, target_cpa=50.0, cap_ratio=0.8)
    assert decision.recommended_cpm <= 50.0 * 0.8 * <expected factor>
```

**改 API 行为** → 加集成测试，覆盖三件事：happy path、权限不足、状态冲突。

```python
async def test_viewer_cannot_update_a_campaign(client, make_user) -> None:
    viewer = await make_user("viewer")
    response = await client.patch(
        API + "/campaigns/camp_x", json={"daily_budget": 1.0}, headers=viewer["headers"]
    )
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "permission_denied"
    assert body["type"] == "https://adoptimizer.dev/errors/permission_denied"
```

**改配置校验** → `test_config.py` 里的模式是**驱动真实环境变量**再 `reload_settings()`，因为配置是从环境读的，直接构造对象测不到解析路径。

约定：

- 测试函数名描述**行为**，不描述实现：`test_last_admin_cannot_be_deactivated` 而不是 `test_set_active_2`
- 断言错误时带上 `response.text`，失败时能直接看到 body
- 不要在测试里 `sleep`。run 执行用 `wait_for` 或轮询状态
- 不要依赖测试执行顺序

---

## 3. 前端测试

```
src/lib/api.test.ts                  HTTP 客户端：token 注入、401→refresh 重试、problem document 解析
src/lib/format.test.ts               数字/百分比/货币/日期格式化
src/lib/passwordPolicy.test.ts       口令强度校验（与后端 policy 对齐）
src/components/charts/chartUtils.test.ts   图表几何计算（scale、path、边界情况）
src/components/ui/components.test.tsx      基础组件渲染与交互
src/stores/auth.test.ts              认证状态机
```

```bash
cd frontend
npm run test            # vitest run
npm run test:watch
npm run coverage
```

配置在 `vite.config.ts` 的 `test` 段：`environment: jsdom`、`globals: true`、`setupFiles: ./src/test/setup.ts`（引入 `@testing-library/jest-dom` 的匹配器）、`css: false`。

**前端测什么、不测什么**

- 测：纯函数（格式化、图表几何、口令策略）、HTTP 客户端行为、状态机
- 不测：整页渲染、路由跳转、视觉样式。这些用 §6 的手工端到端清单覆盖，性价比更高
- 图表**组件**本身不测，测的是 `chartUtils.ts` 里的几何计算——这正是 [ADR-0001](../adr/0001-custom-svg-charts.md) 里"把计算抽成纯函数"的收益

---

## 4. 静态检查

### 4.1 后端

**ruff**（同时负责格式与 lint）

```bash
ruff format --check src tests     # CI 用的检查形式
ruff format src tests             # 本地直接格式化
ruff check src tests
ruff check --fix src tests        # 自动修可修的
```

启用的规则集：

```
E, W      pycodestyle
F         pyflakes（未使用导入/变量等）
I         isort（导入排序）
N         命名规范
UP        pyupgrade（消灭 typing.List 这类弃用写法）
B         bugbear（真实 bug 模式）
A         builtins 遮蔽
C4        推导式简化
SIM       可简化的控制流
TCH       类型检查专用导入
RUF       ruff 自有规则
S         bandit（安全）
PTH       用 pathlib 而不是 os.path
DTZ       时区感知的 datetime
ASYNC     异步误用
RET       返回值风格
ARG       未使用的函数参数
```

忽略项及其理由都写在 `pyproject.toml` 的注释里（`B008` 是 FastAPI 的 `Depends()` 惯用法，`S101` 是测试里的 assert，等等）。`tests/**` 额外放宽 `ARG` 与 `TCH`——pytest fixture 天然是"看起来没用的参数"。

**mypy**（`strict = true`）

```bash
mypy src
```

- 启用 pydantic 插件，模型字段类型能被真正检查
- `warn_unreachable = true`
- `migrations/` 排除（Alembic 生成的代码不符合 strict）
- `tests.*` 放宽 `disallow_untyped_decorators` 等——pytest 的装饰器签名 strict 模式过不了，这是设计使然
- 第三方无存根的模块（`cvxpy`、`arq`、`clickhouse_connect`、`langchain_openai`）单独 `ignore_missing_imports`

**目标是 `mypy src` 零错误**，不是"大部分文件通过"。

### 4.2 前端

```bash
npm run lint        # eslint --max-warnings 0
npm run typecheck   # tsc -p tsconfig.app.json && tsc -p tsconfig.node.json
```

`tsconfig.app.json` 的关键项：`strict`、`noUnusedLocals`、`noUnusedParameters`、`noImplicitOverride`、`noFallthroughCasesInSwitch`。

ESLint 规则要点：

- `@typescript-eslint/no-unused-vars` 为 **error**，但 `^_` 前缀的变量/参数/捕获异常放行（这是"我知道它没用，故意留着"的约定）
- `@typescript-eslint/no-explicit-any` 为 warn（配合 `--max-warnings 0`，等价于禁止）
- `no-console` 只允许 `warn` 与 `error`
- `eqeqeq: ["error", "smart"]`
- `react-hooks` 推荐规则
- `react-refresh/only-export-components`：测试文件里关掉

---

## 5. CI 流水线

`.github/workflows/ci.yml`，触发条件：`push` 到 main/master、所有 `pull_request`、手动 `workflow_dispatch`。

`concurrency` 配置让同一分支的新 push 取消在跑的旧 run，省 CI 时间。顶层 `permissions: contents: read` 是最小权限。

```
┌─────────── backend (ubuntu, py3.12, 20min) ───────────┐
│  checkout → setup-python(cache: pip)                  │
│  pip install -e ".[dev,analytics]"                    │
│  ruff format --check src tests                        │
│  ruff check src tests                                 │
│  mypy src                                             │
│  alembic upgrade → downgrade base → upgrade (SQLite)  │
│  pytest --cov --cov-report=xml --junitxml=junit.xml   │
│  upload: coverage.xml + junit.xml                     │
└───────────────────────────────────────────────────────┘
┌─────────── migrations (ubuntu, py3.12, 15min) ────────┐
│  service: postgres:16.6-alpine（healthcheck 后才开跑）│
│  pip install -e ".[dev,postgres]"                     │
│  alembic upgrade head                                 │
│  alembic check          ← 断言无 autogenerate 漂移     │
│  alembic downgrade base → alembic upgrade head        │
└───────────────────────────────────────────────────────┘
┌─────────── frontend (ubuntu, node22, 20min) ──────────┐   三个 job 并行
│  checkout → setup-node（cache: npm）                  │
│  npm ci（lockfile 已提交，漂移即失败）                │
│  npm run lint → typecheck → test → build              │
│  upload: frontend/dist                                │
└───────────────────────────────────────────────────────┘
              │ needs: [backend, frontend, migrations]
              ▼
┌─────────── images (30min, 非 PR 才跑) ────────────────┐
│  buildx + GHA 层缓存                                   │
│  build Dockerfile.backend  (push: false)              │
│  build Dockerfile.frontend (push: false)              │
└───────────────────────────────────────────────────────┘
┌─────────── manifests (10min) ─────────────────────────┐
│  docker compose config --quiet（dev 与 prod 两层）      │
│  kubectl apply --dry-run=client --validate=false -k    │
│  逐个 manifest dry-run                                 │
└───────────────────────────────────────────────────────┘
```

几个设计决定：

**测试环境变量在 CI 里再设一遍**（`APP__ENVIRONMENT=test`、`LLM__PROVIDER=mock`、`REDIS__ENABLED=false` 等）。测试套件自己会构造 Settings，但仓库里万一有人留了个 `.env`，不该渗进 CI。双保险。

**`images` job 在 PR 上不跑**（`if: github.event_name != 'pull_request'`）。PR 只需要证明 Dockerfile 能构建——但构建镜像慢且吃缓存，所以留到 push。如果你希望 PR 也验证，去掉那个 `if`。

**`manifests` job 用 `--validate=false`**。`--dry-run=client` 若开启服务端校验，就需要 runner 上有可用的 kubeconfig，那样这个 job 只在特定环境能过。关掉之后仍然验证 YAML 结构、字段类型与 kustomize 组装是否正确。

**`secret.example.yaml` 被 CI 显式跳过**。它不参与 kustomize 组装，也不该被 apply。

**`migrations` job 连的是真的 PostgreSQL 服务容器**，不是 SQLite。初始 revision 是手写的，`alembic check` 在 upgrade 之后跑一次 autogenerate，只要产出任何 diff 就让 CI 红。这挡住的是最难查的一类事故：开发与测试走 `Base.metadata.create_all`、预发与生产走 Alembic，两套 schema 悄悄分叉，而分叉的那一半从来没被测过。`backend` job 里另外用一次性 SQLite 跑 `upgrade → downgrade base → upgrade`，保证链路在开发方言上同样可逆。

**列上的 `unique=True` + `index=True` 组合被禁掉了**。那个组合会让 SQLAlchemy 同时产出 UNIQUE 约束和唯一索引，而 autogenerate 只复现其中一个，于是 `alembic check` 永远报漂移。`users.email` 因此改成在 `__table_args__` 里显式声明 `Index("ix_users_email", "email", unique=True)`。以后新增唯一列照这个写法。

**前端只走 `npm ci`。** `frontend/package-lock.json` 已提交，`setup-node` 打开了 `cache: npm` + `cache-dependency-path`。不再保留 `npm install` 回退分支——lockfile 与 `package.json` 一旦漂移，构建立刻红，而不是悄悄解析出另一棵依赖树。`Dockerfile.frontend` 同样优先 `npm ci`。**改了依赖就把 lockfile 一起提交**，否则 CI 会替你发现。

### 5.1 本地跑同一套

```powershell
.\scripts\check.ps1              # 全部
.\scripts\check.ps1 -Only backend
.\scripts\check.ps1 -Skip build,vitest
.\scripts\check.ps1 -Only backend -Skip migrations   # 不碰数据库，只跑静态检查与单测
```

```bash
make check        # = lint types test build，顺序与 CI 一致
```

`check.ps1` 最后打印一张 pass/FAIL/skipped 汇总表，有失败时退出码 1。

### 5.2 还没接上但应该接的

| 项 | 说明 | 优先级 |
|---|---|---|
| `pip-audit` / `npm audit` | 依赖漏洞扫描 | 高 |
| 镜像漏洞扫描（Trivy/Grype） | 基础镜像层 CVE | 中 |
| 覆盖率门禁分模块 | 让 `domain/` 要求 95%+，`infra/ads/` 放宽 | 中 |
| 真实 PostgreSQL 上的业务集成测试 | 迁移链路已由 `migrations` job 覆盖；仍缺在 PG 上验证 JSON 字段查询与 `daily_metrics` 部分唯一索引行为的业务用例 | 中 |
| Playwright 端到端 | 覆盖登录→触发 run→审批的完整路径 | 中 |
| 负载测试 | `k6`/`locust` 打 `/analytics/*` 与 `/runs`，拿到真实容量数字 | 低（当前无生产流量） |

---

## 6. 手工端到端验收清单

CI 覆盖不到的部分。每次发布前在 staging 走一遍（约 15 分钟）。

**认证与账号**
- [ ] 用引导管理员登录成功
- [ ] 修改密码后，旧密码不能登录，新密码可以
- [ ] 修改密码后其他设备的会话被踢下线
- [ ] 连续输错 5 次口令后账号被锁，15 分钟内正确口令也被拒
- [ ] 登出后浏览器后退，页面不能继续调接口

**权限**
- [ ] 建一个 viewer 账号，登录后「活动」页的编辑/删除按钮不可见
- [ ] 用 viewer 的 token 直接 `PATCH /campaigns/{id}` → 403 `permission_denied`
- [ ] 用 optimizer 账号访问 `/admin/audit` → 403
- [ ] 把某账号角色从 optimizer 改为 analyst，该账号**必须重新登录**才能继续（旧会话已吊销）
- [ ] 尝试停用最后一个 admin → 409

**优化闭环**
- [ ] 「运行」页点【新建运行】，选 2–3 个活动，`max_iterations=2`
- [ ] 运行详情页的时间线**实时**逐条出现（不是等结束后一次性刷出）
- [ ] 中途刷新页面，时间线能从 `lastSeq` 续上，不丢事件
- [ ] 运行结束后状态为 `succeeded`，summary 里的 `action_counts` 非空
- [ ] 触发一个长运行，中途点【取消】→ 状态变为 `cancelled`，且**不会**在几秒后又变成 `succeeded`

**审批门**
- [ ] 「动作」页有 `proposed` 提案
- [ ] 未审批直接调 `POST /actions/{id}/execute` → 409 `approval_required`
- [ ] 【批准】后【执行】→ 状态变 `executed`，`executed_at` 有值
- [ ] 重复执行同一动作 → 409
- [ ] 批量批准 3 条，逐条状态都变了
- [ ] 【驳回】带理由，状态变 `rejected`

**告警与分析**
- [ ] 「告警」页有 open 告警，`context` 里能看到观测值与阈值
- [ ] 【确认】后状态变 `acknowledged`，【解决】后变 `resolved`
- [ ] Dashboard 的 KPI 卡片与「分析」页数字一致
- [ ] 图表 hover 有 tooltip，窗口缩放时图表重排（不是溢出或压扁）

**数据接入与调度**
- [ ] `GET /api/v1/ingest/schedule` 里每个源 `registered: true`；生产源 `configured: true`，`synthetic` 不在 `INGEST__SOURCES` 里
- [ ] `adoptimizer scheduler --once` 退出码 0，JSON 里 `ran >= 1`、`failed == 0`
- [ ] 紧接着再跑一次 → `skipped`，`plan.reason` 变成 `covered`（证明水位推进了，也证明重复跑是廉价的）
- [ ] `adoptimizer scheduler --once --dry-run` 之后，下一次真实 tick 的 `plan.reason` 仍是 `first`（干跑没推进水位）
- [ ] 把 `ingest_watermarks` 的 `window_end` 手工改到 20 天前，再跑一次 → `plan.reason` 是 `catchup`，窗口从那天次日开始
- [ ] 改到 40 天前（超过 `INGEST__MAX_CATCHUP_DAYS`）→ `reason` 是 `capped`，`gap_days > 0`，`detail` 里那条 `adoptimizer ingest --start ... --end ...` 跑完能把缺口补上
- [ ] CronJob 真的在跑：`kubectl -n adoptimizer get jobs -l app.kubernetes.io/component=ingest` 有成功记录，`ingest_lag_days` 有 series 且不超过 `INGEST__LOOKBACK_DAYS + 1`

**审计**
- [ ] 「审计」页能看到上面每一步操作，含 actor、action、resource、时间
- [ ] 点开一条 `campaign.updated`，`before`/`after` 能看出改了什么
- [ ] `request_id` 与后端日志里的能对上

**运维面**
- [ ] `/healthz` 200；`/readyz` 200 且所有 check 为 ok
- [ ] `/metrics` 能看到 `http_requests_total` 在增长
- [ ] 生产环境下 `/docs` 与 `/redoc` 是 404
- [ ] 响应头里有 CSP、HSTS（https）、`X-Request-ID`
- [ ] `POST /admin/prune?audit_days=365` 返回删除条数

**部署形态**
- [ ] 多副本时：连续触发 3 个 run，每个的 SSE 时间线都能实时更新（验证 cookie 亲和生效）
- [ ] 滚动更新期间持续请求，无 5xx（验证 `preStop: sleep 10` 与 `maxUnavailable: 0`）
