"""FastAPI application: routes and wiring."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, Response

from .db import connect, init_db
from .schemas import BatchIn
from .settlement import SettlementError, compute_hour, process_batch
from .timeutil import ensure_hour_aligned_utc, hour_str

MAX_RANGE_HOURS = 8784  # 366 days


def create_app(db_path: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(db_path)
        yield

    app = FastAPI(
        title="整点电量结算 API",
        version="1.0.0",
        description="把设备补传/更正的整点累计电量整理成结算用的小时用量。",
        lifespan=lifespan,
    )

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.post("/api/v1/batches", status_code=201, summary="批量提交整点累计读数")
    def submit_batch(payload: BatchIn, response: Response, conn=Depends(get_conn)):
        try:
            result = process_batch(conn, payload)
        except SettlementError as exc:
            raise HTTPException(status_code=exc.http_status, detail=exc.detail)
        replayed = bool(result.pop("replayed", False))
        if replayed:
            response.status_code = 200  # idempotent replay of a known batch_id
        result["replayed"] = replayed
        return result

    @app.get("/api/v1/batches/{batch_id}", summary="查询批次处理结果")
    def get_batch(batch_id: str, conn=Depends(get_conn)):
        row = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="batch not found")
        return {
            "batch_id": row["batch_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "result": json.loads(row["result"]),
        }

    @app.get("/api/v1/meters/{meter_id}/readings", summary="查询电表当前生效读数(最高修订)")
    def list_readings(meter_id: str, conn=Depends(get_conn)):
        rows = conn.execute(
            "SELECT * FROM readings WHERE meter_id = ? ORDER BY hour_ts", (meter_id,)
        ).fetchall()
        return {
            "meter_id": meter_id,
            "readings": [
                {
                    "hour": r["hour_ts"],
                    "cumulative_kwh": r["cumulative_kwh"],
                    "revision": r["revision"],
                    "reset": bool(r["reset_flag"]),
                    "batch_id": r["batch_id"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ],
        }

    @app.get("/api/v1/meters/{meter_id}/hourly-usage", summary="按时段查询小时用量及所用读数修订号")
    def get_hourly_usage(
        meter_id: str,
        start: datetime = Query(..., description="起始整点(含),ISO-8601,需带时区"),
        end: datetime = Query(..., description="结束整点(不含),ISO-8601,需带时区"),
        conn=Depends(get_conn),
    ):
        try:
            start_utc = ensure_hour_aligned_utc(start)
            end_utc = ensure_hour_aligned_utc(end)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"start/end must be timezone-aware and hour-aligned: {exc}"
            )
        if not start_utc < end_utc:
            raise HTTPException(status_code=422, detail="start must be before end")
        n_hours = int((end_utc - start_utc).total_seconds() // 3600)
        if n_hours > MAX_RANGE_HOURS:
            raise HTTPException(
                status_code=422, detail=f"range too large: {n_hours}h > {MAX_RANGE_HOURS}h"
            )

        rows = conn.execute(
            "SELECT * FROM hourly_usage WHERE meter_id = ? AND hour_start >= ? AND hour_start < ?"
            " ORDER BY hour_start",
            (meter_id, hour_str(start_utc), hour_str(end_utc)),
        ).fetchall()
        published = {r["hour_start"]: r for r in rows}

        hours = []
        for i in range(n_hours):
            h = hour_str(start_utc + timedelta(hours=i))
            row = published.get(h)
            if row is not None:
                hours.append(
                    {
                        "meter_id": row["meter_id"],
                        "hour_start": row["hour_start"],
                        "status": row["status"],
                        "usage_kwh": row["usage_kwh"],
                        "reason": row["reason"],
                        "start_reading_revision": row["start_revision"],
                        "end_reading_revision": row["end_revision"],
                        "published_batch_id": row["published_batch_id"],
                    }
                )
            else:
                # Never touched by a published batch -> no readings bracket this
                # hour; derive the (unsettleable) answer on the fly.
                hours.append(compute_hour(conn, meter_id, h))
        return {
            "meter_id": meter_id,
            "start": hour_str(start_utc),
            "end": hour_str(end_utc),
            "hours": hours,
        }

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    return app


app = create_app(os.environ.get("DATABASE_PATH", "settlement.db"))
