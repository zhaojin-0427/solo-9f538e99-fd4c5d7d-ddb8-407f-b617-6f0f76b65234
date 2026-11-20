"""FastAPI 应用工厂与路由。

并发语义：所有写操作在进程内同一把锁内、同一个 SQLite 事务中完成
“校验 -> 写入 -> 重算求解 -> 提交”，按提交顺序串行核算。若求解无解，
事务回滚，库内保留原有对接与年代结论（查询仍返回上一版可行结果）。
"""

from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse

from . import db
from .schemas import (
    CorrelationCreate,
    NotebookSubmit,
    SiteCreate,
    StratumCreate,
    SuperpositionCreate,
)
from .service import (
    load_and_solve,
    load_solve_bundle,
    serialize_contradiction,
    serialize_effective_superpositions,
    serialize_notebook,
    serialize_solution,
    serialize_stratum,
)

MAX_STRATA_PER_SITE = 40


class ConflictError(HTTPException):
    """语义冲突（409）：约束无解或违反业务规则。"""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        content: dict[str, Any] = {"detail": message}
        if details:
            content.update(details)
        super().__init__(status_code=409, detail=content)


class _Infeasible(Exception):
    """事务内部使用：新记录使整站约束无解，触发回滚。"""


class _NotebookRejected(Exception):
    """事务内部使用：整本野外簿提交与其余来源合并后无解，触发回滚。"""


