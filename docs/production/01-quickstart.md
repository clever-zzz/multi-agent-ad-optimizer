# 01 快速开始

四条路径，从"完全不装东西"到"接近生产的容器栈"。**推荐先走路径 A 或 B**，因为它们零外部依赖。

---

## 前置条件

| 组件 | 版本 | 用途 | 是否必需 |
|---|---|---|---|
| Python | 3.12+ | 后端运行时 | 路径 A/B/C 必需 |
| Node.js | 20.11+ | 前端构建 | 路径 A/B/D 必需 |
| npm | 10+ | 前端包管理 | 同上 |
| Docker + Compose | 24+ / v2 | 容器栈 | 仅路径 D |
| make | GNU make 4+ | 快捷命令 | 可选（Windows 用 `scripts\*.ps1` 替代） |
| PostgreSQL / Redis / ClickHouse | 15+ / 7+ / 24+ | 生产依赖 | 本地开发**不需要** |

默认配置（`LLM__PROVIDER=mock` + SQLite + `DATA_MODE=mock`）不需要任何 API Key、不需要联网、不需要容器。

---

## 路径 A：Windows PowerShell（推荐）

```powershell
cd multi-agent-ad-optimizer

# 1. 安装依赖。国内网络需要代理时加 -Proxy
.\scripts\setup.ps1
.\scripts\setup.ps1 -Proxy http://127.0.0.1:7897

# 2. 建表 + 灌种子数据 + 同时启动 API(:8000) 与 Web(:5173)
.\scripts\dev.ps1
```

`setup.ps1` 做的事：

1. 在 `backend\.venv` 建虚拟环境
2. `pip install -e ".[dev,analytics]"`（analytics = CVXPY 凸优化求解器）
3. 复制 `backend\.env.example` → `backend\.env`
4. `npm ci`（`frontend\`，与 CI 和镜像构建一致；没有 lockfile 时退回 `npm install` 并提示你提交）
5. 复制 `frontend\.env.example` → `frontend\.env`

`dev.ps1` 做的事：

1. `adoptimizer migrate`（首次运行会创建 19 张表）
2. `adoptimizer seed`（8 个活动 + 21 天指标 + 管理员账号）
3. 后台起 uvicorn（`--reload`）与 Vite dev server
4. 两路日志合并输出，`Ctrl+C` 一起停

常用参数：

```powershell
.\scripts\dev.ps1 -SkipMigrate -SkipSeed   # 数据已就绪，只想重启
.\scripts\dev.ps1 -BackendOnly             # 只调 API
.\scripts\dev.ps1 -FrontendOnly            # 只调前端
.\scripts\dev.ps1 -BackendPort 9000        # 换端口（会自动同步给 Vite 代理）
```

打开 <http://localhost:5173>，登录：

```
邮箱：admin@adoptimizer.dev
口令：Adm1n!ChangeMe
```

> 这两个值来自 `backend\.env` 的 `SECURITY__BOOTSTRAP_ADMIN_*`。**第一次登录后立刻在「设置 → 修改密码」里改掉。**

---

## 路径 B：make（macOS / Linux / Git Bash）

```bash
make install     # = backend-install + frontend-install
make dev         # API :8000 + Web :5173，Ctrl+C 一起停
```

其他常用目标（`make help` 查看全部）：

```bash
make migrate          # 应用数据库迁移
make seed             # 灌种子数据
make run-once         # 不开服务，直接从 CLI 跑一轮优化并打印 summary
make check            # 跑完 CI 的全部门禁
make up / make down   # Docker Compose 起停
make images           # 本地构建两个镜像
```

> Windows 上 `make dev` 与 `make clean` 会转调 `scripts\dev.ps1` / `scripts\clean.ps1`，因此需要 PowerShell 在 PATH 中（默认就有）。

---

## 路径 C：只跑后端（最小验证）

想先确认 Agent 闭环本身能工作，不碰前端：

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,analytics]"      # Windows
# .venv/bin/pip install -e ".[dev,analytics]"        # macOS/Linux
cp .env.example .env

.venv/Scripts/adoptimizer migrate
.venv/Scripts/adoptimizer seed
.venv/Scripts/adoptimizer run --max-iterations 2
```

`run` 会打印完整的 run summary，形如：

