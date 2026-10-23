"""The settles= argument: tenors, dates and exact times."""

import copy
from datetime import date, datetime, timedelta, timezone

import pytest

import crx
from crx import _settle
from crx.models import Market

from .conftest import open_market
from .test_quote_trade import Venue, clock, venue  # noqa: F401  (fixtures)

UTC = timezone.utc
FIXING = {"city": "Mexico City", "time": "12:00", "zone": "America/Mexico_City", "next_at": 0}


def market(lc=None, min_secs=3600, max_secs=7_776_000, fixing=FIXING):
    raw = {"pair": "USD/MXN"}
    if fixing is not None:
        raw["settlement"] = {"fixing": fixing, "window_secs": 3600}
    return Market(pair="USD/MXN", pair_id="", base="USD", quote="MXN", open=True, paused=False,
                  min_notional=None, max_notional=None, next_open=None, min_tenor_s=min_secs,
                  max_tenor_s=max_secs, raw=raw, next_long_close=lc)


# Thursday 2026-10-22 19:40 UTC (14:40 Mexico City, UTC-6).
NOW = datetime(2026, 10, 22, 19, 40, tzinfo=UTC)
MORNING = datetime(2026, 10, 22, 16, 30, tzinfo=UTC)  # 10:30 Mexico City
LATE = datetime(2026, 10, 22, 17, 30, tzinfo=UTC)  # 30 min before the fixing


def noon_mx(y, m, d):
    return datetime(y, m, d, 18, 0, tzinfo=UTC)  # 12:00 Mexico City


CLOSE_29 = (noon_mx(2026, 10, 29) - timedelta(hours=1), noon_mx(2026, 10, 29) + timedelta(minutes=15))
CLOSE_1H = (NOW + timedelta(minutes=30), NOW + timedelta(hours=3))  # the close holds the end of 1H
WEEKEND = (datetime(2026, 10, 23, 21, 0, tzinfo=UTC), datetime(2026, 10, 25, 22, 15, tzinfo=UTC))

SETTLES = [  # settles, market, now, settlement time
    ("2D", market(), NOW, noon_mx(2026, 10, 26)),             # Saturday moves to Monday
    ("1m", market(), NOW, noon_mx(2026, 11, 23)),             # Sunday 11-22 moves to Monday
    ("1W", market(lc=CLOSE_29), NOW, noon_mx(2026, 10, 30)),  # the long close moves it a day
    ("2026-12-18", market(), NOW, noon_mx(2026, 12, 18)),
    (date(2026, 12, 18), market(), NOW, noon_mx(2026, 12, 18)),
    ("2026-10-22", market(), MORNING, noon_mx(2026, 10, 22)),
    ("1H", market(), NOW, datetime(2026, 10, 22, 20, 43, tzinfo=UTC)),
    ("1H", market(lc=CLOSE_1H), NOW, CLOSE_1H[1]),
    ("2026-10-24T15:00Z", market(), NOW, datetime(2026, 10, 24, 15, 0, tzinfo=UTC)),
    (" 2026-10-24T10:00-05:00 ", market(), NOW, datetime(2026, 10, 24, 15, 0, tzinfo=UTC)),
    ("2026-10-25T22:15Z", market(lc=WEEKEND), NOW, WEEKEND[1]),
    (datetime(2026, 10, 24, 17, 0, tzinfo=timezone(timedelta(hours=2))), market(), NOW,
     datetime(2026, 10, 24, 15, 0, tzinfo=UTC)),
    (datetime(2026, 11, 3, 9, 30, tzinfo=timezone(timedelta(hours=-5))), market(), NOW,
     datetime(2026, 11, 3, 14, 30, tzinfo=UTC)),
]


def test_a_tenor_date_or_exact_time_settles_at_its_time():
    for settles, m, now, want in SETTLES:
        t = _settle.settlement(settles, m, now)
        assert t == want and t.utcoffset() == timedelta(0), (settles, m.next_long_close)


