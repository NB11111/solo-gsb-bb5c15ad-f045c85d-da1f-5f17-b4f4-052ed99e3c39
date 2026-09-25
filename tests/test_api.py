"""接口与判定逻辑测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload, path="/v1/usage/verify"):
    return client.post(path, json=payload)


def _reconcile(payload):
    return _post(payload, path="/v1/usage/reconcile")


# ---------------------------------------------------------------------------
# 正常场景
# ---------------------------------------------------------------------------

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_no_wrap_unique():
    """无回绕，增量在速率上限内（60s * 10/s = 600，不足以多绕一圈）。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:01:00Z", "value": 600},
            ],
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_usage"] == 500
    assert body["errors"] == []
    assert body["segments"][0]["wraps"] == 0
    assert body["segments"][0]["usage"] == 500


def test_single_wrap_unique():
    """一次回绕且唯一：900 -> 100，M=1000，10s 内上限 200。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 20,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 900},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["segments"][0]["wraps"] == 1
    assert body["segments"][0]["usage"] == 200
    assert body["total_usage"] == 200


def test_multi_segment_total():
    """多段混合（回绕 + 不回绕）全部唯一，总用量为各段之和。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 80,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 250},  # +150
                {"timestamp": "2026-09-24T00:00:20Z", "value": 50},   # 回绕 +800
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert [s["usage"] for s in body["segments"]] == [150, 800]
    assert body["segments"][1]["wraps"] == 1
    assert body["total_usage"] == 950


def test_zero_rate_equal_readings():
    """速率为 0 且读数不变：唯一解，用量 0。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 0,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 42},
                {"timestamp": "2026-09-24T01:00:00Z", "value": 42},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_usage"] == 0


def test_fractional_rate_boundary():
    """小数速率按精确分数判定：0.5/s * 10s = 5，增量 5 恰好唯一。"""
    resp = _post(
        {
            "modulus": 100,
            "max_rate_per_second": 0.5,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 10},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 15},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_usage"] == 5


def test_timezone_offset_normalized_to_utc():
    """带偏移的时区会被换算为 UTC 后再判定。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T08:00:00+08:00", "value": 0},
                {"timestamp": "2026-09-24T00:00:30Z", "value": 5},  # 同一时刻后 30s
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["segments"][0]["elapsed_seconds"] == 30
    assert body["total_usage"] == 5


# ---------------------------------------------------------------------------
# 无解 / 多解
# ---------------------------------------------------------------------------

def test_no_solution():
    """增量超过速率上限：1s 内最多 10，但需 800。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 900},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "error"
    assert body["total_usage"] is None
    assert body["segments"] == []
    err = body["errors"][0]
    assert err["reason"] == "no_solution"
    assert err["index"] == 0
    assert err["max_wraps"] < err["min_wraps"]


def test_ambiguous():
    """时间跨度过大导致多个回绕次数都可行。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},  # 1000s, 上限 10000
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "error"
    assert body["total_usage"] is None
    err = body["errors"][0]
    assert err["reason"] == "ambiguous"
    assert err["min_wraps"] == 0
    assert err["max_wraps"] == 9  # k=0..9 均满足


def test_mixed_segments_partial_errors():
    """部分区间唯一、部分出错：返回已判定分段与出错区间，总用量为 null。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 20,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 300},   # 唯一 +200
                {"timestamp": "2026-09-24T00:00:11Z", "value": 900},   # 无解
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "error"
    assert body["total_usage"] is None
    assert len(body["segments"]) == 1
    assert body["segments"][0]["usage"] == 200
    assert len(body["errors"]) == 1
    assert body["errors"][0]["index"] == 1
    assert body["errors"][0]["reason"] == "no_solution"


# ---------------------------------------------------------------------------
# 请求校验（422）
# ---------------------------------------------------------------------------

def test_timestamps_not_strictly_increasing():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:10Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_reading_out_of_modulus_range():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 1000},
            ],
        }
    )
    assert resp.status_code == 422


def test_naive_timestamp_rejected():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_subsecond_timestamp_rejected():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00.500Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_too_few_readings():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [{"timestamp": "2026-09-24T00:00:00Z", "value": 100}],
        }
    )
    assert resp.status_code == 422


def test_negative_rate_rejected():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": -1,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 账单总量对账 /v1/usage/reconcile
# ---------------------------------------------------------------------------

def test_reconcile_resolves_ambiguous_segment():
    """单段多解 [0,9]：账单 3100 隐含 K=3，圈数唯一确定。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 3100,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_wraps"] == 3
    assert body["conflicts"] == []
    seg = body["segments"][0]
    assert seg["wraps"] == 3
    assert seg["usage"] == 3100
    assert seg["min_wraps"] == seg["max_wraps"] == 3
    assert seg["min_usage"] is None and seg["max_usage"] is None


