"""把 SQLite 原始记录装配为求解器输入，并负责结果序列化。"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import db
from .solver import (
    CorrelationInput,
    Solution,
    StratumInput,
    SuperpositionInput,
    normalize_intervals,
    solve,
)


def load_and_solve(conn: sqlite3.Connection, site_id: int) -> Solution:
    strata_rows = db.list_strata(conn, site_id)
    strata: list[StratumInput] = []
    for row in strata_rows:
        intervals = normalize_intervals(db.get_intervals(conn, row["id"]))
        strata.append(
            StratumInput(
                id=row["id"],
                code=f"{row['trench']}@{row['layer_no']}",
                intervals=intervals,
            )
        )

    correlations = [
        CorrelationInput(
            id=r["id"], a_id=r["stratum_a_id"], b_id=r["stratum_b_id"]
        )
        for r in db.list_correlations(conn, site_id)
    ]
    superpositions = [
        SuperpositionInput(id=r["id"], lower_id=r["lower_id"], upper_id=r["upper_id"])
        for r in db.list_superpositions(conn, site_id)
    ]
    return solve(strata, correlations, superpositions)


def serialize_solution(solution: Solution, version: int) -> dict[str, Any]:
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
            }
            for e in solution.group_edges
        ],
        "group_of": {str(sid): gid for sid, gid in solution.group_of.items()},
        "contradiction": None,
        "earliest_assignment": None,
        "latest_assignment": None,
    }
    if solution.contradiction is not None:
        payload["contradiction"] = {
            "reason": solution.contradiction.reason,
            "message": solution.contradiction.message,
            "witness": [
                {"kind": w.kind, "record_id": w.record_id, "detail": w.detail}
                for w in solution.contradiction.witness
            ],
        }
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
