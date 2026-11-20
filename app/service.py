"""把 SQLite 原始记录装配为求解器输入，并负责结果序列化。

叠压的“出处”可以有多处：手工登记、若干本野外簿里的记录。同一对
``(下层, 上层)`` 无论被记在多少处，送入求解器时都只作为**一条**叠压边；
这里负责去重、保留全部出处，并在矛盾见证中把出处还原出来。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from . import db
from .solver import (
    Contradiction,
    CorrelationInput,
    Solution,
    StratumInput,
    SuperpositionInput,
    normalize_intervals,
    solve,
)

Pair = tuple[int, int]

# 只有野外簿出处时，求解器内部边 id = 本偏移 + 簿记录 id。
# 手工叠压 id 从 1 起自增，远小于本偏移，两个 id 空间不会重号。
NOTEBOOK_EDGE_ID_BASE = 1_000_000_000


# ---------- 出处 ----------

@dataclass(frozen=True)
class SuperpositionSource:
    """一条叠压出处的原始记录。"""

    source: str  # "manual" | "notebook"
    record_id: int  # 手工叠压 id 或 notebook_superpositions.id
    lower_id: int
    upper_id: int
    notebook_no: Optional[str] = None
    page: Optional[int] = None
    line: Optional[int] = None
    lower_label: Optional[str] = None
    upper_label: Optional[str] = None

    def describe(self) -> str:
        if self.source == "manual":
            return f"手工记录 #{self.record_id}"
        location = ""
        if self.page is not None and self.line is not None:
            location = f" 第{self.page}页第{self.line}行"
        elif self.page is not None:
            location = f" 第{self.page}页"
        return (
            f"野外簿 {self.notebook_no}{location}"
            f"（{self.lower_label} → {self.upper_label}）"
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": self.source,
            "record_id": self.record_id,
            "lower_id": self.lower_id,
            "upper_id": self.upper_id,
        }
        if self.source == "notebook":
            data.update(
                {
                    "notebook_no": self.notebook_no,
                    "page": self.page,
                    "line": self.line,
                    "lower_label": self.lower_label,
                    "upper_label": self.upper_label,
                }
            )
        return data


def _source_sort_key(src: SuperpositionSource):
    return (
        0 if src.source == "manual" else 1,
        src.notebook_no or "",
        src.page if src.page is not None else 0,
        src.line if src.line is not None else 0,
        src.record_id,
    )


@dataclass
class SuperpositionProvenance:
    """去重后的叠压边及其全部出处。"""

    inputs: list[SuperpositionInput] = field(default_factory=list)
    pair_by_id: dict[int, Pair] = field(default_factory=dict)
    sources_by_id: dict[int, list[SuperpositionSource]] = field(default_factory=dict)
    sources_by_pair: dict[Pair, list[SuperpositionSource]] = field(default_factory=dict)

    def all_sources(self) -> list[SuperpositionSource]:
        out: list[SuperpositionSource] = []
        for pair in sorted(self.sources_by_pair):
            out.extend(self.sources_by_pair[pair])
        return out

    def notebook_issues(
        self, notebook_no: str, contradiction: Optional[Contradiction]
    ) -> list[dict[str, Any]]:
        """本簿中卷入矛盾的页行；用于无解时指出问题位置。"""
        if contradiction is None:
            return []
        seen: set[tuple] = set()
        issues: list[dict[str, Any]] = []
        for witness in contradiction.witness:
            if witness.kind != "superposition":
                continue
            for src in self.sources_by_id.get(witness.record_id, []):
                if src.source != "notebook" or src.notebook_no != notebook_no:
                    continue
                key = (src.page, src.line, src.lower_label, src.upper_label)
                if key in seen:
                    continue
                seen.add(key)
                issues.append(
                    {
                        "kind": "CONTRADICTION",
                        "notebook_no": src.notebook_no,
                        "page": src.page,
                        "line": src.line,
                        "lower_label": src.lower_label,
                        "upper_label": src.upper_label,
                        "lower_id": src.lower_id,
                        "upper_id": src.upper_id,
                        "message": (
                            f"野外簿 {src.notebook_no} 第{src.page}页第{src.line}行"
                            f"（{src.lower_label} → {src.upper_label}）"
                            "与其余来源合并后使约束无解"
                        ),
                    }
                )
        issues.sort(key=lambda i: (i["page"] or 0, i["line"] or 0))
        return issues


def _collect_sources(conn: sqlite3.Connection, site_id: int) -> list[SuperpositionSource]:
    sources: list[SuperpositionSource] = []
    for row in db.list_superpositions(conn, site_id):
        sources.append(
            SuperpositionSource(
                source="manual",
                record_id=row["id"],
                lower_id=row["lower_id"],
                upper_id=row["upper_id"],
            )
        )
    for row in db.list_site_notebook_superpositions(conn, site_id):
        sources.append(
            SuperpositionSource(
                source="notebook",
                record_id=row["id"],
                lower_id=row["lower_id"],
                upper_id=row["upper_id"],
                notebook_no=row["notebook_no"],
                page=row["page"],
                line=row["line"],
                lower_label=row["lower_label"],
                upper_label=row["upper_label"],
            )
        )
    return sources


def build_provenance(conn: sqlite3.Connection, site_id: int) -> SuperpositionProvenance:
    """按 ``(lower_id, upper_id)`` 归并出处；每对只产出一条求解器输入。"""
    sources_by_pair: dict[Pair, list[SuperpositionSource]] = {}
    for src in _collect_sources(conn, site_id):
        sources_by_pair.setdefault((src.lower_id, src.upper_id), []).append(src)

    provenance = SuperpositionProvenance(sources_by_pair=sources_by_pair)
    # 求解器内部的边 id 必须唯一：同一对若已有手工记录，沿用手工叠压 id
    # （与原有 API 一致）；只有野外簿出处时，用簿记录 id 加固定偏移量，
    # 两个 id 空间不会重叠。每对无论多少处出处都只得到一条输入。
    for pair in sorted(sources_by_pair):
        sources = sources_by_pair[pair]
        sources.sort(key=_source_sort_key)
        manual_ids = [s.record_id for s in sources if s.source == "manual"]
        if manual_ids:
            edge_id = min(manual_ids)
        else:
            edge_id = NOTEBOOK_EDGE_ID_BASE + min(
                s.record_id for s in sources if s.source == "notebook"
            )
        provenance.inputs.append(
            SuperpositionInput(id=edge_id, lower_id=pair[0], upper_id=pair[1])
        )
        provenance.pair_by_id[edge_id] = pair
        provenance.sources_by_id[edge_id] = sources
    return provenance


# ---------- 装配与求解 ----------

@dataclass
class SiteRecords:
    strata: list[StratumInput]
    strata_by_id: dict[int, StratumInput]
    correlations: list[CorrelationInput]
    provenance: SuperpositionProvenance


def load_records(conn: sqlite3.Connection, site_id: int) -> SiteRecords:
    strata: list[StratumInput] = []
    for row in db.list_strata(conn, site_id):
        strata.append(
            StratumInput(
                id=row["id"],
                code=f"{row['trench']}@{row['layer_no']}",
                intervals=normalize_intervals(db.get_intervals(conn, row["id"])),
            )
        )
    strata_by_id = {s.id: s for s in strata}
    correlations = [
        CorrelationInput(id=r["id"], a_id=r["stratum_a_id"], b_id=r["stratum_b_id"])
        for r in db.list_correlations(conn, site_id)
    ]
    return SiteRecords(
        strata=strata,
        strata_by_id=strata_by_id,
        correlations=correlations,
        provenance=build_provenance(conn, site_id),
    )


def solve_records(records: SiteRecords) -> Solution:
    return solve(records.strata, records.correlations, records.provenance.inputs)


@dataclass
class SolveBundle:
    solution: Solution
    records: SiteRecords


def load_solve_bundle(conn: sqlite3.Connection, site_id: int) -> SolveBundle:
    records = load_records(conn, site_id)
    return SolveBundle(solution=solve_records(records), records=records)


def load_and_solve(conn: sqlite3.Connection, site_id: int) -> Solution:
    records = load_records(conn, site_id)
    return solve_records(records)


# ---------- 序列化 ----------

def serialize_contradiction(
    contradiction: Contradiction,
    provenance: Optional[SuperpositionProvenance] = None,
) -> dict[str, Any]:
    witness = []
    for w in contradiction.witness:
        item: dict[str, Any] = {
            "kind": w.kind,
            "record_id": w.record_id,
            "detail": w.detail,
        }
        if (
            w.kind == "superposition"
            and provenance is not None
            and w.record_id in provenance.sources_by_id
        ):
            sources = provenance.sources_by_id[w.record_id]
            item["sources"] = [s.to_dict() for s in sources]
            item["detail"] = (
                f"{w.detail}；出处：" + "、".join(s.describe() for s in sources)
            )
        witness.append(item)
    return {
        "reason": contradiction.reason,
        "message": contradiction.message,
        "witness": witness,
    }


def serialize_solution(
    solution: Solution,
    version: int,
    provenance: Optional[SuperpositionProvenance] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "feasible": solution.feasible,
        "version": version,
        "groups": [
            {
                "group_id": g.group_id,
                "member_ids": g.member_ids,
                "domain": [[lo, hi] for lo, hi in g.domain],
                "feasible_years": [[lo, hi] for lo, hi in g.feasible_years],
                "earliest_year": g.earliest_year,
                "latest_year": g.latest_year,
            }
            for g in solution.groups
        ],
        "group_edges": [
            {
                "from_group": e.from_group,
                "to_group": e.to_group,
                "edge_ids": e.edge_ids,
                "sources": [
                    src.to_dict()
                    for eid in e.edge_ids
                    for src in (
                        provenance.sources_by_id.get(eid, [])
                        if provenance is not None
                        else []
                    )
                ],
            }
            for e in solution.group_edges
        ],
        "group_of": {str(sid): gid for sid, gid in solution.group_of.items()},
        "contradiction": None,
        "earliest_assignment": None,
        "latest_assignment": None,
    }
    if solution.contradiction is not None:
        payload["contradiction"] = serialize_contradiction(
            solution.contradiction, provenance
        )
    if solution.earliest_assignment is not None:
        payload["earliest_assignment"] = {
            str(gid): year for gid, year in solution.earliest_assignment.items()
        }
    if solution.latest_assignment is not None:
        payload["latest_assignment"] = {
            str(gid): year for gid, year in solution.latest_assignment.items()
        }
    return payload


def serialize_stratum(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "site_id": row["site_id"],
        "trench": row["trench"],
        "layer_no": row["layer_no"],
        "note": row["note"],
        "intervals": [
            {"year_from": lo, "year_to": hi}
            for lo, hi in db.get_intervals(conn, row["id"])
        ],
        "created": row["created"],
    }


def serialize_notebook(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    layers = [
        {"label": r["label"], "stratum_id": r["stratum_id"]}
        for r in db.list_notebook_layers(conn, row["id"])
    ]
    superpositions = [
        {
            "id": r["id"],
            "lower_label": r["lower_label"],
            "upper_label": r["upper_label"],
            "lower_id": r["lower_id"],
            "upper_id": r["upper_id"],
            "page": r["page"],
            "line": r["line"],
        }
        for r in db.list_notebook_superpositions(conn, row["id"])
    ]
    return {
        "id": row["id"],
        "site_id": row["site_id"],
        "notebook_no": row["notebook_no"],
        "created": row["created"],
        "updated": row["updated"],
        "layers": layers,
        "superpositions": superpositions,
    }


def serialize_effective_superpositions(
    records: SiteRecords, site_id: int, version: int
) -> dict[str, Any]:
    """生效的上下层对及全部出处（手工记录号、簿号、页行号）。"""
    provenance = records.provenance
    pairs = []
    for pair in sorted(provenance.sources_by_pair):
        sources = provenance.sources_by_pair[pair]
        lower = records.strata_by_id.get(pair[0])
        upper = records.strata_by_id.get(pair[1])
        pairs.append(
            {
                "lower_id": pair[0],
                "upper_id": pair[1],
                "lower": lower.code if lower else None,
                "upper": upper.code if upper else None,
                "source_count": len(sources),
                # 无论多少处出处，求解时都只作为一条叠压约束
                "counted_as": 1,
                "manual_ids": [
                    s.record_id for s in sources if s.source == "manual"
                ],
                "notebook_refs": [
                    {
                        "notebook_no": s.notebook_no,
                        "page": s.page,
                        "line": s.line,
                        "lower_label": s.lower_label,
                        "upper_label": s.upper_label,
                    }
                    for s in sources
                    if s.source == "notebook"
                ],
                "sources": [s.to_dict() for s in sources],
            }
        )
    return {
        "site_id": site_id,
        "version": version,
        "pair_count": len(pairs),
        "pairs": pairs,
    }
