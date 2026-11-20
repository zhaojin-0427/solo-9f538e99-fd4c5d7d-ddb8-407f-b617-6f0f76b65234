"""纯函数式约束求解核心。

规则（与存储无关，便于单测）：

1. 层位通过对接关系（无向）合并为连通组，同组视为同一次沉积，必须共用一个年份。
2. 组的可能年代域 = 组内各层位年代段（整年闭区间，可含空档）的交集。
3. 叠压关系（下层 -> 上层，严格）提升为组间边；成环（含同组自环）即无解。
4. 在有向无环图上做两次贪心：
   - 最早解 E：拓扑序，每个组取域内满足 ``y > max(E[前驱])`` 的最小年份；
   - 最晚解 L：逆拓扑序，每个组取域内满足 ``y < min(L[后继])`` 的最大年份。
   任意可行解 F 都满足 E <= F <= L（分量意义），因此组 g 的全部可同时成立年份
   恰为其年代域与闭区间 [E[g], L[g]] 的交集（仍可为含空档的若干闭区间）。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

# 年代域：有序、互不相交、互不相邻的整年闭区间列表，如 [(-500, -401), (-200, -100)]
Domain = list[tuple[int, int]]


def normalize_intervals(intervals: list[tuple[int, int]]) -> Domain:
    """合并重叠或首尾相接（相邻整数年）的闭区间，返回规范化年代域。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[list[int]] = []
    for start, end in ordered:
        if start > end:
            start, end = end, start
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return [(s, e) for s, e in merged]


def intersect_domains(domains: list[Domain]) -> Domain:
    """多个年代域（每个本身已规范化）的交集，结果仍规范化。"""
    if not domains:
        return []
    result: Domain = domains[0]
    for domain in domains[1:]:
        result = _intersect_two(result, domain)
        if not result:
            break
    return result


