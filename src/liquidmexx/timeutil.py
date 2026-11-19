"""UTC timestamp helpers.

All timestamps are stored as fixed-width ISO-8601 UTC strings
(``YYYY-MM-DDTHH:MM:SS.ffffffZ``) so that lexicographic order equals
chronological order inside SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime

_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetimes are not allowed; attach a timezone")
    return dt.astimezone(UTC).strftime(_FMT)


def now_iso() -> str:
    return to_iso(utcnow())


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string (``Z`` or offset) into an aware UTC datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def normalize_iso(value: str) -> str:
    return to_iso(parse_iso(value))


def from_unix(ts: float) -> str:
    return to_iso(datetime.fromtimestamp(ts, UTC))
