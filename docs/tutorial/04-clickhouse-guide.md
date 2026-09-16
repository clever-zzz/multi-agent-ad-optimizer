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
  image: clickhouse/clickhouse-server:24.8-alpine
  profiles: ["analytics"]
  volumes:
    - clickhouse-data:/var/lib/clickhouse
    - ../../init-scripts/clickhouse:/docker-entrypoint-initdb.d:ro
```

> 这个服务**不发布端口**，只挂在内部 `backend` 网络上。后端通过 `CLICKHOUSE__HOST=clickhouse` 找它，见 2.2 前面那段环境变量。

启动：

```bash
cd deploy/compose
cp .env.example .env        # 填两个必填项：SECURITY__JWT_SECRET、SECURITY__BOOTSTRAP_ADMIN_PASSWORD
docker compose --profile analytics up -d
docker compose ps           # 等到 clickhouse 变 healthy
```

初始化 SQL 挂载到容器的 `/docker-entrypoint-initdb.d`，**首次启动**会按文件名顺序执行 `init-scripts/clickhouse/` 下的脚本：`01_create_tables.sql` 建事件表、主数据表与两个物化视图，`02_daily_metrics.sql` 建日报聚合表 `campaign_daily_metrics`。

> 注意「首次启动」：初始化脚本只在数据目录为空时跑。已经起过一次的卷不会重新执行，新加的表要用 `docker compose exec clickhouse clickhouse-client --multiquery < init-scripts/clickhouse/02_daily_metrics.sql` 之类的方式手动补，或者 `down -v` 重来。

然后让后端改走仓库路径。**三个开关要一起拨**，少一个都不会真的走仓库：

```
DATA_MODE=warehouse                 # 报表读仓库，而不是主库的 SQL 聚合降级
CLICKHOUSE__ENABLED=true            # 仓库用 ClickHouse，而不是 SqlAggregateWarehouse
CLICKHOUSE__METRICS_SOURCE=daily    # 读仓库里的哪张表（默认值，写出来是为了自解释）
```

- **本机跑后端**：改 `backend/.env`，另外还要给 `CLICKHOUSE__HOST=localhost`（默认值就是它）。
- **compose 里跑后端**：改 `deploy/compose/.env`。`docker-compose.yml` 的 `x-backend-env` 会把 `CLICKHOUSE__*` 透传给 `api` / `worker`，`CLICKHOUSE__HOST` 的默认值已经是 `clickhouse`，所以只需要把 `DATA_MODE` 和 `CLICKHOUSE__ENABLED` 拨过去。

`CLICKHOUSE__METRICS_SOURCE` 只有两个取值。`daily` 读 `campaign_daily_metrics`，那是 `warehouse sync` 的落点，也是**当前唯一有写入方的表**；`events` 读原始事件流 `ad_events`，形状更富（带 device/country/gender），但这个构建里没有任何代码往里写，所以配它会在启动时告警 —— 原因见 2.3。

### 2.1 验证服务

HTTP 接口（默认 8123）：

```bash
curl "http://localhost:8123/ping"
```

期望返回 `Ok.`。

在容器内用客户端（compose 里不发布端口，所以本机那条 `curl localhost:8123` 只在你单独跑了 ClickHouse 容器时成立）：

```bash
docker compose -f deploy/compose/docker-compose.yml exec clickhouse \
  clickhouse-client --query "SELECT version()"

# 顺手确认后端到仓库的网络是通的，这比本机 curl 更接近真实读取路径
docker compose -f deploy/compose/docker-compose.yml exec api \
  python -c "import urllib.request as u; print(u.urlopen('http://clickhouse:8123/ping').read())"
