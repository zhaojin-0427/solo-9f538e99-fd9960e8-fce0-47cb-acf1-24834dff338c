"""End-to-end tests for the hourly settlement API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

T = "2026-01-01T{:02d}:00:00Z"


def rd(meter: str, hour: int, kwh: float, rev: int, reset: bool = False) -> dict:
    return {
        "meter_id": meter,
        "hour": T.format(hour),
        "cumulative_kwh": kwh,
        "revision": rev,
        "reset": reset,
    }


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def post(client: TestClient, readings: list[dict], batch_id: str | None = None):
    payload: dict = {"readings": readings}
    if batch_id is not None:
        payload["batch_id"] = batch_id
    return client.post("/api/v1/batches", json=payload)


def usage(client: TestClient, meter: str, start_h: int, end_h: int) -> list[dict]:
    resp = client.get(
        f"/api/v1/meters/{meter}/hourly-usage",
        params={"start": T.format(start_h), "end": T.format(end_h)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["hours"]


# ---------------------------------------------------------------- basics


def test_basic_settlement(client):
    resp = post(client, [rd("M1", h, kwh, 1) for h, kwh in
                         [(0, 100), (1, 110), (2, 125), (3, 140)]], "b1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "published"
    assert body["applied"] == 4

    hours = usage(client, "M1", 0, 3)
    assert [h["status"] for h in hours] == ["settled"] * 3
    assert [h["usage_kwh"] for h in hours] == [10.0, 15.0, 15.0]
    assert hours[0]["start_reading_revision"] == 1
    assert hours[0]["end_reading_revision"] == 1
    assert hours[0]["published_batch_id"] == "b1"

    # hour after the last reading is published as unsettleable (no end reading)
    tail = usage(client, "M1", 3, 4)[0]
    assert tail["status"] == "unsettleable"
    assert tail["reason"] == "missing_end_reading"
    assert tail["usage_kwh"] is None


def test_hour_before_first_reading_is_unsettleable(client):
    post(client, [rd("M1", 5, 100, 1), rd("M1", 6, 108, 1)], "b1")
    prev = usage(client, "M1", 4, 5)[0]
    assert prev["status"] == "unsettleable"
    assert prev["reason"] == "missing_start_reading"


# ------------------------------------------------------- revision handling


def test_highest_revision_wins_and_recalculates_adjacent_hours(client):
    post(client, [rd("M1", 0, 100, 1), rd("M1", 1, 110, 1), rd("M1", 2, 120, 1)], "b1")
    assert usage(client, "M1", 0, 2)[0]["usage_kwh"] == 10.0

    # correction: reading at 01:00 revised 110 -> 105 (rev 2)
    resp = post(client, [rd("M1", 1, 105, 2)], "b2")
    assert resp.status_code == 201
    recalculated = {h["hour_start"] for h in resp.json()["published_hours"]}
    assert recalculated == {T.format(0), T.format(1)}  # both adjacent hours

    h0, h1 = usage(client, "M1", 0, 2)
    assert h0["usage_kwh"] == 5.0            # 105 - 100
    assert h0["end_reading_revision"] == 2
    assert h1["usage_kwh"] == 15.0           # 120 - 105
    assert h1["start_reading_revision"] == 2
    assert h1["published_batch_id"] == "b2"


def test_lower_revision_is_discarded(client):
    post(client, [rd("M1", 0, 100, 1), rd("M1", 1, 110, 2)], "b1")
    resp = post(client, [rd("M1", 1, 999, 1)], "b2")  # stale revision
    assert resp.status_code == 201
    assert resp.json()["superseded"] == 1
    assert usage(client, "M1", 0, 1)[0]["usage_kwh"] == 10.0


def test_same_revision_different_content_rejected(client):
    post(client, [rd("M1", 0, 100, 1), rd("M1", 1, 110, 1)], "b1")
    resp = post(client, [rd("M1", 1, 111, 1)], "b2")  # same rev, different kWh
    assert resp.status_code == 409
    assert resp.json()["detail"]["problems"][0]["type"] == "revision_conflict"
    # published usage and stored readings stay untouched
    assert usage(client, "M1", 0, 1)[0]["usage_kwh"] == 10.0
    readings = client.get("/api/v1/meters/M1/readings").json()["readings"]
    assert {r["hour"]: r["cumulative_kwh"] for r in readings} == {
        T.format(0): 100.0,
        T.format(1): 110.0,
    }


def test_same_revision_different_reset_flag_rejected(client):
    post(client, [rd("M1", 0, 100, 1)], "b1")
    resp = post(client, [rd("M1", 0, 100, 1, reset=True)], "b2")
    assert resp.status_code == 409


def test_intra_batch_highest_revision_wins(client):
    resp = post(client, [rd("M1", 0, 100, 1), rd("M1", 0, 105, 2), rd("M1", 1, 110, 1)], "b1")
    assert resp.status_code == 201
    assert resp.json()["applied"] == 2
    assert resp.json()["superseded"] == 1
    assert usage(client, "M1", 0, 1)[0]["usage_kwh"] == 5.0  # 110 - 105


def test_intra_batch_same_revision_conflict_rejected(client):
    resp = post(client, [rd("M1", 0, 100, 1), rd("M1", 0, 101, 1)], "b1")
    assert resp.status_code == 409
    assert client.get("/api/v1/meters/M1/readings").json()["readings"] == []


# ------------------------------------------------------------- validation


def test_unmarked_rollback_rejects_whole_batch(client):
    post(client, [rd("M1", 0, 100, 1), rd("M1", 1, 110, 1)], "b1")
    resp = post(client, [rd("M1", 2, 105, 1), rd("M2", 0, 50, 1)], "b2")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["status"] == "rejected"
    assert detail["problems"][0]["type"] == "unmarked_reset_rollback"

    # nothing of the batch was applied: no M2 reading, hour 01 still open
    assert client.get("/api/v1/meters/M2/readings").json()["readings"] == []
    assert len(client.get("/api/v1/meters/M1/readings").json()["readings"]) == 2
    assert usage(client, "M1", 1, 2)[0]["reason"] == "missing_end_reading"

    # the rejection is recorded and replayed identically
    replay = post(client, [rd("M1", 2, 105, 1), rd("M2", 0, 50, 1)], "b2")
    assert replay.status_code == 422
    assert replay.json()["detail"]["replayed"] is True
    batch = client.get("/api/v1/batches/b2").json()
    assert batch["status"] == "rejected"


def test_marked_reset_is_accepted_and_blocks_only_its_hour(client):
    post(client, [rd("M1", 0, 100, 1), rd("M1", 1, 110, 1)], "b1")
    # counter reset: 02:00 reading drops to 20 but carries the reset flag
    resp = post(client, [rd("M1", 2, 20, 1, reset=True)], "b2")
    assert resp.status_code == 201

    h1 = usage(client, "M1", 1, 2)[0]
    assert h1["status"] == "unsettleable"
    assert h1["reason"] == "counter_reset"
    assert h1["usage_kwh"] is None

    # hours after the reset settle against the new baseline
    post(client, [rd("M1", 3, 35, 1)], "b3")
    h2 = usage(client, "M1", 2, 3)[0]
    assert h2["status"] == "settled"
    assert h2["usage_kwh"] == 15.0


def test_missing_readings_are_unsettleable_and_never_interpolated(client):
    # gap at 01:00: readings only at 00:00 and 02:00
    post(client, [rd("M1", 0, 100, 1), rd("M1", 2, 130, 1)], "b1")
    h0, h1 = usage(client, "M1", 0, 2)
    assert h0["status"] == "unsettleable"
    assert h0["reason"] == "missing_end_reading"
    assert h0["usage_kwh"] is None  # not interpolated to 15
    assert h1["status"] == "unsettleable"
    assert h1["reason"] == "missing_start_reading"

    # backfilling the gap settles both hours
    post(client, [rd("M1", 1, 112, 1)], "b2")
    h0, h1 = usage(client, "M1", 0, 2)
    assert (h0["status"], h0["usage_kwh"]) == ("settled", 12.0)
    assert (h1["status"], h1["usage_kwh"]) == ("settled", 18.0)


# ------------------------------------------------------------- idempotency


def test_replay_same_batch_id_is_idempotent(client):
    payload = [rd("M1", 0, 100, 1), rd("M1", 1, 110, 1)]
    first = post(client, payload, "b1")
    assert first.status_code == 201
    replay = post(client, payload, "b1")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["batch_id"] == "b1"
    # no double counting: usage is still the plain difference
    assert usage(client, "M1", 0, 1)[0]["usage_kwh"] == 10.0


def test_retransmission_without_batch_id_is_a_noop(client):
    payload = [rd("M1", 0, 100, 1), rd("M1", 1, 110, 1)]
    post(client, payload, "b1")
    again = post(client, payload)  # server-generated batch id
    assert again.status_code == 201
    body = again.json()
    assert body["applied"] == 0
    assert body["duplicates"] == 2
    assert body["published_hours"] == []
    assert usage(client, "M1", 0, 1)[0]["usage_kwh"] == 10.0


def test_batch_id_reuse_with_different_payload_conflicts(client):
    post(client, [rd("M1", 0, 100, 1)], "b1")
    resp = post(client, [rd("M1", 0, 200, 1)], "b1")
    assert resp.status_code == 409
    assert "different payload" in resp.json()["detail"]["message"]


# ------------------------------------------------------------------ query


def test_query_reports_revisions_used(client):
    post(client, [rd("M1", 0, 100, 3), rd("M1", 1, 110, 7)], "b1")
    h = usage(client, "M1", 0, 1)[0]
    assert h["start_reading_revision"] == 3
    assert h["end_reading_revision"] == 7


def test_query_covers_hours_without_any_reading(client):
    post(client, [rd("M1", 0, 100, 1), rd("M1", 1, 110, 1)], "b1")
    hours = usage(client, "M1", 0, 4)
    assert [h["status"] for h in hours] == ["settled", "unsettleable",
                                            "unsettleable", "unsettleable"]
    assert hours[2]["reason"] == "missing_reading"  # no readings at 02 and 03
    assert hours[2]["published_batch_id"] is None


def test_query_rejects_bad_ranges(client):
    base = "/api/v1/meters/M1/hourly-usage"
    assert client.get(base, params={"start": "2026-01-01T00:30:00Z",
                                    "end": "2026-01-01T01:00:00Z"}).status_code == 422
    assert client.get(base, params={"start": "2026-01-01T00:00:00",
                                    "end": "2026-01-01T01:00:00Z"}).status_code == 422
    assert client.get(base, params={"start": "2026-01-01T01:00:00Z",
                                    "end": "2026-01-01T01:00:00Z"}).status_code == 422


def test_multi_meter_batch_and_timezone_normalization(client):
    resp = post(client, [
        {**rd("M1", 0, 100, 1), "hour": "2026-01-01T08:00:00+08:00"},  # == 00:00Z
        rd("M1", 1, 110, 1),
        rd("M2", 0, 7, 1),
        rd("M2", 1, 9.5, 1),
    ], "b1")
    assert resp.status_code == 201
    assert usage(client, "M1", 0, 1)[0]["usage_kwh"] == 10.0
    assert usage(client, "M2", 0, 1)[0]["usage_kwh"] == 2.5


def test_invalid_reading_payload_rejected(client):
    resp = post(client, [rd("M1", 0, -5, 1)], "b1")  # negative kWh
    assert resp.status_code == 422
    resp = post(client, [{**rd("M1", 0, 5, 1), "hour": "2026-01-01T00:15:00Z"}], "b2")
    assert resp.status_code == 422
