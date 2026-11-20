"""求解器纯函数单元测试。"""

import pytest

from app.solver import (
    CorrelationInput,
    StratumInput,
    SuperpositionInput,
    clip_domain,
    first_ge,
    intersect_domains,
    last_le,
    normalize_intervals,
    solve,
)


# ---------- 年代域工具 ----------

def test_normalize_merges_overlap_and_adjacent():
    assert normalize_intervals([(5, 10), (10, 12), (20, 25), (26, 30)]) == [
        (5, 12),
        (20, 30),
    ]
    assert normalize_intervals([(3, 1), (10, 8)]) == [(1, 3), (8, 10)]
    assert normalize_intervals([]) == []


def test_intersect_domains_with_gaps():
    a = [(1, 10), (20, 30)]
    b = [(5, 22), (28, 40)]
    assert intersect_domains([a, b]) == [(5, 10), (20, 22), (28, 30)]
    assert intersect_domains([a, [(11, 19)]]) == []


def test_clip_and_bound_helpers():
    domain = [(1, 5), (10, 20), (30, 40)]
    assert first_ge(domain, 8) == 10
    assert first_ge(domain, 41) is None
    assert last_le(domain, 25) == 20
    assert last_le(domain, 0) is None
    assert clip_domain(domain, 12, 32) == [(12, 20), (30, 32)]
    assert clip_domain(domain, 100, 200) == []


# ---------- 端到端求解 ----------

def _s(id_, intervals, code=None):
    return StratumInput(id=id_, code=code or f"L{id_}", intervals=intervals)


def test_basic_grouping_and_order_with_gaps():
    # 1@下, 2@上；对接 1-3, 2-4（两个组）
    strata = [
        _s(1, [(100, 200), (300, 400)]),
        _s(2, [(50, 90), (150, 250)]),
        _s(3, [(120, 350)]),
        _s(4, [(60, 180)]),
    ]
    corrs = [CorrelationInput(1, 1, 3), CorrelationInput(2, 2, 4)]
    sups = [SuperpositionInput(1, lower_id=1, upper_id=2)]
    sol = solve(strata, corrs, sups)

    assert sol.feasible
    assert sol.group_of[1] == sol.group_of[3] == 1
    assert sol.group_of[2] == sol.group_of[4] == 2
    g1 = next(g for g in sol.groups if g.group_id == 1)
    g2 = next(g for g in sol.groups if g.group_id == 2)
    # 组1 域 = [100,200] ∩ [120,350] ∪ [300,400] ∩ [120,350]
    assert g1.domain == [(120, 200), (300, 350)]
    # 组2 域 = [50,90]∪[150,250] ∩ [60,180]
    assert g2.domain == [(60, 90), (150, 180)]
    # 组1 < 组2（严格）：组1 可行年份必须 +1 后仍能在组2 取值
    # 最早解：组1=120，组2=150；最晚解：组2=180，组1 取 <180 的最大值=179
    assert sol.earliest_assignment == {1: 120, 2: 150}
    assert sol.latest_assignment == {1: 179, 2: 180}
    # 组1 可行年份 = 自身域 ∩ [120,179]，跨两段；组2 被夹在 [150,180]
    assert g1.feasible_years == [(120, 179)]
    assert g2.feasible_years == [(150, 180)]


def test_gap_skips_infeasible_value_via_greedy():
    # 下层域 1..10，上层域只有 {1}（单年）；严格小于 -> 无解
    strata = [_s(1, [(1, 10)]), _s(2, [(1, 1)])]
    sol = solve(strata, [], [SuperpositionInput(1, 1, 2)])
    assert not sol.feasible
    assert sol.contradiction.reason == "ORDER_CONFLICT"
    kinds = {(w.kind, w.record_id) for w in sol.contradiction.witness}
    assert ("stratum", 1) in kinds
    assert ("stratum", 2) in kinds
    assert ("superposition", 1) in kinds


def test_gap_feasible_when_spanning_gap():
    # 下层 {1..5, 10..20}，上层 {6..9}：下层取 5，上层取 6..9 可行
    strata = [_s(1, [(1, 5), (10, 20)]), _s(2, [(6, 9)])]
    sol = solve(strata, [], [SuperpositionInput(1, 1, 2)])
    assert sol.feasible
    g1 = sol.groups[0]
    assert g1.feasible_years == [(1, 5)]
    assert g1.latest_year == 5


def test_empty_domain_correlation_rejected_at_solve():
    strata = [_s(1, [(1, 10)]), _s(2, [(20, 30)])]
    sol = solve(strata, [CorrelationInput(1, 1, 2)], [])
    assert not sol.feasible
    assert sol.contradiction.reason == "EMPTY_DOMAIN"
    witness_ids = {w.record_id for w in sol.contradiction.witness}
    assert witness_ids == {1, 2}