def _intersect_two(a: Domain, b: Domain) -> Domain:
    out: list[tuple[int, int]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo <= hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return normalize_intervals(out)


def first_ge(domain: Domain, lower: int) -> Optional[int]:
    """域内 >= lower 的最小年份；不存在返回 None。"""
    for start, end in domain:
        if end < lower:
            continue
        return max(start, lower)
    return None


def last_le(domain: Domain, upper: int) -> Optional[int]:
    """域内 <= upper 的最大年份；不存在返回 None。"""
    for start, end in reversed(domain):
        if start > upper:
            continue
        return min(end, upper)
    return None


def clip_domain(domain: Domain, lower: int, upper: int) -> Domain:
    """取年代域与闭区间 [lower, upper] 的交集。"""
    if lower > upper:
        return []
    return _intersect_two(domain, [(lower, upper)])


@dataclass(frozen=True)
class StratumInput:
    id: int
    code: str  # 展示用，如 "T1@L3"
    intervals: Domain = field(default_factory=list)


@dataclass(frozen=True)
class CorrelationInput:
    id: int
    a_id: int
    b_id: int


@dataclass(frozen=True)
class SuperpositionInput:
    id: int
    lower_id: int
    upper_id: int


@dataclass(frozen=True)
class WitnessRecord:
    """矛盾见证中的一条原始记录。"""

    kind: str  # stratum | correlation | superposition
    record_id: int
    detail: str


@dataclass(frozen=True)
class Contradiction:
    reason: str  # EMPTY_DOMAIN | GROUP_CYCLE | ORDER_CONFLICT
    message: str
    witness: list[WitnessRecord]


@dataclass(frozen=True)
class GroupEdge:
    from_group: int
    to_group: int
    edge_ids: list[int]


@dataclass(frozen=True)
class GroupResult:
    group_id: int
    member_ids: list[int]
    domain: Domain
    feasible_years: Domain
    earliest_year: Optional[int]
    latest_year: Optional[int]


@dataclass(frozen=True)
class Solution:
    feasible: bool
    groups: list[GroupResult]
    group_edges: list[GroupEdge]
    # 连通组映射：层位 id -> 组 id（组 id 取组内最小层位 id）
    group_of: dict[int, int]
    contradiction: Optional[Contradiction] = None
    earliest_assignment: Optional[dict[int, int]] = None
    latest_assignment: Optional[dict[int, int]] = None


class _UnionFind:
    def __init__(self, ids: list[int]) -> None:
        self.parent = {i: i for i in ids}

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # 以较小 id 为根，组 id 稳定可读
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


def solve(
    strata: list[StratumInput],
    correlations: list[CorrelationInput],
    superpositions: list[SuperpositionInput],
) -> Solution:
    """对单个遗址的全部记录求解。"""
    strata_by_id = {s.id: s for s in strata}
    uf = _UnionFind([s.id for s in strata])
    for corr in correlations:
        uf.union(corr.a_id, corr.b_id)

    members: dict[int, list[int]] = defaultdict(list)
    for s in strata:
        members[uf.find(s.id)].append(s.id)
    for gid in members:
        members[gid].sort()
    group_ids = sorted(members)

    def gid_of(sid: int) -> int:
        return uf.find(sid)

    # 组年代域 = 成员层位年代域交集
    group_domain: dict[int, Domain] = {}
    empty_domain_group: Optional[int] = None
    for gid in group_ids:
        domains = [strata_by_id[sid].intervals for sid in members[gid]]
        domain = intersect_domains(domains)
        group_domain[gid] = domain
        if not domain and empty_domain_group is None:
            empty_domain_group = gid

    # 叠压提升为组间边（保留全部原始边 id 作为出处）
    raw_edges: dict[tuple[int, int], list[int]] = defaultdict(list)
    self_loop_edges: list[SuperpositionInput] = []
    for sup in superpositions:
        gl, gu = gid_of(sup.lower_id), gid_of(sup.upper_id)
        if gl == gu:
            self_loop_edges.append(sup)
        else:
            raw_edges[(gl, gu)].append(sup.id)

    adj: dict[int, set[int]] = {gid: set() for gid in group_ids}
    indeg: dict[int, int] = {gid: 0 for gid in group_ids}
    for (gl, gu) in raw_edges:
        if gu not in adj[gl]:
            adj[gl].add(gu)
            indeg[gu] += 1

    group_edges = [
        GroupEdge(from_group=gl, to_group=gu, edge_ids=sorted(ids))
        for (gl, gu), ids in sorted(raw_edges.items())
    ]
    group_of = {s.id: gid_of(s.id) for s in strata}

    def base_groups_result() -> list[GroupResult]:
        return [
            GroupResult(
                group_id=gid,
                member_ids=list(members[gid]),
                domain=group_domain[gid],
                feasible_years=[],
                earliest_year=None,
                latest_year=None,
            )
            for gid in group_ids
        ]

    # 1) 同组自环：同一次沉积的下层又在自己上面，直接成环矛盾
    if self_loop_edges:
        sup0 = self_loop_edges[0]
        gid = gid_of(sup0.lower_id)
        witness = _group_member_witness(gid, members, strata_by_id)
        witness.append(
            WitnessRecord(
                "superposition",
                sup0.id,
                f"下层 {strata_by_id[sup0.lower_id].code} 与上层 "
                f"{strata_by_id[sup0.upper_id].code} 已对接为同一次沉积，不能再叠压",
            )
        )
        return Solution(
            feasible=False,
            groups=base_groups_result(),
            group_edges=group_edges,
            group_of=group_of,
            contradiction=Contradiction(
                "GROUP_CYCLE", "合组后的叠压关系成环（同组自环）", witness
            ),
        )

    # 2) Kahn 拓扑排序 + DFS 找具体环
    topo, cycle_nodes = _topo_sort(group_ids, adj, indeg)
    if cycle_nodes is not None:
        cycle_edge_ids = _cycle_edge_ids(cycle_nodes, raw_edges)
        involved = set(cycle_nodes)
        witness = [
            w
            for gid in sorted(involved)
            for w in _group_member_witness(gid, members, strata_by_id)
        ]
        for eid in cycle_edge_ids:
            sup = next(x for x in superpositions if x.id == eid)
            witness.append(
                WitnessRecord(
                    "superposition",
                    eid,
                    f"{strata_by_id[sup.lower_id].code}（下） -> "
                    f"{strata_by_id[sup.upper_id].code}（上）",
                )
            )
        return Solution(
            feasible=False,
            groups=base_groups_result(),
            group_edges=group_edges,
            group_of=group_of,
            contradiction=Contradiction(
                "GROUP_CYCLE", "合组后的叠压关系成环", witness
            ),
        )

    # 3) 年代域为空：组内层位的年代段没有公共年份
    if empty_domain_group is not None:
        gid = empty_domain_group
        return Solution(
            feasible=False,
            groups=base_groups_result(),
            group_edges=group_edges,
            group_of=group_of,
            contradiction=Contradiction(
                "EMPTY_DOMAIN",
                "同组层位的可能年代段交集为空，无法共用一个年份",
                _group_member_witness(gid, members, strata_by_id),
            ),
        )

    predecessors: dict[int, list[int]] = {gid: [] for gid in group_ids}
    successors: dict[int, list[int]] = {gid: list(adj[gid]) for gid in group_ids}
    for u in group_ids:
        for v in adj[u]:
            predecessors[v].append(u)
    for gid in group_ids:
        predecessors[gid].sort()
        successors[gid].sort()

    # 4) 最早解（拓扑序贪心）
    earliest: dict[int, int] = {}
    for gid in topo:
        bound = group_domain[gid][0][0]
        for pred in predecessors[gid]:
            candidate = earliest[pred] + 1
            if candidate > bound:
                bound = candidate
        value = first_ge(group_domain[gid], bound)
        if value is None:
            return Solution(
                feasible=False,
                groups=base_groups_result(),
                group_edges=group_edges,
                group_of=group_of,
                contradiction=Contradiction(
                    "ORDER_CONFLICT",
                    f"组 {gid} 的最早可能年份不晚于 {group_domain[gid][-1][1]}，"
                    f"但叠压关系要求不早于 {bound}",
                    _chain_witness(
                        gid, raw_edges, members,
                        strata_by_id, superpositions,
                    ),
                ),
            )
        earliest[gid] = value

    # 5) 最晚解（逆拓扑序贪心）
    latest: dict[int, int] = {}
    for gid in reversed(topo):
        bound = group_domain[gid][-1][1]
        for succ in successors[gid]:
            candidate = latest[succ] - 1
            if candidate < bound:
                bound = candidate
        value = last_le(group_domain[gid], bound)
        if value is None:
            return Solution(
                feasible=False,
                groups=base_groups_result(),
                group_edges=group_edges,
                group_of=group_of,
                contradiction=Contradiction(
                    "ORDER_CONFLICT",
                    f"组 {gid} 的最晚可能年份不早于 {group_domain[gid][0][0]}，"
                    f"但叠压关系要求不晚于 {bound}",
                    _chain_witness(
                        gid, raw_edges, members,
                        strata_by_id, superpositions,
                    ),
                ),
            )
        latest[gid] = value

    groups: list[GroupResult] = []
    for gid in group_ids:
        feasible_years = clip_domain(
            group_domain[gid], earliest[gid], latest[gid]
        )
        groups.append(
            GroupResult(
                group_id=gid,
                member_ids=list(members[gid]),
                domain=group_domain[gid],
                feasible_years=feasible_years,
                earliest_year=earliest[gid],
                latest_year=latest[gid],
            )
        )

    return Solution(
        feasible=True,
        groups=groups,
        group_edges=group_edges,
        group_of=group_of,
        earliest_assignment=earliest,
        latest_assignment=latest,
    )


def _topo_sort(
    group_ids: list[int],
    adj: dict[int, set[int]],
    indeg: dict[int, int],
) -> tuple[list[int], Optional[list[int]]]:
    """Kahn 排序；若有环返回 (部分序列, 环上节点列表)。"""
    indeg = dict(indeg)
    queue = sorted(gid for gid in group_ids if indeg[gid] == 0)
    topo: list[int] = []
    while queue:
        node = queue.pop(0)
        topo.append(node)
        for nxt in sorted(adj[node]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
        queue.sort()
    if len(topo) == len(group_ids):
        return topo, None
    return topo, _find_cycle(group_ids, adj)


def _find_cycle(group_ids: list[int], adj: dict[int, set[int]]) -> list[int]:
    """DFS 三色标记，返回环上的节点序列（首尾相同）。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {gid: WHITE for gid in group_ids}
    stack: list[int] = []
    cycle: list[int] = []

    def dfs(node: int) -> bool:
        color[node] = GRAY
        stack.append(node)
        for nxt in sorted(adj[node]):
            if color[nxt] == GRAY:
                idx = stack.index(nxt)
                cycle.extend(stack[idx:])
                cycle.append(nxt)
                return True
            if color[nxt] == WHITE and dfs(nxt):
                return True
        stack.pop()
        color[node] = BLACK
        return False

    for gid in group_ids:
        if color[gid] == WHITE and dfs(gid):
            break
    return cycle


def _cycle_edge_ids(
    cycle_nodes: list[int],
    raw_edges: dict[tuple[int, int], list[int]],
) -> list[int]:
    ids: list[int] = []
    for u, v in zip(cycle_nodes, cycle_nodes[1:]):
        ids.append(sorted(raw_edges[(u, v)])[0])
    return ids


def _group_member_witness(
    gid: int,
    members: dict[int, list[int]],
    strata_by_id: dict[int, StratumInput],
) -> list[WitnessRecord]:
    out: list[WitnessRecord] = []
    for sid in members[gid]:
        s = strata_by_id[sid]
        out.append(
            WitnessRecord(
                "stratum",
                s.id,
                f"{s.code} 年代段 {_fmt_intervals(s.intervals)}",
            )
        )
    return out


def _chain_witness(
    failed_gid: int,
    raw_edges: dict[tuple[int, int], list[int]],
    members: dict[int, list[int]],
    strata_by_id: dict[int, StratumInput],
    superpositions: list[SuperpositionInput],
) -> list[WitnessRecord]:
    """收集与冲突组处于同一弱连通分量的全部原始记录（本身即矛盾子集）。

    最早解可能在链中段失败，仅向上游回溯会漏掉下游组；故在组间 DAG 的
    无向视图上遍历，把整条约束链（含分叉、汇合）的组与边全部纳入见证。
    ``neighbors`` 保留调用方向语义，实际遍历忽略边方向。
    """
    sup_by_id = {s.id: s for s in superpositions}

    undirected: dict[int, set[int]] = defaultdict(set)
    for gl, gu in raw_edges:
        undirected[gl].add(gu)
        undirected[gu].add(gl)

    seen_nodes: set[int] = {failed_gid}
    stack = [failed_gid]
    while stack:
        node = stack.pop()
        for nxt in undirected.get(node, ()):
            if nxt not in seen_nodes:
                seen_nodes.add(nxt)
                stack.append(nxt)

    seen_edges: set[int] = set()
    for (gl, gu), edge_ids in raw_edges.items():
        if gl in seen_nodes and gu in seen_nodes:
            seen_edges.update(edge_ids)

    witness: list[WitnessRecord] = []
    for gid in sorted(seen_nodes):
        witness.extend(_group_member_witness(gid, members, strata_by_id))
    for eid in sorted(seen_edges):
        sup = sup_by_id[eid]
        witness.append(
            WitnessRecord(
                "superposition",
                eid,
                f"{strata_by_id[sup.lower_id].code}（下） -> "
                f"{strata_by_id[sup.upper_id].code}（上）",
            )
        )
    return witness


def _fmt_intervals(domain: Domain) -> str:
    if not domain:
        return "∅"
    return ", ".join(f"[{lo}, {hi}]" for lo, hi in domain)
