"""Trade finality reads only the explicit ``trade_final`` and ``trade_provisional`` marks."""

from crx import client as cl

from .conftest import fixture
from .test_quote_trade import RFQ, TXH, clock, trade_polls, venue  # noqa: F401 (fixtures)

FIX = fixture("trade-finality.json")


def test_each_view_reads_its_word_answer_and_marks():
    for row in FIX["views"]:
        v = row["view"]
        word = cl._trade_word(v)
        assert (word, cl._answered(word), cl._final(v), cl._provisional(v)) == (
            row["word"], row["answered"], row["final"], row["provisional"]), row["case"]


def test_each_position_row_reads_its_mark(make_client, session):
    for row in FIX["positions"]:
        session.routes[("GET", "/positions")] = {"positions": [row["row"]]}
        (p,) = make_client().positions()
        assert (p.status, p.final) == (row["row"]["status"], row["final"]), row["case"]


def served(venue, session, extra):
    """Serve the venue's view merged with ``extra``, which the test may change in place."""
    session.routes[("GET", f"/rfqs/{RFQ}")] = lambda req: {**venue.view(req), **extra}


def test_a_provisional_open_reads_final_once_marked(make_client, venue, session, clock):
    venue.script = [("open", TXH)]
    extra = {"trade_provisional": True, "trade_final": False}
    served(venue, session, extra)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    t = c.trade(q, wait=30)
    assert (t.provisional, t.final) == (True, False)
    extra.clear()
    extra["trade_final"] = True
    t = c.trade(q, wait=30)
    assert (t.status, t.provisional, t.final) == ("open", False, True)
    n = len(trade_polls(session))
    assert c.trade(q, wait=30).final is True and len(trade_polls(session)) == n