def test_group_cycle_detected():
    # 1<2<3 叠压，再用对接把 1 和 3 合组 -> 自环
    strata = [_s(i, [(1, 100)]) for i in (1, 2, 3)]
    corrs = [CorrelationInput(1, 1, 3)]
    sups = [
        SuperpositionInput(1, 1, 2),
        SuperpositionInput(2, 2, 3),
    ]
    sol = solve(strata, corrs, sups)
    assert not sol.feasible
    assert sol.contradiction.reason == "GROUP_CYCLE"
    assert any(w.kind == "superposition" for w in sol.contradiction.witness)


def test_cycle_between_three_groups():
    strata = [_s(i, [(1, 100)]) for i in (1, 2, 3)]
    sups = [
        SuperpositionInput(1, 1, 2),
        SuperpositionInput(2, 2, 3),
        SuperpositionInput(3, 3, 1),
    ]
    sol = solve(strata, [], sups)
    assert not sol.feasible
    assert sol.contradiction.reason == "GROUP_CYCLE"
    edge_ids = {w.record_id for w in sol.contradiction.witness if w.kind == "superposition"}
    assert edge_ids == {1, 2, 3}


def test_undone_correlation_recomputes_groups():
    strata = [_s(1, [(1, 100)]), _s(2, [(1, 100)]), _s(3, [(1, 100)])]
    corrs_all = [CorrelationInput(1, 1, 2), CorrelationInput(2, 2, 3)]

    merged = solve(strata, corrs_all, [])
    assert len(merged.groups) == 1

    # 撤销 1-2 后只剩 2-3 同组
    split = solve(strata, [corrs_all[1]], [])
    assert split.feasible
    assert len(split.groups) == 2
    assert split.group_of[2] == split.group_of[3]
    assert split.group_of[1] != split.group_of[2]


def test_chain_feasible_years_clipped_to_any_feasible_value():
    # 三层链 1 < 2 < 3，宽域，验证每个组可行年份集合 = [E, L]
    strata = [_s(i, [(1, 10)]) for i in (1, 2, 3)]
    sups = [SuperpositionInput(1, 1, 2), SuperpositionInput(2, 2, 3)]
    sol = solve(strata, [], sups)
    assert sol.feasible
    by_id = {g.group_id: g for g in sol.groups}
    assert by_id[1].feasible_years == [(1, 8)]
    assert by_id[2].feasible_years == [(2, 9)]
    assert by_id[3].feasible_years == [(3, 10)]


def test_order_conflict_witness_covers_full_chain():
    # 4 个被钉死在同一年的层位无法满足严格链 1<2<3<4：最早解在中段失败，
    # 见证仍须包含下游全部组与边
    strata = [_s(i, [(5, 5)]) for i in (1, 2, 3, 4)]
    sups = [SuperpositionInput(i, i, i + 1) for i in (1, 2, 3)]
    sol = solve(strata, [], sups)
    assert not sol.feasible
    assert sol.contradiction.reason == "ORDER_CONFLICT"
    witness = sol.contradiction.witness
    assert {w.record_id for w in witness if w.kind == "stratum"} == {1, 2, 3, 4}
    assert {w.record_id for w in witness if w.kind == "superposition"} == {1, 2, 3}


def test_order_conflict_witness_excludes_unrelated_group():
    # 无关的独立可行组不得被卷入矛盾见证
    strata = [_s(1, [(5, 5)]), _s(2, [(5, 5)]), _s(9, [(1, 100)])]
    sol = solve(strata, [], [SuperpositionInput(1, 1, 2)])
    assert not sol.feasible
    ids = {w.record_id for w in sol.contradiction.witness}
    assert 9 not in ids


def test_diamond_dag_witness():
    strata = [_s(1, [(1, 1)]), _s(2, [(1, 10)]), _s(3, [(1, 10)]), _s(4, [(1, 1)])]
    sups = [
        SuperpositionInput(1, 1, 2),
        SuperpositionInput(2, 1, 3),
        SuperpositionInput(3, 2, 4),
        SuperpositionInput(4, 3, 4),
    ]
    sol = solve(strata, [], sups)
    assert not sol.feasible
    witness = sol.contradiction.witness
    assert {w.record_id for w in witness if w.kind == "stratum"} == {1, 2, 3, 4}
    assert {w.record_id for w in witness if w.kind == "superposition"} == {1, 2, 3, 4}


def test_negative_years_bce():
    strata = [_s(1, [(-500, -400)]), _s(2, [(-450, -300)])]
    sol = solve(strata, [], [SuperpositionInput(1, 1, 2)])
    assert sol.feasible
    g1, g2 = sol.groups
    # 严格 y1 < y2：最早解 (-500, -450)，最晚解 (-400, -300)
    assert sol.earliest_assignment == {1: -500, 2: -450}
    assert sol.latest_assignment == {1: -400, 2: -300}
    # g1 取 -400 时 g2 仍可取 -399..-300，故 -400 可行
    assert g1.feasible_years == [(-500, -400)]
    assert g2.feasible_years == [(-450, -300)]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