REFUSALS = [  # settles, market, now, words, details
    ("3M", market(), NOW, "past the longest trade", {}),  # 2027-01-22: 92 days
    ("24H", market(), NOW, "settles is a tenor", {}),
    ("2026-10-24T25:00Z", market(), NOW, "settles is a tenor", {}),
    (30, market(), NOW, "settles is a tenor", {}),
    ("2026-10-24", market(), NOW, "2026-10-24 is a Saturday: USD/MXN settles Monday to Friday; the next date that "
     "settles is 2026-10-26", {"next_date": "2026-10-26"}),
    ("2026-10-24T15:00", market(), NOW, 'settles needs a timezone, e.g. "2026-10-24T15:00Z"', {}),
    (datetime(2026, 11, 3, 9, 30), market(), NOW, "settles needs a timezone", {}),
    ("1M", market(fixing={"time": "noon", "zone": "America/Mexico_City"}), NOW,
     "/markets names no fixing time for USD/MXN", {}),
    ("2026-10-29", market(lc=CLOSE_29), NOW, "USD/MXN takes no settlement from 2026-10-29 17:00 UTC to "
     "2026-10-29 18:15 UTC; the next date that settles is 2026-10-30", {}),
    ("2026-10-22", market(), LATE, "less than 60 minutes from now; the next date that settles is 2026-10-23", {}),
    ("23H", market(max_secs=86_400 // 2), NOW, "23H is past the longest trade", {}),
    ("2026-10-24T15:00Z", market(lc=WEEKEND), NOW, "USD/MXN takes no settlement from 2026-10-23 21:00 UTC to "
     "2026-10-25 22:15 UTC; the next time that settles is 2026-10-25 22:15 UTC", {"next_time": "2026-10-25T22:15:00Z"}),
    ("2027-03-01T18:00Z", market(), NOW, "2027-03-01 18:00 UTC is past the longest trade", {"next_time": None}),
]


def test_a_settles_that_does_not_settle_raises():
    for settles, m, now, words, details in REFUSALS:
        with pytest.raises(crx.BadRequest) as e:
            _settle.settlement(settles, m, now)
        assert words in str(e.value) and {k: e.value.details[k] for k in details} == details, settles


def a_weekday(days):
    """Return the first weekday at least ``days`` from today."""
    d = datetime.now(UTC).date() + timedelta(days=days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def noon_on(d):
    return datetime(d.year, d.month, d.day, 18, 0, tzinfo=UTC)


def with_fixing(markets):
    m = copy.deepcopy(markets)
    for row in m["markets"]:
        row["settlement"] = {"fixing": dict(FIXING), "window_secs": 3600}
        row["tenor"]["min_secs"] = 3600
    return m


@pytest.fixture
def fixing_venue(session, health, markets, clock):
    v = Venue(session, health, markets, clock)
    session.routes[("GET", "/markets")] = open_market(with_fixing(markets), "USD/MXN")
    return v


def test_quote_sends_the_fixing_and_names_it_settles_at(make_client, fixing_venue, clock):
    c = make_client(clock=clock)
    d = a_weekday(40)
    q = c.quote("USD/MXN", "buy", 25_000, settles=d.isoformat())
    assert fixing_venue.rfq_body["expiry"] == int(noon_on(d).timestamp() * 1000)
    assert q.settles_at == q.expiry == noon_on(d)
    assert q.settles_at != q.expires_at


def test_settles_and_expiry_together_raise_before_any_post(make_client, fixing_venue, session, clock):
    c = make_client(clock=clock)
    with pytest.raises(crx.BadRequest, match="pass settles or expiry, not both"):
        c.quote("USD/MXN", "buy", 25_000, settles="1M", expiry=timedelta(days=30))
    assert not [x for x in session.calls if x["method"] == "POST"]


def test_an_exact_time_under_the_minimum_raises_before_any_post(make_client, fixing_venue, session, clock):
    soon = datetime.now(UTC) + timedelta(minutes=30)
    with pytest.raises(crx.BadRequest, match="less than 60 minutes from now; the next time that settles is") as e:
        make_client(clock=clock).ask("USD/MXN", "buy", 25_000, settles=soon)
    assert e.value.details["next_time"].endswith(":00Z")
    assert not [x for x in session.calls if x["method"] == "POST"]


# The gateway's 422 on POST /rfqs for a taker whose deposit the collateral check does not count yet.
SCREEN_ERROR = ("free collateral 0.000000 USD does not cover the 12.442982 USD this trade can take from it; "
                "deposit collateral or ask for a smaller notional")
SCREEN_422 = (422, {"error": SCREEN_ERROR, "code": "insufficient_collateral", "outcome": "insufficient_collateral",
                    "message": "Not enough free collateral. Deposit USDC or reduce the size.",
                    "details": {"side": "taker", "required": "12.442982", "available": "0.000000", "currency": "USD",
                                "fold": 41}})


def free_of(account, free):
    """Return a /balance body for ``account`` with ``free`` USDC of free collateral."""
    return {"account": account.address.lower(), "chain": "avax-fuji", "state": "live", "collateral": free,
            "free": free, "equity": free, "im": "0", "mm": "0", "open_legs": 0, "as_of": 1_790_000_000_000,
            "withdraw": {"live": False, "nonce": "0", "last": None}}


def test_1h_sent_again_after_a_collateral_refusal_takes_a_new_end(make_client, venue, session, clock, account):
    session.routes[("POST", "/rfqs")] = [SCREEN_422] * 5 + [venue.open_rfq]
    session.routes[("GET", "/balance")] = free_of(account, "100.000000")
    before, start = datetime.now(UTC), clock()
    q = make_client(clock=clock).quote("USD/MXN", "buy", 25_000, settles="1H")
    posts = [x["body"] for x in session.calls if x["method"] == "POST" and x["path"] == "/rfqs"]
    assert len(posts) == 6 and clock() == start + 75
    assert len({p["client_rfq_id"] for p in posts}) == 1
    assert all({k: v for k, v in p.items() if k != "expiry"} == {k: v for k, v in posts[0].items() if k != "expiry"}
               for p in posts)
    for i, p in enumerate(posts):  # each send: 1 h 3 min past its own time, at least
        assert p["expiry"] >= int(before.timestamp() * 1000) + i * 15_000 + 3_780_000
    assert q.settles_at == datetime.fromtimestamp(posts[-1]["expiry"] / 1000, UTC)
