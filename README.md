# 环境检测实验室结果核对与 IMAP 采集 API (FastAPI)

两套能力合在一个服务中：

1. **IMAP 采集**：从仪器共享邮箱按 UID 递增收取 UTF-8 CSV 附件，整封校验
   通过后原子写入 SQLite，并记录邮箱 `UIDVALIDITY` 与已完成 UID；
2. **归属核对**：把缺少样品号的仪器结果按**孔位 + 检测项目 + 时间容差**
   归入进样计划，在所有最优配对方案中识别歧义，供人工确认后再导入 LIMS。

---

## 一、IMAP 采集

### 采集规则

- 连接参数全部来自环境变量（见下表），请求体为空，直接 `POST` 触发；
- 按 **UID 递增**取信；已完成 UID 自动跳过，**已提交邮件不重入**；
- 仅处理 **UTF-8 编码的 CSV 附件**（`.csv`），非 CSV 附件忽略，
  无 CSV 附件的邮件可记为完成（零行）；
- CSV 表头固定为
  `result_id,well_position,analyte,measured_at,measured_value`；
- `measured_at` 解析为 **UTC 整数秒**（支持纯整数 Unix 秒或 ISO-8601，
  朴素时间按 UTC）；`measured_value` **始终保留字符串**，不做数值化；
- 每封邮件全部 CSV 附件**合计最多 100 行**；
- 同一结果 ID：内容完全一致则去重；任一内容字段不同则**拒绝整封**；
- 每封邮件的全部 CSV 校验通过后，结果与进度在**同一个 SQLite 事务**提交；
- 断线、坏行、取信失败、内容冲突、UIDVALIDITY 变化 →
  **停在该封之前**，不写入、不推进进度，下次调用从该 UID 继续（重启可续采）；
- `UIDVALIDITY` 与已保存值不一致时**拒绝采集并保留旧数据**。

### 并发安全

两个 API 进程（`api-a` / `api-b`）共享同一个 SQLite 文件：

- SQLite 启用 **WAL + busy_timeout**，读写互不阻塞；
- 采集使用 `collect_lock` 单行心跳锁互斥：同一时刻只有一个进程采集，
  第二个请求收到 `409`，因此不会漏信、重复入库或越过失败邮件；
- 持锁进程崩溃后，心跳超过阈值（默认 300 秒）的残留锁可被安全接管。

### 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `IMAP_HOST` | （必填） | IMAP 主机 |
| `IMAP_PORT` | `993` | IMAP 端口 |
| `IMAP_USERNAME` | （必填） | 登录用户名 |
| `IMAP_PASSWORD` | 空 | 登录密码 |
| `IMAP_MAILBOX` | `INBOX` | 邮箱文件夹 |
| `IMAP_USE_SSL` | `true` | 是否使用 SSL（本地演示源为 `false`） |
| `IMAP_CONNECT_TIMEOUT` | `30` | 连接/读写超时（秒） |
| `IMAP_BATCH_MAX_MESSAGES` | `200` | 单次调用最多处理邮件数 |
| `COLLECT_DB_PATH` | `/data/collect.db` | SQLite 文件路径 |
| `COLLECT_LOCK_STALE_SECONDS` | `300` | 采集锁过期接管阈值（秒） |