def test_reconcile_mixed_unique_and_ranges():
    """两段多解，K=5：段1 仍为范围 [0,4]，段2 为范围 [1,5]，均不给确定用量。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 5000,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},
                {"timestamp": "2026-09-24T00:33:20Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_wraps"] == 5
    s1, s2 = body["segments"]
    assert (s1["min_wraps"], s1["max_wraps"]) == (0, 4)
    assert s1["wraps"] is None and s1["usage"] is None
    assert (s1["min_usage"], s1["max_usage"]) == (100, 4100)
    assert (s2["min_wraps"], s2["max_wraps"]) == (1, 5)
    assert (s2["min_usage"], s2["max_usage"]) == (900, 4900)


def test_reconcile_feasible_endpoint_all_unique():
    """K 取区间端点时各段圈数全部唯一，确定用量之和恰为账单总量。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 1000,  # K = sum_min = 1
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},
                {"timestamp": "2026-09-24T00:33:20Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert [s["usage"] for s in body["segments"]] == [100, 900]
    assert sum(s["usage"] for s in body["segments"]) == 1000


def test_reconcile_incompatible_modulus():
    """总量与首尾读数差模 M 不同余：返回不相容冲突及可行区间。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 5500,  # 应为 1000 的整数倍
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},
                {"timestamp": "2026-09-24T00:33:20Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "conflict"
    assert body["segments"] == []
    assert body["total_wraps"] is None
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "total"
    assert conflict["reason"] == "total_incompatible_modulus"
    assert conflict["expected_remainder"] == 0
    assert conflict["feasible_min_total"] == 1000
    assert conflict["feasible_max_total"] == 19000


def test_reconcile_out_of_range():
    """总量同余但总圈数超出可行和：返回超界冲突。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 0,  # 首尾相同要求 K=0，但回绕段至少 1 圈
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},
                {"timestamp": "2026-09-24T00:33:20Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "conflict"
    conflict = body["conflicts"][0]
    assert conflict["reason"] == "total_out_of_range"
    assert conflict["total_wraps"] == 0
    assert conflict["feasible_min_total"] == 1000


def test_reconcile_segment_no_solution_conflict():
    """某段本身无解（超过速率上限）：以分段冲突返回，不做总量推断。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 20,
            "total_usage": 300,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 300},  # 可行
                {"timestamp": "2026-09-24T00:00:11Z", "value": 900},  # 无解
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["segments"] == []
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "segment"
    assert conflict["index"] == 1
    assert conflict["reason"] == "no_solution"
    assert conflict["max_wraps"] < conflict["min_wraps"]


def test_reconcile_negative_total_rejected():
    """账单总用量为负数：422。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": -1,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_reconcile_total_must_be_integer():
    """账单总用量必须为整数：422。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 12.5,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_reconcile_zero_total_with_flat_readings():
    """首尾相同且全部段都允许 0 圈时，账单 0 可行且各段唯一为 0。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 0,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    seg = body["segments"][0]
    assert seg["wraps"] == 0 and seg["usage"] == 0


def test_reconcile_reuses_reading_validation():
    """新接口复用读数校验：读数越界、时间戳不递增均 422。"""
    base = {
        "modulus": 1000,
        "max_rate_per_second": 10,
        "total_usage": 100,
    }
    resp = _reconcile(
        {
            **base,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 1000},
            ],
        }
    )
    assert resp.status_code == 422
    resp = _reconcile(
        {
            **base,
            "readings": [
                {"timestamp": "2026-09-24T00:00:10Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 按日用量范围 /v1/usage/daily-range
# ---------------------------------------------------------------------------

def _daily_range(payload):
    return _post(payload, path="/v1/usage/daily-range")


def test_daily_range_cross_midnight_split():
    """跨午夜区间被切成两个子段，用量按 UTC 自然日分别汇总。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T23:59:50Z", "value": 900},
                {"timestamp": "2026-09-25T00:00:10Z", "value": 100},  # 回绕 +200
                {"timestamp": "2026-09-25T00:00:20Z", "value": 150},  # +50
            ],
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["conflicts"] == []
    assert body["note"]
    assert [d["date"] for d in body["days"]] == ["2026-09-24", "2026-09-25"]

    day1, day2 = body["days"]
    # 区间 0 总增量 200，两个子段容量各 100，只能各分 100
    assert day1["min_usage"] == day1["max_usage"] == 100
    assert len(day1["sub_segments"]) == 1
    part = day1["sub_segments"][0]
    assert part["segment_index"] == 0 and part["part_index"] == 0
    assert part["start_timestamp"] == "2026-09-24T23:59:50Z"
    assert part["end_timestamp"] == "2026-09-25T00:00:00Z"
    assert part["elapsed_seconds"] == 10
    assert part["capacity"] == 100
    # 次日：区间 0 后半段 100 + 区间 1 的 50
    assert day2["min_usage"] == day2["max_usage"] == 150
    assert [(p["segment_index"], p["part_index"]) for p in day2["sub_segments"]] == [
        (0, 1),
        (1, 0),
    ]


def test_daily_range_upper_bounds_not_simultaneous():
    """单日上下界各自可达到，但两日上界不能同时成立（总量固定为 50）。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T23:59:50Z", "value": 100},
                {"timestamp": "2026-09-25T00:00:10Z", "value": 150},  # +50，k 只能为 0
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    day1, day2 = body["days"]
    # 总增量 50 可任意分到两个子段（容量各 100）：每日 [0, 50]
    assert (day1["min_usage"], day1["max_usage"]) == (0, 50)
    assert (day2["min_usage"], day2["max_usage"]) == (0, 50)
    # 响应须注明各日上界不保证同时成立
    assert "同时" in body["note"]


def test_daily_range_multi_interval_same_day_aggregated():
    """同一自然日内多个读数区间的子段界分别求和。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 20,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 300},  # +200
                {"timestamp": "2026-09-24T00:00:20Z", "value": 450},  # +150
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert len(body["days"]) == 1
    day = body["days"][0]
    assert day["date"] == "2026-09-24"
    assert day["min_usage"] == day["max_usage"] == 350
    assert len(day["sub_segments"]) == 2


def test_daily_range_span_multiple_midnights():
    """跨两个午夜：切成 3 个子段，分别落在 3 个 UTC 自然日。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 0.01,  # 容量 36 + 864 + 36 = 936
            "readings": [
                {"timestamp": "2026-09-24T23:00:00Z", "value": 500},
                {"timestamp": "2026-09-26T01:00:00Z", "value": 600},  # +100
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert [d["date"] for d in body["days"]] == [
        "2026-09-24",
        "2026-09-25",
        "2026-09-26",
    ]
    durations = [
        d["sub_segments"][0]["elapsed_seconds"] for d in body["days"]
    ]
    assert durations == [3600, 86400, 3600]
    capacities = [d["sub_segments"][0]["capacity"] for d in body["days"]]
    assert capacities == [36, 864, 36]
    # k 只能为 0（Δ=100，C=936）；两个边界日最多各装 36，
    # 中间日子段下界 = 100 - 36 - 36 = 28，上界为 100
    assert [
        (d["min_usage"], d["max_usage"]) for d in body["days"]
    ] == [(0, 36), (28, 100), (0, 36)]


def test_daily_range_midnight_to_midnight_single_day():
    """恰好午夜到午夜：单个子段，全部用量归起始日，不产生次日条目。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 1,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 0},
                {"timestamp": "2026-09-25T00:00:00Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert [d["date"] for d in body["days"]] == ["2026-09-24"]
    day = body["days"][0]
    # k ∈ [0, 86]：Δ ∈ [100, 86100]，单子段上下界即总增量范围
    assert day["min_usage"] == 100
    assert day["max_usage"] == 86100
    assert day["sub_segments"][0]["elapsed_seconds"] == 86400


def test_daily_range_fractional_rate_floored_per_subsegment():
    """容量按子段 floor(速率×秒数)：0.5/s 下 5s+5s 容量为 2+2=4，增量 5 无解。"""
    resp = _daily_range(
        {
            "modulus": 100,
            "max_rate_per_second": 0.5,
            "readings": [
                {"timestamp": "2026-09-24T23:59:55Z", "value": 0},
                {"timestamp": "2026-09-25T00:00:05Z", "value": 5},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["days"] == []
    conflict = body["conflicts"][0]
    assert conflict["index"] == 0
    assert conflict["reason"] == "no_solution"
    assert conflict["total_capacity"] == 4  # floor(0.5*5) + floor(0.5*5)
    assert conflict["required_min_total"] == 5
    assert conflict["max_wraps"] < conflict["min_wraps"]


def test_daily_range_conflict_no_feasible_allocation():
    """增量超过各子段容量之和：返回区间索引与冲突原因，不返回日用量。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 900},  # 需 800，容量 10
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["days"] == []
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["index"] == 0
    assert conflict["reason"] == "no_solution"
    assert conflict["min_wraps"] == 0
    assert conflict["max_wraps"] == -1
    assert conflict["total_capacity"] == 10
    assert conflict["required_min_total"] == 800


def test_daily_range_conflict_reports_each_infeasible_interval():
    """多个区间均无可行分配时逐一报告索引。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 900},  # 无解
                {"timestamp": "2026-09-24T00:00:02Z", "value": 100},  # 回绕 +200，容量 10，无解
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "conflict"
    assert [c["index"] for c in body["conflicts"]] == [0, 1]


def test_daily_range_bounds_match_brute_force():
    """跨日多解场景：与穷举所有可行 k 及子段分配得到的每日极值一致。"""
    resp = _daily_range(
        {
            "modulus": 100,
            "max_rate_per_second": 3,
            "readings": [
                {"timestamp": "2026-09-24T20:00:00Z", "value": 90},
                {"timestamp": "2026-09-25T08:00:00Z", "value": 10},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"

    # 穷举：k 使 0 <= Δ <= C 时，x0 在 [max(0, Δ-c1), min(c0, Δ)] 连续取值
    diff = 10 - 90
    caps = [3 * 14400, 3 * 28800]  # 20:00->24:00 与 00:00->08:00
    total_cap = sum(caps)
    min0 = max0 = min1 = max1 = None
    k = 0
    while True:
        delta = diff + k * 100
        if delta > total_cap:
            break
        if delta >= 0:
            lo0, hi0 = max(0, delta - caps[1]), min(caps[0], delta)
            lo1, hi1 = delta - hi0, delta - lo0
            min0 = lo0 if min0 is None else min(min0, lo0)
            max0 = hi0 if max0 is None else max(max0, hi0)
            min1 = lo1 if min1 is None else min(min1, lo1)
            max1 = hi1 if max1 is None else max(max1, hi1)
        k += 1

    days = {d["date"]: d for d in body["days"]}
    assert (days["2026-09-24"]["min_usage"], days["2026-09-24"]["max_usage"]) == (
        min0,
        max0,
    )
    assert (days["2026-09-25"]["min_usage"], days["2026-09-25"]["max_usage"]) == (
        min1,
        max1,
    )


def test_daily_range_reuses_request_validation():
    """复用读数/模数/速率校验：越界读数、不递增、缺时区、亚秒、负速率均 422。"""
    base = {"modulus": 1000, "max_rate_per_second": 10}
    bad_readings = [
        [  # 读数越界
            {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
            {"timestamp": "2026-09-24T00:00:01Z", "value": 1000},
        ],
        [  # 时间戳不递增
            {"timestamp": "2026-09-24T00:00:10Z", "value": 100},
            {"timestamp": "2026-09-24T00:00:10Z", "value": 200},
        ],
        [  # 缺时区
            {"timestamp": "2026-09-24T00:00:00", "value": 100},
            {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
        ],
        [  # 亚秒
            {"timestamp": "2026-09-24T00:00:00.500Z", "value": 100},
            {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
        ],
    ]
    for readings in bad_readings:
        resp = _daily_range({**base, "readings": readings})
        assert resp.status_code == 422, readings
    # 负速率
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": -1,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422