```json
{
  "run_id": "run_01M20W625V0P54D3VHRKB479TN",
  "completed": true,
  "status": "succeeded",
  "summary": {
    "status": "succeeded",
    "iterations": 2,
    "campaigns": 8,
    "creatives_generated": 24,
    "bidding_decisions": 8,
    "budget_adjustments": 8,
    "actions": 11,
    "actions_proposed": 58,
    "actions_suppressed": 47,
    "critic_findings": 51,
    "tool_preflights": 36,
    "preflights_blocked": 0,
    "action_counts": {"pause_campaign": 8, "pause_creative": 2, "refresh_creative": 1},
    "alerts_raised": 9,
    "health": {"status": "warning", "score": 69.52, "portfolio": {"ctr": 0.01339, "roas": 11.5251}, "...": 0},
    "usage": {"prompt_tokens": 1824, "completion_tokens": 1894, "total_tokens": 3718, "cost_usd": 0.0, "calls": 8, "duration_s": 1.335},
    "messages": [{"agent": "monitor", "content": "Monitored 8 campaigns. Health 69.5/100 (warning)...", "iteration": 0}, "..."],
    "tools": {"calls": 46, "writes": 36, "dry_runs": 36, "refusals": 0, "...": 0}
  }
}
```

`usage` 由网关按 run 归集，与 `llm_spend` 表逐行同源——控制台上的 token 数和花费不会和账本对不上。两点容易误读：mock provider 不计费，所以 `cost_usd` 恒为 `0.0`（token 数仍然是真实统计的）；缓存命中的调用不重复记账，因此同一批 prompt 紧接着再跑一次，`usage` 可能显示 0，这不是丢数据。

再启动 API 并用 Swagger 手动点一遍：

```bash
.venv/Scripts/adoptimizer serve
```

- OpenAPI 文档：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 存活探针：<http://localhost:8000/healthz>
- 就绪探针（含依赖聚合）：<http://localhost:8000/readyz>
- Prometheus 指标：<http://localhost:8000/metrics>
- 运行时配置（免鉴权，只暴露安全字段）：<http://localhost:8000/system/info>

命令行取 token，方便 curl：

```bash
.venv/Scripts/adoptimizer token --email admin@adoptimizer.dev
# 口令走隐藏输入，不会进 shell history
```

### 数据入口：采集与调度

优化闭环吃的指标不只有种子数据这一条路。先干跑一趟，报告会逐条说明哪些记录会被拒、为什么：

```bash
.venv/Scripts/adoptimizer ingest --source synthetic --days 7 --dry-run
```

去掉 `--dry-run` 就真写库。落库是 upsert，一个 `(活动, 创意, 日期)` 槽位只有一行，所以重复拉不会把指标翻倍：

```bash
.venv/Scripts/adoptimizer ingest --source synthetic --days 7
```

调度器把这件事从"一次性命令"变成"按日历补窗口"：

```bash
.venv/Scripts/adoptimizer scheduler --once     # 跑一趟就退出，K8s CronJob 调的就是它
.venv/Scripts/adoptimizer scheduler            # 常驻循环，需要 INGEST__SCHEDULER_ENABLED=true
```

窗口是从日历推出来的，不是从游标推的：漏跑一次，下一趟会往回补到"上一个已覆盖日的次日"；补不动了（超过 `INGEST__MAX_CATCHUP_DAYS`）就在 `plan.reason` 里报 `capped`、在 `plan.detail` 里给出确切的回填命令，而不是悄悄把窗口收窄。多副本同时跑也安全——数据库租约保证同一个 feed 同一时刻只有一趟在拉。

干跑会留下一条 `dry_run=true` 的批次记录（这是刻意的：排练过和从没跑过必须能区分开），但一行指标都不写、水位也不推进。

---

## 路径 D：Docker Compose

完整的 PostgreSQL + Redis + API + Web 栈：

```bash
docker compose -f deploy/compose/docker-compose.yml up --build -d
docker compose -f deploy/compose/docker-compose.yml ps      # 等到全部 healthy
```

- Web 控制台：<http://localhost:8080>
- API：<http://localhost:8000>（`/docs`）

可选 profile：

```bash
# 带后台 worker（arq）
docker compose -f deploy/compose/docker-compose.yml --profile worker up -d

# 带 ClickHouse 分析仓库（DATA_MODE=warehouse）
docker compose -f deploy/compose/docker-compose.yml --profile analytics up -d
```

停止并清数据：

```bash
docker compose -f deploy/compose/docker-compose.yml down -v
```

生产化覆盖层（固定 tag、只读根文件系统、副本数、资源上限）见 [04 部署](04-deployment.md)。

---

## 验证它真的在工作

跑完上面任一路径后，按顺序确认这 7 件事：

**1. 依赖健康**

```bash
curl -s http://localhost:8000/readyz | python -m json.tool
```

期望 `status: ok`，且 `checks.database.status == "ok"`、`checks.llm.provider == "mock"`、`checks.orchestrator.mode` 为 `langgraph`（装了 langgraph）或 `sequential`（未装，等价降级）。

**2. 种子数据到位**

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@adoptimizer.dev","password":"Adm1n!ChangeMe"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s "http://localhost:8000/api/v1/campaigns?page_size=50" -H "Authorization: Bearer $TOKEN" \
  | python -c 'import sys,json;d=json.load(sys.stdin);print(d["total"],"campaigns")'
