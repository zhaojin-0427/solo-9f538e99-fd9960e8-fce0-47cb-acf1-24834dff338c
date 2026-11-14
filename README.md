# 整点电量结算 API

把设备补传、更正的**整点累计电量(kWh)**整理成结算用的**小时用量**。
FastAPI + SQLite 实现,批次事务化、幂等发布,附 Docker Compose 一键启动。

## 运行

### Docker Compose(推荐)

```bash
docker compose up --build
# API 监听 http://localhost:8000 ,文档见 http://localhost:8000/docs
```

SQLite 数据库落在命名卷 `settlement-data` 中(容器内 `/data/settlement.db`)。

### 本地开发

```bash
pip install -r requirements-dev.txt
DATABASE_PATH=./settlement.db uvicorn app.main:app --reload
python -m pytest          # 运行测试(19 个用例)
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/batches` | 批量提交整点累计读数(补传/更正) |
| `GET` | `/api/v1/batches/{batch_id}` | 查询批次处理结果(含被拒绝原因) |
| `GET` | `/api/v1/meters/{meter_id}/hourly-usage?start=…&end=…` | 按时段查询小时用量及所用读数修订号 |
| `GET` | `/api/v1/meters/{meter_id}/readings` | 查询电表当前生效读数(最高修订) |

### 提交批次

```json
POST /api/v1/batches
{
  "batch_id": "20261114-001",          // 可选;客户端幂等键,缺省由服务端生成
  "readings": [
    {"meter_id": "M1001", "hour": "2026-11-14T00:00:00Z",
     "cumulative_kwh": 1024.0, "revision": 1, "reset": false},
    {"meter_id": "M1001", "hour": "2026-11-14T01:00:00Z",
     "cumulative_kwh": 1036.5, "revision": 1}
  ]
}
```

- `hour` 必须带时区且整点对齐(分秒为 0),内部统一归一到 UTC。
- 响应:`201` 新发布 / `200` 相同 `batch_id`+相同内容的幂等回放 /
  `409` 修订冲突 / `422` 校验失败或参数非法。响应含 `applied`(写入/升级条数)、
  `superseded`(被更高修订覆盖而丢弃)、`duplicates`(完全重复)、
  `published_hours`(本次发布/改算的小时)。

### 查询小时用量

```
GET /api/v1/meters/M1001/hourly-usage?start=2026-11-14T00:00:00Z&end=2026-11-14T05:00:00Z
```

返回 `[start, end)` 内**每个小时**一行:

```json
{
  "hour_start": "2026-11-14T02:00:00Z",
  "status": "unsettleable",
  "usage_kwh": null,
  "reason": "counter_reset",
  "start_reading_revision": 1,
  "end_reading_revision": 1,
  "published_batch_id": "20261114-001"
}
```

## 业务规则

**修订与冲突**
- 同一电表同一整点,只保留**最高修订号**的读数;迟到的低修订直接丢弃。
- **同一修订号重传但内容不同**(kWh 或重置标记不一致)→ 整批拒绝(`409`)。
- 同修订同内容 → 幂等 no-op,不产生任何变化。

**整批校验、原子生效**
- 批次在单事务内先合并读数,再按**合并后的完整读数序列**校验:任一被触及电表
  出现**未标重置的累计回退** → 整批拒绝(`422`)。
- 拒绝的批次不改变任何状态:读数表与**已发布小时用量保持不变**;拒绝原因随
  `batch_id` 记录,相同内容重放得到相同答复。

**发布与改算**
- 成功批次**一次发布**:对每条变化的读数,同步改算其**相邻两个小时**
  (以该读数收尾的小时和以该读数开头的小时),upsert 到小时用量表。
- 小时用量 = 相邻两个整点累计读数之差,不做任何插值。
- 用量表以 `(电表, 小时)` 为主键,**重复提交不重复入账**;相同 `batch_id`
  重放在写库前即短路返回原结果。

**不可结算(不插值,返回原因)**
- `missing_reading` / `missing_start_reading` / `missing_end_reading`:缺整点读数。
- `counter_reset`:该小时内发生计数器重置(小时末读数带 `reset` 标记),
  差值无意义;重置之后的小时以新基线正常结算。
- 带 `reset: true` 的读数允许累计值下降(作为新基线),否则回退即拒绝整批。

## 项目结构

```
app/
  main.py        # FastAPI 路由与应用工厂
  settlement.py  # 批次合并、校验、发布核心逻辑
  db.py          # SQLite 连接与建表
  schemas.py     # 请求模型(整点对齐、非负 kWh 等校验)
  timeutil.py    # 整点 UTC 时间处理
tests/test_api.py  # 19 个端到端用例
Dockerfile / docker-compose.yml
```
