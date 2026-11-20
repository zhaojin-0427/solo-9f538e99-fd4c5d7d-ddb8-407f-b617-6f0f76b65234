"""野外簿整本提交、出处去重与撤销的接口测试。"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "notebook.db"))
    with TestClient(app) as c:
        yield c


def _make_site(client, name="二里头遗址"):
    r = client.post("/sites", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["record"]


def _add_stratum(client, site_id, trench, layer_no, intervals):
    r = client.post(
        f"/sites/{site_id}/strata",
        json={
            "trench": trench,
            "layer_no": layer_no,
            "intervals": [{"year_from": lo, "year_to": hi} for lo, hi in intervals],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["record"]


def _submit(client, site_id, notebook_no, layers, superpositions, expected=201):
    r = client.put(
        f"/sites/{site_id}/notebooks",
        json={
            "notebook_no": notebook_no,
            "layers": layers,
            "superpositions": superpositions,
        },
    )
    assert r.status_code == expected, r.text
    return r.json()


def _sup(lower_label, upper_label, page, line):
    return {
        "lower_label": lower_label,
        "upper_label": upper_label,
        "page": page,
        "line": line,
    }


def _pair_count(solution):
    return len(solution["group_edges"])


# ---------- 基本提交与出处 ----------

def test_notebook_submit_resolves_labels_and_records_source(client):
    site = _make_site(client)
    sid = site["id"]
    lower = _add_stratum(client, sid, "T1", "L3", [(100, 200)])
    upper = _add_stratum(client, sid, "T1", "L2", [(150, 300)])

    body = _submit(
        client,
        sid,
        "NK-1",
        [
            {"label": "3", "stratum_id": lower["id"]},
            {"label": "2", "stratum_id": upper["id"]},
        ],
        [_sup("3", "2", 12, 4)],
    )
    assert body["replaced"] is False
    assert body["record"]["notebook_no"] == "NK-1"
    assert body["record"]["superpositions"][0]["lower_id"] == lower["id"]
    assert body["record"]["superpositions"][0]["upper_id"] == upper["id"]

    sources = client.get(f"/sites/{sid}/superposition-sources").json()
    assert sources["pair_count"] == 1
    pair = sources["pairs"][0]
    assert (pair["lower_id"], pair["upper_id"]) == (lower["id"], upper["id"])
    assert pair["counted_as"] == 1
    (src,) = pair["sources"]
    assert src["source"] == "notebook"
    assert src["notebook_no"] == "NK-1"
    assert (src["page"], src["line"]) == (12, 4)
    assert (src["lower_label"], src["upper_label"]) == ("3", "2")

    # 年代结论按叠压生效
    sol = client.get(f"/sites/{sid}/solution").json()
    assert sol["feasible"] is True
    assert sol["earliest_assignment"][str(lower["id"])] < sol[
        "earliest_assignment"
    ][str(upper["id"])]


def test_same_pair_recorded_in_two_notebooks_counts_once(client):
    site = _make_site(client)
    sid = site["id"]
    lower = _add_stratum(client, sid, "T1", "L3", [(1, 100)])
    upper = _add_stratum(client, sid, "T1", "L2", [(2, 200)])
    layers = [
        {"label": "A", "stratum_id": lower["id"]},
        {"label": "B", "stratum_id": upper["id"]},
    ]

    _submit(client, sid, "NK-1", layers, [_sup("A", "B", 1, 1)])
    _submit(client, sid, "NK-2", layers, [_sup("A", "B", 7, 9)])

    sources = client.get(f"/sites/{sid}/superposition-sources").json()
    assert sources["pair_count"] == 1  # 只算一条叠压
    pair = sources["pairs"][0]
    assert pair["counted_as"] == 1
    assert pair["source_count"] == 2
    assert {(s["notebook_no"], s["page"], s["line"]) for s in pair["sources"]} == {
        ("NK-1", 1, 1),
        ("NK-2", 7, 9),
    }
    # 求解结果仍只有一条组间边
    sol = client.get(f"/sites/{sid}/solution").json()
    assert len(sol["group_edges"]) == 1


def test_notebook_and_manual_same_pair_count_once(client):
    site = _make_site(client)
    sid = site["id"]
    lower = _add_stratum(client, sid, "T1", "L3", [(1, 100)])
    upper = _add_stratum(client, sid, "T1", "L2", [(2, 200)])
    r = client.post(
        f"/sites/{sid}/superpositions",
        json={"lower_id": lower["id"], "upper_id": upper["id"]},
    )
    assert r.status_code == 201, r.text

    _submit(
        client,
        sid,
        "NK-1",
        [
            {"label": "A", "stratum_id": lower["id"]},
            {"label": "B", "stratum_id": upper["id"]},
        ],
        [_sup("A", "B", 2, 3)],
    )
    pair = client.get(f"/sites/{sid}/superposition-sources").json()["pairs"][0]
    assert pair["counted_as"] == 1
    assert {s["source"] for s in pair["sources"]} == {"manual", "notebook"}
    assert client.get(f"/sites/{sid}/solution").json()["feasible"] is True


# ---------- 整本替换 ----------

def test_resubmit_replaces_whole_notebook_and_reinterprets(client):
    site = _make_site(client)
    sid = site["id"]
    a = _add_stratum(client, sid, "T1", "L3", [(1, 100)])
    b = _add_stratum(client, sid, "T1", "L2", [(2, 200)])
    c = _add_stratum(client, sid, "T1", "L1", [(3, 300)])

    _submit(
        client,
        sid,
        "NK-1",
        [
            {"label": "3", "stratum_id": a["id"]},
            {"label": "2", "stratum_id": b["id"]},
            {"label": "1", "stratum_id": c["id"]},
        ],
        [_sup("3", "2", 1, 1)],
    )
    assert len(client.get(f"/sites/{sid}/notebooks/NK-1").json()["layers"]) == 3

    # 再次提交：页行不变，但对照改为 3->c、2->a，叠压含义整本重解
    body = _submit(
        client,
        sid,
        "NK-1",
        [
            {"label": "3", "stratum_id": c["id"]},
            {"label": "2", "stratum_id": a["id"]},
            {"label": "1", "stratum_id": b["id"]},
        ],
        [_sup("3", "2", 1, 1)],
        expected=200,
    )
    assert body["replaced"] is True
    rec = body["record"]["superpositions"]
    assert len(rec) == 1
    assert (rec[0]["lower_id"], rec[0]["upper_id"]) == (c["id"], a["id"])
    assert len(body["record"]["layers"]) == 3
    pair = client.get(f"/sites/{sid}/superposition-sources").json()["pairs"][0]
    assert (pair["lower_id"], pair["upper_id"]) == (c["id"], a["id"])


def test_resubmit_only_rewrites_own_notebook_sources(client):
    site = _make_site(client)
    sid = site["id"]
    a = _add_stratum(client, sid, "T1", "L3", [(1, 100)])
    b = _add_stratum(client, sid, "T1", "L2", [(2, 200)])
    _submit(client, sid, "NK-9", [
        {"label": "x", "stratum_id": a["id"]},
        {"label": "y", "stratum_id": b["id"]},
    ], [_sup("x", "y", 3, 3)])

    # 另一簿提交完全不同的关系；替换 NK-9 不影响它
    _submit(client, sid, "NK-1", [
        {"label": "x", "stratum_id": b["id"]},
        {"label": "y", "stratum_id": a["id"]},
    ], [])
    _submit(client, sid, "NK-9", [
        {"label": "x", "stratum_id": a["id"]},
        {"label": "y", "stratum_id": b["id"]},
    ], [], expected=200)
    sources = client.get(f"/sites/{sid}/superposition-sources").json()
    assert sources["pair_count"] == 0


# ---------- 引用无效 ----------

def test_invalid_label_reports_page_line_and_writes_nothing(client):
    site = _make_site(client)
    sid = site["id"]
    a = _add_stratum(client, sid, "T1", "L1", [(1, 100)])
    r = client.put(
        f"/sites/{sid}/notebooks",
        json={
            "notebook_no": "NK-1",
            "layers": [{"label": "1", "stratum_id": a["id"]}],
            "superpositions": [_sup("1", "9", 4, 7)],
        },
    )
    assert r.status_code == 409
    body = r.json()
    assert body["notebook_no"] == "NK-1"
    (issue,) = body["issues"]
    assert (issue["page"], issue["line"]) == (4, 7)
    assert issue["kind"] == "REFERENCE"
    assert "9" in issue["message"]
    assert client.get(f"/sites/{sid}/notebooks").json() == []
    assert client.get(f"/sites/{sid}/superposition-sources").json()["pair_count"] == 0


def test_stratum_from_other_site_is_invalid(client):
    site = _make_site(client)
    other = _make_site(client, "另一个遗址")
    foreign = _add_stratum(client, other["id"], "T9", "L1", [(1, 10)])
    r = client.put(
        f"/sites/{site['id']}/notebooks",
        json={
            "notebook_no": "NK-1",
            "layers": [{"label": "1", "stratum_id": foreign["id"]}],
            "superpositions": [],
        },
    )
    assert r.status_code == 409
    assert "不属于" in r.json()["issues"][0]["message"]


# ---------- 无解 ----------

def test_infeasible_notebook_keeps_old_book_and_solution(client):
    site = _make_site(client)
    sid = site["id"]
    lower = _add_stratum(client, sid, "T1", "L3", [(100, 100)])
    upper = _add_stratum(client, sid, "T1", "L2", [(200, 200)])
    _submit(client, sid, "NK-1", [
        {"label": "3", "stratum_id": lower["id"]},
        {"label": "2", "stratum_id": upper["id"]},
    ], [_sup("3", "2", 1, 1)])
    before = client.get(f"/sites/{sid}/solution").json()

    # 替换为反向叠压 -> 与旧簿无关，但本簿自身次序与年代冲突
    r = client.put(
        f"/sites/{sid}/notebooks",
        json={
            "notebook_no": "NK-1",
            "layers": [
                {"label": "1", "stratum_id": upper["id"]},
                {"label": "2", "stratum_id": lower["id"]},
            ],
            "superpositions": [_sup("1", "2", 5, 2)],
        },
    )
    assert r.status_code == 409
    body = r.json()
    assert body["notebook_no"] == "NK-1"
    assert body["issues"][0]["page"] == 5
    assert body["issues"][0]["line"] == 2
    assert body["contradiction"]["reason"] in {"ORDER_CONFLICT", "GROUP_CYCLE"}
    assert body["previous_solution"]["feasible"] is True

    # 旧簿与旧结论保留
    nb = client.get(f"/sites/{sid}/notebooks/NK-1").json()
    assert nb["superpositions"][0]["page"] == 1
    assert nb["superpositions"][0]["lower_id"] == lower["id"]
    after = client.get(f"/sites/{sid}/solution").json()
    assert after == before


def test_notebook_conflict_with_manual_record_reports_book_page(client):
    site = _make_site(client)
    sid = site["id"]
    low = _add_stratum(client, sid, "T1", "L1", [(100, 100)])  # 只能 100 年
    high = _add_stratum(client, sid, "T2", "L1", [(50, 90)])  # 最晚 90 年
    # 手工登记 high -> low（可行：90 < 100），簿里写 low -> high（与手工+年代冲突）
    assert client.post(
        f"/sites/{sid}/superpositions",
        json={"lower_id": high["id"], "upper_id": low["id"]},
    ).status_code == 201

    r = client.put(
        f"/sites/{sid}/notebooks",
        json={
            "notebook_no": "NK-1",
            "layers": [
                {"label": "1", "stratum_id": low["id"]},
                {"label": "2", "stratum_id": high["id"]},
            ],
            "superpositions": [_sup("1", "2", 8, 3)],
        },
    )
    assert r.status_code == 409
    issues = r.json()["issues"]
    assert issues and issues[0]["page"] == 8 and issues[0]["line"] == 3
    # 手工记录仍在，叠压来源未变
    assert len(client.get(f"/sites/{sid}/superpositions").json()) == 1


# ---------- 撤销与出处移除 ----------

def test_removing_last_source_removes_superposition(client):
    site = _make_site(client)
    sid = site["id"]
    a = _add_stratum(client, sid, "T1", "L3", [(1, 100)])
    b = _add_stratum(client, sid, "T1", "L2", [(2, 200)])
    layers = [
        {"label": "A", "stratum_id": a["id"]},
        {"label": "B", "stratum_id": b["id"]},
    ]
    _submit(client, sid, "NK-1", layers, [_sup("A", "B", 1, 1)])
    _submit(client, sid, "NK-2", layers, [_sup("A", "B", 2, 2)])

    # 撤一本：另一本仍在，叠压仍生效
    assert client.delete(f"/sites/{sid}/notebooks/NK-1").status_code == 204
    sources = client.get(f"/sites/{sid}/superposition-sources").json()
    assert sources["pair_count"] == 1
    assert [s["notebook_no"] for s in sources["pairs"][0]["sources"]] == ["NK-2"]
    assert client.get(f"/sites/{sid}/solution").json()["feasible"] is True

    # 撤最后一处：叠压从求解中移除，两组不再有次序约束
    assert client.delete(f"/sites/{sid}/notebooks/NK-2").status_code == 204
    sources = client.get(f"/sites/{sid}/superposition-sources").json()
    assert sources["pair_count"] == 0
    sol = client.get(f"/sites/{sid}/solution").json()
    assert sol["group_edges"] == []


def test_manual_delete_does_not_touch_notebook_source(client):
    site = _make_site(client)
    sid = site["id"]
    a = _add_stratum(client, sid, "T1", "L3", [(1, 100)])
    b = _add_stratum(client, sid, "T1", "L2", [(2, 200)])
    manual = client.post(
        f"/sites/{sid}/superpositions",
        json={"lower_id": a["id"], "upper_id": b["id"]},
    ).json()["record"]
    _submit(client, sid, "NK-1", [
        {"label": "A", "stratum_id": a["id"]},
        {"label": "B", "stratum_id": b["id"]},
    ], [_sup("A", "B", 1, 1)])

    assert client.delete(f"/superpositions/{manual['id']}").status_code == 200
    sources = client.get(f"/sites/{sid}/superposition-sources").json()
    assert sources["pair_count"] == 1
    assert [s["source"] for s in sources["pairs"][0]["sources"]] == ["notebook"]


# ---------- 持久化 ----------
def test_notebook_sources_survive_restart(tmp_path):
    db_path = str(tmp_path / "restart.db")
    with TestClient(create_app(db_path=db_path)) as c1:
        site = _make_site(c1)
        sid = site["id"]
        a = _add_stratum(c1, sid, "T1", "L3", [(1, 100)])
        b = _add_stratum(c1, sid, "T1", "L2", [(2, 200)])
        _submit(c1, sid, "NK-1", [
            {"label": "A", "stratum_id": a["id"]},
            {"label": "B", "stratum_id": b["id"]},
        ], [_sup("A", "B", 3, 4)])
        before = c1.get(f"/sites/{sid}/superposition-sources").json()

    with TestClient(create_app(db_path=db_path)) as c2:
        after = c2.get(f"/sites/{site['id']}/superposition-sources").json()
        assert after == before
        sol = c2.get(f"/sites/{site['id']}/solution").json()
        assert sol["feasible"] is True
        assert len(sol["group_edges"]) == 1
