"""M8 替测 API 端到端：候选核对、批准、幂等重跑、失败回滚、撤销与缺口页渲染。"""

from __future__ import annotations

import csv
import io


def _import_substitute(api):
    response = api.post(
        "/api/samples/import",
        json={"sample": "substitute_humidity", "offsets": "device_offsets"},
    )
    assert response.status_code == 200
    return response.json()


def test_candidate_and_approve_flow(api):
    """核对通过后批准，网格、窗口、导出都带原测点身份与逐桶来源。"""
    payload = _import_substitute(api)
    assert payload["run_id"]

    candidate = api.get(
        "/api/substitute/candidates",
        params={"sensor_id": "TH-01", "donor_sensor_id": "TH-02"},
    ).json()
    assert candidate["eligible"] is True
    assert candidate["gap_buckets"] == [
        "2026-03-01T00:04:00Z",
        "2026-03-01T00:05:00Z",
        "2026-03-01T00:06:00Z",
        "2026-03-01T00:07:00Z",
    ]

    approve = api.post(
        "/api/substitute/approve",
        json={"sensor_id": "TH-01", "donor_sensor_id": "TH-02"},
    )
    assert approve.status_code == 200
    body = approve.json()
    assert body["applied"] == 4
    assert body["reused"] is False
    decision_id = body["decision_id"]

    # 同一决定重跑不新增结果。
    rerun = api.post(
        "/api/substitute/approve",
        json={"sensor_id": "TH-01", "donor_sensor_id": "TH-02"},
    ).json()
    assert rerun["reused"] is True
    assert rerun["decision_id"] == decision_id

    decisions = api.get("/api/substitute/decisions").json()["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["status"] == "APPROVED"

    grid_csv = api.get("/api/export/grid.csv").content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(grid_csv)))
    sub_rows = [row for row in rows if row["state"] == "SUBSTITUTED"]
    assert len(sub_rows) == 4
    assert all(row["sensor_id"] == "TH-01" for row in sub_rows)
    assert all(row["donor_sensor_id"] == "TH-02" for row in sub_rows)
    assert {row["donor_source_row"] for row in sub_rows} == {"9", "10", "11", "12"}

    evidence = api.get(
        "/api/export/evidence",
        params={"sensor_id": "TH-01", "bucket": "2026-03-01T00:04:00Z"},
    ).json()
    assert evidence["substitution"]["decision_id"] == decision_id

    # 缺口页渲染替测区块与决定号。
    page = api.get("/gaps").text
    assert "替测核对" in page
    assert decision_id in page
    assert "SUBSTITUTED_NEIGHBOR" in page

    # 网格页出现替测状态。
    grid_page = api.get("/grid").text
    assert "SUBSTITUTED" in grid_page

    revoke = api.post("/api/substitute/revoke", json={"decision_id": decision_id})
    assert revoke.status_code == 200
    assert revoke.json()["restored_gaps"] == 4
    again = api.get("/api/substitute/decisions").json()["decisions"]
    assert again[0]["status"] == "REVOKED"

    # 撤销后缺口还原，替测表单重新可用；决定头仍留审计。
    gap_sensors = api.get("/api/substitute/gap-sensors").json()["sensors"]
    assert [item["sensor_id"] for item in gap_sensors] == ["TH-01"]
    assert gap_sensors[0]["gap_buckets"] == 4
    assert "当前没有待替测的缺口桶" not in api.get("/gaps").text


def test_approve_rejects_ineligible_donor(api):
    """量程不一致的邻表批准给 400，缺口原样保留。"""
    payload = _import_substitute(api)
    response = api.post(
        "/api/substitute/approve",
        json={"sensor_id": "TH-01", "donor_sensor_id": "PM-02"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "SUBSTITUTE_RANGE_MISMATCH"
    run = api.get("/api/batches").json()
    # 最新批次计数未被失败请求改写。
    latest = run["runs"][0]
    assert latest["run_id"] == payload["run_id"]
    assert latest["gaps"] == 4


def test_approve_rejects_non_gap_bucket_and_keeps_gaps(api):
    """把实采桶当缺口替测给 409/400，且没有任何替测留痕落库。"""
    _import_substitute(api)
    response = api.post(
        "/api/substitute/approve",
        json={
            "sensor_id": "TH-01",
            "donor_sensor_id": "TH-02",
            "buckets": ["2026-03-01T00:00:00Z"],
        },
    )
    assert response.status_code in (400, 409)
    assert response.json()["error"] == "SUBSTITUTE_NOT_GAP"
    assert api.get("/api/substitute/decisions").json()["decisions"] == []


def test_gap_sensors_and_candidate_listing(api):
    """缺口传感器清单与候选枚举只返回有缺口的原测点。"""
    _import_substitute(api)
    sensors = api.get("/api/substitute/gap-sensors").json()["sensors"]
    assert [item["sensor_id"] for item in sensors] == ["TH-01"]
    listing = api.post(
        "/api/substitute/candidate-list", json={"sensor_id": "TH-01"}
    ).json()
    donors = {item["donor_sensor_id"]: item for item in listing["candidates"]}
    assert donors["TH-02"]["eligible"] is True
    assert donors["PM-02"]["eligible"] is False


def test_substitute_endpoints_without_run_404(api):
    """没有批次时替测接口给 NO_RUN 而不是 500。"""
    assert api.get("/api/substitute/gap-sensors").status_code == 404
    assert api.get("/api/substitute/decisions").status_code == 404


def test_gaps_page_empty_state_without_gaps(api):
    """无网格的批次（仅表头）在缺口页显示替测空态，而不是报错。"""
    api.post(
        "/api/samples/import",
        json={"sample": "header_only", "offsets": "device_offsets"},
    )
    page = api.get("/gaps")
    assert page.status_code == 200
    assert "当前没有待替测的缺口桶" in page.text