```

### 2.2 确认后端真的用上了

后端有 `/readyz` 与管理员健康端点会上报 `clickhouse_enabled`。也可以看启动日志：

- `clickhouse_connected` —— 连上了，走仓库路径
- `clickhouse_unavailable` —— 驱动没装（需要 `pip install -e ".[clickhouse]"`）
- `clickhouse_enabled_but_unreachable_falling_back_to_sql` —— 配了但连不上，**自动回退**到 SQL 聚合路径
- `clickhouse_events_source_has_no_writer` —— 连上了，但 `metrics_source` 配的是 `events`，见 2.3

再确认数据真的搬进去了：

```bash
adoptimizer warehouse status                    # 写入侧通不通；不通时退出码非零
adoptimizer warehouse sync --days 30 --dry-run  # 先看这个窗口有多少行
adoptimizer warehouse sync --days 30            # 真搬。可反复重跑，见 4.2
```

### 2.3 为什么"连上了"还不算配对了

降级机制在这里会反过来咬人。读 ClickHouse 读到空结果时，后端**按设计**回退到主库，所以把 `metrics_source` 配成一张没有写入方的表，表现是"一切正常"：`/readyz` 绿、报表有数、没有异常 —— 数字其实一直来自主库，而你以为在读仓库。

唯一能看穿它的两个地方是启动日志里的 `clickhouse_events_source_has_no_writer`，和 Prometheus 上的 `warehouse_reads_total{outcome="empty"}` 持续为高。这也是 `build_warehouse` 在连上之后立刻调一次告警的原因：**能连上不代表配对了**。

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

本项目中 **`campaigns`**、**`creatives`**、**`audience_profiles`** 使用 `ReplacingMergeTree(updated_at)` 或 `ReplacingMergeTree(created_at)`，适合「同一实体多次写入，保留最新快照」的场景。**`campaign_daily_metrics`**（`02_daily_metrics.sql`）用的也是 `ReplacingMergeTree(updated_at)`，排序键 `(campaign_id, creative_id, stat_date)` 刻意和主库那张表的唯一约束对齐 —— 目的就是让重放一次 backfill **就地修正**同一行，而不是追加一份副本。

**注意**：查询时若未处理重复行，可能仍看到旧版本，直到合并完成；严谨场景可用 `FINAL` 或自行聚合（有性能代价）。

**这句"注意"在本项目里不是理论问题，而是已经付过一次的代价。**

`adoptimizer warehouse sync` 被设计成可以对同一个 30 天窗口反复重跑（漏一天补一天、失败重跑都靠这个性质），所以"同一行存在多个版本"是**常态**而不是边界情况。而 `ReplacingMergeTree` 的去重要等 ClickHouse 后台挑到那次合并，合并之前读到的就是全部版本。结果是：从第二次 sync 起，窗口内的曝光、花费、收入全部翻倍 —— 而 ROAS 是两个同样翻倍的数相除，看起来完全正常；CTR、CPA 同理。**下游没有任何一处会报错**，翻倍的总量就这么直接进了 Agent 的预算分配与出价打分。

所以 `ClickHouseWarehouse` 读 `campaign_daily_metrics` 时一律带 `FINAL`（`infra/warehouse.py` 的 `_relation` 属性），把去重放在读取时而不是等后台合并。`ad_events` 是普通 `MergeTree` —— 一行就是一次投放，没有"版本"可言 —— 因此原样读，不付这份代价。两个回归测试在 `tests/integration/test_warehouse.py`：一个断言重放的窗口读出来不翻倍，另一个断言 events 源的 SQL 里**没有** `FINAL`（不该付的成本也别付）。

> 因为本机通常没有 Docker，这两个测试断言的是**生成的 SQL 形状**而不是真实合并行为。它们能挡住"忘了加 `FINAL`"这类回归，挡不住"`FINAL` 语义本身理解错了"。真上 ClickHouse 时值得手工重放一次 30 天窗口对一下总花费。

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

下表与 `init-scripts/clickhouse/` 下的两个脚本一致：前八张来自 `01_create_tables.sql`，最后一张来自 `02_daily_metrics.sql`。

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
| `campaign_daily_metrics` | ReplacingMergeTree(updated_at) | **日报聚合表**：主库 `daily_metrics` 的镜像、`warehouse sync` 的落点，也是默认 `metrics_source=daily` 的读取目标（读取时带 `FINAL`，见 4.2） |

**设计思路一句话**：「明细进 `ad_events`，常用报表走物化视图；配置类实体用 ReplacingMergeTree 表达最新状态。」

实际跑起来的主力其实是第九张。平台 API 交回来的是**日报**而不是逐次曝光，所以 `warehouse sync` 把主库的 `daily_metrics` 镜像进 `campaign_daily_metrics`，后端默认也从这张表读；`ad_events` 是等真有事件流之后才切的形状。这一点在 6.2 会再展开。

### 6.2 两条读取路径，一个 Protocol

真正的重点在这里。`backend/src/adoptimizer/infra/warehouse.py` 的文件 docstring：

> Two implementations share one protocol: ClickHouse for real event volumes and a SQL aggregate reader for environments without a warehouse. **Callers never branch on which one is active.**

结构：

```
:83   class MetricsWarehouse(Protocol)      ← 契约
:105  class SqlAggregateWarehouse           ← 读主库的 daily_metrics 聚合表
:303  class ClickHouseWarehouse             ← 读 ClickHouse：默认 daily 源，可选 events 源
:730  async def build_warehouse(session)    ← 工厂，按配置与连通性选实现
```

（行号会随改动漂移，认类名比认行号可靠。）

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
            warehouse.warn_if_source_has_no_writer()
            return warehouse
        logger.warning("clickhouse_enabled_but_unreachable_falling_back_to_sql")
    return SqlAggregateWarehouse(session)
```

