"""M8 替测批准：候选核对、批准留痕、幂等重跑、校验失败回滚、撤销还原与导出身份保留。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app import config, store
from app.errors import GridlabError
from app.modules.m6_batches import service as batch_service
from app.modules.m7_export import csv_out, evidence
from app.modules.m8_substitute import candidates, workflow

GAP_BUCKETS = [
    "2026-03-01T00:04:00Z",
    "2026-03-01T00:05:00Z",
    "2026-03-01T00:06:00Z",
    "2026-03-01T00:07:00Z",
]
DONOR_VALUES = {
    "2026-03-01T00:04:00Z": ("46.0", 9),
    "2026-03-01T00:05:00Z": ("46.3", 10),
    "2026-03-01T00:06:00Z": ("46.5", 11),
    "2026-03-01T00:07:00Z": ("46.7", 12),
}


@pytest.fixture()
def run_sub(conn):
    return batch_service.execute_run(
        conn, sample="substitute_humidity", offsets="device_offsets"
    )


def grid_state(conn, run_id, sensor_id, bucket):
    row = store.one(
        conn,
        "SELECT * FROM grid WHERE run_id = ? AND sensor_id = ? AND bucket_utc = ?",
        (run_id, sensor_id, bucket),
    )
    return dict(row) if row else None


def test_scenario_has_four_gap_buckets(conn, run_sub):
    """检修停报场景：TH-01 连续 4 桶缺口，邻表 TH-02 同期仍在采样。"""
    counts = run_sub["counts"]
    assert counts["gaps"] == 4
    assert counts["substituted"] == 0
    for bucket in GAP_BUCKETS:
        row = grid_state(conn, run_sub["run_id"], "TH-01", bucket)
        assert row["state"] == "GAP"
        assert row["value"] is None
        assert row["donor_sensor_id"] is None


def test_candidate_checks_overlap_range_and_diff(conn, run_sub):
    """候选核对同时给出时段重叠、量程、单位、采样率与读数差异五项结论。"""
    candidate = candidates.evaluate(conn, run_sub["run_id"], "TH-01", "TH-02")
    assert candidate.eligible is True
    names = {item["name"]: item for item in candidate.checks}
    assert set(names) == {"量程", "单位", "采样率", "时段重叠", "读数差异"}
    assert all(item["ok"] for item in candidate.checks)
    assert candidate.gap_buckets == GAP_BUCKETS
    assert candidate.uncovered_buckets == []
    assert candidate.max_abs_diff == Decimal("0.4")
    assert candidate.span == Decimal("100")
    assert [item.bucket_utc for item in candidate.donor_buckets] == GAP_BUCKETS


def test_range_mismatch_rejects_candidate(conn, run_sub):
    """量程不同的传感器（PM-02）不能作为湿度计替测邻表。"""
    candidate = candidates.evaluate(conn, run_sub["run_id"], "TH-01", "PM-02")
    assert candidate.eligible is False
    assert "SUBSTITUTE_RANGE_MISMATCH" in candidate.failures
    assert "SUBSTITUTE_RATE_MISMATCH" in candidate.failures


def test_rate_mismatch_rejects_candidate(conn, run_sub):
    """同单位但采样率不同（CO-03 五分钟率）不允许替测一分钟率湿度计。"""
    candidate = candidates.evaluate(conn, run_sub["run_id"], "TH-01", "CO-03")
    assert candidate.eligible is False
    assert "SUBSTITUTE_RATE_MISMATCH" in candidate.failures


def test_approve_keeps_identity_and_records_source_per_bucket(conn, run_sub):
    """批准后网格行仍是原测点 TH-01，逐桶带邻表来源，状态为 SUBSTITUTED。"""
    result = workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02")
    assert result["applied"] == 4
    assert result["status"] == "APPROVED"
    assert result["decision_id"].startswith("SB-")
    for bucket in GAP_BUCKETS:
        row = grid_state(conn, run_sub["run_id"], "TH-01", bucket)
        donor_value, donor_row = DONOR_VALUES[bucket]
        assert row["sensor_id"] == "TH-01"  # 原测点身份不丢
        assert row["state"] == "SUBSTITUTED"
        assert row["reason"] == config.SUBSTITUTE_REASON
        assert row["value"] == donor_value
        assert row["donor_sensor_id"] == "TH-02"
        assert row["donor_source_row"] == donor_row

    backing = store.rows(
        conn,
        "SELECT * FROM substitutions WHERE run_id = ? AND decision_id = ?",
        (run_sub["run_id"], result["decision_id"]),
    )
    assert len(backing) == 4
    assert {row["bucket_utc"] for row in backing} == set(GAP_BUCKETS)
    assert all(row["sensor_id"] == "TH-01" for row in backing)
    assert all(row["donor_sensor_id"] == "TH-02" for row in backing)
    assert {row["prior_reason"] for row in backing} == {"GAP_TOO_LONG"}


def test_same_decision_rerun_is_idempotent(conn, run_sub):
    """同一决定重跑返回 reused，不新增网格行、留痕行或决定头。"""
    first = workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02")
    grid_before = store.value(conn, "SELECT COUNT(*) FROM grid", (), 0)
    sub_before = store.value(conn, "SELECT COUNT(*) FROM substitutions", (), 0)
    decisions_before = store.value(conn, "SELECT COUNT(*) FROM substitution_decisions", (), 0)

    second = workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02")
    assert second["reused"] is True
    assert second["decision_id"] == first["decision_id"]
    assert second["buckets"] == first["buckets"]
    assert store.value(conn, "SELECT COUNT(*) FROM grid", (), 0) == grid_before
    assert store.value(conn, "SELECT COUNT(*) FROM substitutions", (), 0) == sub_before
    assert store.value(conn, "SELECT COUNT(*) FROM substitution_decisions", (), 0) == decisions_before

    # 显式带同一批桶重跑，decision_id 同样确定，仍然幂等。
    third = workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02", GAP_BUCKETS)
    assert third["reused"] is True
    assert third["decision_id"] == first["decision_id"]


def test_approval_failure_rolls_back_to_gaps(conn, run_sub):
    """校验失败（邻表在某桶没有同分钟读数）时整体回滚，原缺口一条不丢。"""
    before = store.rows(
        conn,
        "SELECT sensor_id, bucket_utc, state, value FROM grid"
        " WHERE run_id = ? AND state = 'GAP' ORDER BY bucket_utc",
        (run_sub["run_id"],),
    )
    tampered = GAP_BUCKETS[:3] + ["2026-03-01T00:30:00Z"]  # 最后一桶邻表无读数且非缺口
    with pytest.raises(GridlabError) as error:
        workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02", tampered)
    assert error.value.code in {"SUBSTITUTE_NOT_GAP", "SUBSTITUTE_NO_DONOR"}

    after = store.rows(
        conn,
        "SELECT sensor_id, bucket_utc, state, value FROM grid"
        " WHERE run_id = ? AND state = 'GAP' ORDER BY bucket_utc",
        (run_sub["run_id"],),
    )
    assert [dict(row) for row in after] == [dict(row) for row in before]
    assert store.value(conn, "SELECT COUNT(*) FROM substitutions", (), 0) == 0
    assert store.value(conn, "SELECT COUNT(*) FROM substitution_decisions", (), 0) == 0
    run = batch_service.history.get(conn, run_sub["run_id"])
    assert run["counts"]["substituted"] == 0
    assert run["counts"]["gaps"] == 4


def test_cannot_approve_non_gap_bucket(conn, run_sub):
    """实采桶不是缺口，直接拒绝而不是覆盖原值。"""
    with pytest.raises(GridlabError) as error:
        workflow.approve(
            conn,
            run_sub["run_id"],
            "TH-01",
            "TH-02",
            ["2026-03-01T00:00:00Z"],
        )
    assert error.value.code == "SUBSTITUTE_NOT_GAP"
    observed = grid_state(conn, run_sub["run_id"], "TH-01", "2026-03-01T00:00:00Z")
    assert observed["state"] == "OBSERVED"
    assert observed["value"] == "45.0"


def test_empty_bucket_list_and_self_donor_rejected(conn, run_sub):
    """显式空桶列表与把自己当邻表都直接拒绝，且不落任何留痕。"""
    with pytest.raises(GridlabError) as error:
        workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02", [])
    assert error.value.code == "SUBSTITUTE_NOT_GAP"
    candidate = candidates.evaluate(conn, run_sub["run_id"], "TH-01", "TH-01")
    assert candidate.eligible is False
    assert candidate.failures == ["SUBSTITUTE_SELF_DONOR"]
    assert store.value(conn, "SELECT COUNT(*) FROM substitutions", (), 0) == 0
    assert store.value(conn, "SELECT COUNT(*) FROM substitution_decisions", (), 0) == 0


def test_windows_recomputed_with_substituted_counted(conn, run_sub):
    """替测桶与实有桶同计覆盖度；批准后窗口由 PARTIAL 转 OK。"""
    windows_before = store.rows(
        conn,
        "SELECT * FROM windows WHERE run_id = ? AND sensor_id = 'TH-01'",
        (run_sub["run_id"],),
    )
    assert {row["state"] for row in windows_before} == {"PARTIAL"}
    workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02")
    windows_after = store.rows(
        conn,
        "SELECT * FROM windows WHERE run_id = ? AND sensor_id = 'TH-01' ORDER BY window_start",
        (run_sub["run_id"],),
    )
    assert all(row["state"] == "OK" for row in windows_after)
    assert sum(row["observed"] for row in windows_after) == 10
    # 邻表窗口不被波及。
    donor_windows = store.rows(
        conn,
        "SELECT COUNT(*) AS n FROM windows WHERE run_id = ? AND sensor_id = 'TH-02'",
        (run_sub["run_id"],),
    )
    assert donor_windows[0]["n"] == 1


def test_grid_account_still_balanced_after_approval(conn, run_sub):
    """网格账把替测作为独立一态：应有 = 实有 + 补齐 + 缺口 + 替测。"""
    workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02")
    view = batch_service.reconcile_view(conn, run_sub["run_id"])
    assert view["balanced"] is True
    grid_line = next(line for line in view["lines"] if line["name"] == "网格账")
    assert grid_line["note"] == "应有 = 实有 + 补齐 + 缺口 + 替测"
    assert Decimal(grid_line["delta"]) == 0
    counts = batch_service.history.get(conn, run_sub["run_id"])["counts"]
    assert counts["grid_points"] == (
        counts["observed"] + counts["filled"] + counts["gaps"] + counts["substituted"]
    )


def test_substituted_sum_does_not_leak_into_observed(conn, run_sub):
    """替测值单独合计，采纳/实有合计保持不变。"""
    before = batch_service.history.get(conn, run_sub["run_id"])["totals"]
    workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02")
    after = batch_service.history.get(conn, run_sub["run_id"])["totals"]
    assert after["accepted_sum"] == before["accepted_sum"]
    assert after["observed_sum"] == before["observed_sum"]
    assert Decimal(after["substituted_sum"]) == Decimal("46.0") + Decimal("46.3") + Decimal("46.5") + Decimal("46.7")
    assert Decimal(after["grid_sum"]) == Decimal(after["observed_sum"]) + Decimal(after["filled_sum"])


def test_revoke_restores_original_gaps_and_reasons(conn, run_sub):
    """撤销把每个 SUBSTITUTED 桶还原成批准前的 GAP 与原原因码，留痕清空、决定头转 REVOKED。"""
    result = workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02")
    revoked = workflow.revoke(conn, run_sub["run_id"], result["decision_id"])
    assert revoked["restored_gaps"] == 4
    assert revoked["status"] == "REVOKED"
    for bucket in GAP_BUCKETS:
        row = grid_state(conn, run_sub["run_id"], "TH-01", bucket)
        assert row["state"] == "GAP"
        assert row["reason"] == "GAP_TOO_LONG"
        assert row["value"] is None
        assert row["donor_sensor_id"] is None
        assert row["donor_source_row"] is None
    assert store.value(conn, "SELECT COUNT(*) FROM substitutions", (), 0) == 0
    decision = store.one(
        conn,
        "SELECT * FROM substitution_decisions WHERE run_id = ? AND decision_id = ?",
        (run_sub["run_id"], result["decision_id"]),
    )
    assert decision["status"] == "REVOKED"
    run = batch_service.history.get(conn, run_sub["run_id"])
    assert run["counts"]["gaps"] == 4
    assert run["counts"]["substituted"] == 0
    assert run["counts"]["balanced"] is True


def test_export_and_evidence_carry_identity_and_source(conn, run_sub):
    """网格 CSV 与单元格证据都保留原测点身份，并逐桶给出邻表来源。"""
    result = workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02")
    _, rows = csv_out.grid_export(conn, run_sub["run_id"])
    sub_rows = [row for row in rows if row[6] == "SUBSTITUTED"]
    assert len(sub_rows) == 4
    for row in sub_rows:
        assert row[1] == "TH-01"          # 导出仍是原测点
        assert row[10] == "TH-02"         # 邻表身份
        assert row[11] != ""              # 邻表来源行
    payload = evidence.cell(conn, run_sub["run_id"], "TH-01", GAP_BUCKETS[0])
    assert payload["sensor_id"] == "TH-01"
    assert payload["state"] == "SUBSTITUTED"
    assert payload["substitution"]["donor_sensor_id"] == "TH-02"
    assert payload["substitution"]["donor_source_row"] == 9
    assert payload["substitution"]["decision_id"] == result["decision_id"]
    assert payload["substitution"]["donor_sample"]["source_row"] == 9


def test_verify_detects_substitution_overlay_and_backing(conn, run_sub):
    """重新推导把替测视为 GAP 上的人工覆盖层，并要求每桶都有来源留痕。"""
    workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02")
    gaps_check = batch_service.verify_gaps(conn, run_sub["run_id"])
    assert gaps_check["verified"] is True
    assert gaps_check["stored"]["substituted"] == 4
    grid_check = batch_service.verify_grid(conn, run_sub["run_id"])
    assert grid_check["verified"] is True
    assert grid_check["substituted"] == 4
    assert grid_check["orphan_substituted"] == []


def test_records_never_lost_across_approve_and_revoke(conn, run_sub):
    """批准与撤销前后网格总行数、采纳行数与原始行数保持不变。"""
    grid_total = store.value(conn, "SELECT COUNT(*) FROM grid", (), 0)
    sample_total = store.value(conn, "SELECT COUNT(*) FROM samples", (), 0)
    result = workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02")
    workflow.revoke(conn, run_sub["run_id"], result["decision_id"])
    assert store.value(conn, "SELECT COUNT(*) FROM grid", (), 0) == grid_total
    assert store.value(conn, "SELECT COUNT(*) FROM samples", (), 0) == sample_total
    run = batch_service.history.get(conn, run_sub["run_id"])
    assert run["counts"]["raw_records"] == 16
    assert run["counts"]["accepted"] == 16


def test_diff_too_large_blocks_candidate(conn, run_sub):
    """读数差异超阈值时候选核对不通过，页面不会开放批准。"""
    candidate = candidates.evaluate(
        conn, run_sub["run_id"], "TH-01", "TH-02", tolerance=Decimal("0.001")
    )
    assert candidate.eligible is False
    assert "SUBSTITUTE_DIFF_TOO_LARGE" in candidate.failures
    diff_check = next(item for item in candidate.checks if item["name"] == "读数差异")
    assert diff_check["ok"] is False
    # 默认阈值（量程 100 的 10%）下同样的两表差异可以接受，阈值集中在 config。
    default = candidates.evaluate(conn, run_sub["run_id"], "TH-01", "TH-02")
    assert default.eligible is True
    assert str(default.tolerance) == config.SUBSTITUTE_RATE_TOLERANCE


def test_decisions_listing_and_substituted_view(conn, run_sub):
    """决定清单与替测桶视图可读出每桶来源。"""
    result = workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02")
    decisions = workflow.decisions_view(conn, run_sub["run_id"])
    assert len(decisions) == 1
    assert decisions[0]["decision_id"] == result["decision_id"]
    assert decisions[0]["buckets"] == 4
    points = workflow.substituted_points(conn, run_sub["run_id"])
    assert len(points) == 4
    assert {point["donor_source_row"] for point in points} == {9, 10, 11, 12}


def test_partial_bucket_approval_then_second_decision(conn, run_sub):
    """显式批准部分缺口桶：其余仍是 GAP；两个决定各自留痕、编号不同。"""
    first = workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02", GAP_BUCKETS[:2])
    assert first["applied"] == 2
    mid = batch_service.history.get(conn, run_sub["run_id"])["counts"]
    assert mid["substituted"] == 2
    assert mid["gaps"] == 2
    second = workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02", GAP_BUCKETS[2:])
    assert second["decision_id"] != first["decision_id"]
    assert second["applied"] == 2
    final = batch_service.history.get(conn, run_sub["run_id"])["counts"]
    assert final["substituted"] == 4
    assert final["gaps"] == 0
    assert store.value(conn, "SELECT COUNT(*) FROM substitution_decisions", (), 0) == 2
    assert store.value(conn, "SELECT COUNT(*) FROM substitutions", (), 0) == 4


def test_revoke_is_idempotent_and_missing_decision_404(conn, run_sub):
    """重复撤销不改动数据；不存在的决定号给 NOT_FOUND。"""
    result = workflow.approve(conn, run_sub["run_id"], "TH-01", "TH-02")
    first = workflow.revoke(conn, run_sub["run_id"], result["decision_id"])
    second = workflow.revoke(conn, run_sub["run_id"], result["decision_id"])
    assert first["status"] == second["status"] == "REVOKED"
    assert second["reused"] is True
    assert second["restored_gaps"] == 0
    run = batch_service.history.get(conn, run_sub["run_id"])
    assert run["counts"]["gaps"] == 4
    with pytest.raises(GridlabError) as error:
        workflow.revoke(conn, run_sub["run_id"], "SB-NOPE")
    assert error.value.code == "SUBSTITUTE_NOT_FOUND"