### API

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /api/v1/collect/imap` | 触发一轮增量采集，无请求体 |
| `GET  /api/v1/collect/status` | UIDVALIDITY、完成进度、锁状态 |
| `POST /api/v1/results/query` | 按最多 50 个结果 ID 查询，返回可直接用作 `result_rows` 的数组 |

`POST /api/v1/results/query` 请求体：

```json
{ "result_ids": ["R-0001", "R-0002"] }
```

响应中的 `result_rows` 字段结构与核对接口完全一致，可直接复制到
`POST /api/v1/reconcile` 的 `result_rows`。

### 采集响应示例（停在坏行邮件之前）

```json
{
  "status": "stopped",
  "mailbox": "lab@imap-server/INBOX",
  "uidvalidity": 1700000000,
  "processed_uids": [1, 2, 3],
  "processed_count": 3,
  "inserted_rows": 6,
  "duplicate_rows": 0,
  "messages_without_csv": 1,
  "failed_uid": 4,
  "reason": "UID 4: 无法解析时间 'not-a-timestamp'",
  "remaining_pending_uids": [4, 5, 6]
}
```

---

## 二、归属核对

- 输入两组各不超过 50 条 JSON：
  - **计划行**：唯一行号 `row_id`、样品号 `sample_no`（缺失为 `null` 或空串）、
    孔位 `well_position`、检测项目 `analyte`、计划时间 `planned_at`（整数 UTC 秒）。
  - **仪器结果**：唯一结果号 `result_id`、孔位、检测项目、实测时间 `measured_at`、
    检测值 `measured_value`（数字或字符串，如 `"未检出"`，原样保留）。
- 仅当孔位、检测项目相同，且 `|实测时间 - 计划时间| <= tolerance_seconds`
  时允许配对，一对一。
- 优化目标为字典序：
  1. **配对数最多**；
  2. 在配对数最多的方案中，**绝对时间差总和最小**。
- 对每条结果返回它在**全部最优方案**中：
  - `possible_plan_row_ids`：可能归属的计划行号（可能为空）；
  - `can_be_unmatched`：是否可能在某个最优方案中未匹配。
- 只有 **归属始终唯一且始终匹配** 时才回填 `sample_no`，否则为 `null`，
  避免歧义结果被误导入；检测值始终原样带回。

### 算法

二分图（结果 ↔ 计划行）上的**最小费用最大流（MCMF，Dijkstra + 势函数）**，
边权为绝对时间差，天然实现“先最大流、再最小费用”。得到一个最优方案后，在
**reduced cost = 0 的残余图**上求强连通分量（Kosaraju），一次性刻画所有最优方案。
全部为整数运算，无浮点误差。复杂度对 50+50 的上限绰绰有余。

---

## 三、启动（Docker Compose）

```bash
docker compose up --build
```

启动三个服务：

| 服务 | 地址 | 作用 |
| --- | --- | --- |
| `imap-server` | 容器内 `3143` | 纯标准库本地 IMAP 演示源，预置 6 封邮件，无需外部账号 |
| `api-a` | <http://localhost:8000> | API 进程 A |
| `api-b` | <http://localhost:8010> | API 进程 B，与 A 共享同一 SQLite，验证并发互斥 |

并发互斥可手动复现：同时向两个端口发起采集请求，其中一个返回 `409`：

```bash
curl -s -o /tmp/a.json -w "A: %{http_code}\n" -X POST http://localhost:8000/api/v1/collect/imap &
curl -s -o /tmp/b.json -w "B: %{http_code}\n" -X POST http://localhost:8010/api/v1/collect/imap &
wait
```

SQLite 与演示邮件分别保存在命名卷 `collect-data`、`mailbox-data` 中。

### 一键体验采集 → 查询 → 核对

```bash
# 1) 首次采集（预置的第 4 封邮件含坏行，会停在 UID4 之前）
curl -s -X POST http://localhost:8000/api/v1/collect/imap | python -m json.tool

# 2) 查看进度
curl -s http://localhost:8000/api/v1/collect/status | python -m json.tool

# 3) 按结果 ID 查询，拿到 result_rows
curl -s -X POST http://localhost:8000/api/v1/results/query \
  -H "Content-Type: application/json" \
  --data @demo/result_query.json | python -m json.tool

# 4) 原核对接口（演示请求）
curl -s -X POST http://localhost:8000/api/v1/reconcile \
  -H "Content-Type: application/json" \
  --data @demo/request.json | python -m json.tool
```

### 断线演示

在 `docker-compose.yml` 的 `imap-server` 上取消 `IMAPD_DROP_UID: "3"`
注释后重启，服务器会在 FETCH UID 3 发送中途断开一次 TCP：
采集响应 `status=stopped`、`failed_uid=3`，再次调用即从 UID 3 续采。

### 不用 Docker 的本地演示

```bash
bash demo/run_local_demo.sh
# 自定义端口/目录：
PORT_A=8500 PORT_B=8510 IMAP_PORT=3148 RUN_DIR=/tmp/lab-demo \
  bash demo/run_local_demo.sh
```

脚本会启动本地 IMAP 源与两个真实 uvicorn 进程，完整演示坏行停止、
并发 409 互斥、结果查询、修复后续采与去重。

## 四、文档与测试

- Swagger UI（含采集/查询/核对示例，可在线发请求）: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
- OpenAPI JSON: <http://localhost:8000/openapi.json>
- 健康检查: <http://localhost:8000/health>

```bash
# 容器内运行全部测试（无需本地 Python 环境）
docker compose --profile test run --rm tests

# 或本地
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

测试覆盖：CSV/邮件解析与编码校验、SQLite 事务原子性/去重/冲突/锁、
本地 IMAP 协议、坏行与断线停止及续采、UIDVALIDITY 变化、两线程并发互斥、
采集与查询 API、原核对算法（共 70+ 用例）。

## 五、目录

```
app/
  main.py        FastAPI 路由（采集/查询/核对）与 OpenAPI 示例
  config.py      环境配置
  db.py          SQLite：results/mailboxes/collected_uids/collect_lock
  collector.py   采集编排：取信 -> 整封校验 -> 原子提交
  imapclient.py  imaplib 封装（LOGIN/SELECT/UID SEARCH/UID FETCH）
  mailparse.py   MIME 附件筛选（仅 UTF-8 CSV）
  csvparse.py    CSV 解析、UTC 整数秒时间、100 行上限
  schemas.py     Pydantic 模型与输入校验
  matching.py    MCMF + 零费用残余图 SCC 核对算法
tools/
  imap_server.py        纯标准库本地 IMAP4rev1 演示源（支持断线/热重载）
  make_fixtures.py      生成 6 封演示邮件
  fix_fixture_message.py 修复坏邮件演示工具
demo/
  request.json          核对接口演示请求
  result_query.json     结果查询演示请求
  run_local_demo.sh     本地双进程端到端演示脚本
tests/                  单元与集成测试
Dockerfile / docker-compose.yml / requirements*.txt
```
