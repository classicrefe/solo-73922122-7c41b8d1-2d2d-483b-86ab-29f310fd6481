"""matching 核心算法单元测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.matching import solve


def plan(row_id: str, well: str, analyte: str, t: int, sample_no="S"):
    return SimpleNamespace(
        row_id=row_id,
        sample_no=sample_no,
        well_position=well,
        analyte=analyte,
        planned_at=t,
    )


def result(result_id: str, well: str, analyte: str, t: int, value=1.0):
    return SimpleNamespace(
        result_id=result_id,
        well_position=well,
        analyte=analyte,
        measured_at=t,
        measured_value=value,
    )


def test_unique_matching_fills_single_option():
    plans = [plan("P1", "A01", "Pb", 1000), plan("P2", "A01", "Pb", 2000)]
    results = [result("R1", "A01", "Pb", 1005), result("R2", "A01", "Pb", 2010)]
    out = solve(plans, results, tolerance_seconds=60)

    assert out.pair_count == 2
    assert out.total_time_diff_seconds == 15
    v0, v1 = out.verdicts
    assert v0.eligible_plan_indices == [0] and v0.can_be_unmatched is False
    assert v1.eligible_plan_indices == [1] and v1.can_be_unmatched is False


def test_zero_cost_symmetric_alternatives_are_both_reported():
    plans = [plan("P1", "A01", "Pb", 1000), plan("P2", "A01", "Pb", 1000)]
    results = [result("R1", "A01", "Pb", 1000), result("R2", "A01", "Pb", 1000)]
    out = solve(plans, results, tolerance_seconds=60)

    assert out.pair_count == 2
    assert out.total_time_diff_seconds == 0
    for v in out.verdicts:
        assert v.eligible_plan_indices == [0, 1]
        assert v.can_be_unmatched is False  # 最大配对数为 2，任何最优方案都满配


def test_surplus_result_can_be_unmatched():
    plans = [plan("P1", "A01", "Pb", 1000)]
    results = [result("R1", "A01", "Pb", 1000), result("R2", "A01", "Pb", 1000)]
    out = solve(plans, results, tolerance_seconds=60)

    assert out.pair_count == 1
    for v in out.verdicts:
        assert v.eligible_plan_indices == [0]
        assert v.can_be_unmatched is True  # 任一结果都可能成为未匹配的那个


def test_tolerance_is_inclusive_boundary():
    plans = [plan("P1", "A01", "Pb", 1000)]
    in_bound = solve(plans, [result("R1", "A01", "Pb", 1060)], 60)
    over_bound = solve(plans, [result("R1", "A01", "Pb", 1061)], 60)

    assert in_bound.verdicts[0].eligible_plan_indices == [0]
    assert in_bound.verdicts[0].can_be_unmatched is False
    assert over_bound.verdicts[0].eligible_plan_indices == []
    assert over_bound.verdicts[0].can_be_unmatched is True


def test_well_and_analyte_must_both_match():
    plans = [
        plan("P1", "A01", "Pb", 1000),
        plan("P2", "A02", "Cd", 1000),
    ]
    results = [
        result("R1", "A01", "Cd", 1000),   # 孔位同、项目不同
        result("R2", "A02", "Pb", 1000),   # 孔位不同、项目同
    ]
    out = solve(plans, results, 60)
    assert out.pair_count == 0
    assert all(v.eligible_plan_indices == [] for v in out.verdicts)
    assert all(v.can_be_unmatched for v in out.verdicts)


def test_empty_inputs():
    out = solve([], [], 60)
    assert out.pair_count == 0
    assert out.total_time_diff_seconds == 0
    assert out.verdicts == []
    assert out.representative_matching == []


def test_cheaper_total_time_diff_wins_among_full_matchings():
    plans = [plan("P1", "A01", "Pb", 1000), plan("P2", "A01", "Pb", 1100)]
    results = [result("R1", "A01", "Pb", 1000), result("R2", "A01", "Pb", 1090)]
    out = solve(plans, results, 100)

    # 直配: 0 + 10 = 10；交叉配: 100 + 90 = 190
    assert out.pair_count == 2
    assert out.total_time_diff_seconds == 10
    assert out.verdicts[0].eligible_plan_indices == [0]
    assert out.verdicts[1].eligible_plan_indices == [1]
    assert all(not v.can_be_unmatched for v in out.verdicts)


def test_max_cardinality_takes_precedence_over_cost():
    plans = [plan("P1", "A01", "Pb", 1000)]
    results = [
        result("R1", "A01", "Pb", 1000),  # 费用 0
        result("R2", "A01", "Pb", 1060),  # 费用 60，容差内
    ]
    out = solve(plans, results, 60)

    assert out.pair_count == 1
    assert out.total_time_diff_seconds == 0
    v0, v1 = out.verdicts
    # 费用 60 的替代方案不是最优 => R1 始终匹配，R2 始终未匹配
    assert v0.eligible_plan_indices == [0] and v0.can_be_unmatched is False
    assert v1.eligible_plan_indices == [] and v1.can_be_unmatched is True


def test_groups_solved_independently():
    plans = [
        plan("P1", "A01", "Pb", 1000),
        plan("P2", "B02", "Cd", 5000),
    ]
    results = [
        result("R1", "A01", "Pb", 1000),
        result("R2", "B02", "Cd", 5050),
        result("R3", "C03", "Hg", 9000),
    ]
    out = solve(plans, results, 60)
    assert out.pair_count == 2
    assert out.total_time_diff_seconds == 50
    assert out.verdicts[0].eligible_plan_indices == [0]
    assert out.verdicts[1].eligible_plan_indices == [1]
    assert out.verdicts[2].eligible_plan_indices == []


def test_representative_matching_is_a_valid_optimum():
    plans = [plan("P1", "A01", "Pb", 1000), plan("P2", "A01", "Pb", 1000)]
    results = [result("R1", "A01", "Pb", 1000), result("R2", "A01", "Pb", 1000)]
    out = solve(plans, results, 60)

    matched_r = [r for r, _ in out.representative_matching]
    matched_p = [p for _, p in out.representative_matching]
    assert sorted(matched_r) == [0, 1]
    assert sorted(matched_p) == [0, 1]
    assert len(out.representative_matching) == out.pair_count


def test_max_size_input_runs_fast_and_stays_consistent():
    """50+50 全连通同组实例：重跑 MCMF 50 次也应快速完成，且结论自洽。"""
    import time

    plans = [plan(f"P{i}", "A01", "Pb", 1000 + i * 10) for i in range(50)]
    results = [result(f"R{i}", "A01", "Pb", 1000 + i * 10) for i in range(50)]
    start = time.perf_counter()
    out = solve(plans, results, tolerance_seconds=10_000)
    elapsed = time.perf_counter() - start

    assert elapsed < 5.0
    assert out.pair_count == 50
    assert out.total_time_diff_seconds == 0
    # 所有结果时间点互异且容差极大 => 每个结果恰好只有一个同时间点计划行，
    # 但其余计划行也在容差内，会形成大量零费用环 => 全部归属歧义。
    for v in out.verdicts:
        assert v.can_be_unmatched is False
        assert len(v.eligible_plan_indices) >= 1


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_brute_force_agreement_on_small_instances(n):
    """对小规模随机实例，与枚举全部可行配对的结果逐项对照。

    n=5 时每个实例的排列级方案数量可控（全量部分匹配约数百个），可全枚举；
    更大规模的正确性由最小费用流/残余图理论及其他用例保证。
    """
    import itertools
    import random

    for seed in range(30):
        rng = random.Random(20261111 + seed * 97 + n)
        plans = [plan(f"P{i}", "A01", "Pb", rng.randint(0, 100)) for i in range(n)]
        results = [
            result(f"R{i}", "A01", "Pb", rng.randint(0, 100)) for i in range(n)
        ]
        tol = rng.randint(0, 30)
        out = solve(plans, results, tol)

        # 枚举所有部分匹配（排列子集），找字典序最优：先最大配对数，再最小费用。
        # 注意不能在 k 递增过程中清空已收集的方案，必须全部枚举后再统一比较。
        best = None  # (配对数, -总费用) 最大化
        optimal_matchings = []
        for k in range(n + 1):
            for result_subset in itertools.combinations(range(n), k):
                for plan_perm in itertools.permutations(range(n), k):
                    edges = tuple(zip(result_subset, plan_perm))
                    if all(
                        abs(results[r].measured_at - plans[p].planned_at) <= tol
                        for r, p in edges
                    ):
                        cost = sum(
                            abs(results[r].measured_at - plans[p].planned_at)
                            for r, p in edges
                        )
                        key = (k, -cost)
                        if best is None or key > best:
                            best = key
                            optimal_matchings = [frozenset(edges)]
                        elif key == best:
                            optimal_matchings.append(frozenset(edges))

        expected_pairs, neg_cost = best
        assert out.pair_count == expected_pairs
        assert out.total_time_diff_seconds == -neg_cost

        for r in range(n):
            expected_plans = sorted(
                {p for m in optimal_matchings for (rr, p) in m if rr == r}
            )
            expected_unmatched = any(
                r not in {rr for rr, _ in m} for m in optimal_matchings
            )
            v = out.verdicts[r]
            assert v.eligible_plan_indices == expected_plans
            assert v.can_be_unmatched == expected_unmatched