```

期望输出 `8 campaigns`。

**3. 优化 run 能跑完**

```bash
curl -s -X POST http://localhost:8000/api/v1/runs \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"max_iterations":2,"window_days":7}'
```

记下 `id`，几秒后：

```bash
curl -s http://localhost:8000/api/v1/runs/<RUN_ID> -H "Authorization: Bearer $TOKEN" \
  | python -c 'import sys,json;d=json.load(sys.stdin);print(d["run"]["status"], d["run"]["summary"]["action_counts"])'
```

期望 `status == "succeeded"` 且 `action_counts` 非空。

**4. 审批门生效**

`SECURITY__REQUIRE_ACTION_APPROVAL=true` 时，未审批的动作直接执行必须被拒：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://localhost:8000/api/v1/actions/<ACTION_ID>/execute \
  -H "Authorization: Bearer $TOKEN"
```

期望 `409`（Conflict，未审批）。审批后重试期望 `200`。

**5. RBAC 生效**

用 viewer 角色账号调写接口，期望 `403` 且 body 是 problem document：

```json
{"type":"https://adoptimizer.dev/errors/permission_denied","title":"Permission denied","status":403,"detail":"...","code":"permission_denied"}
```

**6. 前端联通**

打开 <http://localhost:5173>（或 Compose 的 :8080），登录后：
- Dashboard 有 KPI 卡片和趋势图（不是骨架屏卡住）
- 「运行」页能看到刚才那次 run，点进去有实时时间线
- 「动作」页有待审批提案，点「批准」→「执行」状态会变
- 「告警」页有 open 告警，可以 ack / resolve
- 「审计」页（admin）能看到你刚才每一步操作

**7. 采集与调度能跑**

```bash
.venv/Scripts/adoptimizer scheduler --once     # 第一次：plan.reason 是 first / catchup，真的拉
.venv/Scripts/adoptimizer scheduler --once     # 第二次：plan.reason 是 covered，不再重复拉
```

再看三个只读状态接口：

```bash
curl -s http://localhost:8000/api/v1/ingest/schedule -H "Authorization: Bearer $TOKEN" | python -m json.tool
curl -s http://localhost:8000/api/v1/ingest/sources  -H "Authorization: Bearer $TOKEN" | python -m json.tool
curl -s "http://localhost:8000/api/v1/ingest/batches?page_size=5" -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

`/schedule` 是纯只读的——它只做计划、不拉数据，所以轮询它不会启动任何工作。三个 GET 都只要 `metrics:read`（viewer 也有），只有 `POST /ingest/metrics` 需要 `metrics:write`（admin / ingestor）。`plan.reason` 的完整取值与排查表见 [05 运维手册](05-operations-runbook.md)。

---

## 常见问题

**`pip install` 超时 / `npm install` 卡在 registry**

```powershell
.\scripts\setup.ps1 -Proxy http://127.0.0.1:7897
```

脚本会同时设置 `HTTP_PROXY` / `HTTPS_PROXY` / `NPM_CONFIG_PROXY` / `NPM_CONFIG_HTTPS_PROXY`。

**端口被占用**

```powershell
.\scripts\dev.ps1 -BackendPort 9000      # API 换端口，Vite 代理自动跟随
```

前端端口在 `frontend\vite.config.ts` 的 `server.port`（`strictPort: false`，被占用会自动 +1）。

**Windows 控制台中文乱码 / GBK 报错**

后端 CLI 已在入口把 stdout 重配为 UTF-8。如果仍然乱码，先执行 `chcp 65001`。

**`alembic upgrade head` 报 table already exists**

说明你先启动了服务（SQLite 会自动建表），再跑迁移。`init_database` 在自动建表后会写入 `alembic_version` 戳，正常情况下不会冲突；如果确实撞上了，删掉 `backend\adoptimizer.db` 重来，或只用迁移这一条路径。

**前端能打开但所有请求 401**

access token 默认 30 分钟过期，前端会自动用 refresh token 轮换。如果 refresh 也失效（例如后台重启换了 `SECURITY__JWT_SECRET`），重新登录即可。

**`readyz` 返回 503**

看 body 里 `checks` 哪一项是 `unavailable`。最常见是 Redis 没起（把 `REDIS__ENABLED=false` 可降级为进程内缓存）或数据库连不上。

**改了配置不生效**

配置在进程启动时读取一次并缓存（`get_settings` 用 `lru_cache`）。改 `.env` 后必须重启后端。

---

## 下一步

- 想理解它是怎么搭起来的 → [02 架构](02-architecture.md)
- 想直接调接口 → [03 API 参考](03-api-reference.md)
- 准备上线 → [04 部署](04-deployment.md)
- 上线前请务必读 → [06 安全](06-security.md) 与 [08 限制](08-limitations-and-roadmap.md)