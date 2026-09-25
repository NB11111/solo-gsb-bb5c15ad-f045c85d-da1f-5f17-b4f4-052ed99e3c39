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
