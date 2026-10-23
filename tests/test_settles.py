"""The settles= argument: an exact UTC time (aware datetime, ISO 8601 with Z or an offset, Unix seconds)."""

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
FORMS = "settles is an exact UTC time: a datetime with a timezone, an ISO 8601 time with Z or an offset"


def market(lc=None, min_secs=3600, max_secs=7_776_000, fixing=FIXING):
    raw = {"pair": "USD/MXN"}
    if fixing is not None:
        raw["settlement"] = {"fixing": fixing, "window_secs": 3600}
    return Market(pair="USD/MXN", pair_id="", base="USD", quote="MXN", open=True, paused=False,
                  min_notional=None, max_notional=None, next_open=None, min_tenor_s=min_secs,
                  max_tenor_s=max_secs, raw=raw, next_long_close=lc)


# Thursday 2026-10-22 19:40 UTC (14:40 Mexico City, UTC-6).
NOW = datetime(2026, 10, 22, 19, 40, tzinfo=UTC)


def noon_mx(y, m, d):
    return datetime(y, m, d, 18, 0, tzinfo=UTC)  # 12:00 Mexico City


CLOSE_23 = (noon_mx(2026, 11, 23) - timedelta(hours=1), noon_mx(2026, 11, 23) + timedelta(minutes=15))
WEEKEND = (datetime(2026, 10, 23, 21, 0, tzinfo=UTC), datetime(2026, 10, 25, 22, 15, tzinfo=UTC))

SETTLES = [  # settles, market, settlement time
    ("2026-10-24T15:00Z", market(), datetime(2026, 10, 24, 15, 0, tzinfo=UTC)),  # a Saturday: never moved
    (" 2026-10-24T10:00-05:00 ", market(), datetime(2026, 10, 24, 15, 0, tzinfo=UTC)),
    ("2026-10-25T22:15Z", market(lc=WEEKEND), WEEKEND[1]),
    ("2026-10-22T20:40Z", market(), NOW + timedelta(hours=1)),  # at the minimum
    (datetime(2026, 10, 24, 17, 0, tzinfo=timezone(timedelta(hours=2))), market(),
     datetime(2026, 10, 24, 15, 0, tzinfo=UTC)),
    (datetime(2026, 11, 3, 9, 30, tzinfo=timezone(timedelta(hours=-5))), market(),
     datetime(2026, 11, 3, 14, 30, tzinfo=UTC)),
    (1792854000, market(), datetime(2026, 10, 24, 15, 0, tzinfo=UTC)),
    (1792854000.25, market(), datetime(2026, 10, 24, 15, 0, 0, 250_000, tzinfo=UTC)),
    (1792966500, market(lc=WEEKEND), WEEKEND[1]),
    (1792854000, market(fixing=None), datetime(2026, 10, 24, 15, 0, tzinfo=UTC)),  # needs no fixing
]


def test_an_exact_time_settles_as_given():
    for settles, m, want in SETTLES:
        t = _settle.settlement(settles, m, NOW)
        assert t == want and t.utcoffset() == timedelta(0), (settles, m.next_long_close)


def test_a_unix_time_in_seconds_is_the_same_instant_as_its_iso_form():
    for iso, secs in [("2026-10-24T15:00Z", 1792854000), ("2026-10-24T10:00-05:00", 1792854000),
                      ("2026-11-03T14:30:15Z", 1793716215)]:
        want = _settle.settlement(iso, market(), NOW)
        for settles in (secs, float(secs)):
            t = _settle.settlement(settles, market(), NOW)
            assert t == want and t.utcoffset() == timedelta(0) and t.timestamp() == secs, (iso, settles)


REMOVED = ["1H", "23H", "24H", "1D", "2D", "1W", "1M", "1m", " 1M ", "2M", "3M", "2026-12-18", "2026-10-22",
           date(2026, 12, 18), timedelta(days=30), True, False, "30", "", None]


def test_a_tenor_hour_tenor_date_or_other_form_raises_naming_the_forms():
    for settles in REMOVED:
        with pytest.raises(crx.BadRequest) as e:
            _settle.settlement(settles, market(), NOW)
        assert str(e.value).startswith(FORMS) and e.value.details == {"field": "settles"}, settles


REFUSALS = [  # settles, market, words, details
    ("2026-10-24T25:00Z", market(), FORMS, {}),
    (30, market(), "1970-01-01 00:00:30 UTC is less than 60 minutes from now", {}),
    (1792854000000, market(), "settles as a number is a Unix time in seconds, UTC", {"field": "settles"}),
    (1792854000000.0, market(), "settles as a number is a Unix time in seconds, UTC", {}),
    (float("nan"), market(), "settles as a number is a Unix time in seconds, UTC", {}),
    ("2026-10-22T20:10Z", market(), "2026-10-22 20:10 UTC is less than 60 minutes from now; the next time that "
     "settles is 2026-10-22 20:43 UTC", {"next_time": "2026-10-22T20:43:00Z", "min_secs": 3600}),
    (int(NOW.timestamp()) + 1800, market(), "2026-10-22 20:10 UTC is less than 60 minutes from now; the next "
     "time that settles is 2026-10-22 20:43 UTC", {"next_time": "2026-10-22T20:43:00Z", "min_secs": 3600}),
    (1792854000, market(lc=WEEKEND), "USD/MXN takes no settlement from 2026-10-23 21:00 UTC",
     {"next_time": "2026-10-25T22:15:00Z"}),
    (1803924000, market(), "2027-03-01 18:00 UTC is past the longest trade", {"next_time": None}),
    ("2026-10-24T15:00", market(), 'settles needs a timezone, e.g. "2026-10-24T15:00Z"', {}),
    (datetime(2026, 11, 3, 9, 30), market(), "settles needs a timezone", {}),
    ("2026-10-24T15:00Z", market(lc=WEEKEND), "USD/MXN takes no settlement from 2026-10-23 21:00 UTC to "
     "2026-10-25 22:15 UTC; the next time that settles is 2026-10-25 22:15 UTC", {"next_time": "2026-10-25T22:15:00Z"}),
    ("2027-03-01T18:00Z", market(), "2027-03-01 18:00 UTC is past the longest trade", {"next_time": None}),
]


