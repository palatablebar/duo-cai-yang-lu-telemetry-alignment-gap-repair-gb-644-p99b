"""M8 替测批准入口：候选核对、批准、撤销与决定清单（JSON API，页面交互由缺口页发起）。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import errors, store
from app.modules.m6_batches import history

from . import service, workflow

router = APIRouter(tags=["m8 替测批准"])


class ApproveBody(BaseModel):
    sensor_id: str = Field(min_length=1, max_length=32)
    donor_sensor_id: str = Field(min_length=1, max_length=32)
    buckets: list[str] | None = None
    run_id: str | None = Field(default=None, max_length=64)


class RevokeBody(BaseModel):
    decision_id: str = Field(min_length=1, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)


class CandidateBody(BaseModel):
    sensor_id: str = Field(min_length=1, max_length=32)
    donor_sensor_id: str = Field(min_length=1, max_length=32)
    run_id: str | None = Field(default=None, max_length=64)


class CandidateListBody(BaseModel):
    sensor_id: str = Field(min_length=1, max_length=32)
    run_id: str | None = Field(default=None, max_length=64)


def _resolve_run(conn, run_id: str | None):
    run = history.get(conn, run_id) if run_id else history.latest(conn)
    if run is None:
        raise errors.NotFoundError("还没有批次", code="NO_RUN")
    return run


@router.get("/api/substitute/gap-sensors")
def gap_sensors(run_id: str | None = None):
    conn = store.session()
    try:
        run = _resolve_run(conn, run_id)
        return {"run_id": run["run_id"], "sensors": service.gap_sensors(conn, run["run_id"])}
    finally:
        conn.close()


@router.get("/api/substitute/candidates")
def candidates(sensor_id: str, donor_sensor_id: str, run_id: str | None = None):
    conn = store.session()
    try:
        run = _resolve_run(conn, run_id)
        return service.candidate_view(conn, run["run_id"], sensor_id.strip(), donor_sensor_id.strip())
    finally:
        conn.close()


@router.post("/api/substitute/candidate-list")
def candidates_all(body: CandidateListBody):
    conn = store.session()
    try:
        run = _resolve_run(conn, body.run_id)
        return {
            "run_id": run["run_id"],
            "sensor_id": body.sensor_id,
            "candidates": service.candidates_overview(conn, run["run_id"], body.sensor_id.strip()),
        }
    finally:
        conn.close()


@router.post("/api/substitute/approve")
def approve(body: ApproveBody):
    conn = store.session()
    try:
        run = _resolve_run(conn, body.run_id)
        return workflow.approve(
            conn,
            run["run_id"],
            body.sensor_id.strip(),
            body.donor_sensor_id.strip(),
            body.buckets,
        )
    finally:
        conn.close()


@router.post("/api/substitute/revoke")
def revoke(body: RevokeBody):
    conn = store.session()
    try:
        run = _resolve_run(conn, body.run_id)
        return workflow.revoke(conn, run["run_id"], body.decision_id.strip())
    finally:
        conn.close()


@router.get("/api/substitute/decisions")
def decisions(run_id: str | None = None):
    conn = store.session()
    try:
        run = _resolve_run(conn, run_id)
        return {"run_id": run["run_id"], "decisions": service.decisions(conn, run["run_id"])}
    finally:
        conn.close()
