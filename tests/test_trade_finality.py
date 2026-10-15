"""Trade finality reads explicit fields only. ``open`` or ``refused`` answers the accept and ends the hold, as
before. ``final`` reads only the mark (``trade_final``; ``final`` on a /positions row) on an ``open``: a missing
mark, an ``open`` or elapsed time is never final. ``provisional`` reads only ``trade_provisional``. Fixtures:
(a) provisional, (b) an open not final yet, (c) API17 and API18 shapes with no mark, (d) final, (e) bad marks."""

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


@pytest.mark.parametrize("row", FIX["positions"], ids=[r["case"] for r in FIX["positions"]])
def test_each_position_row_reads_its_mark(make_client, session, row):
    session.routes[("GET", "/positions")] = {"positions": [row["row"]]}
    (p,) = make_client().positions()
    assert (p.status, p.final) == (row["row"]["status"], row["final"])


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


def test_trades_keeps_the_final_marks_as_served(make_client, session):
    session.routes[("GET", "/trades")] = {"trades": FIX["events_api19"], "seq": 10}
    got = [(e.type, e.data.get("provisional"), e.data.get("final")) for e in make_client().trades()]
    assert got == [("trade.opened", True, False), ("trade.retracted", None, None), ("trade.opened", None, False),
                   ("trade.final", None, True)]


def test_a_final_trade_reads_final_and_is_never_read_again(make_client, venue, session, clock):
    # (d): trade_final true on an open. No take-back follows: a repeat call reads nothing.
    venue.script = [("open", TXH)]
    served(venue, session, {"trade_final": True})
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    t = c.trade(q)
    assert (t.status, t.provisional, t.final) == ("open", False, True)
    n = len(trade_polls(session))
    again = c.trade(q)
    assert (again.status, again.final) == ("open", True) and len(trade_polls(session)) == n


def test_a_provisional_open_reads_final_once_marked(make_client, venue, session, clock):
    # (a) then (d): the repeat call reads the mark; from then on it reads nothing.
    venue.script = [("open", TXH)]
    extra = {"trade_provisional": True, "trade_final": False}
    served(venue, session, extra)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    t = c.trade(q)
    assert (t.provisional, t.final) == (True, False)
    extra.clear()
    extra["trade_final"] = True
    t = c.trade(q)
    assert (t.status, t.provisional, t.final) == ("open", False, True)
    n = len(trade_polls(session))
    assert c.trade(q).final is True and len(trade_polls(session)) == n


def test_past_the_quote_end_a_final_position_reads_final(make_client, venue, session, clock):
    venue.script = [("sending", None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert c.trade(q).status == "sending"
    session.routes[("GET", f"/rfqs/{RFQ}")] = (503, {"code": "upstream", "error": "down"})
    row = dict(FIX["positions"][2]["row"], rfq_id=RFQ)
    session.routes[("GET", "/positions")] = {"positions": [row]}
    clock.t = q.rfq["quote_expiry_max"] + 121
    t = c.trade(q)
    assert (t.status, t.final) == ("open", True)


@pytest.mark.parametrize("extra, want", [
    ({"trade_provisional": True, "trade_final": False}, (True, False)),
    ({"trade_provisional": True}, (True, False)),
    ({"trade_final": True}, (False, True)),
    ({}, (False, False)),
])
def test_maker_confirm_reads_the_marks(session, health, tmp_path, extra, want):
    clock = Clock(MAKER_T0)
    maker = new_maker(session, health, tmp_path, clock)
    gw = Gateway(session, clock)
    sent = maker.send_quote(rfq_obj(), "5.41")
    gw.accept()
    session.routes[("GET", f"/rfqs/{sent.rfq_id}")] = lambda req: {**gw.view(req), **extra}
    t = maker.confirm(sent)
    assert (t.status, t.tx, (t.provisional, t.final)) == ("open", MAKER_TXH, want)
