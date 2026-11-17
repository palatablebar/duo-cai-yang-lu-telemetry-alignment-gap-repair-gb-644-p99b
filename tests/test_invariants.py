"""不变量：行账守恒、网格守恒、幂等重跑、补齐不回流。"""

from __future__ import annotations

from decimal import Decimal

from app import store
from app.modules.m6_batches import service

from expected import EDGE, GAP, MAIN


def test_row_account_conserved(conn):
    """行账守恒：原始行 = 采纳 + 重复 + 隔离，换样例也成立。"""
    for sample, expected in (("telemetry_raw", MAIN), ("gap_series", GAP), ("midnight_edge", EDGE)):
        result = service.execute_run(conn, sample=sample, offsets="device_offsets")
        counts = result["counts"]
        assert counts["raw_records"] == (
            counts["accepted"] + counts["duplicates"] + counts["quarantined"]
        ), sample
        assert counts["raw_records"] == expected["raw_records"]
        assert result["balanced"] is True


def test_grid_account_conserved(conn, run_main):
    """网格守恒：应有桶 = 实有 + 补齐 + 缺口，且等于逐传感器应有之和。"""
    counts = run_main["counts"]
    assert counts["expected_buckets"] == (
        counts["observed"] + counts["filled"] + counts["gaps"]
    )
    per_sensor = store.rows(
        conn,
        "SELECT sensor_id, COUNT(*) AS n FROM grid WHERE run_id = ? GROUP BY sensor_id",
        (run_main["run_id"],),
    )
    assert sum(row["n"] for row in per_sensor) == counts["expected_buckets"]
    assert counts["grid_points"] == counts["expected_buckets"]


def test_rerun_is_idempotent(conn, run_main):
    """幂等重跑：行数、合计、导出摘要都不变。"""
    before_runs = store.value(conn, "SELECT COUNT(*) FROM runs", (), 0)
    before_grid = store.value(conn, "SELECT COUNT(*) FROM grid", (), 0)
    before_sum = Decimal(run_main["totals"]["grid_sum"])

    again = service.execute_run(conn, sample="telemetry_raw", offsets="device_offsets")
    assert again["reused"] is True
    assert store.value(conn, "SELECT COUNT(*) FROM runs", (), 0) == before_runs
    assert store.value(conn, "SELECT COUNT(*) FROM grid", (), 0) == before_grid
    assert Decimal(again["totals"]["grid_sum"]) == before_sum
    assert again["counts"]["accepted"] == MAIN["accepted"]

    fresh = service.execute_run(conn, sample="telemetry_raw", offsets="device_offsets")
    assert fresh["counts"] == again["counts"]
    assert fresh["totals"] == again["totals"]


def test_filled_values_do_not_leak_into_accepted(conn, run_main):
    """补齐值不回流采纳合计：数值账两边都只算实有。"""
    totals = run_main["totals"]
    assert Decimal(totals["accepted_sum"]) == Decimal(totals["observed_sum"])
    assert Decimal(totals["grid_sum"]) == Decimal(totals["accepted_sum"]) + Decimal(
        totals["filled_sum"]
    )
    accepted_rows = store.value(
        conn, "SELECT COUNT(*) FROM samples WHERE run_id = ?", (run_main["run_id"],), 0
    )
    assert accepted_rows == MAIN["accepted"]
    filled_rows = store.value(
        conn,
        "SELECT COUNT(*) FROM grid WHERE run_id = ? AND state = 'FILLED'",
        (run_main["run_id"],),
        0,
    )
    assert filled_rows == MAIN["filled"]


def test_watermark_monotonic_across_replay(conn, run_main):
    """迟到只由到达顺序决定：同一文件重放得到同一批迟到标记。"""
    late_before = [
        dict(row)
        for row in store.rows(
            conn, "SELECT sensor_id, bucket_utc FROM samples WHERE run_id = ? AND late = 1",
            (run_main["run_id"],),
        )
    ]
    second = service.execute_run(conn, sample="telemetry_raw", offsets="device_offsets")
    late_after = [
        dict(row)
        for row in store.rows(
            conn, "SELECT sensor_id, bucket_utc FROM samples WHERE run_id = ? AND late = 1",
            (second["run_id"],),
        )
    ]
    assert late_before == late_after
    assert len(late_before) == MAIN["late"]


def test_substitution_overlay_keeps_grid_account_conserved(conn):
    """替测只改变状态分布：批准前后网格总行数守恒，四账始终相等。"""
    from app.modules.m8_substitute import workflow

    run = service.execute_run(conn, sample="substitute_humidity", offsets="device_offsets")
    run_id = run["run_id"]
    total_before = store.value(conn, "SELECT COUNT(*) FROM grid", (), 0)
    samples_before = store.value(conn, "SELECT COUNT(*) FROM samples", (), 0)

    decision = workflow.approve(conn, run_id, "TH-01", "TH-02")
    mid = service.history.get(conn, run_id)
    assert mid["counts"]["grid_points"] == total_before
    assert service.reconcile_view(conn, run_id)["balanced"] is True

    workflow.revoke(conn, run_id, decision["decision_id"])
    after = service.history.get(conn, run_id)
    assert after["counts"]["grid_points"] == total_before
    assert store.value(conn, "SELECT COUNT(*) FROM samples", (), 0) == samples_before
    assert service.reconcile_view(conn, run_id)["balanced"] is True
    # 撤销后网格逐行回到批准前状态。
    assert after["counts"]["gaps"] == run["counts"]["gaps"]
    assert after["counts"]["substituted"] == 0


def test_substitution_rerun_creates_no_extra_rows(conn):
    """同一替测决定连跑三次，留痕表与决定头都只有一份。"""
    from app.modules.m8_substitute import workflow

    run = service.execute_run(conn, sample="substitute_humidity", offsets="device_offsets")
    first = workflow.approve(conn, run["run_id"], "TH-01", "TH-02")
    workflow.approve(conn, run["run_id"], "TH-01", "TH-02")
    workflow.approve(conn, run["run_id"], "TH-01", "TH-02", first["buckets"])
    assert store.value(conn, "SELECT COUNT(*) FROM substitution_decisions", (), 0) == 1
    assert store.value(conn, "SELECT COUNT(*) FROM substitutions", (), 0) == 4
    assert store.value(
        conn,
        "SELECT COUNT(*) FROM grid WHERE run_id = ? AND state = 'SUBSTITUTED'",
        (run["run_id"],),
        0,
    ) == 4
