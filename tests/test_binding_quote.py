"""Binding quotes: the chain's sign_mode, and the accept answers a taker sees."""

import copy
import logging
import time
from decimal import Decimal

import pytest

import crx

from .conftest import Clock
from .test_quote_trade import LEG, QID, RFQ, Venue, accepts, recovers, sent_nothing

QID2 = "0x" + "66" * 32


@pytest.fixture
def clock():
    return Clock(time.time())


def serve_mode(session, health, mode, name="key"):
    """/health with the Fuji row's sign_mode set (None: no field); ``name`` is the row's chain field."""
    h = copy.deepcopy(health)
    for c in h["chains"]:
        if mode is not None and c["key"] == "avax-fuji":
            c["sign_mode"] = mode
        if name != "key":
            c[name] = c.pop("key")
    session.routes[("GET", "/health")] = h


def venue_in(mode, session, health, markets, clock, sign_mode="quote", name="key"):
    v = Venue(session, health, markets, clock, mode=mode)
    serve_mode(session, health, sign_mode, name)
    return v


def health_reads(session):
    return session.paths("GET").count("/health")


# ---------- sign_mode: the lapse mirror stays only where the maker still signs a Side ----------


@pytest.mark.parametrize("mode", ["one_call", "legacy"])
@pytest.mark.parametrize("sign_mode,lapse", [(None, True), ("side", True), ("other", True), ("quote", False)])
def test_lapse_mirror_only_in_side_mode(make_client, session, health, markets, clock, account, caplog,
                                        mode, sign_mode, lapse):
    # None and "side" are the positive controls: the flow and the log are the trade-sign ones.
    venue = venue_in(mode, session, health, markets, clock, sign_mode)
    caplog.set_level(logging.INFO, logger="crx")
    c = make_client(clock=clock)
    t = c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert (t.status, t.quote_id) == ("open", QID) and sent_nothing(session)
    assert recovers(venue, venue.template, venue.side_sig) == account.address
    assert ("the maker signs by" in caplog.text) is lapse
    assert ("the maker's quote binds" in caplog.text) is (not lapse and mode == "one_call")
    assert c._sign_mode() == ("quote" if sign_mode == "quote" else "side")
    assert health_reads(session) == 1  # read once per client


def test_health_row_named_chain(make_client, session, health, markets, clock, caplog):
    venue_in("one_call", session, health, markets, clock, "quote", name="chain")
    caplog.set_level(logging.INFO, logger="crx")
    c = make_client(clock=clock)
    assert c.trade(c.quote("USD/MXN", "buy", 25_000)).status == "open"
    assert c._sign_mode() == "quote" and "the maker signs by" not in caplog.text


# ---------- 409 quote_dropped: the best live quote, taken once ----------


def drops(venue, session, best_edit, then=None):
    """The accept on QID answers 409 quote_dropped. ``best_edit`` shapes the best live quote it names
    (None: no best). ``then`` answers the accept on that best quote; default: the venue."""
    def accept(req):
        body = req["body"]
        if body["quote_id"] == QID:
            best = None
            if best_edit is not None:
                venue.row_edit = dict(best_edit)
                best = venue.row()
                best["side_template"] = venue.draft = venue.make_template(
                    venue.taker_arm(req["headers"]["x-crx-address"]))
            return (409, {"code": "quote_dropped", "error": "the maker dropped this quote",
                          "details": {"best": best}})
        return then(req) if then else venue.accept(req)
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = accept


@pytest.mark.parametrize("side,rate,taken", [
    ("buy", "18.650000", True), ("buy", "18.700000", True), ("buy", "18.750000", False),
    ("sell", "18.750000", True), ("sell", "18.650000", False),
])
def test_dropped_quote_takes_the_best_once(make_client, session, health, markets, clock, account, side, rate, taken):
    venue = venue_in("one_call", session, health, markets, clock)
    drops(venue, session, {"quote_id": QID2, "rate": rate})
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", side, 25_000)
    assert q.rate == Decimal("18.700000")
    if taken:
        t = c.trade(q)
        assert (t.status, t.quote_id, str(t.rate)) == ("open", QID2, rate)
        first, second = accepts(session)
        assert (first["quote_id"], second["quote_id"]) == (QID, QID2) and set(second) == {"quote_id", "sig"}
        assert recovers(venue, venue.template, second["sig"]) == account.address
        assert venue.template["own_leg_id"] == LEG  # the same taker leg: the best quote binds this taker too
    else:
        with pytest.raises(crx.QuoteDropped) as ei:
            c.trade(q)
        e = ei.value
        assert isinstance(e, crx.QuoteExpired) and e.code == "quote_dropped" and e.status == 409
        assert (e.best.quote_id, str(e.best.rate), e.best.rfq_id) == (QID2, rate, RFQ)
        assert len(accepts(session)) == 1
        t = c.trade(e.best)  # the caller's one more call
        assert (t.status, t.quote_id) == ("open", QID2) and len(accepts(session)) == 2
    assert f"/rfqs/{RFQ}/side" not in session.paths("POST") and sent_nothing(session)


