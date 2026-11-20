"""FastAPI 接口集成测试（真实 SQLite 文件 + TestClient）。"""

import concurrent.futures

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _make_site(client, name="仰韶村遗址"):
    r = client.post("/sites", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["record"]


def _add_stratum(client, site_id, trench, layer_no, intervals, note=None):
    r = client.post(
        f"/sites/{site_id}/strata",
        json={
            "trench": trench,
            "layer_no": layer_no,
            "intervals": [
                {"year_from": lo, "year_to": hi} for lo, hi in intervals
            ],
            "note": note,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["record"]


def test_openapi_docs_available(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_full_happy_path_grouping_and_query(client):
    site = _make_site(client)
    sid = site["id"]

    s1 = _add_stratum(client, sid, "T1", "L3", [(-5000, -4000), (-3000, -2000)])
    s2 = _add_stratum(client, sid, "T1", "L2", [(-4500, -2500)])
    s3 = _add_stratum(client, sid, "T2", "L3", [(-4800, -2200)])
    s4 = _add_stratum(client, sid, "T2", "L2", [(-4000, -2600)])

    # 叠压：T1 L3 在下、L2 在上
    r = client.post(
        f"/sites/{sid}/superpositions",
        json={"lower_id": s1["id"], "upper_id": s2["id"]},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        f"/sites/{sid}/superpositions",
        json={"lower_id": s3["id"], "upper_id": s4["id"]},
    )
    assert r.status_code == 201, r.text

    # 跨探方对接
    r = client.post(
        f"/sites/{sid}/correlations",
        json={"stratum_a_id": s1["id"], "stratum_b_id": s3["id"]},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        f"/sites/{sid}/correlations",
        json={"stratum_a_id": s2["id"], "stratum_b_id": s4["id"]},
    )
    assert r.status_code == 201, r.text

    sol = client.get(f"/sites/{sid}/solution").json()
    assert sol["feasible"] is True
    assert len(sol["groups"]) == 2
    g_lower = next(g for g in sol["groups"] if s1["id"] in g["member_ids"])
    g_upper = next(g for g in sol["groups"] if s2["id"] in g["member_ids"])
    assert s3["id"] in g_lower["member_ids"]
    assert s4["id"] in g_upper["member_ids"]
    # 可行年份区间保留空档（第二段被叠压约束削尾）
    assert g_lower["feasible_years"] == [[-4800, -4000], [-3000, -2601]]
    assert g_upper["feasible_years"] == [[-4000, -2600]]
    # 最早/最晚解都满足下层严格早于上层
    assert sol["earliest_assignment"][str(g_lower["group_id"])] < \
        sol["earliest_assignment"][str(g_upper["group_id"])]
    assert sol["latest_assignment"][str(g_lower["group_id"])] < \
        sol["latest_assignment"][str(g_upper["group_id"])]
    # 组域带空档
    assert any(lo <= -3000 <= hi for lo, hi in g_lower["domain"])

    # 按原始层位查询同组成员与可行年代段
    view = client.get(f"/strata/{s3['id']}/correlation-view").json()
    assert {m["id"] for m in view["members"]} == {s1["id"], s3["id"]}
    assert view["feasible_years"] == g_lower["feasible_years"]
    assert view["version"] == sol["version"]


def test_infeasible_correlation_keeps_previous_state(client):
    site = _make_site(client)
    sid = site["id"]
    s1 = _add_stratum(client, sid, "T1", "L1", [(100, 200)])
    s2 = _add_stratum(client, sid, "T2", "L1", [(300, 400)])  # 与 s1 无共同年份

    r = client.post(
        f"/sites/{sid}/correlations",
        json={"stratum_a_id": s1["id"], "stratum_b_id": s2["id"]},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["contradiction"]["reason"] == "EMPTY_DOMAIN"
    # 矛盾见证列出了两条原始层位记录
    witness_ids = {w["record_id"] for w in body["contradiction"]["witness"]}
    assert witness_ids == {s1["id"], s2["id"]}
    # 旧结论保留：仍为两个独立组，且可行
    previous = body["previous_solution"]
    assert previous["feasible"] is True
    assert len(previous["groups"]) == 2

    # 库里确实没有这条对接
    corr = client.get(f"/sites/{sid}/correlations").json()
    assert corr == []


def test_infeasible_superposition_returns_witness_and_keeps_data(client):
    site = _make_site(client)
    sid = site["id"]
    low = _add_stratum(client, sid, "T1", "L1", [(100, 100)])  # 只能是 100 年
    high = _add_stratum(client, sid, "T2", "L1", [(50, 90)])   # 最晚 90 年

    r = client.post(
        f"/sites/{sid}/superpositions",
        json={"lower_id": low["id"], "upper_id": high["id"]},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["contradiction"]["reason"] == "ORDER_CONFLICT"
    edge_witness = [
        w for w in body["contradiction"]["witness"] if w["kind"] == "superposition"
    ]
    assert edge_witness and edge_witness[0]["detail"]
    assert body["previous_solution"]["feasible"] is True
    assert client.get(f"/sites/{sid}/superpositions").json() == []


def test_cycle_after_correlation_rejected(client):
    site = _make_site(client)
    sid = site["id"]
    a = _add_stratum(client, sid, "T1", "L1", [(1, 100)])
    b = _add_stratum(client, sid, "T1", "L2", [(1, 100)])
    c = _add_stratum(client, sid, "T2", "L1", [(1, 100)])

    assert client.post(
        f"/sites/{sid}/superpositions",
        json={"lower_id": a["id"], "upper_id": b["id"]},
    ).status_code == 201
    # 对接 a-c 后再压 b->c：组{a,c} -> b -> 组{a,c}，成环
    assert client.post(
        f"/sites/{sid}/correlations",
        json={"stratum_a_id": a["id"], "stratum_b_id": c["id"]},
    ).status_code == 201
    r = client.post(
        f"/sites/{sid}/superpositions",
        json={"lower_id": b["id"], "upper_id": c["id"]},
    )
    assert r.status_code == 409
    assert r.json()["contradiction"]["reason"] == "GROUP_CYCLE"
    # 已有的对接保留
    assert len(client.get(f"/sites/{sid}/correlations").json()) == 1


def test_undo_correlation_recomputes(client):
    site = _make_site(client)
    sid = site["id"]
    a = _add_stratum(client, sid, "T1", "L1", [(1, 100)])
    b = _add_stratum(client, sid, "T2", "L1", [(1, 100)])
    c = _add_stratum(client, sid, "T3", "L1", [(1, 100)])

    r1 = client.post(
        f"/sites/{sid}/correlations",
        json={"stratum_a_id": a["id"], "stratum_b_id": b["id"]},
    ).json()
    r2 = client.post(
        f"/sites/{sid}/correlations",
        json={"stratum_a_id": b["id"], "stratum_b_id": c["id"]},
    ).json()
    assert len(client.get(f"/sites/{sid}/solution").json()["groups"]) == 1

    # 撤销 a-b，按剩余对接重算：{b,c} 与 {a}
    r = client.delete(f"/correlations/{r1['record']['id']}")
    assert r.status_code == 200
    sol = r.json()["solution"]
    assert len(sol["groups"]) == 2
    assert sol["feasible"] is True
    # 另一条对接还在
    assert len(client.get(f"/sites/{sid}/correlations").json()) == 1
    assert r2["record"]["id"] == client.get(f"/sites/{sid}/correlations").json()[0]["id"]


def test_max_40_strata_enforced(client):
    site = _make_site(client)
    sid = site["id"]
    for i in range(40):
        _add_stratum(client, sid, f"T{i}", "L1", [(1, 100)])
    r = client.post(
        f"/sites/{sid}/strata",
        json={
            "trench": "T40",
            "layer_no": "L1",
            "intervals": [{"year_from": 1, "year_to": 100}],
        },
    )
    assert r.status_code == 409
    assert "40" in r.json()["detail"]


def test_same_trench_correlation_rejected_and_duplicates(client):
    site = _make_site(client)
    sid = site["id"]
    a = _add_stratum(client, sid, "T1", "L1", [(1, 100)])
    b = _add_stratum(client, sid, "T1", "L2", [(1, 100)])
    c = _add_stratum(client, sid, "T2", "L1", [(1, 100)])

    r = client.post(
        f"/sites/{sid}/correlations",
        json={"stratum_a_id": a["id"], "stratum_b_id": b["id"]},
    )
    assert r.status_code == 409

    ok = client.post(
        f"/sites/{sid}/correlations",
        json={"stratum_a_id": a["id"], "stratum_b_id": c["id"]},
    )
    assert ok.status_code == 201
    dup = client.post(
        f"/sites/{sid}/correlations",
        json={"stratum_a_id": c["id"], "stratum_b_id": a["id"]},  # 反向重复
    )
    assert dup.status_code == 409


def test_validation_errors(client):
    _make_site(client)
    # 反向区间
    r = client.post(
        "/sites/1/strata",
        json={
            "trench": "T1",
            "layer_no": "L1",
            "intervals": [{"year_from": 100, "year_to": 50}],
        },
    )
    assert r.status_code == 422
    # 缺少年代段
    r = client.post(
        "/sites/1/strata",
        json={"trench": "T1", "layer_no": "L2", "intervals": []},
    )
    assert r.status_code == 422


def test_concurrent_writes_serialized_and_all_versions_committed(client):
    site = _make_site(client)
    sid = site["id"]

    def add(i):
        return client.post(
            f"/sites/{sid}/strata",
            json={
                "trench": f"T{i}",
                "layer_no": "L1",
                "intervals": [{"year_from": 1, "year_to": 1000}],
            },
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(add, range(20)))
    assert all(r.status_code == 201 for r in results), [r.text for r in results if r.status_code != 201]
    sol = client.get(f"/sites/{sid}/solution").json()
    total_version = sol["version"]
    # 1 次建站 + 20 次层位
    assert total_version == 21
    assert len(sol["groups"]) == 20


def test_persistence_across_app_restart(tmp_path):
    db_path = str(tmp_path / "persist.db")
    with TestClient(create_app(db_path=db_path)) as c1:
        site = _make_site(c1)
        s = _add_stratum(c1, site["id"], "T1", "L1", [(100, 200)])
        version_before = c1.get(f"/sites/{site['id']}/solution").json()["version"]

    # 重启：同一数据库文件，分组与年代结论必须原样恢复
    with TestClient(create_app(db_path=db_path)) as c2:
        sol = c2.get(f"/sites/{site['id']}/solution").json()
        assert sol["feasible"] is True
        assert sol["version"] == version_before
        view = c2.get(f"/strata/{s['id']}/correlation-view").json()
        assert view["members"][0]["trench"] == "T1"
        assert view["feasible_years"] == [[100, 200]]