三重降级：没启用 → SQL；启用了但驱动没装 → SQL；启用了但连不上 → SQL。**调用方永远不需要知道当前是哪个。**

连上之后那行 `warn_if_source_has_no_writer()` 是给降级机制打的补丁，理由和 2.3 是同一件事：降级很好用，但它同时也会**掩盖配错** —— 读到空表就静默回退主库，于是"配的源没有写入方"这种错误在功能上一切正常，只有日志和指标能看出来。所以能在连上的当场说出来，就不要留给运维去猜。

`ClickHouseWarehouse.campaign_snapshots` 的真实 SQL。同一个方法按 `metrics_source` 生成两种形状，下面是 **`events` 源**（原始事件流，一行一次投放，所以"计数"就是聚合）：

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

而**默认的 `daily` 源**读的是已经聚合过的镜像表，三处都不一样：度量从 `countIf` 换成 `sum(...)`，`FROM` 换成 6.3 节那个塌缩子查询，表名后面带 `FINAL`（4.2）：

```sql
SELECT campaign_id, sum(impressions) AS impressions, /* ... */ sum(revenue) AS revenue
FROM (
  SELECT campaign_id, stat_date,
         if(countIf(creative_id = '') > 0,
            sumIf(cost, creative_id = ''),
            sumIf(cost, creative_id != '')) AS cost,   /* 每个度量一份 */
         /* ... */
  FROM {db:Identifier}.campaign_daily_metrics FINAL
  WHERE stat_date >= today() - toIntervalDay(greatest({days:UInt16}, 1) - 1)
  GROUP BY campaign_id, stat_date
)
GROUP BY campaign_id
```

窗口谓词里那个 `- 1` 也是必须的：`stat_date` 是**日历日**，"最近 7 天含今天"只能往前推 6 天。漏掉它，仓库会对同一个请求给出比 SQL 路径多一天的数据，而两边都不报错。`greatest(..., 1)` 则是挡 `days=0`：占位符按 `UInt16` 绑定，0 减 1 会绕回 65535，读的是整张表而不是空集。

注意几个工程细节：
- **参数化查询**：`{db:Identifier}`、`{days:UInt16}`、`{ids:Array(String)}` 是 ClickHouse 的类型化参数占位符，不是字符串拼接 —— 防注入
- **阻塞驱动移出事件循环**：`_to_thread()` 用 `asyncio.to_thread` 包了同步的 `clickhouse_connect` 调用，不阻塞 FastAPI 的事件循环
- **查询失败返回空列表而不是抛异常**（`_query` 的 `except` 分支），由调用方的健康检查去暴露问题；同时每次读取都在 `warehouse_reads_total{backend,outcome}` 上记一笔（`ok` / `empty` / `degraded`）—— 因为"静默回退到主库"和"确实没数据"必须能被区分开，否则 2.3 那种配错永远查不出来

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
6. **慎用 `FINAL`**：会强制合并语义，数据量大时成本高 —— 但"慎用"不等于"不用"。本项目在 `campaign_daily_metrics` 上是**必须**用的（4.2）：写入侧被设计成可重放，读取侧不带 `FINAL` 就会把重放的版本全加起来，而这不报错。这笔交易是"每次查询付一点合并成本"换"数字不会翻倍"，在正确性面前没有悬念。真要省，可以改用 `argMax` 自行聚合，或把重放窗口收窄
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
- 后端通过 **`MetricsWarehouse` Protocol** 抽象两条读取路径（ClickHouse / 主库 SQL 聚合表），工厂按配置与连通性选择，**连不上自动降级**，调用方不感知。ClickHouse 一侧默认读 `campaign_daily_metrics`（`metrics_source=daily`），因为那是当前唯一有写入方的表；`events` 是等真有事件流之后才切的显式选项，配早了会告警。
- **降级会掩盖配错**：读到空就回退主库，所以"配的源没有写入方"在功能上完全看不出来。这类问题只能靠启动日志和 `warehouse_reads_total` 的 `outcome` 计数兜住 —— 能连上不等于配对了。
- **双粒度重复计数**是"聚合表"这类存储的陷阱，不是某个后端的陷阱：同一天同一笔花费同时存在活动级槽位与素材级明细，必须二选一。ClickHouse 的 `events` 源读原始事件流，只有一种粒度，天然免疫；但 `daily` 源读的是主库那张表的镜像，两种粒度一起搬过去了，所以 `_delivery_relation()` 在 SQL 里重做了同一次塌缩。
- 与 PostgreSQL 互补而非替代：事务型数据留在主库，海量追加型事件进仓库。

下一篇：《部署与运行指南》。
