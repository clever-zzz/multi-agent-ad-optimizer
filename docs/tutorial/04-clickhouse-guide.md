# ClickHouse 实战教程（零基础版）

本教程介绍 **ClickHouse** 是什么、如何用 Docker 运行、常用 SQL、MergeTree 家族与物化视图，并详细说明**本仓库**中的表设计与查询思路，最后给出优化技巧及与 MySQL/PostgreSQL 的对比。

> **先看清一件事：ClickHouse 在本项目里是可选的，默认关闭。**
>
> `backend/.env.example` 里 `CLICKHOUSE__ENABLED=false`、`DATA_MODE=mock`。默认配置下系统走 SQLite/PostgreSQL 的聚合读取路径，**完全不需要 ClickHouse 也能跑通全部功能**。
>
> 本文的价值在于：① 讲清 ClickHouse 本身的原理（面试常问）；② 讲清本项目**两条数据读取路径**的设计，以及为什么它们必须语义一致。
>
> 早期 demo 的 `python/src/data/clickhouse_client.py` 已随 demo 移除；当前的实现是 `backend/src/adoptimizer/infra/warehouse.py`。

---

## 1. ClickHouse 是什么

**ClickHouse** 是一款面向**在线分析（OLAP）**的列式数据库管理系统，由 Yandex 开源，现由社区与公司共同维护。它擅长：

- **海量事件数据**的快速聚合（曝光、点击、转化日志等）
- **多维分析**（按活动、创意、国家、设备分组统计）
- **近实时**写入与查询（取决于集群规模与表设计）

### 1.1 列式 vs 行式（用例子理解）

**行式数据库（如 MySQL InnoDB、PostgreSQL）**：一行是一条完整记录，适合「按主键读一整行」的事务型业务。

**列式数据库（ClickHouse）**：同一列的数据物理上靠在一起，适合「只读少数几列并对大量行做聚合」。

举例：表有 100 列，你只查 `sum(cost)` 和 `count()`：

- 行式库往往要扫整行或大量页
- 列式库**只读 cost 列**，IO 更少，压缩比更高

广告曝光/点击日志正是「列多、行数巨大、查询多为聚合」的典型场景。

---

## 2. 安装（Docker 方式）

ClickHouse 在 `deploy/compose/docker-compose.yml` 里被放在 **`analytics` profile** 下，默认不启动：

```yaml
clickhouse:
  image: clickhouse/clickhouse-server:24.3
  profiles: ["analytics"]
  volumes:
    - clickhouse-data:/var/lib/clickhouse
    - ../../init-scripts/clickhouse:/docker-entrypoint-initdb.d:ro
```

启动：

```bash
cd deploy/compose
cp .env.example .env        # 填两个必填项：SECURITY__JWT_SECRET、SECURITY__BOOTSTRAP_ADMIN_PASSWORD
docker compose --profile analytics up -d
docker compose ps           # 等到 clickhouse 变 healthy
```

初始化 SQL 挂载到容器的 `/docker-entrypoint-initdb.d`，**首次启动**会执行 `init-scripts/clickhouse/01_create_tables.sql` 自动建库建表。

然后让后端改走仓库路径（改 `backend/.env`）：

```
CLICKHOUSE__ENABLED=true
DATA_MODE=warehouse
```

### 2.1 验证服务

HTTP 接口（默认 8123）：

```bash
curl "http://localhost:8123/ping"
```

期望返回 `Ok.`。

在容器内用客户端：

```bash
docker exec -it <clickhouse容器名> clickhouse-client --query "SELECT version()"
```

> 容器名取决于 compose 项目名，用 `docker compose ps` 查。

### 2.2 确认后端真的用上了

后端有 `/readyz` 与管理员健康端点会上报 `clickhouse_enabled`。也可以看启动日志：

- `clickhouse_connected` —— 连上了，走仓库路径
- `clickhouse_unavailable` —— 驱动没装（需要 `pip install -e ".[clickhouse]"`）
- `clickhouse_enabled_but_unreachable_falling_back_to_sql` —— 配了但连不上，**自动回退**到 SQL 聚合路径

---

## 3. 基本 SQL 操作

下列示例默认数据库为 `ad_optimizer`（由初始化脚本创建）。

### 3.1 查看库与表

```sql
SHOW DATABASES;
USE ad_optimizer;
SHOW TABLES;
```

