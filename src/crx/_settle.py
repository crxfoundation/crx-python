"""Resolve ``settles=`` (an hour tenor, a tenor, a date or a time) to a settlement time."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import BadRequest
from .models import Market

TENOR = re.compile(r"([1-9][0-9]{0,2})([DWM])", re.I)
HOURS = re.compile(r"([1-9]|1[0-9]|2[0-3])H", re.I)
INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-9]{2}.*")
MIN_SECS = 3_600  # shortest tenor when /markets gives none
# The gateway checks the tenor on its own clock. 180 s covers a client clock up to 120 s behind the
# gateway's (its timestamp window) and 60 s to open the request.
OPEN_MARGIN_SECS = 180
DAYS_READ = 31  # days to search forward for a settlement day
FORMS = ('settles is a tenor such as "1H", "1W" or "1M", a date such as "2026-12-18", or a time with a '
         'timezone such as "2026-10-24T15:00Z"')
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


def _day(value: Any) -> date:
    """Parse a ``date`` or ``YYYY-MM-DD`` text. Raise BadRequest on any other value."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise BadRequest(FORMS, details={"field": "settles"})


def _instant(value: Any) -> datetime | None:
    """Parse a date-time text, or None for another form. Raise BadRequest when unreadable or without an offset."""
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


def _utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC" if t.second else "%Y-%m-%d %H:%M UTC")


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _limits(m: Market) -> tuple[int, int | None]:
    """Return the shortest and longest tenor of ``m`` in seconds, with None for no longest."""
    return (m.min_tenor_s if type(m.min_tenor_s) is int else MIN_SECS,
            m.max_tenor_s if type(m.max_tenor_s) is int else None)


def is_hours(settles: Any) -> bool:
    """Return True for an hour tenor (``"1H"`` to ``"23H"``), which counts from the send time."""
    return isinstance(settles, str) and HOURS.fullmatch(settles.strip()) is not None


def earliest(m: Market, now: datetime, secs: int = 0) -> datetime:
    """Return ``now`` plus ``secs`` (at least the shortest tenor) and ``OPEN_MARGIN_SECS``, rounded up to
    the minute. An end inside the next long close moves to the close's end."""
    t = now + timedelta(seconds=max(secs, _limits(m)[0]) + OPEN_MARGIN_SECS)
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


class _Rule:
    """Settlement rule of one market at ``now`` (UTC)."""

    def __init__(self, m: Market, at: time, zone: ZoneInfo, now: datetime):
        self.m, self.at, self.zone, self.now = m, at, zone, now
        self.min_secs = m.min_tenor_s if type(m.min_tenor_s) is int else MIN_SECS
        self.max_secs = m.max_tenor_s if type(m.max_tenor_s) is int else None

    def instant(self, day: date) -> datetime:
        """Return the fixing time on ``day`` in UTC."""
        return datetime.combine(day, self.at, tzinfo=self.zone).astimezone(timezone.utc)

    def refusal(self, day: date) -> str | None:
        """Return why ``day`` does not settle, or None. The caller checks the longest tenor."""
        if day.weekday() >= 5:
            return f"{day} is a {day:%A}: {self.m.pair} settles Monday to Friday"
        t = self.instant(day)
        lc = self.m.next_long_close
        if lc is not None and lc[0] <= t < lc[1]:
            return f"{self.m.pair} takes no settlement from {_utc(lc[0])} to {_utc(lc[1])}"
        if (t - self.now).total_seconds() < self.min_secs:
            return f"{_utc(t)} is less than {self.min_secs // 60} minutes from now"
        return None

    def next_day(self, day: date) -> date | None:
        """Return the first settlement day from ``day`` within ``DAYS_READ`` days, or None."""
        for _ in range(DAYS_READ):
            if self.refusal(day) is None:
                return day
            day += timedelta(days=1)
        return None


def settlement(settles: Any, m: Market, now: datetime) -> datetime:
    """Return the UTC settlement time that ``settles`` names on ``m`` at ``now``, or raise BadRequest."""
    settles = _instant(settles) or settles
    if isinstance(settles, datetime):
        if settles.utcoffset() is None:
            raise BadRequest(NO_ZONE, details={"field": "settles"})
        return _at(settles.astimezone(timezone.utc), m, now)
    if is_hours(settles):
        t = earliest(m, now, int(settles.strip()[:-1]) * 3_600)
        hi = _limits(m)[1]
        if hi is not None and (t - now).total_seconds() > hi:
            raise BadRequest(f"{settles.strip()} is past the longest trade: {m.pair} settles at most "
                             f"{hi // 86_400} days from now", details={"pair": m.pair, "max_secs": hi})
        return t
    tenor = TENOR.fullmatch(settles.strip()) if isinstance(settles, str) else None
    day = None if tenor else _day(settles)
    fx = fixing(m)
    if fx is None:
        raise BadRequest(f"/markets names no fixing time for {m.pair}: pass settles as a datetime with a timezone",
                         details={"pair": m.pair})
    rule = _Rule(m, fx[0], fx[1], now)
    if tenor:
        n, unit = int(tenor.group(1)), tenor.group(2).upper()
        today = now.astimezone(rule.zone).date()
        start = _add_months(today, n) if unit == "M" else today + timedelta(days=n * (7 if unit == "W" else 1))
        day = rule.next_day(start)
        if day is None:
            raise BadRequest(f"{m.pair} settles on no day within {DAYS_READ} days of {start}",
                             details={"pair": m.pair, "settles": settles})
    else:
        why = rule.refusal(day)
        if why is not None:
            nxt = rule.next_day(day + timedelta(days=1))
            raise BadRequest(why + (f"; the next date that settles is {nxt}" if nxt else ""),
                             details={"pair": m.pair, "settles": str(day), "next_date": str(nxt) if nxt else None})
    t = rule.instant(day)
    if rule.max_secs is not None and (t - now).total_seconds() > rule.max_secs:
        raise BadRequest(f"{day} is past the longest trade: {m.pair} settles at most "
                         f"{rule.max_secs // 86_400} days from now",
                         details={"pair": m.pair, "settles": str(day), "max_secs": rule.max_secs})
    return t