@pytest.mark.parametrize("best_edit", [None, {"quote_id": QID2, "rfq_id": "0x" + "77" * 32}, {"quote_id": QID}])
def test_dropped_quote_without_a_best_raises(make_client, session, health, markets, clock, best_edit):
    # No best, a best on another RFQ, or the dropped quote itself: nothing more is posted.
    venue = venue_in("one_call", session, health, markets, clock)
    drops(venue, session, best_edit)
    c = make_client(clock=clock)
    with pytest.raises(crx.QuoteDropped) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.best is None and len(accepts(session)) == 1 and sent_nothing(session)


def test_dropped_twice_raises(make_client, session, health, markets, clock):
    venue = venue_in("one_call", session, health, markets, clock)
    drops(venue, session, {"quote_id": QID2, "rate": "18.600000"},
          then=lambda req: (409, {"code": "quote_dropped", "error": "dropped", "details": {"best": None}}))
    c = make_client(clock=clock)
    with pytest.raises(crx.QuoteDropped) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.best is None
    assert [b["quote_id"] for b in accepts(session)] == [QID, QID2] and sent_nothing(session)


def test_dropped_slug_in_error_field(make_client, session, health, markets, clock):
    # The same answer with the code in `error` and `best` at the top level.
    venue = venue_in("one_call", session, health, markets, clock)

    def accept(req):
        if req["body"]["quote_id"] == QID:
            return (409, {"error": "quote_dropped", "best": None})
        return venue.accept(req)
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = accept
    c = make_client(clock=clock)
    with pytest.raises(crx.QuoteDropped):
        c.trade(c.quote("USD/MXN", "buy", 25_000))


# ---------- other accept answers: typed, no retry, nothing sent ----------


@pytest.mark.parametrize("mode", ["quote", "side"])
def test_quote_not_yours(make_client, session, health, markets, clock, mode):
    venue_in("one_call", session, health, markets, clock, mode)
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (
        409, {"code": "quote_not_yours", "error": "this quote was made for another request"})
    c = make_client(clock=clock)
    with pytest.raises(crx.QuoteNotYours) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.code == "quote_not_yours" and not isinstance(ei.value, crx.TradeUnknown)
    assert len(accepts(session)) == 1 and sent_nothing(session)


@pytest.mark.parametrize("details,headers,wait", [
    ({"retry_after_secs": 42}, {"Retry-After": "42"}, 42.0),
    ({}, {"Retry-After": "17"}, 17.0),
    ({}, {}, None),
])
def test_rate_limited(make_client, session, health, markets, clock, details, headers, wait):
    venue_in("one_call", session, health, markets, clock)
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (
        429, {"code": "rate_limited", "error": "too many refused trades this hour", "details": details}, headers)
    c = make_client(clock=clock)
    with pytest.raises(crx.RateLimited) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.retry_after == wait and ei.value.status == 429
    assert len(accepts(session)) == 1 and sent_nothing(session)


def test_stale_nonce_re_signs_in_quote_mode(make_client, session, health, markets, clock, account):
    venue = venue_in("one_call", session, health, markets, clock)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    first = q.raw["side_template"]
    venue.stale = 1
    assert c.trade(q).status == "open"
    a, b = accepts(session)
    assert recovers(venue, first, a["sig"]) == account.address
    assert recovers(venue, venue.template, b["sig"]) == account.address
    assert venue.template["own_leg_id"] == first["own_leg_id"] == LEG
    assert int(venue.template["own_nonce"]) > int(first["own_nonce"])


# ---------- quote(): a dropped pick is never returned ----------


@pytest.mark.parametrize("live", [True, False])  # False: the positive control, no live quote left
def test_dropped_pick_is_skipped(make_client, session, health, markets, clock, live):
    venue = venue_in("one_call", session, health, markets, clock)
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


# ---------- trade templates where the maker's quote binds ----------


@pytest.mark.parametrize("mode", ["one_call", "legacy"])
def test_trade_template_in_quote_mode(make_client, session, health, markets, clock, account, mode):
    venue = venue_in(mode, session, health, markets, clock)
    venue.kind = "trade"
    c = make_client(clock=clock)
    assert c.trade(c.quote("USD/MXN", "sell", 25_000)).status == "open"
    msg = venue.template["typed_data"]["message"]
    assert msg["side"] == "sell" and msg["summary"].startswith("sell 25 000 USD vs MXN at 18.7 MXN per USD")
    assert recovers(venue, venue.template, venue.side_sig) == account.address


def test_dropped_quote_best_signs_its_own_trade(make_client, session, health, markets, clock, account):
    venue = venue_in("one_call", session, health, markets, clock)
    venue.kind = "trade"
    drops(venue, session, {"quote_id": QID2, "rate": "18.650000"})
    c = make_client(clock=clock)
    t = c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert (t.status, t.quote_id) == ("open", QID2)
    assert venue.template["typed_data"]["message"]["rateE6"] == "18650000"  # the best quote's rate, not the first
    assert recovers(venue, venue.template, accepts(session)[-1]["sig"]) == account.address


def test_stale_trade_template_is_checked_again(make_client, session, health, markets, clock, account):
    venue = venue_in("one_call", session, health, markets, clock)
    venue.kind = "trade"
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    venue.stale = 1
    venue.on_stale = lambda: setattr(venue, "typed_edit", lambda td: td["message"].update(rateE6="18800000"))
    with pytest.raises(crx.TradeUnknown, match="differs at message.rateE6"):
        c.trade(q)  # the first Trade was signed and posted: never "nothing happened"
    assert len([b for b in accepts(session) if "sig" in b]) == 1