def _default_db_path() -> str:
    configured = os.environ.get("DB_PATH")
    if configured:
        return configured
    # 容器内 /data 由卷挂载；本地裸跑时回退到工作目录下的 data/
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/archaeo.db"
    return os.path.join(os.getcwd(), "data", "archaeo.db")


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or _default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    conn = db.connect(db_path)
    db.init_db(conn)
    write_lock = threading.Lock()

    app = FastAPI(
        title="考古地层对接 API",
        version="1.1.0",
        description=(
            "录入同一遗址（最多 40 个层位）的探方、层位编号与可能沉积年代段"
            "（整年闭区间，可含空档），登记跨探方层位对接与下层→上层叠压关系；"
            "直接或间接对接的层位视为同一次沉积并共用一个年份，自动完成分组、"
            "成环检测与可行年代段计算。\n\n"
            "野外簿：`PUT /sites/{site_id}/notebooks` 按簿号整本提交簿内层号到"
            "现有层位 id 的对照与带页行号的上下层记录；同一簿号再次提交整本替换。"
            "同一对上下层记在多本簿或手工记录中时，求解只算一条叠压。"
            "生效的全部上下层对及出处见 "
            "`GET /sites/{site_id}/superposition-sources`，收录于 "
            "`GET /sites/{site_id}/docs/superpositions`。"
        ),
    )
    app.state.conn = conn
    app.state.write_lock = write_lock

    # ---------- 内部辅助 ----------

    def _require_site(site_id: int):
        site = db.get_site(conn, site_id)
        if site is None:
            raise HTTPException(404, f"遗址 {site_id} 不存在")
        return site

    def _require_stratum(stratum_id: int, site_id: int):
        row = db.get_stratum(conn, stratum_id)
        if row is None or row["site_id"] != site_id:
            raise HTTPException(
                404, f"层位 {stratum_id} 不属于遗址 {site_id} 或不存在"
            )
        return row

    def _solution_payload(site_id: int) -> dict[str, Any]:
        bundle = load_solve_bundle(conn, site_id)
        return serialize_solution(
            bundle.solution, db.get_version(conn), bundle.records.provenance
        )

    def _contradiction_payload(site_id: int) -> dict[str, Any]:
        """读取当前连接视图（未提交的新记录）中的矛盾见证。"""
        bundle = load_solve_bundle(conn, site_id)
        if bundle.solution.contradiction is None:  # 理论上不会发生
            return {"reason": "UNKNOWN", "message": "约束不成立", "witness": []}
        return serialize_contradiction(
            bundle.solution.contradiction, bundle.records.provenance
        )

    def _current_provenance(site_id: int):
        return load_solve_bundle(conn, site_id).records.provenance

    def _reject_infeasible(site_id: int, message: str) -> None:
        """事务内调用：先抓取新视图中的矛盾，再抛异常触发事务回滚。"""
        contradiction = _contradiction_payload(site_id)
        raise _Infeasible((message, contradiction))
    # ---------- 遗址 ----------

    @app.post("/sites", status_code=201, tags=["sites"])
    def create_site(body: SiteCreate):
        with write_lock:
            if db.get_site_by_name(conn, body.name) is not None:
                raise ConflictError(f"遗址名称 {body.name!r} 已存在")
            with db.transaction(conn):
                site = db.create_site(conn, body.name)
                db.bump_version(conn)
            return {"record": dict(site), "solution": _solution_payload(site["id"])}

    @app.get("/sites", tags=["sites"])
    def list_sites():
        return [dict(r) for r in db.list_sites(conn)]

    @app.get("/sites/{site_id}", tags=["sites"])
    def get_site(site_id: int):
        return dict(_require_site(site_id))

    # ---------- 层位 ----------

    @app.post("/sites/{site_id}/strata", status_code=201, tags=["strata"])
    def create_stratum(site_id: int, body: StratumCreate):
        _require_site(site_id)
        intervals = [(iv.year_from, iv.year_to) for iv in body.intervals]
        with write_lock:
            if db.count_strata(conn, site_id) >= MAX_STRATA_PER_SITE:
                raise ConflictError(
                    f"同一遗址最多 {MAX_STRATA_PER_SITE} 个层位，已达上限"
                )
            if db.find_stratum(conn, site_id, body.trench, body.layer_no) is not None:
                raise ConflictError(
                    f"探方 {body.trench} 的层位 {body.layer_no} 已存在"
                )
            try:
                with db.transaction(conn):
                    row = db.create_stratum(
                        conn, site_id, body.trench, body.layer_no, body.note, intervals
                    )
                    db.bump_version(conn)
                    if not load_and_solve(conn, site_id).feasible:
                        _reject_infeasible(
                            site_id, "新层位的年代段与现有约束无共同解，已拒绝录入"
                        )
            except _Infeasible as exc:
                message, contradiction = exc.args[0]
                raise ConflictError(
                    message,
                    {
                        "contradiction": contradiction,
                        "previous_solution": _solution_payload(site_id),
                    },
                )
            return {
                "record": serialize_stratum(conn, row),
                "solution": _solution_payload(site_id),
            }

    @app.get("/sites/{site_id}/strata", tags=["strata"])
    def list_strata(site_id: int):
        _require_site(site_id)
        return [serialize_stratum(conn, r) for r in db.list_strata(conn, site_id)]

    @app.get("/strata/{stratum_id}", tags=["strata"])
    def get_stratum(stratum_id: int):
        row = db.get_stratum(conn, stratum_id)
        if row is None:
            raise HTTPException(404, f"层位 {stratum_id} 不存在")
        return serialize_stratum(conn, row)

    # ---------- 对接 ----------

    @app.post("/sites/{site_id}/correlations", status_code=201, tags=["correlations"])
    def create_correlation(site_id: int, body: CorrelationCreate):
        _require_site(site_id)
        if body.stratum_a_id == body.stratum_b_id:
            raise ConflictError("对接的两个层位不能相同")
        with write_lock:
            row_a = _require_stratum(body.stratum_a_id, site_id)
            row_b = _require_stratum(body.stratum_b_id, site_id)
            if row_a["trench"] == row_b["trench"]:
                raise ConflictError(
                    "对接只能登记不同探方的层位（同一探方层位编号天然分层）"
                )
            if db.find_correlation(
                conn, body.stratum_a_id, body.stratum_b_id
            ) is not None:
                raise ConflictError("这两个层位之间的对接已存在")
            try:
                with db.transaction(conn):
                    row = db.create_correlation(
                        conn, site_id, body.stratum_a_id, body.stratum_b_id
                    )
                    db.bump_version(conn)
                    if not load_and_solve(conn, site_id).feasible:
                        _reject_infeasible(
                            site_id, "该对接会使约束无解（无法共用年份或叠压成环），已拒绝"
                        )
            except _Infeasible as exc:
                message, contradiction = exc.args[0]
                raise ConflictError(
                    message,
                    {
                        "contradiction": contradiction,
                        "previous_solution": _solution_payload(site_id),
                    },
                )
            return {"record": dict(row), "solution": _solution_payload(site_id)}

    @app.get("/sites/{site_id}/correlations", tags=["correlations"])
    def list_correlations(site_id: int):
        _require_site(site_id)
        return [dict(r) for r in db.list_correlations(conn, site_id)]

    @app.delete("/correlations/{correlation_id}", tags=["correlations"])
    def delete_correlation(correlation_id: int):
        with write_lock:
            row = db.get_correlation(conn, correlation_id)
            if row is None:
                raise HTTPException(404, f"对接 {correlation_id} 不存在")
            site_id = row["site_id"]
            with db.transaction(conn):
                db.delete_correlation(conn, correlation_id)
                db.bump_version(conn)
            # 撤销只会放松约束，按剩余对接重算必然可行
            return {"deleted": correlation_id, "solution": _solution_payload(site_id)}

    # ---------- 叠压 ----------

    @app.post("/sites/{site_id}/superpositions", status_code=201, tags=["superpositions"])
    def create_superposition(site_id: int, body: SuperpositionCreate):
        _require_site(site_id)
        if body.lower_id == body.upper_id:
            raise ConflictError("下层与上层不能是同一个层位")
        with write_lock:
            _require_stratum(body.lower_id, site_id)
            _require_stratum(body.upper_id, site_id)
            if db.find_superposition(conn, body.lower_id, body.upper_id) is not None:
                raise ConflictError("该叠压关系已存在")
            try:
                with db.transaction(conn):
                    row = db.create_superposition(
                        conn, site_id, body.lower_id, body.upper_id
                    )
                    db.bump_version(conn)
                    if not load_and_solve(conn, site_id).feasible:
                        _reject_infeasible(
                            site_id, "该叠压关系无解（成环或下层沉积年晚于上层），已拒绝"
                        )
            except _Infeasible as exc:
                message, contradiction = exc.args[0]
                raise ConflictError(
                    message,
                    {
                        "contradiction": contradiction,
                        "previous_solution": _solution_payload(site_id),
                    },
                )
            return {"record": dict(row), "solution": _solution_payload(site_id)}

    @app.get("/sites/{site_id}/superpositions", tags=["superpositions"])
    def list_superpositions(site_id: int):
        _require_site(site_id)
        return [dict(r) for r in db.list_superpositions(conn, site_id)]

    @app.delete("/superpositions/{superposition_id}", tags=["superpositions"])
    def delete_superposition(superposition_id: int):
        with write_lock:
            row = db.get_superposition(conn, superposition_id)
            if row is None:
                raise HTTPException(404, f"叠压关系 {superposition_id} 不存在")
            site_id = row["site_id"]
            with db.transaction(conn):
                db.delete_superposition(conn, superposition_id)
                db.bump_version(conn)
            return {"deleted": superposition_id, "solution": _solution_payload(site_id)}

    # ---------- 野外簿 ----------

    def _notebook_issue(
        notebook_no: str,
        page: int | None,
        line: int | None,
        lower_label: str | None,
        upper_label: str | None,
        message: str,
    ) -> dict[str, Any]:
        location = ""
        if page is not None and line is not None:
            location = f" 第{page}页第{line}行"
        elif page is not None:
            location = f" 第{page}页"
        return {
            "kind": "REFERENCE",
            "notebook_no": notebook_no,
            "page": page,
            "line": line,
            "lower_label": lower_label,
            "upper_label": upper_label,
            "message": f"野外簿 {notebook_no}{location}：{message}",
        }

    def _resolve_notebook(
        site_id: int, body: NotebookSubmit
    ) -> tuple[dict[str, int], list[dict[str, Any]]]:
        """校验并解析整本提交；不做任何写入。

        返回 (簿内层号 -> 层位 id, 已解析的记录列表)。任何页行引用无效
        都抛出 ConflictError，并一次列出全部问题页行。
        """
        issues: list[dict[str, Any]] = []
        labels: dict[str, int] = {}
        seen_labels: set[str] = set()
        for entry in body.layers:
            if entry.label in seen_labels:
                issues.append(
                    _notebook_issue(
                        body.notebook_no,
                        None,
                        None,
                        None,
                        None,
                        f"簿内层号 {entry.label!r} 重复对照",
                    )
                )
                continue
            seen_labels.add(entry.label)
            stratum = db.get_stratum(conn, entry.stratum_id)
            if stratum is None or stratum["site_id"] != site_id:
                issues.append(
                    _notebook_issue(
                        body.notebook_no,
                        None,
                        None,
                        entry.label,
                        None,
                        f"层号 {entry.label!r} 对照的层位 {entry.stratum_id} "
                        f"不属于遗址 {site_id} 或不存在",
                    )
                )
                continue
            labels[entry.label] = entry.stratum_id

        resolved: list[dict[str, Any]] = []
        for record in body.superpositions:
            missing = [
                label
                for label in (record.lower_label, record.upper_label)
                if label not in labels
            ]
            if missing:
                issues.append(
                    _notebook_issue(
                        body.notebook_no,
                        record.page,
                        record.line,
                        record.lower_label,
                        record.upper_label,
                        "引用的簿内层号 "
                        + "、".join(repr(m) for m in missing)
                        + " 未在本簿对照中定义，或对照的层位无效",
                    )
                )
                continue
            lower_id = labels[record.lower_label]
            upper_id = labels[record.upper_label]
            if lower_id == upper_id:
                issues.append(
                    _notebook_issue(
                        body.notebook_no,
                        record.page,
                        record.line,
                        record.lower_label,
                        record.upper_label,
                        "下层与上层经对照指向同一个层位",
                    )
                )
                continue
            resolved.append(
                {
                    "lower_label": record.lower_label,
                    "upper_label": record.upper_label,
                    "lower_id": lower_id,
                    "upper_id": upper_id,
                    "page": record.page,
                    "line": record.line,
                }
            )

        if issues:
            raise ConflictError(
                f"野外簿 {body.notebook_no} 有 {len(issues)} 处引用问题，整本未写入",
                {
                    "notebook_no": body.notebook_no,
                    "issues": issues,
                    "previous_solution": _solution_payload(site_id),
                },
            )
        return labels, resolved

    @app.put("/sites/{site_id}/notebooks", status_code=201, tags=["notebooks"])
    def submit_notebook(site_id: int, body: NotebookSubmit, response: Response):
        """按簿号整本提交（不存在则新建）。同一簿号再次提交即整本替换。"""
        _require_site(site_id)
        with write_lock:
            existing = db.get_notebook(conn, site_id, body.notebook_no)
            is_replace = existing is not None
            if is_replace:
                response.status_code = 200

            labels, resolved = _resolve_notebook(site_id, body)
            replaced = is_replace
            try:
                with db.transaction(conn):
                    notebook = db.upsert_notebook(conn, site_id, body.notebook_no)
                    for label, stratum_id in labels.items():
                        db.add_notebook_layer(
                            conn, notebook["id"], label, stratum_id
                        )
                    for record in resolved:
                        db.add_notebook_superposition(
                            conn,
                            notebook["id"],
                            record["lower_label"],
                            record["upper_label"],
                            record["lower_id"],
                            record["upper_id"],
                            record["page"],
                            record["line"],
                        )
                    db.bump_version(conn)
                    solution = load_and_solve(conn, site_id)
                    if not solution.feasible:
                        contradiction = _contradiction_payload(site_id)
                        provenance = _current_provenance(site_id)
                        issues = provenance.notebook_issues(
                            body.notebook_no,
                            solution.contradiction,
                        )
                        if not issues:
                            # 防御性兜底：见证未点名本簿时，仍逐条列出本簿页行
                            issues = [
                                _notebook_issue(
                                    body.notebook_no,
                                    r["page"],
                                    r["line"],
                                    r["lower_label"],
                                    r["upper_label"],
                                    "与其余来源合并后使约束无解",
                                )
                                for r in resolved
                            ]
                        raise _NotebookRejected(
                            (
                                f"野外簿 {body.notebook_no} 与其余来源合并后无解，"
                                "本次提交未生效，旧簿与年代结论保留",
                                contradiction,
                                issues,
                            )
                        )
            except _NotebookRejected as exc:
                message, contradiction, issues = exc.args[0]
                raise ConflictError(
                    message,
                    {
                        "notebook_no": body.notebook_no,
                        "issues": issues,
                        "contradiction": contradiction,
                        "previous_solution": _solution_payload(site_id),
                    },
                )
            return {
                "record": serialize_notebook(conn, notebook),
                "replaced": replaced,
                "solution": _solution_payload(site_id),
            }

    @app.get("/sites/{site_id}/notebooks", tags=["notebooks"])
    def list_notebooks(site_id: int):
        _require_site(site_id)
        return [
            serialize_notebook(conn, r) for r in db.list_notebooks(conn, site_id)
        ]

    @app.get("/sites/{site_id}/notebooks/{notebook_no}", tags=["notebooks"])
    def get_notebook(site_id: int, notebook_no: str):
        _require_site(site_id)
        row = db.get_notebook(conn, site_id, notebook_no)
        if row is None:
            raise HTTPException(
                404, f"遗址 {site_id} 下不存在野外簿 {notebook_no!r}"
            )
        return serialize_notebook(conn, row)

    @app.delete(
        "/sites/{site_id}/notebooks/{notebook_no}", status_code=204, tags=["notebooks"]
    )
    def delete_notebook(site_id: int, notebook_no: str):
        """撤去整本出处：仅本簿的叠压来源消失，其余出处与手工记录不受影响。"""
        _require_site(site_id)
        with write_lock:
            row = db.get_notebook(conn, site_id, notebook_no)
            if row is None:
                raise HTTPException(
                    404, f"遗址 {site_id} 下不存在野外簿 {notebook_no!r}"
                )
            with db.transaction(conn):
                db.delete_notebook(conn, row["id"])
                db.bump_version(conn)
            return Response(status_code=204)

    # ---------- 查询 ----------

    @app.get("/sites/{site_id}/solution", tags=["query"])
    def get_solution(site_id: int):
        """整站分组、组间叠压边与每组可同时成立的全部年代段。"""
        _require_site(site_id)
        with write_lock:  # 读也排队在写之后，保证读到的是完整已提交状态
            return _solution_payload(site_id)

    @app.get("/sites/{site_id}/superposition-sources", tags=["query"])
    def get_superposition_sources(site_id: int):
        """生效的全部上下层对及每一处的出处（簿号、页行号、手工记录号）。"""
        _require_site(site_id)
        with write_lock:
            bundle = load_solve_bundle(conn, site_id)
            version = db.get_version(conn)
            return serialize_effective_superpositions(
                bundle.records, site_id, version
            )

    @app.get("/sites/{site_id}/docs/superpositions", tags=["query"])
    def get_docs_superpositions(site_id: int):
        """`/docs` 页面上的同一份出处查询：生效上下层对及全部出处。"""
        return get_superposition_sources(site_id)

    @app.get("/strata/{stratum_id}/correlation-view", tags=["query"])
    def get_stratum_correlation_view(stratum_id: int):
        """按原始层位查询：同组全部成员 + 该组可与其他层位同时成立的全部年代段。"""
        row = db.get_stratum(conn, stratum_id)
        if row is None:
            raise HTTPException(404, f"层位 {stratum_id} 不存在")
        site_id = row["site_id"]
        with write_lock:
            bundle = load_solve_bundle(conn, site_id)
            version = db.get_version(conn)
        solution = bundle.solution
        gid = solution.group_of[stratum_id]
        group = next(g for g in solution.groups if g.group_id == gid)
        members = [
            serialize_stratum(conn, db.get_stratum(conn, sid))
            for sid in group.member_ids
        ]
        return {
            "stratum_id": stratum_id,
            "group_id": gid,
            "version": version,
            "feasible": solution.feasible,
            "members": members,
            "group_domain": [[lo, hi] for lo, hi in group.domain],
            "feasible_years": [[lo, hi] for lo, hi in group.feasible_years],
            "earliest_year": group.earliest_year,
            "latest_year": group.latest_year,
            "contradiction": serialize_solution(
                solution, version, bundle.records.provenance
            )["contradiction"],
        }

    @app.exception_handler(ConflictError)
    def _conflict_handler(_request, exc: ConflictError):
        return JSONResponse(status_code=409, content=exc.detail)

    return app


app = create_app()