### 3.2 查询示例

```sql
SELECT campaign_id, count() AS cnt
FROM ad_events
GROUP BY campaign_id
ORDER BY cnt DESC
LIMIT 10;
```

### 3.3 插入示例（MergeTree 家族表）

ClickHouse 的 `INSERT` 常用批量形式：

```sql
INSERT INTO ad_events (
  event_id, campaign_id, creative_id, event_type,
  cost, revenue, platform, device, country, age_group, gender, event_time
) VALUES (
  'evt-001', 'cmp-1', 'cr-1', 'click',
  0.5, 0, 'google', 'mobile', 'CN', '25-34', 'male', now()
);
```

生产环境更推荐**批量插入**（多行 VALUES 或文件导入），以降低合并压力。

### 3.4 常见数据类型（本项目中出现的）

- `String`、`Float64`、`UInt64`、`UInt8`、`Date`、`DateTime`
- `Nullable(Date)`：可为空的日期
- `Enum8(...)`：枚举，节省存储
- `Array(String)`：字符串数组（如兴趣标签）

---

## 4. MergeTree 引擎家族介绍

ClickHouse 的核心存储引擎是 **MergeTree** 及其变种。本项目的 `init-scripts/clickhouse/01_create_tables.sql` 使用了多种引擎。

### 4.1 MergeTree()

**特点**：支持分区、排序键、主键稀疏索引；适合**追加型事实表**。

本项目中 **`ad_events`**、**`bid_logs`** 使用 `MergeTree()`，并按时间分区：

```sql
PARTITION BY toYYYYMM(event_time)
ORDER BY (campaign_id, creative_id, event_time);
```

含义简述：

- **分区**：按月份拆分数据目录，便于删除旧分区、提升查询剪枝效果
- **排序键**：同类 `campaign_id` 的数据物理上更接近，按活动查时更快

### 4.2 ReplacingMergeTree

**特点**：后台合并时按排序键「保留一条」，通常配合**版本列**处理**更新语义**（最终一致）。

本项目中 **`campaigns`**、**`creatives`**、**`audience_profiles`** 使用 `ReplacingMergeTree(updated_at)` 或 `ReplacingMergeTree(created_at)`，适合「同一实体多次写入，保留最新快照」的场景。

**注意**：查询时若未处理重复行，可能仍看到旧版本，直到合并完成；严谨场景可用 `FINAL` 或自行聚合（有性能代价）。

### 4.3 SummingMergeTree

**特点**：合并时**对数值列自动求和**，适合**预聚合**。

本项目中两个**物化视图**的目标表使用 `SummingMergeTree()`，用于按天/按小时累加曝光、点击、消耗等。

---

## 5. 物化视图原理和使用

### 5.1 原理（直观版）

**普通视图**：保存的是查询定义，每次查询现算。

**物化视图（Materialized View）**：在**源表有数据写入时**，ClickHouse **自动**把计算结果写入另一张表（目标表）。查询时直接读目标表，速度更快。

可以理解为：**写入路径上的增量预计算**。

### 5.2 本项目中的物化视图

在 `init-scripts/clickhouse/01_create_tables.sql` 中：

1. **`campaign_creative_stats_mv`**
   - 源：`ad_events`
   - 聚合维度：`campaign_id`, `creative_id`, `stat_date`（按天）
   - 指标：曝光、点击、转化、成本、收入等

2. **`hourly_stats_mv`**
   - 源：`ad_events`
   - 聚合维度：`campaign_id`, `stat_hour`（按小时）
   - 用途：实时监控、趋势图

### 5.3 查询物化视图示例

```sql
SELECT
    campaign_id,
    sum(impressions) AS impressions,
    sum(clicks)      AS clicks
FROM ad_optimizer.campaign_creative_stats_mv
GROUP BY campaign_id;
```

因底层是 `SummingMergeTree`，合并前后结果在极端情况下可能有细微差别，生产上常再包一层 `sum()` 保证稳定（如上例）。

> **一个反直觉的事实**：本项目的 `ClickHouseWarehouse` **并不读这两个物化视图**，而是直接对 `ad_events` 做 `countIf` / `sumIf` 聚合。原因见第 6.2 节 —— 这是一个有意思的设计取舍。

---

## 6. 在本项目中的数据层设计

### 6.1 表清单

下表与 `init-scripts/clickhouse/01_create_tables.sql` 一致：

