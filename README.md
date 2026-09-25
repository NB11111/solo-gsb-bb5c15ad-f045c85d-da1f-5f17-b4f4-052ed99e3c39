# 累计水表读数用量复核 API

基于 FastAPI 的用量复核服务：输入一组带秒精度 UTC 时间戳的累计水表读数、计数器模数与每秒最大计数增量，对每段相邻读数判定满足**非负增量**与**速率上限**的整圈回绕次数是否唯一。唯一时返回分段用量与总用量；无解或多解时返回出错区间与原因，**不猜测圈数**。

此外提供**账单总量对账**接口：在同一组非负增量与速率上限约束下，额外接收账单总用量，以整数区间运算收紧各段仍可行的回绕次数范围；只有圈数被唯一确定的段才给出用量，其余段仅返回范围，冲突时给出明确原因。

以及**按日用量范围**接口：相邻读数跨 UTC 日时按午夜切成子段（每段容量 `floor(速率×秒数)`），在「总增量 = 读数差 + k×模数 可分配到各子段」的约束下，给出覆盖时段内**各 UTC 自然日的最小、最大可行整数用量**；每日边界均可分别达到，但各日上界不保证同时成立。

## 快速启动（Docker，一键）

```bash
docker compose up --build
```

依赖在镜像构建时自动安装，无需本地环境。启动后（默认监听宿主机 **8000** 端口）：

- API 地址：`http://localhost:8000`
- 用量复核：`POST http://localhost:8000/v1/usage/verify`
- 账单总量对账：`POST http://localhost:8000/v1/usage/reconcile`
- 按日用量范围：`POST http://localhost:8000/v1/usage/daily-range`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`http://localhost:8000/health`

### 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 宿主机端口 | `8000` | 在 `docker-compose.yml` 的 `ports` 中修改，如 `"8080:8000"` |
| 容器内端口 | `8000` | 由 `Dockerfile` 的 `CMD` 指定，一般无需修改 |

无其他必需配置（无数据库、无外部依赖）。

