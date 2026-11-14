"""Hour-aligned UTC timestamp helpers.

All timestamps are stored in SQLite as ISO-8601 UTC strings
(``YYYY-MM-DDTHH:MM:SSZ``), which sort lexicographically in chronological
order.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

HOUR = timedelta(hours=1)
FMT = "%Y-%m-%dT%H:%M:%SZ"


def ensure_hour_aligned_utc(dt: datetime) -> datetime:
    """Normalize to UTC and require whole-hour alignment."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    dt = dt.astimezone(timezone.utc)
    if dt.minute or dt.second or dt.microsecond:
        raise ValueError("timestamp must be aligned to a whole hour")
    return dt


def hour_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def parse_hour_str(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def shift_hour_str(s: str, hours: int) -> str:
    return hour_str(parse_hour_str(s) + HOUR * hours)
