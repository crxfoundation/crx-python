"""Trade finality reads explicit fields only. ``open`` or ``refused`` answers the accept and ends the hold, as
before. ``final`` reads only a mark the view sends: a missing key, an ``open`` or elapsed time is never final.
``provisional`` reads only ``trade_provisional``. Fixtures: (a) provisional, (b) an open with no key, (c) the
API17 and API18 shapes."""

import dataclasses

import pytest

import crx
from crx import client as cl

from .conftest import Clock, fixture
from .test_maker import TXH as MAKER_TXH
from .test_maker import Gateway, new_maker, rfq_obj
from .test_maker import T0 as MAKER_T0
from .test_quote_trade import RFQ, TXH, accepts, clock, trade_polls, venue  # noqa: F401 (fixtures)

FIX = fixture("trade-finality.json")
RFQ2 = "0x" + "12" * 32


@pytest.mark.parametrize("row", FIX["views"], ids=[r["case"] for r in FIX["views"]])
def test_each_view_reads_its_word_answer_and_marks(row):
    v = row["view"]
    word = cl._trade_word(v)
    assert (word, cl._answered(word), cl._final(v), cl._provisional(v)) == (
        row["word"], row["answered"], row["final"], row["provisional"])


def test_no_view_is_final_without_a_mark():
    # (b) and (c): no key, and every word, still reads not final.
    for row in FIX["views"]:
        assert cl._final(row["view"]) is False


def other_rfq(q):
    return dataclasses.replace(q, rfq_id=RFQ2, rfq={**q.rfq, "rfq_id": RFQ2})


def served(venue, session, extra):
    """The venue's view with ``extra`` members (a dict changed in place by the test)."""
    session.routes[("GET", f"/rfqs/{RFQ}")] = lambda req: {**venue.view(req), **extra}


def test_a_provisional_open_reads_open_provisional_and_ends_the_hold(make_client, venue, session, clock):
    # (a): the hold ends on the open, as before; the open is marked provisional.
    venue.script = [("open", TXH)]
    extra = {"trade_provisional": True}
    served(venue, session, extra)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    t = c.trade(q)
    assert (t.status, t.tx, t.provisional) == ("open", TXH, True)
    session.routes[("POST", f"/rfqs/{RFQ2}/accept")] = (409, {"code": "round_closed", "error": "closed"})
    with pytest.raises(crx.QuoteExpired):
        c.trade(other_rfq(q))  # not held: the second quote goes to the gateway
    assert len([x for x in session.calls if x["path"] == f"/rfqs/{RFQ2}/accept"]) >= 1


def test_an_open_with_no_key_is_read_again(make_client, venue, session, clock):
    # (b): no key never reads final, so a repeat call reads the view again.
    venue.script = [("open", TXH)]
    extra = {}
    served(venue, session, extra)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert (c.trade(q).status, c._accepts[RFQ]["final"]) == ("open", False)
    n = len(trade_polls(session))
    t = c.trade(q)
    assert (t.status, t.provisional) == ("open", False) and len(trade_polls(session)) == n + 1
    assert len(accepts(session)) == 1


def test_a_repeat_call_reads_the_provisional_mark_drop(make_client, venue, session, clock):
    venue.script = [("open", TXH)]
    extra = {"trade_provisional": True}
    served(venue, session, extra)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert c.trade(q).provisional is True
    extra.clear()
    t = c.trade(q)
    assert (t.status, t.provisional) == ("open", False) and len(accepts(session)) == 1


def test_a_taken_back_open_holds_the_next_quote_again(make_client, venue, session, clock):
    # trade.retracted: the view reads pending again. The repeat call reads it; the hold is back.
    venue.script = [("open", TXH)]
    extra = {"trade_provisional": True}
    served(venue, session, extra)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert c.trade(q).status == "open"
    venue.script = [("pending", None)]
    extra.clear()
    assert c.trade(q).status == "pending"
    with pytest.raises(crx.TradeUnknown):
        c.trade(other_rfq(q))
    venue.script = [("open", TXH)]
    with pytest.raises(crx.EarlierOutcome) as ei:
        c.trade(other_rfq(q))
    assert ei.value.details == {"rfq_id": RFQ, "status": "open"} and len(accepts(session)) == 1


def test_a_failed_reread_keeps_the_open(make_client, venue, session, clock):
    venue.script = [("open", TXH)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert c.trade(q).status == "open"
    session.routes[("GET", f"/rfqs/{RFQ}")] = (503, {"code": "upstream", "error": "down"})
    assert (c.trade(q).status, c._accepts[RFQ]["told"]) == ("open", True)


def test_trades_keeps_the_provisional_key_as_served(make_client, session):
    session.routes[("GET", "/trades")] = {"trades": FIX["events"], "seq": 9}
    c = make_client()
    got = [(e.type, e.data.get("provisional")) for e in c.trades()]
    assert got == [("trade.opened", True), ("trade.retracted", None), ("trade.opened", None)]


@pytest.mark.parametrize("extra, want", [({"trade_provisional": True}, True), ({}, False)])
def test_maker_confirm_reads_the_provisional_mark(session, health, tmp_path, extra, want):
    clock = Clock(MAKER_T0)
    maker = new_maker(session, health, tmp_path, clock)
    gw = Gateway(session, clock)
    sent = maker.send_quote(rfq_obj(), "5.41")
    gw.accept()
    session.routes[("GET", f"/rfqs/{sent.rfq_id}")] = lambda req: {**gw.view(req), **extra}
    t = maker.confirm(sent)
    assert (t.status, t.tx, t.provisional) == ("open", MAKER_TXH, want)
