"""Resolve ``settles=`` (an exact UTC time) to a settlement time, and the default settlement."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import BadRequest
from .models import Market

INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-9]{2}.*")
MIN_SECS = 3_600  # shortest tenor when /markets gives none
# The gateway checks the tenor on its own clock. 180 s covers a client clock up to 120 s behind the
# gateway's (its timestamp window) and 60 s to open the request.
OPEN_MARGIN_SECS = 180
DAYS_READ = 31  # days to search forward for a settlement day
SECS_MAX = 100_000_000_000  # 5138-11-16 UTC; a larger Unix time is in milliseconds or finer
FORMS = ('settles is an exact UTC time: a datetime with a timezone, an ISO 8601 time with Z or an offset '
         'such as "2026-10-24T15:00Z", or a Unix time in seconds such as 1792854000')
UNIX = "settles as a number is a Unix time in seconds, UTC, such as 1792854000"
NO_ZONE = 'settles needs a timezone, e.g. "2026-10-24T15:00Z" or datetime(..., tzinfo=timezone.utc)'


def fixing(m: Market) -> tuple[time, ZoneInfo] | None:
    """Return the fixing time and zone of the /markets row of ``m``, or None when absent or unreadable."""
    s = m.raw.get("settlement")
    f = s.get("fixing") if isinstance(s, dict) else None
    if not isinstance(f, dict) or not isinstance(f.get("time"), str) or not isinstance(f.get("zone"), str):
        return None
    try:
        hh, mm = f["time"].split(":")
        return time(int(hh), int(mm)), ZoneInfo(f["zone"])
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _add_months(d: date, n: int) -> date:
    """Add ``n`` months to ``d``, capping the day at the month's last day."""
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return date(y, m, min(d.day, (nxt - timedelta(days=1)).day))


def _instant(value: Any) -> datetime | None:
    """Parse a date-time text, or None for another value. Raise BadRequest when unreadable or without an offset."""
    if not isinstance(value, str) or not INSTANT.fullmatch(value.strip()):
        return None
    text = value.strip()
    try:
        t = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1] in "Zz" else text)
    except ValueError:
        raise BadRequest(FORMS, details={"field": "settles"}) from None
    if t.utcoffset() is None:
        raise BadRequest(NO_ZONE, details={"field": "settles"})
    return t


def _unix(value: Any) -> datetime | None:
    """Return the UTC time of a Unix time in seconds (an int or a float, not a bool), or None for another
    value. Raise BadRequest when the number is not finite or is milliseconds or finer."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if math.isfinite(value) and abs(value) < SECS_MAX:
            return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        pass
    raise BadRequest(f"{UNIX}; {value!r} is not one", details={"field": "settles"})


def _utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC" if t.second else "%Y-%m-%d %H:%M UTC")


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _limits(m: Market) -> tuple[int, int | None]:
    """Return the shortest and longest tenor of ``m`` in seconds, with None for no longest."""
    return (m.min_tenor_s if type(m.min_tenor_s) is int else MIN_SECS,
            m.max_tenor_s if type(m.max_tenor_s) is int else None)


def earliest(m: Market, now: datetime) -> datetime:
    """Return ``now`` plus the shortest tenor and ``OPEN_MARGIN_SECS``, rounded up to the minute. An end
    inside the next long close moves to the close's end."""
    t = now + timedelta(seconds=_limits(m)[0] + OPEN_MARGIN_SECS)
    if t.second or t.microsecond:
        t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
    lc = m.next_long_close
    if lc is not None and lc[0] <= t < lc[1]:
        t = lc[1]
    return t.astimezone(timezone.utc)


def _at(t: datetime, m: Market, now: datetime) -> datetime:
    """Return ``t`` unchanged, or raise BadRequest with ``details['next_time']`` when it does not settle."""
    lo, hi = _limits(m)
    lc, ahead = m.next_long_close, (t - now).total_seconds()
    why, nxt = None, None
    if ahead < lo:
        why, nxt = f"{_utc(t)} is less than {lo // 60} minutes from now", earliest(m, now)
    elif lc is not None and lc[0] <= t < lc[1]:
        why, nxt = f"{m.pair} takes no settlement from {_utc(lc[0])} to {_utc(lc[1])}", lc[1]
    elif hi is not None and ahead > hi:
        why = f"{_utc(t)} is past the longest trade: {m.pair} settles at most {hi // 86_400} days from now"
    if why is None:
        return t
    raise BadRequest(why + (f"; the next time that settles is {_utc(nxt)}" if nxt else ""),
                     details={"pair": m.pair, "settles": _iso(t), "min_secs": lo, "max_secs": hi,
                              "next_time": _iso(nxt) if nxt else None})


def settlement(settles: Any, m: Market, now: datetime) -> datetime:
    """Return the UTC settlement time of the exact time ``settles`` on ``m`` at ``now``, or raise BadRequest."""
    t = _instant(settles) or _unix(settles)
    if t is None and isinstance(settles, datetime):
        if settles.utcoffset() is None:
            raise BadRequest(NO_ZONE, details={"field": "settles"})
        t = settles
    if t is None:
        raise BadRequest(FORMS, details={"field": "settles"})
    return _at(t.astimezone(timezone.utc), m, now)


def default(m: Market, now: datetime) -> datetime:
    """Return the settlement when ``settles`` is not given: the pair's fixing one month from now, on the
    first weekday whose fixing is outside the next long close. Raise BadRequest when no day settles."""
    fx = fixing(m)
    if fx is None:
        raise BadRequest(f"/markets names no fixing time for {m.pair}: pass settles", details={"pair": m.pair})
    at, zone = fx
    lo, hi = _limits(m)
    lc = m.next_long_close
    day = _add_months(now.astimezone(zone).date(), 1)
    for _ in range(DAYS_READ):
        t = datetime.combine(day, at, tzinfo=zone).astimezone(timezone.utc)
        if day.weekday() < 5 and not (lc is not None and lc[0] <= t < lc[1]) and (t - now).total_seconds() >= lo:
            break
        day += timedelta(days=1)
    else:
        raise BadRequest(f"{m.pair} settles on no day within {DAYS_READ} days of one month from now: pass settles",
                         details={"pair": m.pair})
    if hi is not None and (t - now).total_seconds() > hi:
        raise BadRequest(f"{day} is past the longest trade: {m.pair} settles at most {hi // 86_400} days from now: "
                         "pass settles", details={"pair": m.pair, "settles": str(day), "max_secs": hi})
    return t