| 表名 | 引擎 | 作用 |
|---|---|---|
| `campaigns` | ReplacingMergeTree | 广告活动主数据：预算、状态、平台等 |
| `creatives` | ReplacingMergeTree | 创意素材：标题、类型、A/B 组 |
| `ad_events` | MergeTree | **核心事实表**：曝光/点击/转化事件 |
| `campaign_creative_stats_mv` | 物化视图 → SummingMergeTree | 按天聚合 Campaign×Creative |
| `hourly_stats_mv` | 物化视图 → SummingMergeTree | 按小时聚合 Campaign |
| `audience_profiles` | ReplacingMergeTree | 受众画像与预估规模 |
| `bid_logs` | MergeTree | 竞价日志：出价、是否竞得、eCPM 等 |
| `optimization_logs` | MergeTree | Agent/系统优化动作审计 |

**设计思路一句话**：「明细进 `ad_events`，常用报表走物化视图；配置类实体用 ReplacingMergeTree 表达最新状态。」

### 6.2 两条读取路径，一个 Protocol

真正的重点在这里。`backend/src/adoptimizer/infra/warehouse.py` 的文件 docstring：

> Two implementations share one protocol: ClickHouse for real event volumes and a SQL aggregate reader for environments without a warehouse. **Callers never branch on which one is active.**

结构：

```
:37   class MetricsWarehouse(Protocol)      ← 契约
:59   class SqlAggregateWarehouse           ← 读主库的 daily_metrics 聚合表
:256  class ClickHouseWarehouse             ← 读 ClickHouse 的 ad_events
:474  async def build_warehouse(session)    ← 工厂，按配置与连通性选实现
```

Protocol 定义了 6 个方法：

```python
class MetricsWarehouse(Protocol):
    name: str
    async def campaign_snapshots(self, campaign_ids, *, days) -> list[PerformanceSnapshot]
    async def creative_snapshots(self, campaign_id, *, days) -> list[dict]
    async def audience_observations(self, campaign_ids, *, days) -> list[SegmentObservation]
    async def timeseries(self, campaign_id, *, days) -> list[dict]
    async def healthcheck(self) -> dict
    async def close(self) -> None
```

工厂的选择逻辑（含**自动降级**）：

```python
async def build_warehouse(session) -> MetricsWarehouse:
    settings = get_settings().clickhouse
    if settings.enabled:
        warehouse = ClickHouseWarehouse(settings)
        if await warehouse.connect():
            return warehouse
        logger.warning("clickhouse_enabled_but_unreachable_falling_back_to_sql")
    return SqlAggregateWarehouse(session)
```

三重降级：没启用 → SQL；启用了但驱动没装 → SQL；启用了但连不上 → SQL。**调用方永远不需要知道当前是哪个。**

`ClickHouseWarehouse.campaign_snapshots` 的真实 SQL：

```sql
SELECT campaign_id,
       countIf(event_type = 'impression')  AS impressions,
       countIf(event_type = 'click')       AS clicks,
       countIf(event_type = 'conversion')  AS conversions,
       sumIf(cost,    event_type IN ('impression','click')) AS cost,
       sumIf(revenue, event_type = 'conversion')            AS revenue
FROM {db:Identifier}.ad_events
WHERE event_time >= now() - toIntervalDay({days:UInt16})
GROUP BY campaign_id
```

注意几个工程细节：
- **参数化查询**：`{db:Identifier}`、`{days:UInt16}`、`{ids:Array(String)}` 是 ClickHouse 的类型化参数占位符，不是字符串拼接 —— 防注入
- **阻塞驱动移出事件循环**：`_to_thread()` 用 `asyncio.to_thread` 包了同步的 `clickhouse_connect` 调用，不阻塞 FastAPI 的事件循环
- **查询失败返回空列表而不是抛异常**（`:300-301`），由调用方的健康检查去暴露问题

### 6.3 双粒度陷阱：为什么两个实现必须语义一致

这是本项目数据层**最值得讲的一个坑**，也是 `SqlAggregateWarehouse` 的类 docstring 花大篇幅解释的东西：

> Each delivery day is stored at **two granularities**: one slot with `creative_id IS NULL` holding the day's total, and one row per creative holding the breakdown of **that same total**. Summing both granularities would **double every KPI**, so campaign and timeseries aggregates read the campaign-level slot, and fall back to the breakdown only for buckets that have no campaign-level slot at all.

