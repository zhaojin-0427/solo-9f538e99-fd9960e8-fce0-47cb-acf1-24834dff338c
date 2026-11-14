"""Batch merge, validation and settlement publishing.

Rules implemented here
----------------------
* Per (meter, hour) the highest revision wins; a lower revision arriving
  later is discarded.  Re-submitting the *same* revision with *different*
  content (kWh or reset flag) rejects the whole batch with 409.
* A batch is applied transactionally: readings are merged, then the merged
  series of every touched meter is validated.  An unmarked counter rollback
  (cumulative decrease without a reset flag) rejects the whole batch with
  422.  On any rejection nothing changes -- published hourly usage stays
  as it was.
* On success the batch publishes exactly once: every hour adjacent to a
  changed reading (the hour ending at the reading and the hour starting at
  it) is recomputed and upserted.  Settlement rows are keyed by
  (meter, hour), so re-submitting the same batch can never double-count.
* Hour usage is the plain difference of the two bracketing cumulative
  readings.  Missing readings or a counter reset inside the hour make it
  unsettleable with a reason -- values are never interpolated.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .schemas import BatchIn, ReadingIn
from .timeutil import hour_str, shift_hour_str

SETTLED = "settled"
UNSETTLEABLE = "unsettleable"

UPSERT_USAGE_SQL = """
INSERT INTO hourly_usage (
    meter_id, hour_start, status, usage_kwh, reason,
    start_revision, end_revision, published_batch_id, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (meter_id, hour_start) DO UPDATE SET
    status             = excluded.status,
    usage_kwh          = excluded.usage_kwh,
    reason             = excluded.reason,
    start_revision     = excluded.start_revision,
    end_revision       = excluded.end_revision,
    published_batch_id = excluded.published_batch_id,
    updated_at         = excluded.updated_at
"""


class SettlementError(Exception):
    """Aborts a batch; carries the HTTP status and the response detail body."""

    def __init__(self, http_status: int, detail: dict[str, Any]) -> None:
        super().__init__(detail.get("message", "settlement error"))
        self.http_status = http_status
        self.detail = detail


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def payload_hash(readings: list[ReadingIn]) -> str:
    """Order-independent fingerprint of a batch payload (UTC-normalized)."""
    canonical = [
        {
            "meter_id": r.meter_id,
            "hour": hour_str(r.hour),
            "cumulative_kwh": r.cumulative_kwh,
            "revision": r.revision,
            "reset": r.reset,
        }
        for r in readings
    ]
    canonical.sort(
        key=lambda d: (d["meter_id"], d["hour"], d["revision"], d["cumulative_kwh"], d["reset"])
    )
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fetch_reading(conn: sqlite3.Connection, meter_id: str, hour_ts: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT meter_id, hour_ts, cumulative_kwh, revision, reset_flag, batch_id, updated_at "
        "FROM readings WHERE meter_id = ? AND hour_ts = ?",
        (meter_id, hour_ts),
    ).fetchone()


def compute_hour(conn: sqlite3.Connection, meter_id: str, hour_start: str) -> dict[str, Any]:
    """Derive the settlement row for one hour from the current readings."""
    r0 = fetch_reading(conn, meter_id, hour_start)
    r1 = fetch_reading(conn, meter_id, shift_hour_str(hour_start, 1))
    row: dict[str, Any] = {
        "meter_id": meter_id,
        "hour_start": hour_start,
        "status": UNSETTLEABLE,
        "usage_kwh": None,
        "reason": None,
        "start_reading_revision": r0["revision"] if r0 else None,
        "end_reading_revision": r1["revision"] if r1 else None,
        "published_batch_id": None,
    }
    if r0 is None and r1 is None:
        row["reason"] = "missing_reading"
    elif r0 is None:
        row["reason"] = "missing_start_reading"
    elif r1 is None:
        row["reason"] = "missing_end_reading"
    elif r1["reset_flag"]:
        # The counter was reset inside this hour; a plain difference is
        # meaningless and we do not interpolate.
        row["reason"] = "counter_reset"
    else:
        usage = r1["cumulative_kwh"] - r0["cumulative_kwh"]
        if usage < 0:
            # Unreachable for published batches (validation forbids it);
            # kept as a defensive guard for derived rows.
            row["reason"] = "unmarked_reset_rollback"
        else:
            row.update(status=SETTLED, usage_kwh=round(usage, 6))
    return row


def _reject(http_status: int, message: str, batch_id: str, problems: list[dict]) -> SettlementError:
    return SettlementError(
        http_status,
        {"status": "rejected", "batch_id": batch_id, "message": message, "problems": problems},
    )


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _record_rejection(conn: sqlite3.Connection, batch_id: str, digest: str, exc: SettlementError) -> None:
    """Persist the rejection so an identical replay gets the same answer."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO batches (batch_id, payload_hash, status, http_status, result, created_at)"
            " VALUES (?, ?, 'rejected', ?, ?, ?)",
            (batch_id, digest, exc.http_status, json.dumps(exc.detail, ensure_ascii=False), utcnow()),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        _rollback_quietly(conn)


def process_batch(conn: sqlite3.Connection, payload: BatchIn) -> dict[str, Any]:
    """Merge, validate and publish one batch.  Raises SettlementError on rejection."""
    batch_id = payload.batch_id or uuid.uuid4().hex
    digest = payload_hash(payload.readings)

    conn.execute("BEGIN IMMEDIATE")
    record_rejection = False
    try:
        existing = conn.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if existing is not None:
            stored = json.loads(existing["result"])
            if existing["payload_hash"] != digest:
                raise SettlementError(
                    409,
                    {
                        "status": "rejected",
                        "batch_id": batch_id,
                        "message": "batch_id already used with a different payload",
                    },
                )
            conn.execute("COMMIT")
            if existing["status"] == "published":
                return {**stored, "replayed": True}
            raise SettlementError(existing["http_status"], {**stored, "replayed": True})

        record_rejection = True
        now = utcnow()

        # ---- 1) fold duplicates inside the batch: highest revision wins ----
        groups: dict[tuple[str, str], list[ReadingIn]] = {}
        for r in payload.readings:
            groups.setdefault((r.meter_id, hour_str(r.hour)), []).append(r)

        winners: dict[tuple[str, str], ReadingIn] = {}
        superseded = 0
        problems: list[dict[str, Any]] = []
        for (meter_id, ts), items in groups.items():
            best_rev = max(i.revision for i in items)
            top = [i for i in items if i.revision == best_rev]
            first = top[0]
            if any(i.cumulative_kwh != first.cumulative_kwh or i.reset != first.reset for i in top[1:]):
                problems.append(
                    {
                        "type": "revision_conflict",
                        "scope": "batch",
                        "meter_id": meter_id,
                        "hour": ts,
                        "revision": best_rev,
                        "message": "same revision submitted with different content within the batch",
                    }
                )
                continue
            winners[(meter_id, ts)] = first
            superseded += len(items) - 1
        if problems:
            raise _reject(409, "conflicting readings inside the batch", batch_id, problems)

        # ---- 2) merge into the effective readings table ----
        applied = duplicates = 0
        changed: dict[str, set[str]] = {}
        for (meter_id, ts), r in sorted(winners.items()):
            current = fetch_reading(conn, meter_id, ts)
            if current is None:
                conn.execute(
                    "INSERT INTO readings (meter_id, hour_ts, cumulative_kwh, revision, reset_flag,"
                    " batch_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (meter_id, ts, r.cumulative_kwh, r.revision, int(r.reset), batch_id, now),
                )
                applied += 1
                changed.setdefault(meter_id, set()).add(ts)
            elif current["revision"] > r.revision:
                superseded += 1  # a higher revision is already stored
            elif current["revision"] == r.revision:
                if current["cumulative_kwh"] == r.cumulative_kwh and bool(current["reset_flag"]) == r.reset:
                    duplicates += 1  # idempotent retransmission
                else:
                    problems.append(
                        {
                            "type": "revision_conflict",
                            "scope": "stored",
                            "meter_id": meter_id,
                            "hour": ts,
                            "revision": r.revision,
                            "stored": {
                                "cumulative_kwh": current["cumulative_kwh"],
                                "reset": bool(current["reset_flag"]),
                            },
                            "received": {"cumulative_kwh": r.cumulative_kwh, "reset": r.reset},
                            "message": "same revision already stored with different content",
                        }
                    )
            else:
                conn.execute(
                    "UPDATE readings SET cumulative_kwh = ?, revision = ?, reset_flag = ?,"
                    " batch_id = ?, updated_at = ? WHERE meter_id = ? AND hour_ts = ?",
                    (r.cumulative_kwh, r.revision, int(r.reset), batch_id, now, meter_id, ts),
                )
                applied += 1
                changed.setdefault(meter_id, set()).add(ts)
        if problems:
            raise _reject(409, "readings conflict with stored revisions", batch_id, problems)

        # ---- 3) validate the merged series of every touched meter ----
        for meter_id in sorted({m for (m, _) in winners}):
            rows = conn.execute(
                "SELECT hour_ts, cumulative_kwh, reset_flag FROM readings"
                " WHERE meter_id = ? ORDER BY hour_ts",
                (meter_id,),
            ).fetchall()
            prev: sqlite3.Row | None = None
            for row in rows:
                if (
                    prev is not None
                    and not row["reset_flag"]
                    and row["cumulative_kwh"] < prev["cumulative_kwh"]
                ):
                    problems.append(
                        {
                            "type": "unmarked_reset_rollback",
                            "meter_id": meter_id,
                            "from_hour": prev["hour_ts"],
                            "to_hour": row["hour_ts"],
                            "from_cumulative_kwh": prev["cumulative_kwh"],
                            "to_cumulative_kwh": row["cumulative_kwh"],
                            "message": "cumulative reading decreased without a reset flag",
                        }
                    )
                prev = row
        if problems:
            raise _reject(422, "readings failed post-merge validation", batch_id, problems)

        # ---- 4) publish: recompute every hour adjacent to a changed reading ----
        affected: set[tuple[str, str]] = set()
        for meter_id, hours in changed.items():
            for ts in hours:
                affected.add((meter_id, ts))  # hour starting at the reading
                affected.add((meter_id, shift_hour_str(ts, -1)))  # hour ending at it

        published: list[dict[str, Any]] = []
        for meter_id, hour_start in sorted(affected):
            usage = compute_hour(conn, meter_id, hour_start)
            usage["published_batch_id"] = batch_id
            conn.execute(
                UPSERT_USAGE_SQL,
                (
                    usage["meter_id"],
                    usage["hour_start"],
                    usage["status"],
                    usage["usage_kwh"],
                    usage["reason"],
                    usage["start_reading_revision"],
                    usage["end_reading_revision"],
                    batch_id,
                    now,
                ),
            )
            published.append(usage)

        result: dict[str, Any] = {
            "batch_id": batch_id,
            "status": "published",
            "applied": applied,
            "superseded": superseded,
            "duplicates": duplicates,
            "published_hours": published,
        }
        conn.execute(
            "INSERT INTO batches (batch_id, payload_hash, status, http_status, result, created_at)"
            " VALUES (?, ?, 'published', 201, ?, ?)",
            (batch_id, digest, json.dumps(result, ensure_ascii=False), now),
        )
        conn.execute("COMMIT")
        return result
    except SettlementError as exc:
        _rollback_quietly(conn)
        if record_rejection:
            _record_rejection(conn, batch_id, digest, exc)
        raise
    except Exception:
        _rollback_quietly(conn)
        raise
