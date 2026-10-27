"""Time helpers. All stored datetimes are UTC; display is Asia/Kolkata (IST)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
JST = ZoneInfo("Asia/Tokyo")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def from_unix(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """MongoDB returns naive UTC datetimes; normalise to aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_ist(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(IST)


def fmt_ist(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    return to_ist(dt).strftime("%d %b %Y, %I:%M %p IST")
