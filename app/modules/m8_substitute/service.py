"""替测视图编排：缺口页需要的候选核对、决定清单与替测桶明细。"""

from __future__ import annotations

from app import store
from app.modules.m2_timebase import contract

from . import candidates, workflow


def gap_sensors(conn, run_id: str) -> list[dict]:
    """当前仍有 GAP 桶的传感器，附同量程邻表清单，供页面选择。"""
    rows = store.rows(
        conn,
        "SELECT sensor_id, COUNT(*) AS n FROM grid WHERE run_id = ? AND state = 'GAP'"
        " GROUP BY sensor_id ORDER BY sensor_id",
        (run_id,),
    )
    result = []
    for row in rows:
        spec = contract.spec(row["sensor_id"])
        peers = []
        if spec is not None:
            for other_id, other in sorted(contract.SENSORS.items()):
                if other_id == row["sensor_id"]:
                    continue
                peers.append(
                    {
                        "sensor_id": other_id,
                        "same_range": other.low == spec.low and other.high == spec.high,
                        "same_unit": other.unit == spec.unit,
                        "same_rate": other.rate_minutes == spec.rate_minutes,
                    }
                )
        result.append(
            {
                "sensor_id": row["sensor_id"],
                "gap_buckets": row["n"],
                "unit": spec.unit if spec else "",
                "peers": peers,
            }
        )
    return result


def candidate_view(conn, run_id: str, sensor_id: str, donor_sensor_id: str) -> dict:
    return candidates.evaluate(conn, run_id, sensor_id, donor_sensor_id).as_dict()


def candidates_overview(conn, run_id: str, sensor_id: str) -> list[dict]:
    return [item.as_dict() for item in candidates.donors_for(conn, run_id, sensor_id)]


def decisions(conn, run_id: str) -> list[dict]:
    return workflow.decisions_view(conn, run_id)


def substituted(conn, run_id: str) -> list[dict]:
    rows = workflow.substituted_points(conn, run_id)
    for row in rows:
        spec = contract.spec(row["sensor_id"])
        row["places"] = spec.places if spec else 3
    return rows
