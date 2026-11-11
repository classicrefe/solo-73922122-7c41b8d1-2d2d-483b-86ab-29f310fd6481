# 环境检测实验室结果核对 API (FastAPI)

把缺少样品号的旧仪器结果按**孔位 + 检测项目 + 时间容差**归入进样计划，
在所有最优配对方案中识别歧义，供人工确认后再导入实验室系统（LIMS）。

## 规则

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

## 算法

二分图（结果 ↔ 计划行）上的**最小费用最大流（MCMF，Dijkstra + 势函数）**，
边权为绝对时间差，天然实现“先最大流、再最小费用”。得到一个最优方案后，
在 **reduced cost = 0 的残余图**上求强连通分量（Kosaraju），一次性刻画所有最优方案：

- 一条未使用的可配对边能出现在某个最优方案中 ⇔ 两端在同一强连通分量内
  （即该边位于零费用交替环上，换边不改变配对数与总费用）；
- 一条结果可能未匹配 ⇔ 它当前未匹配，或源点可在零费用残余图到达它
  （存在零费用增广/退流路径）。

全部为整数运算，无浮点误差。复杂度对 50+50 的上限绰绰有余。

## 启动（Docker Compose）

```bash
docker compose up --build
# 服务: http://localhost:8000
```

## 文档与演示

- Swagger UI（可在线发请求）: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
- OpenAPI JSON: <http://localhost:8000/openapi.json>
- 健康检查: <http://localhost:8000/health>

演示请求：

```bash
curl -s -X POST http://localhost:8000/api/v1/reconcile \
  -H "Content-Type: application/json" \
  --data @demo/request.json | python -m json.tool
```

或直接打开 `/docs` → `POST /api/v1/reconcile` → Try it out → 粘贴
`demo/request.json`。

演示数据包含三类情形：

1. 唯一最优归属 → 回填样品号；
2. 两个结果对两个计划行等价可互换（总时间差相同）→ 不回填样品号；
3. 孔位/项目不符或时间超容差 → 只能未匹配。

## 运行测试

```bash
# 容器内（无需本地 Python 环境）
docker compose --profile test run --rm tests

# 或本地
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
pytest -q
```

## 目录

```
app/
  main.py        FastAPI 路由、应用入口
  schemas.py     请求/响应 Pydantic 模型与校验
  matching.py    MCMF + 零费用残余图 SCC 核心算法
demo/request.json  演示请求体
tests/           单元测试与 API 集成测试
Dockerfile / docker-compose.yml / requirements.txt
```