def test_a_settles_that_does_not_settle_raises():
    for settles, m, words, details in REFUSALS:
        with pytest.raises(crx.BadRequest) as e:
            _settle.settlement(settles, m, NOW)
        assert words in str(e.value) and {k: e.value.details[k] for k in details} == details, settles


def test_the_default_is_the_fixing_one_month_ahead_on_a_day_that_settles():
    assert _settle.default(market(), NOW) == noon_mx(2026, 11, 23)  # Sunday 11-22 moves to Monday
    assert _settle.default(market(lc=CLOSE_23), NOW) == noon_mx(2026, 11, 24)  # the long close moves it a day
    with pytest.raises(crx.BadRequest, match="past the longest trade"):
        _settle.default(market(max_secs=86_400), NOW)


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


def test_quote_sends_an_iso_time_and_names_it_settles_at(make_client, fixing_venue, clock):
    c = make_client(clock=clock)
    d = a_weekday(40)
    q = c.quote("USD/MXN", "buy", 25_000, settles=noon_on(d).isoformat().replace("+00:00", "Z"))
    assert fixing_venue.rfq_body["expiry"] == int(noon_on(d).timestamp() * 1000)
    assert q.settles_at == q.expiry == noon_on(d)
    assert q.settles_at != q.expires_at


@pytest.mark.parametrize("settles", ["1M", "1H", "2026-12-18"])
def test_quote_and_ask_refuse_a_removed_form_before_any_post(make_client, fixing_venue, session, clock, settles):
    for call in ("quote", "ask"):
        with pytest.raises(crx.BadRequest, match="settles is an exact UTC time"):
            getattr(make_client(clock=clock), call)("USD/MXN", "buy", 25_000, settles=settles)
    assert not [x for x in session.calls if x["method"] == "POST"]


def test_settles_and_expiry_together_raise_before_any_post(make_client, fixing_venue, session, clock):
    c = make_client(clock=clock)
    with pytest.raises(crx.BadRequest, match="pass settles or expiry, not both"):
        c.quote("USD/MXN", "buy", 25_000, settles=1792854000, expiry=timedelta(days=30))
    assert not [x for x in session.calls if x["method"] == "POST"]


def test_quote_and_ask_send_a_unix_time_as_given(make_client, fixing_venue, clock):
    d = a_weekday(3)
    want = datetime(d.year, d.month, d.day, 14, 30, 15, tzinfo=UTC)
    q = make_client(clock=clock).quote("USD/MXN", "buy", 25_000, settles=int(want.timestamp()))
    assert fixing_venue.rfq_body["expiry"] == int(want.timestamp()) * 1000 and q.settles_at == want
    a = make_client(clock=clock).ask("USD/MXN", "buy", 25_000, settles=want.timestamp() + 0.5)
    assert fixing_venue.rfq_body["expiry"] == int(want.timestamp()) * 1000 + 500
    assert a.settles_at == want + timedelta(milliseconds=500)


def test_a_unix_time_under_the_minimum_raises_before_any_post(make_client, fixing_venue, session, clock):
    soon = int(datetime.now(UTC).timestamp()) + 1800
    with pytest.raises(crx.BadRequest, match="less than 60 minutes from now; the next time that settles is") as e:
        make_client(clock=clock).quote("USD/MXN", "buy", 25_000, settles=soon)
    assert e.value.details["next_time"].endswith(":00Z")
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


def test_an_exact_time_sent_again_after_a_collateral_refusal_keeps_its_end(make_client, venue, session, clock,
                                                                           account):
    session.routes[("POST", "/rfqs")] = [SCREEN_422] * 5 + [venue.open_rfq]
    session.routes[("GET", "/balance")] = free_of(account, "100.000000")
    start = clock()
    end = int(datetime.now(UTC).timestamp()) + 2 * 3600
    q = make_client(clock=clock).quote("USD/MXN", "buy", 25_000, settles=end)
    posts = [x["body"] for x in session.calls if x["method"] == "POST" and x["path"] == "/rfqs"]
    assert len(posts) == 6 and clock() == start + 75
    assert all(p == posts[0] for p in posts) and posts[0]["expiry"] == end * 1000
    assert q.settles_at == datetime.fromtimestamp(end, UTC)