主库的 `daily_metrics` 表对**同一天同一笔花费**存了两种粒度：

```
campaign_id  stat_date    creative_id  cost
camp_A       2026-09-01   NULL         1000     ← 活动级汇总槽位
camp_A       2026-09-01   crea_1        600     ← 素材级明细
camp_A       2026-09-01   crea_2        400     ← 素材级明细
                                       ────
                              明细合计 = 1000   ← 和汇总槽位是同一笔钱
```

**天真地 `SUM(cost)` 会得到 2000，翻倍。** 正确做法是 `_slot(breakdown)` 谓词二选一：

```python
@staticmethod
def _slot(breakdown: bool):
    """Predicate selecting exactly one of the two stored granularities."""
    if breakdown:
        return DailyMetric.creative_id.is_not(None)
    return DailyMetric.creative_id.is_(None)
```

`_merged_slots()` 先用活动级槽位，只对**没有活动级槽位的桶**才回退到明细求和。

**ClickHouse 的 `events` 源天生没有这个问题** —— 它读的是原始事件流 `ad_events`，一条事件就是一次投放，**只有一种粒度**。这也是 6.2 节那个"反直觉事实"的答案：**`ClickHouseWarehouse` 宁可对海量明细表现场聚合，也不读物化视图**，因为明细表不存在重复计数的可能。这是一个用查询成本换正确性的取舍。

**但 `daily` 源不免疫，而且它现在是主路径。** `campaign_daily_metrics` 是 `daily_metrics` 的镜像，`adoptimizer warehouse sync` 刻意**不合并槽位**地搬运每一行——在搬运时合并等于把创意明细丢掉，镜像就成了有损副本。所以两种粒度在仓库里照样并存，只是活动级汇总行的 `creative_id` 从 `NULL` 变成了**空串**（`String` 列没有 NULL 语义）。

`ClickHouseWarehouse._delivery_relation()` 把同一条规则写成 SQL：先按 `(campaign_id, stat_date)` 塌缩成一行，每个度量取

```sql
if(countIf(creative_id = '') > 0,
   sumIf(cost, creative_id = ''),     -- 这个桶有汇总行，用它
   sumIf(cost, creative_id != ''))    -- 没有，用明细之和兜底
   AS cost
```

外层再对塌缩后的行 `sum()`。`countIf` 是**按桶**判定的，不是按活动——理由和主库一样：只有明细没有汇总行的活动不能因此从报表里消失。`events` 源走的是原表，一个字都不变。

顺带一个同源的坑：`_window_filter()` 在 `daily` 下必须写 `today() - toIntervalDay(days - 1)`。`stat_date` 是日历日，"最近 7 天含今天"往前只推 6 天；漏掉 `- 1` 就会多读一天，而且和 SQL 路径**对同一个请求给出不同答案**，两边都不报错。

**这个坑的真实代价**：项目里曾经有两个读取器（`repositories/campaigns.py` 的 `snapshots()` 与 `warehouse.py` 的 `_merged_slots()`），其中一个漏了槽位合并逻辑。结果是所有活动的花费被虚增 1.2～1.8 倍，进而让 `burn_rate` 异常检测把大量正常活动误判为超支。**没有任何报错、没有任何测试失败** —— 因为数字"看起来是合理的"，只是量级错了。

修复方式是给这两个读取器加一个**一致性测试**（`tests/integration/test_warehouse.py`），断言同一份数据从两条路径读出来的快照必须逐项相等。

**而同一个坑后来又出现了两次**：ClickHouse 的 `daily` 源，以及看板趋势 `AnalyticsService.timeseries()`——后者也是直接对 `daily_metrics` 裸 `SUM()`，趋势线因此高了一倍。两处都已收敛到同一条规则上（`MetricRepository.merged_slots()` / `_delivery_relation()`），并各自补了"必须和另一个读取器逐项相等"的测试。

**教训**：当同一个契约有多个实现时，"它们行为一致"必须被测试固化，不能靠人记住。而且**读取器的数量会随功能增长**——加一个仓库镜像、加一个看板图表，都是加了一条读取路径。第一次漂移靠人发现，第二次靠测试，第三次开始你得靠"新读取器必须复用旧读取器"的结构约束。

---

## 7. 查询优化技巧（入门向）

