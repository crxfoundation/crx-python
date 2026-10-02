"""The accept answers a taker sees on a binding quote: a dropped quote and its best live quote,
a quote made for another request, the refused-trade limit."""

import time
from decimal import Decimal

import pytest

import crx

from .conftest import Clock
from .test_quote_trade import QID, QID2, RFQ, Venue, accepts, recovers, sent_nothing, signed_posts


@pytest.fixture
def clock():
    return Clock(float(int(time.time())))


@pytest.fixture
def venue(session, health, markets, clock):
    return Venue(session, health, markets, clock)


# ---------- 409 quote_dropped: the best live quote, taken once ----------


def drops(venue, session, best_edit, then=None):
    """The accept on QID answers 409 quote_dropped. ``best_edit`` shapes the best live quote it names
    (None: no best); the best row carries its own trade template. ``then`` answers the accept on that
    best quote; default: the venue."""
    def accept(req):
        body = req["body"]
        if body["quote_id"] == QID:
            best = None
            if best_edit is not None:
                best = dict(venue.row(), **best_edit)
                venue.rates[best["quote_id"]] = best["rate"]
                best["trade_template"] = venue.make_template(best["quote_id"])
            return (409, {"code": "quote_dropped", "error": "the maker dropped this quote", "details": {"best": best}})
        return then(req) if then else venue.accept(req)
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = accept


@pytest.mark.parametrize("side,rate,taken", [
    ("buy", "18.650000", True), ("buy", "18.700000", True), ("buy", "18.750000", False),
    ("sell", "18.750000", True), ("sell", "18.650000", False),
])
def test_dropped_quote_takes_the_best_once(make_client, venue, session, clock, account, side, rate, taken):
    drops(venue, session, {"quote_id": QID2, "rate": rate})
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", side, 25_000)
    assert q.rate == Decimal("18.700000")
    if taken:
        t = c.trade(q)
        assert (t.status, t.quote_id, str(t.rate)) == ("open", QID2, rate)
        first, second = accepts(session)
        assert (first["quote_id"], second["quote_id"]) == (QID, QID2) and set(second) == {"quote_id", "sig"}
    else:
        with pytest.raises(crx.QuoteDropped) as ei:
            c.trade(q)
        e = ei.value
        assert isinstance(e, crx.QuoteExpired) and e.code == "quote_dropped" and e.status == 409
        assert (e.best.quote_id, str(e.best.rate), e.best.rfq_id) == (QID2, rate, RFQ)
        assert len(accepts(session)) == 1
        t = c.trade(e.best)  # the caller's one more call
        assert (t.status, t.quote_id) == ("open", QID2) and len(accepts(session)) == 2
    # The best quote binds this taker's own leg, at its own rate.
    msg = venue.template["typed_data"]["message"]
    assert msg["ownLegId"] == venue.leg and msg["rateE6"] == str(int(Decimal(rate) * 10**6))
    assert recovers(venue.template, accepts(session)[-1]["sig"]) == account.address
    assert sent_nothing(session)


@pytest.mark.parametrize("best_edit", [None, {"quote_id": QID2, "rfq_id": "0x" + "77" * 32}, {"quote_id": QID}])
def test_dropped_quote_without_a_best_raises(make_client, venue, session, clock, best_edit):
    # No best, a best on another RFQ, or the dropped quote itself: nothing more is posted.
    drops(venue, session, best_edit)
    c = make_client(clock=clock)
    with pytest.raises(crx.QuoteDropped) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.best is None and len(accepts(session)) == 1 and sent_nothing(session)


@pytest.mark.parametrize("edit", [{"expires_at": 1}, {"status": "dropped"}, {"status": None}])
def test_a_dropped_quotes_best_must_be_live(make_client, venue, session, clock, edit):
    def accept(req):
        best = {k: v for k, v in dict(venue.row(), quote_id=QID2, **edit).items() if v is not None}
        return (409, {"code": "quote_dropped", "error": "the maker dropped this quote", "details": {"best": best}})
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = accept
    c = make_client(clock=clock)
    with pytest.raises(crx.QuoteDropped) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.best is None and len(accepts(session)) == 1


def test_dropped_twice_raises(make_client, venue, session, clock):
    drops(venue, session, {"quote_id": QID2, "rate": "18.600000"},
          then=lambda req: (409, {"code": "quote_dropped", "error": "dropped", "details": {"best": None}}))
    c = make_client(clock=clock)
    with pytest.raises(crx.QuoteDropped) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.best is None
    assert [b["quote_id"] for b in accepts(session)] == [QID, QID2] and sent_nothing(session)


def test_dropped_slug_in_error_field(make_client, venue, session, clock):
    # The same answer with the code in `error` and `best` at the top level.
    def accept(req):
        if req["body"]["quote_id"] == QID:
            return (409, {"error": "quote_dropped", "best": None})
        return venue.accept(req)
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = accept
    c = make_client(clock=clock)
    with pytest.raises(crx.QuoteDropped):
        c.trade(c.quote("USD/MXN", "buy", 25_000))


# ---------- other accept answers: typed, no retry, nothing sent ----------


def test_quote_not_yours(make_client, venue, session, clock):
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (
        409, {"code": "quote_not_yours", "error": "this quote was made for another request"})
    c = make_client(clock=clock)
    with pytest.raises(crx.QuoteNotYours) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.code == "quote_not_yours" and not isinstance(ei.value, crx.TradeUnknown)
    assert len(accepts(session)) == len(signed_posts(session)) == 1 and sent_nothing(session)


@pytest.mark.parametrize("details,headers,wait", [
    ({"retry_after_secs": 42}, {"Retry-After": "42"}, 42.0),
    ({}, {"Retry-After": "17"}, 17.0),
    ({}, {}, None),
])
def test_rate_limited(make_client, venue, session, clock, details, headers, wait):
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (
        429, {"code": "rate_limited", "error": "too many refused trades this hour", "details": details}, headers)
    c = make_client(clock=clock)
    with pytest.raises(crx.RateLimited) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.retry_after == wait and ei.value.status == 429
    assert len(accepts(session)) == 1 and sent_nothing(session)


# ---------- quote(): a dropped pick is never returned ----------


@pytest.mark.parametrize("live", [True, False])  # False: the positive control, no live quote left
def test_dropped_pick_is_skipped(make_client, venue, session, clock, live):
    venue.waited = False
    session.routes[("GET", f"/rfqs/{RFQ}")] = lambda req: {
        "quote": dict(venue.row(), status="dropped"),
        "quotes": [dict(venue.row(), quote_id=QID2)] if live else [dict(venue.row(), status="dropped")]}
    c = make_client(clock=clock)
    t0 = clock()
    if live:
        assert c.quote("USD/MXN", "buy", 25_000, wait=10).quote_id == QID2
        assert clock() == t0
    else:
        with pytest.raises(crx.NoQuotes):
            c.quote("USD/MXN", "buy", 25_000, wait=3)
