"""替测批准工作流：候选复核 → 逐桶落账 → 窗口与四账重算，全部在一个事务里。

纪律：
- 未批准前原测点网格行保持 GAP，本模块不做任何隐式补洞；
- 批准只把 GAP 行改成 SUBSTITUTED，sensor_id 始终是原测点，替测值来源逐桶留痕；
- 校验任一桶失败即抛异常，事务回滚，原缺口原样保留，已写记录不会留下半套；
- decision_id 由输入确定性派生，同一决定重跑返回 reused，不新增任何行；
- 撤销把 SUBSTITUTED 行还原成批准前的 GAP（含原原因码），替测留痕转决策头仍可审计。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from app import config, errors, manifest, store
from app.modules.m2_timebase import coerce, contract
from app.modules.m4_grid import windows as windows_mod
from app.modules.m6_batches import compare, history

from . import candidates

STATE_SUBSTITUTED = "SUBSTITUTED"


@dataclass(frozen=True)
class AppliedBucket:
    bucket_utc: str
    value: str
    donor_source_row: int
    prior_reason: str


def decision_id_for(run_id: str, sensor_id: str, donor_sensor_id: str, buckets: list[str]) -> str:
    material = manifest.canonical_json(
        {
            "run_id": run_id,
            "sensor_id": sensor_id,
            "donor_sensor_id": donor_sensor_id,
            "buckets": sorted(buckets),
            "kind": "substitute",
        }
    )
    return "SB-" + manifest.sha256_bytes(material.encode("utf-8"))[:10].upper()


def _decision_row(conn, run_id: str, decision_id: str):
    return store.one(
        conn,
        "SELECT * FROM substitution_decisions WHERE run_id = ? AND decision_id = ?",
        (run_id, decision_id),
    )


def _existing_decision(conn, run_id: str, decision_id: str) -> dict | None:
    row = _decision_row(conn, run_id, decision_id)
    if row is None or row["status"] != "APPROVED":
        return None
    items = store.rows(
        conn,
        "SELECT bucket_utc, value, donor_source_row FROM substitutions"
        " WHERE run_id = ? AND decision_id = ? ORDER BY bucket_utc",
        (run_id, decision_id),
    )
    return {
        "decision_id": decision_id,
        "run_id": run_id,
        "sensor_id": row["sensor_id"],
        "donor_sensor_id": row["donor_sensor_id"],
        "status": row["status"],
        "reused": True,
        "buckets": [item["bucket_utc"] for item in items],
        "applied": len(items),
        "approved_at": row["approved_at"],
    }


def _validate_buckets(conn, run_id: str, sensor_id: str, donor_sensor_id: str, buckets: list[str]):
    """逐桶校验：目标必须仍是 GAP，邻表同分钟必须有实采，值必须落在原测点量程内。"""
    if not buckets:
        raise errors.InputError("没有可替测的缺口桶", code="SUBSTITUTE_NOT_GAP")
    if len(buckets) != len(set(buckets)):
        raise errors.InputError("替测桶重复", code="SUBSTITUTE_DUPLICATE_BUCKET")

    spec = contract.spec(sensor_id)
    donor_spec = contract.spec(donor_sensor_id)
    if spec is None or donor_spec is None:
        raise errors.InputError("原测点或邻表不在契约内", code="UNKNOWN_SENSOR")

    donor_rows = store.rows(
        conn,
        "SELECT bucket_utc, value, source_row FROM samples"
        " WHERE run_id = ? AND sensor_id = ? ORDER BY bucket_utc",
        (run_id, donor_sensor_id),
    )
    donor_map = {row["bucket_utc"]: row for row in donor_rows}

    applied: list[AppliedBucket] = []
    for bucket in sorted(buckets):
        grid_row = store.one(
            conn,
            "SELECT state, reason FROM grid WHERE run_id = ? AND sensor_id = ? AND bucket_utc = ?",
            (run_id, sensor_id, bucket),
        )
        if grid_row is None:
            raise errors.InputError(
                f"桶 {bucket} 不在原测点网格内",
                code="SUBSTITUTE_NOT_GAP",
                detail={"sensor_id": sensor_id, "bucket_utc": bucket},
            )
        if grid_row["state"] != "GAP":
            raise errors.InputError(
                f"桶 {bucket} 当前状态为 {grid_row['state']}，不是缺口，不能替测",
                code="SUBSTITUTE_NOT_GAP",
                detail={"sensor_id": sensor_id, "bucket_utc": bucket, "state": grid_row["state"]},
            )
        donor_row = donor_map.get(bucket)
        if donor_row is None:
            raise errors.InputError(
                f"邻表 {donor_sensor_id} 在 {bucket} 没有同分钟实采读数",
                code="SUBSTITUTE_NO_DONOR",
                detail={"donor_sensor_id": donor_sensor_id, "bucket_utc": bucket},
            )
        value = Decimal(donor_row["value"])
        if value < spec.low or value > spec.high:
            raise errors.InputError(
                f"邻表读数 {value} 超出原测点量程 {spec.low}~{spec.high}",
                code="OUT_OF_RANGE",
                detail={"bucket_utc": bucket, "value": str(value)},
            )
        applied.append(
            AppliedBucket(
                bucket_utc=bucket,
                value=str(value),
                donor_source_row=donor_row["source_row"],
                prior_reason=grid_row["reason"],
            )
        )
    return applied


def _recompute_windows(conn, run_id: str, sensor_id: str) -> None:
    """按落库网格重算该传感器的窗口覆盖；SUBSTITUTED 与 OBSERVED 同计为实有。"""
    window_rows = store.rows(
        conn,
        "SELECT window_start, expected FROM windows WHERE run_id = ? AND sensor_id = ?"
        " ORDER BY window_start",
        (run_id, sensor_id),
    )
    grid_rows = store.rows(
        conn,
        "SELECT bucket_utc, state FROM grid WHERE run_id = ? AND sensor_id = ?",
        (run_id, sensor_id),
    )
    covered = {
        row["bucket_utc"]
        for row in grid_rows
        if row["state"] in ("OBSERVED", STATE_SUBSTITUTED)
    }
    payload = []
    for row in window_rows:
        start = row["window_start"]
        stop_text = _plus_minutes(start, config.WINDOW_MINUTES)
        observed = sum(1 for bucket in covered if start <= bucket < stop_text)
        coverage = windows_mod.ratio(observed, row["expected"])
        payload.append(
            (
                run_id,
                sensor_id,
                start,
                observed,
                row["expected"],
                str(coverage),
                windows_mod.state_of(coverage),
            )
        )
    conn.execute(
        "DELETE FROM windows WHERE run_id = ? AND sensor_id = ?", (run_id, sensor_id)
    )
    store.insert_many(
        conn,
        "INSERT INTO windows(run_id, sensor_id, window_start, observed, expected, coverage, state)"
        " VALUES (?,?,?,?,?,?,?)",
        payload,
    )


def _plus_minutes(utc_text: str, minutes: int) -> str:
    return coerce.minute_text(coerce.to_utc(utc_text) + timedelta(minutes=minutes))


def _refresh_run_accounts(conn, run_id: str) -> None:
    """重算批次计数、合计与四账行；替测值单独统计，不回流采纳/实有合计。"""
    run = history.get(conn, run_id)
    counts = dict(run["counts"])
    state_counts = {
        row["state"]: row["n"]
        for row in store.rows(
            conn, "SELECT state, COUNT(*) AS n FROM grid WHERE run_id = ? GROUP BY state", (run_id,)
        )
    }
    counts["observed"] = state_counts.get("OBSERVED", 0)
    counts["filled"] = state_counts.get("FILLED", 0)
    counts["gaps"] = state_counts.get("GAP", 0)
    counts["substituted"] = state_counts.get(STATE_SUBSTITUTED, 0)
    counts["grid_points"] = sum(state_counts.values())

    totals = dict(run["totals"])
    # 用 Decimal 字符串求和，避免 CAST REAL 的浮点往返；替测值单独统计，不回流实有合计。
    decimal_sum = Decimal("0")
    for row in store.rows(
        conn,
        "SELECT value FROM grid WHERE run_id = ? AND state = 'SUBSTITUTED'",
        (run_id,),
    ):
        decimal_sum += Decimal(row["value"])
    totals["substituted_sum"] = str(
        decimal_sum.quantize(Decimal(1).scaleb(-config.VALUE_PLACES), rounding=ROUND_HALF_UP)
    )

    windows_stored = int(
        store.value(conn, "SELECT COUNT(*) FROM windows WHERE run_id = ?", (run_id,), 0)
    )
    counts["windows_stored"] = windows_stored
    lines = compare.reconcile_lines(counts, totals)
    counts["balanced"] = compare.is_balanced(lines)

    conn.execute("UPDATE runs SET counts_json = ? WHERE run_id = ?", (
        manifest.canonical_json(counts), run_id,
    ))
    conn.execute("UPDATE runs SET totals_json = ? WHERE run_id = ?", (
        manifest.canonical_json(totals), run_id,
    ))
    conn.execute("DELETE FROM reconcile WHERE run_id = ?", (run_id,))
    store.insert_many(
        conn,
        "INSERT INTO reconcile(run_id, line_no, name, expected, actual, delta, state, note)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                run_id,
                line.line_no,
                line.name,
                str(line.expected),
                str(line.actual),
                str(line.delta),
                line.state,
                line.note,
            )
            for line in lines
        ],
    )


def _find_pair_decision(conn, run_id: str, sensor_id: str, donor_sensor_id: str) -> dict | None:
    """无显式桶重跑时，按原测点/邻表对找回已批准决定，保证同一决定不新增结果。"""
    row = store.one(
        conn,
        "SELECT decision_id FROM substitution_decisions"
        " WHERE run_id = ? AND sensor_id = ? AND donor_sensor_id = ? AND status = 'APPROVED'"
        " ORDER BY approved_at DESC, decision_id LIMIT 1",
        (run_id, sensor_id, donor_sensor_id),
    )
    if row is None:
        return None
    return _existing_decision(conn, run_id, row["decision_id"])


def approve(
    conn,
    run_id: str,
    sensor_id: str,
    donor_sensor_id: str,
    buckets: list[str] | None = None,
) -> dict:
    sensor_id = (sensor_id or "").strip()
    donor_sensor_id = (donor_sensor_id or "").strip()

    # 未显式给桶（None）时取当前缺口列表；显式给空列表是调用错误，不能偷偷改成全量。
    chosen: list[str] | None = sorted(buckets) if buckets is not None else None
    if chosen is not None and not chosen:
        raise errors.InputError("替测桶列表为空", code="SUBSTITUTE_NOT_GAP")
    if chosen is None:
        gap_rows = store.rows(
            conn,
            "SELECT bucket_utc FROM grid WHERE run_id = ? AND sensor_id = ? AND state = 'GAP'"
            " ORDER BY bucket_utc",
            (run_id, sensor_id),
        )
        chosen = [row["bucket_utc"] for row in gap_rows]
        if not chosen:
            cached_pair = _find_pair_decision(conn, run_id, sensor_id, donor_sensor_id)
            if cached_pair is not None:
                return cached_pair
    decision_id = decision_id_for(run_id, sensor_id, donor_sensor_id, chosen)
    cached = _existing_decision(conn, run_id, decision_id)
    if cached is not None:
        return cached

    candidate = candidates.evaluate(conn, run_id, sensor_id, donor_sensor_id)
    if not candidate.eligible:
        raise errors.InputError(
            "替测候选核对未通过：" + "、".join(candidate.failures),
            code=candidate.failures[0],
            detail={"checks": candidate.checks, "failures": candidate.failures},
        )

    # 全部桶先校验通过，再开事务写库；任何异常都会回滚到原缺口。
    applied = _validate_buckets(conn, run_id, sensor_id, donor_sensor_id, chosen)

    with store.transaction(conn):
        # 事务内复查幂等：并发下第二个请求不会重复落账。
        cached = _existing_decision(conn, run_id, decision_id)
        if cached is not None:
            return cached

        approved_at = history.now_text()
        for item in applied:
            conn.execute(
                "UPDATE grid SET state = 'SUBSTITUTED', reason = ?, value = ?,"
                " donor_sensor_id = ?, donor_source_row = ?"
                " WHERE run_id = ? AND sensor_id = ? AND bucket_utc = ? AND state = 'GAP'",
                (
                    config.SUBSTITUTE_REASON,
                    item.value,
                    donor_sensor_id,
                    item.donor_source_row,
                    run_id,
                    sensor_id,
                    item.bucket_utc,
                ),
            )
            changed = conn.execute("SELECT changes()").fetchone()[0]
            if changed == 0:
                raise errors.StateError(
                    f"桶 {item.bucket_utc} 在落账时已不是缺口",
                    code="SUBSTITUTE_NOT_GAP",
                )
            store.insert_many(
                conn,
                "INSERT INTO substitutions(run_id, decision_id, sensor_id, donor_sensor_id,"
                " bucket_utc, value, donor_source_row, reason, approved_at, prior_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        run_id,
                        decision_id,
                        sensor_id,
                        donor_sensor_id,
                        item.bucket_utc,
                        item.value,
                        item.donor_source_row,
                        config.SUBSTITUTE_REASON,
                        approved_at,
                        item.prior_reason,
                    )
                ],
            )

        max_diff = candidate.max_abs_diff
        store.insert_many(
            conn,
            "INSERT INTO substitution_decisions(run_id, decision_id, sensor_id, donor_sensor_id,"
            " status, buckets, tolerance, max_diff, approved_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    run_id,
                    decision_id,
                    sensor_id,
                    donor_sensor_id,
                    "APPROVED",
                    len(applied),
                    str(candidate.tolerance),
                    "" if max_diff is None else str(max_diff),
                    approved_at,
                )
            ],
        )
        _recompute_windows(conn, run_id, sensor_id)
        _refresh_run_accounts(conn, run_id)

    return {
        "decision_id": decision_id,
        "run_id": run_id,
        "sensor_id": sensor_id,
        "donor_sensor_id": donor_sensor_id,
        "status": "APPROVED",
        "reused": False,
        "buckets": [item.bucket_utc for item in applied],
        "applied": len(applied),
        "approved_at": approved_at,
        "candidate": candidate.as_dict(),
    }


def revoke(conn, run_id: str, decision_id: str) -> dict:
    row = _decision_row(conn, run_id, decision_id)
    if row is None:
        raise errors.NotFoundError(
            "替测决定不存在",
            code="SUBSTITUTE_NOT_FOUND",
            detail={"run_id": run_id, "decision_id": decision_id},
        )
    if row["status"] == "REVOKED":
        # 重复撤销幂等：决定已撤销时原样返回，不再改动网格与计数。
        return {
            "decision_id": decision_id,
            "run_id": run_id,
            "sensor_id": row["sensor_id"],
            "status": "REVOKED",
            "restored_gaps": 0,
            "buckets": [],
            "reused": True,
        }

    with store.transaction(conn):
        items = store.rows(
            conn,
            "SELECT bucket_utc, prior_reason FROM substitutions"
            " WHERE run_id = ? AND decision_id = ? ORDER BY bucket_utc",
            (run_id, decision_id),
        )
        for item in items:
            conn.execute(
                "UPDATE grid SET state = 'GAP', reason = ?, value = NULL,"
                " donor_sensor_id = NULL, donor_source_row = NULL"
                " WHERE run_id = ? AND sensor_id = ? AND bucket_utc = ? AND state = 'SUBSTITUTED'",
                (
                    item["prior_reason"],
                    run_id,
                    row["sensor_id"],
                    item["bucket_utc"],
                ),
            )
        conn.execute(
            "DELETE FROM substitutions WHERE run_id = ? AND decision_id = ?",
            (run_id, decision_id),
        )
        conn.execute(
            "UPDATE substitution_decisions SET status = 'REVOKED' WHERE run_id = ? AND decision_id = ?",
            (run_id, decision_id),
        )
        _recompute_windows(conn, run_id, row["sensor_id"])
        _refresh_run_accounts(conn, run_id)

    return {
        "decision_id": decision_id,
        "run_id": run_id,
        "sensor_id": row["sensor_id"],
        "status": "REVOKED",
        "restored_gaps": len(items),
        "buckets": [item["bucket_utc"] for item in items],
    }


def decisions_view(conn, run_id: str) -> list[dict]:
    rows = store.rows(
        conn,
        "SELECT * FROM substitution_decisions WHERE run_id = ? ORDER BY approved_at, decision_id",
        (run_id,),
    )
    result = []
    for row in rows:
        items = store.rows(
            conn,
            "SELECT bucket_utc, value, donor_source_row FROM substitutions"
            " WHERE run_id = ? AND decision_id = ? ORDER BY bucket_utc",
            (run_id, row["decision_id"]),
        )
        result.append(
            {
                "decision_id": row["decision_id"],
                "sensor_id": row["sensor_id"],
                "donor_sensor_id": row["donor_sensor_id"],
                "status": row["status"],
                "buckets": row["buckets"],
                "tolerance": row["tolerance"],
                "max_diff": row["max_diff"],
                "approved_at": row["approved_at"],
                "items": [dict(item) for item in items],
            }
        )
    return result


def substituted_points(conn, run_id: str) -> list[dict]:
    rows = store.rows(
        conn,
        "SELECT * FROM substitutions WHERE run_id = ? ORDER BY sensor_id, bucket_utc",
        (run_id,),
    )
    return [dict(row) for row in rows]
