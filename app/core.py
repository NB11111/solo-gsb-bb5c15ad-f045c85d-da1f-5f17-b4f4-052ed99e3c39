"""用量复核核心判定逻辑。

对每段相邻读数 (r1, r2)，真实增量为 delta = (r2 - r1) + k * modulus，
其中 k 为整圈回绕次数（非负整数）。约束条件：

    delta >= 0                          （非负增量）
    delta <= max_rate * elapsed_seconds （速率上限）

所有满足约束的 k 构成一个连续整数区间 [k_min, k_max]：

    k_min = 满足 diff + k*M >= 0 的最小非负整数
    k_max = 满足 diff + k*M <= max_delta 的最大整数

判定：
    k_max <  k_min  -> 无解 (no_solution)
    k_max == k_min  -> 唯一解，取该 k
    k_max >  k_min  -> 多解 (ambiguous)，不猜测圈数

速率上限使用 Fraction 精确计算，避免浮点边界误差。

账单总量对账（reconcile_total）：
    设账单总用量为 B，各段首尾读数差之和望远镜相消为首尾读数差：
        B = Σ delta_i = Σ diff_i + M * Σ k_i = (r_last - r_first) + M * K
    故总回绕次数 K = (B - (r_last - r_first)) / M 必须为非负整数
    （账单总量与模数不相容），且满足 Σ k_min <= K <= Σ k_max
    （超出可行总量）。固定 K 后，各段仍可行的圈数范围完全由整数区间
    运算给出，无需枚举组合：

        lo_i' = max(lo_i, K - Σ_j hi_j + hi_i)
        hi_i' = min(hi_i, K - Σ_j lo_j + lo_i)

    lo_i' == hi_i' 的段圈数唯一，可确定用量；其余段仅保留范围，不猜测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional, Sequence, Tuple

STATUS_OK = "ok"
STATUS_NO_SOLUTION = "no_solution"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_CONFLICT = "conflict"

# 账单总量对账冲突原因
REASON_TOTAL_INCOMPATIBLE_MODULUS = "total_incompatible_modulus"
REASON_TOTAL_OUT_OF_RANGE = "total_out_of_range"


@dataclass(frozen=True)
class SegmentSolution:
    """单个相邻读数区间的判定结果。"""

    index: int
    status: str
    elapsed_seconds: int
    # 唯一解时填充
    wraps: Optional[int]
    usage: Optional[int]
    # 可行回绕次数区间 [min_wraps, max_wraps]；无解时 max_wraps < min_wraps
    min_wraps: int
    max_wraps: int
    max_allowed: Fraction  # 该区间允许的最大增量（速率上限 * 秒数）


def solve_segment(
    index: int,
    start_value: int,
    end_value: int,
    elapsed_seconds: int,
    modulus: int,
    max_rate_per_second: Fraction,
) -> SegmentSolution:
    """判定一段相邻读数的回绕次数是否唯一。"""
    diff = end_value - start_value
    max_allowed = max_rate_per_second * elapsed_seconds

    # k_min: diff + k*M >= 0 的最小非负整数 k
    if diff >= 0:
        k_min = 0
    else:
        # ceil(-diff / M)，此时 0 < -diff < M，结果恒为 1
        k_min = (-diff + modulus - 1) // modulus

    # k_max: diff + k*M <= max_allowed 的最大整数 k
    k_max = math.floor((max_allowed - diff) / modulus)

    if k_max < k_min:
        status, wraps, usage = STATUS_NO_SOLUTION, None, None
    elif k_max == k_min:
        status, wraps = STATUS_OK, k_min
        usage = diff + k_min * modulus
    else:
        status, wraps, usage = STATUS_AMBIGUOUS, None, None

    return SegmentSolution(
        index=index,
        status=status,
        elapsed_seconds=elapsed_seconds,
        wraps=wraps,
        usage=usage,
        min_wraps=k_min,
        max_wraps=k_max,
        max_allowed=max_allowed,
    )


@dataclass(frozen=True)
class NarrowedSegment:
    """账单总量约束后，单段仍可行的回绕次数整数区间。"""

    index: int
    diff: int
    min_wraps: int
    max_wraps: int

    @property
    def unique(self) -> bool:
        """圈数是否已被总量约束唯一确定。"""
        return self.min_wraps == self.max_wraps

    @property
    def wraps(self) -> Optional[int]:
        return self.min_wraps if self.unique else None

    def usage(self, modulus: int) -> Optional[int]:
        """圈数唯一时该段确定用量，否则为 None。"""
        return self.diff + self.min_wraps * modulus if self.unique else None

    def usage_bounds(self, modulus: int) -> Tuple[int, int]:
        """该区间用量的最小/最大可能值。"""
        return (
            self.diff + self.min_wraps * modulus,
            self.diff + self.max_wraps * modulus,
        )


@dataclass(frozen=True)
class ReconcileResult:
    """账单总量对账结果。"""

    status: str  # STATUS_OK 或 STATUS_CONFLICT
    total_wraps: Optional[int]  # 总量隐含的总回绕次数 K（不相容时为 None）
    narrowed: Tuple[NarrowedSegment, ...]
    conflicts: Tuple[str, ...]  # REASON_TOTAL_* 原因
    feasible_min_total: int
    feasible_max_total: int


def reconcile_total(
    diffs: Sequence[int],
    wrap_bounds: Sequence[Tuple[int, int]],
    total_usage: int,
    modulus: int,
) -> ReconcileResult:
    """以账单总用量约束整条读数序列的回绕次数总和。

    :param diffs: 各段首尾读数差 r2 - r1
    :param wrap_bounds: 各段在非负增量与速率上限下的可行圈数区间
        ``(k_min, k_max)``，调用方需保证每段均可行（k_max >= k_min）
    :param total_usage: 账单总用量（非负整数）
    :param modulus: 计数器模数
    :return: 对账结果；可行时 ``narrowed`` 给出各段收紧后的圈数区间
    """
    if len(diffs) != len(wrap_bounds):
        raise ValueError("diffs 与 wrap_bounds 数量必须一致")

    lows = [lo for lo, _ in wrap_bounds]
    highs = [hi for _, hi in wrap_bounds]
    sum_low, sum_high = sum(lows), sum(highs)
    end_minus_start = sum(diffs)  # 望远镜相消：末读数 - 首读数

    feasible_min_total = end_minus_start + modulus * sum_low
    feasible_max_total = end_minus_start + modulus * sum_high

    numerator = total_usage - end_minus_start
    conflicts = []
    # 总量必须与首尾读数差模 M 同余（等价于总回绕次数为非负整数）
    if numerator % modulus != 0:
        conflicts.append(REASON_TOTAL_INCOMPATIBLE_MODULUS)
        total_wraps: Optional[int] = None
    else:
        total_wraps = numerator // modulus
        if not (sum_low <= total_wraps <= sum_high):
            conflicts.append(REASON_TOTAL_OUT_OF_RANGE)

    if conflicts:
        return ReconcileResult(
            status=STATUS_CONFLICT,
            total_wraps=total_wraps,
            narrowed=(),
            conflicts=tuple(conflicts),
            feasible_min_total=feasible_min_total,
            feasible_max_total=feasible_max_total,
        )

    # K 固定：其余各段圈数之和为 K - k_i，由其余段的区间和反推出 k_i 的范围
    narrowed = []
    for i, (lo, hi) in enumerate(wrap_bounds):
        other_high = sum_high - hi  # 除 i 外各段最大圈数之和
        other_low = sum_low - lo  # 除 i 外各段最小圈数之和
        new_lo = max(lo, total_wraps - other_high)
        new_hi = min(hi, total_wraps - other_low)
        narrowed.append(
            NarrowedSegment(
                index=i,
                diff=diffs[i],
                min_wraps=new_lo,
                max_wraps=new_hi,
            )
        )

    return ReconcileResult(
        status=STATUS_OK,
        total_wraps=total_wraps,
        narrowed=tuple(narrowed),
        conflicts=(),
        feasible_min_total=feasible_min_total,
        feasible_max_total=feasible_max_total,
    )