1. **尽量带时间条件**：`ad_events` 按 `toYYYYMM(event_time)` 分区，用 `event_time` 范围可剪枝分区
2. **优先查物化视图**：日报、看板类查询用 `campaign_creative_stats_mv` / `hourly_stats_mv`（本项目后端出于 6.3 的粒度考量直接读明细，但你自己写报表时物化视图仍然是更快的选择）
3. **避免 `SELECT *`**：列式存储下只选需要的列，收益远大于行式库
4. **用 `countIf` / `sumIf` 一次扫描出多个指标**，而不是跑多条 SQL（见 6.2 的实例）
5. **大结果集分页**：用 `LIMIT` + 排序键有序分页；深度分页可改用「上次最大值」游标
6. **慎用 `FINAL`**：会强制合并语义，数据量大时成本高
7. **批量写入**：小批量高频插入会造成合并压力；尽量批量

---

## 8. 与 MySQL / PostgreSQL 的对比

| 维度 | MySQL / PostgreSQL（典型 OLTP） | ClickHouse（OLAP） |
|---|---|---|
| 典型场景 | 订单、用户、权限、事务 | 日志、埋点、报表、监控 |
| 事务 ACID | 强 | 弱（不适合强事务） |
| 更新删除 | 行级更新常见 | 更偏向追加；更新需专门引擎 |
| JOIN | 复杂 JOIN 常见 | 大 JOIN 需谨慎优化 |
| 聚合性能 | 数据量大时吃力 | 强项 |
| 生态工具 | 极成熟 | 在分析栈中成熟 |

**本项目的实际分工正是这个结论的落地**：

- **PostgreSQL / SQLite**（主库）存：用户、权限、会话、活动主数据、提案、告警、审批状态、事件日志 —— 全是**需要事务和行级更新**的东西
- **ClickHouse**（可选仓库）存：广告投放明细事件 —— **海量、追加、只做聚合查询**

两者**共存**，通过 `MetricsWarehouse` Protocol 对上层的 Agent 完全透明。

---

## 9. 动手练习建议

1. `docker compose --profile analytics up -d` 起 ClickHouse，用 `clickhouse-client` 对 `ad_events` 插入若干行测试数据
2. 等待或触发合并后，查询两个物化视图是否有汇总行
3. 打开 `backend/src/adoptimizer/infra/warehouse.py`，把 `ClickHouseWarehouse.campaign_snapshots` 里的 SQL 复制到客户端执行，对比返回结果
4. **进阶（最有价值的一个）**：在 `backend/.env` 里把 `CLICKHOUSE__ENABLED` 改成 `true` 但指向一个不存在的地址，启动后端，观察日志里的 `clickhouse_enabled_but_unreachable_falling_back_to_sql`，确认功能一切正常 —— 亲手验证降级路径
5. **进阶**：读 `tests/integration/test_warehouse.py`，找到 `test_it_agrees_with_the_warehouse_reader` 和 `test_it_agrees_with_the_warehouse_trend`，理解它们在防什么；再找 `test_a_rollup_bucket_is_not_summed_with_its_breakdown`，看仓库侧那条规则是怎么被断言的

---

## 10. 小结

- ClickHouse 是**列式 OLAP** 数据库，适合广告**事件明细 + 聚合分析**；本项目用它作**可选**的分析仓库，默认关闭。
- 表设计用 **MergeTree / ReplacingMergeTree / SummingMergeTree** 与**物化视图**完成分层存储：明细进 `ad_events`，报表走物化视图，配置类实体用 Replacing 表达最新状态。
- 后端通过 **`MetricsWarehouse` Protocol** 抽象两条读取路径（ClickHouse 明细聚合 / SQL 聚合表），工厂按配置与连通性选择，**连不上自动降级**，调用方不感知。
- **双粒度重复计数**是"聚合表"这类存储的陷阱，不是某个后端的陷阱：同一天同一笔花费同时存在活动级槽位与素材级明细，必须二选一。ClickHouse 的 `events` 源读原始事件流，只有一种粒度，天然免疫；但 `daily` 源读的是主库那张表的镜像，两种粒度一起搬过去了，所以 `_delivery_relation()` 在 SQL 里重做了同一次塌缩。
- 与 PostgreSQL 互补而非替代：事务型数据留在主库，海量追加型事件进仓库。

下一篇：《部署与运行指南》。
