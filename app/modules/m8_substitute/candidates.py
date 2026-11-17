"""替测候选核对：同站邻表能否为缺口桶提供替测值。

核对三件事，全部通过才给出可批准的候选：
1. 量程一致（low/high 相同）、单位一致、采样率一致——同量程湿度计才能互替；
2. 时段重叠——每个缺口桶在邻表同一分钟必须有实采样本；
3. 读数差异——重叠时段两表都实采的桶，读数差占量程比例不得超过阈值。

核对只读取已采纳样本与契约，不写任何数据；批准与否由 M8 工作流另行决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app import config, store
from app.modules.m2_timebase import contract


@dataclass(frozen=True)
class DonorBucket:
    bucket_utc: str
    donor_value: str
    donor_source_row: int


@dataclass
class Candidate:
    run_id: str
    sensor_id: str
    donor_sensor_id: str
    eligible: bool
    checks: list[dict] = field(default_factory=list)
    gap_buckets: list[str] = field(default_factory=list)
    donor_buckets: list[DonorBucket] = field(default_factory=list)
    uncovered_buckets: list[str] = field(default_factory=list)
    overlap: list[dict] = field(default_factory=list)
    max_abs_diff: Decimal | None = None
    tolerance: Decimal = Decimal(config.SUBSTITUTE_RATE_TOLERANCE)
    span: Decimal = Decimal("0")
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "sensor_id": self.sensor_id,
            "donor_sensor_id": self.donor_sensor_id,
            "eligible": self.eligible,
            "checks": self.checks,
            "gap_buckets": self.gap_buckets,
            "donor_buckets": [
                {
                    "bucket_utc": item.bucket_utc,
                    "donor_value": item.donor_value,
                    "donor_source_row": item.donor_source_row,
                }
                for item in self.donor_buckets
            ],
            "uncovered_buckets": self.uncovered_buckets,
            "overlap": self.overlap,
            "max_abs_diff": None if self.max_abs_diff is None else str(self.max_abs_diff),
            "tolerance": str(self.tolerance),
            "span": str(self.span),
            "failures": self.failures,
        }


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def _sample_map(conn, run_id: str, sensor_id: str) -> dict:
    rows = store.rows(
        conn,
        "SELECT bucket_utc, value, source_row FROM samples"
        " WHERE run_id = ? AND sensor_id = ? ORDER BY bucket_utc",
        (run_id, sensor_id),
    )
    return {row["bucket_utc"]: row for row in rows}


def evaluate(
    conn,
    run_id: str,
    sensor_id: str,
    donor_sensor_id: str,
    *,
    tolerance: Decimal | None = None,
) -> Candidate:
    """逐桶核对候选替测；任何一项不过都返回 eligible=False 与原因码，不抛异常。"""
    rate = Decimal(config.SUBSTITUTE_RATE_TOLERANCE) if tolerance is None else Decimal(tolerance)
    candidate = Candidate(
        run_id=run_id,
        sensor_id=sensor_id,
        donor_sensor_id=donor_sensor_id,
        eligible=False,
        tolerance=rate,
    )

    target_spec = contract.spec(sensor_id)
    donor_spec = contract.spec(donor_sensor_id)
    if target_spec is None or donor_spec is None:
        candidate.failures.append("UNKNOWN_SENSOR")
        candidate.checks.append(_check("契约", False, "原测点或邻表不在传感器契约内"))
        return candidate
    if sensor_id == donor_sensor_id:
        candidate.failures.append("SUBSTITUTE_SELF_DONOR")
        candidate.checks.append(_check("邻表", False, "邻表不能是原测点自身，必须选另一只表"))
        return candidate

    same_range = target_spec.low == donor_spec.low and target_spec.high == donor_spec.high
    same_unit = contract.unit_key(target_spec.unit) == contract.unit_key(donor_spec.unit)
    same_rate = target_spec.rate_minutes == donor_spec.rate_minutes
    span = donor_spec.high - donor_spec.low
    candidate.span = span
    candidate.checks.append(
        _check(
            "量程",
            same_range,
            f"原测点 {target_spec.low}~{target_spec.high} / 邻表 {donor_spec.low}~{donor_spec.high}",
        )
    )
    candidate.checks.append(
        _check("单位", same_unit, f"原测点 {target_spec.unit} / 邻表 {donor_spec.unit}")
    )
    candidate.checks.append(
        _check(
            "采样率",
            same_rate,
            f"原测点 {target_spec.rate_minutes} 分钟 / 邻表 {donor_spec.rate_minutes} 分钟",
        )
    )
    if not same_range:
        candidate.failures.append("SUBSTITUTE_RANGE_MISMATCH")
    if not same_unit:
        candidate.failures.append("SUBSTITUTE_UNIT_MISMATCH")
    if not same_rate:
        candidate.failures.append("SUBSTITUTE_RATE_MISMATCH")

    gap_rows = store.rows(
        conn,
        "SELECT bucket_utc FROM grid WHERE run_id = ? AND sensor_id = ? AND state = 'GAP'"
        " ORDER BY bucket_utc",
        (run_id, sensor_id),
    )
    candidate.gap_buckets = [row["bucket_utc"] for row in gap_rows]

    donor_samples = _sample_map(conn, run_id, donor_sensor_id)
    target_samples = _sample_map(conn, run_id, sensor_id)
    if not donor_samples:
        candidate.failures.append("SUBSTITUTE_NO_DONOR")
        candidate.checks.append(_check("时段重叠", False, "邻表在本批次没有任何实采样本"))
    if not candidate.gap_buckets:
        candidate.failures.append("SUBSTITUTE_NOT_GAP")
        candidate.checks.append(_check("缺口", False, "原测点当前没有缺口桶，无需替测"))

    for bucket in candidate.gap_buckets:
        donor_row = donor_samples.get(bucket)
        if donor_row is None:
            candidate.uncovered_buckets.append(bucket)
            continue
        candidate.donor_buckets.append(
            DonorBucket(
                bucket_utc=bucket,
                donor_value=donor_row["value"],
                donor_source_row=donor_row["source_row"],
            )
        )

    if candidate.gap_buckets and donor_samples:
        covered = len(candidate.gap_buckets) - len(candidate.uncovered_buckets)
        overlap_ok = covered == len(candidate.gap_buckets)
        candidate.checks.append(
            _check(
                "时段重叠",
                overlap_ok,
                f"缺口 {len(candidate.gap_buckets)} 桶，邻表同分钟有读数 {covered} 桶",
            )
        )
        if not overlap_ok:
            candidate.failures.append("SUBSTITUTE_NO_DONOR")

    # 读数差异：两表同分钟都实采的桶逐桶比较（替测前的共同运行段）。
    max_diff: Decimal | None = None
    for bucket, target_row in sorted(target_samples.items()):
        donor_row = donor_samples.get(bucket)
        if donor_row is None:
            continue
        diff = abs(Decimal(target_row["value"]) - Decimal(donor_row["value"]))
        candidate.overlap.append(
            {
                "bucket_utc": bucket,
                "target_value": target_row["value"],
                "donor_value": donor_row["value"],
                "abs_diff": str(diff),
            }
        )
        max_diff = diff if max_diff is None else max(max_diff, diff)
    candidate.max_abs_diff = max_diff

    if max_diff is None or span <= 0:
        diff_ok = max_diff is not None and span > 0
        detail = "两表没有共同实采的分钟桶，无法核对读数差异" if max_diff is None else "量程跨度为 0"
        candidate.checks.append(_check("读数差异", diff_ok, detail))
        if not diff_ok:
            candidate.failures.append("SUBSTITUTE_DIFF_TOO_LARGE")
    else:
        ratio = (max_diff / span).quantize(Decimal("0.0001"))
        diff_ok = ratio <= rate
        candidate.checks.append(
            _check(
                "读数差异",
                diff_ok,
                f"最大差 {max_diff} / 量程 {span} = {ratio}，阈值 {rate}",
            )
        )
        if not diff_ok:
            candidate.failures.append("SUBSTITUTE_DIFF_TOO_LARGE")

    candidate.eligible = not candidate.failures
    return candidate


def donors_for(conn, run_id: str, sensor_id: str) -> list[Candidate]:
    """枚举契约内所有其他同单位传感器作为候选邻表，逐个核对。"""
    target_spec = contract.spec(sensor_id)
    if target_spec is None:
        return []
    results = []
    for other_id in sorted(contract.SENSORS):
        if other_id == sensor_id:
            continue
        results.append(evaluate(conn, run_id, sensor_id, other_id))
    return results