## 本地运行（可选）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest
```

## API 说明

### `POST /v1/usage/verify`

**请求体**

```json
{
  "modulus": 1000,
  "max_rate_per_second": 20,
  "readings": [
    { "timestamp": "2026-09-24T00:00:00Z", "value": 900 },
    { "timestamp": "2026-09-24T00:00:10Z", "value": 100 }
  ]
}
```

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `modulus` | 整数 | `>= 1`，计数器模数（回绕周期） |
| `max_rate_per_second` | 数值 | `>= 0`，每秒最大计数增量，支持小数（按精确分数计算，无浮点误差） |
| `readings` | 数组 | 至少 2 条；`timestamp` 为秒精度且带时区（建议 UTC，其他时区会换算为 UTC），全序列**严格递增**；`value` 为整数且 `0 <= value < modulus` |

**响应（全部区间唯一，`200`）**

```json
{
  "status": "ok",
  "modulus": 1000,
  "segments": [
    {
      "index": 0,
      "start_timestamp": "2026-09-24T00:00:00Z",
      "end_timestamp": "2026-09-24T00:00:10Z",
      "start_reading": 900,
      "end_reading": 100,
      "elapsed_seconds": 10,
      "wraps": 1,
      "usage": 200
    }
  ],
  "errors": [],
  "total_usage": 200
}
```

**响应（存在出错区间，`200`）**

```json
{
  "status": "error",
  "modulus": 1000,
  "segments": [],
  "errors": [
    {
      "index": 0,
      "start_timestamp": "2026-09-24T00:00:00Z",
      "end_timestamp": "2026-09-24T00:16:40Z",
      "start_reading": 100,
      "end_reading": 200,
      "elapsed_seconds": 1000,
      "reason": "ambiguous",
      "detail": "多解：回绕次数 k 在 [0, 9] 内均满足非负增量与速率上限约束，无法唯一确定用量，不做猜测",
      "min_wraps": 0,
      "max_wraps": 9
    }
  ],
  "total_usage": null
}
```

- `reason = "no_solution"`：没有任何回绕次数同时满足两条约束（通常是增量超过速率上限）。
- `reason = "ambiguous"`：存在多个可行回绕次数，无法唯一确定用量。
- 存在出错区间时，`segments` 仍返回其余唯一判定的区间，但 `total_usage` 为 `null`。

**请求校验失败（`422`）**：时间戳未严格递增、缺时区、含亚秒、读数越界、参数非法等，返回 FastAPI 标准错误明细。

### `POST /v1/usage/reconcile`

账单总量对账。请求体在复核接口字段基础上增加 `total_usage`（非负整数账单总用量），读数、模数、速率等校验与 `/v1/usage/verify` 完全一致。

**请求体**

```json
{
  "modulus": 1000,
  "max_rate_per_second": 10,
  "total_usage": 5000,
  "readings": [
    { "timestamp": "2026-09-24T00:00:00Z", "value": 100 },
    { "timestamp": "2026-09-24T00:16:40Z", "value": 200 },
    { "timestamp": "2026-09-24T00:33:20Z", "value": 100 }
  ]
}
```

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `total_usage` | 整数 | `>= 0`，账单总用量（非负整数） |
| 其余字段 | — | 与 `/v1/usage/verify` 相同 |

**响应（账单可行，`200`，`status = "ok"`）**

```json
{
  "status": "ok",
  "modulus": 1000,
  "total_usage": 5000,
  "total_wraps": 5,
  "segments": [
    {
      "index": 0,
      "start_timestamp": "2026-09-24T00:00:00Z",
      "end_timestamp": "2026-09-24T00:16:40Z",
      "start_reading": 100,
      "end_reading": 200,
      "elapsed_seconds": 1000,
      "min_wraps": 0,
      "max_wraps": 4,
      "wraps": null,
      "usage": null,
      "min_usage": 100,
      "max_usage": 4100
    },
    {
      "index": 1,
      "start_timestamp": "2026-09-24T00:16:40Z",
      "end_timestamp": "2026-09-24T00:33:20Z",
      "start_reading": 200,
      "end_reading": 100,
      "elapsed_seconds": 1000,
      "min_wraps": 1,
      "max_wraps": 5,
      "wraps": null,
      "usage": null,
      "min_usage": 900,
      "max_usage": 4900
    }
  ],
  "conflicts": []
}
```

当某段收紧后圈数唯一（`min_wraps == max_wraps`），`wraps` 与 `usage` 给出确定值，`min_usage` / `max_usage` 为 `null`；仍有多解的段反之，**只给范围不猜测**。所有段确定用量之和恒等于 `total_usage`。

**响应（冲突，`200`，`status = "conflict"`）**

分段原本无解时：

```json
{
  "status": "conflict",
  "modulus": 1000,
  "total_usage": 300,
  "total_wraps": null,
  "segments": [],
  "conflicts": [
    {
      "kind": "segment",
      "index": 1,
      "start_timestamp": "2026-09-24T00:00:10Z",
      "end_timestamp": "2026-09-24T00:00:11Z",
      "start_reading": 300,
      "end_reading": 900,
      "elapsed_seconds": 1,
      "reason": "no_solution",
      "detail": "无解：最可能增量 600 已超出该区间 1s 内允许的最大增量 20（k 需同时满足 k>=0 与 k<=-1，区间为空）",
      "min_wraps": 0,
      "max_wraps": -1
    }
  ]
}
```

账单总量不相容或超界时（`kind = "total"`）：

```json
{
  "kind": "total",
  "reason": "total_incompatible_modulus",
  "detail": "账单总量与模数不相容：总量 B 必须满足 B ≡ 0 (mod 1000)（首尾读数差为 0），收到 5500（余数 500），总回绕次数不是整数",
  "feasible_min_total": 1000,
  "feasible_max_total": 19000,
  "expected_remainder": 0,
  "total_wraps": null
}
```

冲突原因：

- `kind = "segment"`，`reason = "no_solution"`：某段在非负增量/速率上限下原本无解（复用复核接口的逐段判定）。
- `kind = "total"`，`reason = "total_incompatible_modulus"`：账单总量与首尾读数差模 `modulus` 不同余，总回绕次数不是整数；`expected_remainder` 给出应有的余数。
- `kind = "total"`，`reason = "total_out_of_range"`：总回绕次数超出各段可行圈数之和 `[Σk_min, Σk_max]`，即可行账单总量不在 `[feasible_min_total, feasible_max_total]` 内。

存在分段无解冲突时不再进行总量推断；总量冲突时 `segments` 为空。`total_usage` 为负数或非整数等请求校验错误返回 `422`。

### `POST /v1/usage/daily-range`

按日用量范围。请求体与 `/v1/usage/verify` 完全相同（读数、模数、每秒最大速率的校验一致，**不接收账单总量**），返回覆盖时段内各 UTC 自然日的最小、最大可行整数用量。

**请求体**

```json
{
  "modulus": 1000,
  "max_rate_per_second": 10,
  "readings": [
    { "timestamp": "2026-09-24T23:59:50Z", "value": 900 },
    { "timestamp": "2026-09-25T00:00:10Z", "value": 100 },
    { "timestamp": "2026-09-25T00:00:20Z", "value": 150 }
  ]
}
```

**响应（全部区间存在可行分配，`200`，`status = "ok"`）**

```json
{
  "status": "ok",
  "modulus": 1000,
  "days": [
    {
      "date": "2026-09-24",
      "min_usage": 100,
      "max_usage": 100,
      "sub_segments": [
        {
          "segment_index": 0,
          "part_index": 0,
          "start_timestamp": "2026-09-24T23:59:50Z",
          "end_timestamp": "2026-09-25T00:00:00Z",
          "elapsed_seconds": 10,
          "capacity": 100,
          "min_usage": 100,
          "max_usage": 100
        }
      ]
    },
    {
      "date": "2026-09-25",
      "min_usage": 150,
      "max_usage": 150,
      "sub_segments": [
        {
          "segment_index": 0,
          "part_index": 1,
          "start_timestamp": "2026-09-25T00:00:00Z",
          "end_timestamp": "2026-09-25T00:00:10Z",
          "elapsed_seconds": 10,
          "capacity": 100,
          "min_usage": 100,
          "max_usage": 100
        },
        {
          "segment_index": 1,
          "part_index": 0,
          "start_timestamp": "2026-09-25T00:00:10Z",
          "end_timestamp": "2026-09-25T00:00:20Z",
          "elapsed_seconds": 10,
          "capacity": 100,
          "min_usage": 50,
          "max_usage": 50
        }
      ]
    }
  ],
  "note": "每个 UTC 自然日的 min_usage/max_usage 均可在某个可行回绕次数 k 及子段分配下分别达到；不同日期的上界不保证能同时成立，请勿将各日 max_usage 直接相加。",
  "conflicts": []
}
```

上例中区间 0（`900 -> 100`，读数差 -800）的总增量为 `-800 + k×1000`，只有 `k = 1` 即 200 可行；它跨午夜被切成两个 10 秒子段，容量各为 `floor(10×10) = 100`，200 只能拆成 `100 + 100`，故 9 月 24 日与 25 日各得 100。

**边界可分别达到（重要）**

每个子段的 `min_usage` / `max_usage` 都是在**某个**可行回绕次数 `k` 及其余子段的某种分配下可真正取到的精确界（整数区间运算，不枚举组合）；同一天内各子段的下界（或上界）可同时达到，因此日 `min_usage` / `max_usage` 为该日全部子段对应界之和，且该日总界同样可达到。

但**不同日期的上界不保证能同时成立**：例如 `23:59:50 读数 100 -> 次日 00:00:10 读数 150`（速率 10/s），总增量固定为 50，两个子段容量各 100，分配 `(50, 0)` 时前一天取上界 50、次日取下界 0，分配 `(0, 50)` 时正好相反——每日都是 `[0, 50]`，但不存在「两日均为 50」的可行分配。因此**禁止把各日 `max_usage` 相加当作全时段最大总用量**；该提醒也固定包含在成功响应的 `note` 字段中。

**子段与归属**

- 相邻读数跨 UTC 午夜时，按午夜切成左闭右开 `[起点, 终点)` 的子段；恰在午夜结束的子段用量归**起始日**（用量发生在午夜之前），因此一个「午夜 00:00 到次日 00:00」的完整区间只产生起始日一条子段。
- 子段容量为 `floor(max_rate_per_second × 子段秒数)`（精确分数，逐段向下取整后再求和；不是按整段速率算完再取整）。
- `days` 按日期升序，仅包含被子段覆盖的日期；`sub_segments` 内按读数区间序号与子段序号排列。

**响应（存在无可行分配的区间，`200`，`status = "conflict"`）**

任一读数区间的总增量（`diff + k×modulus`，`k` 为非负整数）无法以非负整数分配到各子段（即即使最小可行总增量也超过子段容量之和）时，**不返回任何日用量**（`days` 为空），并逐一给出冲突区间索引及原因：

```json
{
  "status": "conflict",
  "modulus": 1000,
  "days": [],
  "note": "每个 UTC 自然日的 min_usage/max_usage 均可在某个可行回绕次数 k 及子段分配下分别达到；不同日期的上界不保证能同时成立，请勿将各日 max_usage 直接相加。",
  "conflicts": [
    {
      "index": 0,
      "start_timestamp": "2026-09-24T23:59:59Z",
      "end_timestamp": "2026-09-25T00:00:01Z",
      "start_reading": 100,
      "end_reading": 900,
      "elapsed_seconds": 2,
      "reason": "no_solution",
      "detail": "无解：总增量 diff + k×1000 中，满足非负的最小值（k=0）已是 800，但各午夜子段容量 floor(速率×秒数) 之和仅为 20（k 需同时满足 k>=0 与 k<=-1，区间为空），无法把非负整数增量分配到各子段",
      "min_wraps": 0,
      "max_wraps": -1,
      "total_capacity": 20,
      "required_min_total": 800
    }
  ]
}
```

冲突字段：

- `index`：相邻读数区间序号（从 0 起）；同一响应会列出全部无可行分配的区间。
- `reason`：固定为 `"no_solution"`，此时 `max_wraps < min_wraps`。
- `total_capacity`：各子段容量之和（可分配总增量上界）；`required_min_total`：满足非负约束的最小总增量（`diff + min_wraps × modulus`），前者小于后者即冲突。

请求校验失败（时间戳未严格递增、缺时区、含亚秒、读数越界、参数非法等）与其他接口一致返回 `422`。

## 判定规则

对每段相邻读数 `(r1, t1) -> (r2, t2)`，真实增量为 `delta = (r2 - r1) + k * modulus`，其中 `k >= 0` 为整圈回绕次数。约束：

1. 非负增量：`delta >= 0`
2. 速率上限：`delta <= max_rate_per_second * (t2 - t1)`

可行 `k` 构成连续整数区间 `[k_min, k_max]`：

- `k_max < k_min` → 无解（`no_solution`）
- `k_max == k_min` → 唯一解，取该 `k` 计算用量
- `k_max > k_min` → 多解（`ambiguous`），不猜测圈数

### 账单总量对账

设账单总用量为 `B`。各段增量望远镜相消，总量恒等式为：

```
B = Σ delta_i = (r_last - r_first) + modulus * K
```

其中 `K = Σ k_i` 为整条序列的回绕次数总和。因此：

1. `B - (r_last - r_first)` 必须为 `modulus` 的非负整数倍，否则账单总量与模数不相容（`total_incompatible_modulus`）；
2. `K` 必须落在 `[Σ k_min, Σ k_max]` 内，否则超出可行总量（`total_out_of_range`，可行总量区间随之给出）。

固定 `K` 后，由「其余各段圈数之和 = `K - k_i`」的区间和直接反推每段仍可行的圈数范围（**整数区间运算，不枚举组合**）：

```
lo_i' = max(k_min_i, K - Σ_{j≠i} k_max_j)
hi_i' = min(k_max_i, K - Σ_{j≠i} k_min_j)
```

`lo_i' == hi_i'` 的段圈数唯一、确定用量；其余段返回 `[lo_i', hi_i']` 及对应用量范围，不做猜测。

### 按日用量范围

相邻读数区间先按 UTC 午夜切成时长 `s_1, ..., s_m` 秒的子段（不跨日），每个子段容量为：

```
c_j = floor(max_rate_per_second × s_j)
C   = Σ c_j                       （各子段容量之和）
```

区间总增量仍为 `Δ = diff + k × modulus`（`diff = r2 - r1`，`k >= 0` 为非负整数）。设子段用量 `x_j` 为非负整数、`x_j <= c_j`、`Σ x_j = Δ`。因为除各自容量外没有其他耦合约束，这样的分配存在**当且仅当**：

```
0 <= Δ <= C
```

故可行 `k` 仍构成连续整数区间：

```
k_min = 满足 diff + k*M >= 0 的最小非负整数
k_max = floor((C - diff) / M)
k_max < k_min  → 无可行分配（no_solution）
```

可行时，子段 `j` 的用量在**某个**可行 `k` 及其余子段某种分配下的精确上下界为（其余子段尽量装满 / 本段尽量装满，两端分别由 `k = k_min` 与 `k = k_max` 取到）：

```
lo_j = max(0, diff + k_min*M - (C - c_j))
hi_j = min(c_j, diff + k_max*M)
```

各子段归属其**起始时刻**所在的 UTC 自然日（左闭右开），同一天内：

```
day_min = Σ_{子段 j 在该日} lo_j      （当日可达到的最小用量）
day_max = Σ_{子段 j 在该日} hi_j      （当日可单独达到的最大用量）
```

`day_min` 在所有区间同时取 `k = k_min` 时各日一起达到；但 `day_max` 一般只对当日有利（`k` 与分配偏向该日），**各日上界不保证同时成立**。

## 项目结构

```
app/
  main.py   # FastAPI 应用、请求/响应模型与校验
  core.py   # 回绕次数判定逻辑（Fraction 精确计算）
tests/
  test_api.py
Dockerfile
docker-compose.yml
requirements.txt        # 运行时依赖
requirements-dev.txt    # 测试依赖
```
