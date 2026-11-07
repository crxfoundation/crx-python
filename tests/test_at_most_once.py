"""trade() and withdraw() sign at most once while the first call has no final answer."""

import time

import pytest
import requests

import crx
from .conftest import WEEK  # noqa: F401

from .conftest import RFQ2, Clock, other_rfq
from .test_money import LANDED, Chain, balance_body, gate, item_row
from .test_quote_trade import QID, RFQ, TXH, accepts, clock, signed_posts, venue  # noqa: F401 (fixtures)


def fault_after(take, fault):
    """Return a route that lets the gateway take the post, then fails with ``fault``."""
    def route(req):
        take(req)
        if fault == "timeout":
            raise requests.ReadTimeout("read timed out")
        if fault == "dropped":
            raise requests.ConnectionError("connection reset by peer")
        if fault == "mark_unavailable":
            return (503, {"code": "mark_unavailable", "error": "no mark", "details": {"pair": "USD/MXN"}})
        return (502, "<html>bad gateway</html>")
    return route


def faulty_accept(venue, session, fault):
    """Fail the signed accept with ``fault`` after the gateway takes it."""
    hit = fault_after(venue.accept, fault)
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = lambda req: hit(req) if "sig" in req["body"] else venue.accept(req)


def accepts_on(session, rfq_id):
    return [x for x in session.calls if x["method"] == "POST" and x["path"] == f"/rfqs/{rfq_id}/accept"]


def faulted_quote(make_client, venue, session, clock, fault):
    """Return a client and its quote; the gateway takes the signed accept, then fails with ``fault``."""
    venue.script = [("sending", None)]  # no final answer within the wait
    faulty_accept(venue, session, fault)
    c = make_client(clock=clock)
    return c, c.quote("USD/MXN", "buy", 25_000, settles=WEEK)


def test_trade_retry_after_a_fault_signs_and_posts_nothing(make_client, venue, session, clock):
    """The fault is a read timeout: the first call reads ``sending``."""
    c, q = faulted_quote(make_client, venue, session, clock, "timeout")
    assert c.trade(q, wait=30).status == "sending"
    t = c.trade(q, wait=30)
    assert t.status == "sending" and (t.rfq_id, t.quote_id) == (RFQ, QID)
    (body,) = accepts(session)
    assert "sig" in body


def test_trade_retry_after_mark_unavailable_signs_and_posts_nothing(make_client, venue, session, clock):
    """The fault is 503 mark_unavailable: the first call raises it, and the gateway may keep the round."""
    c, q = faulted_quote(make_client, venue, session, clock, "mark_unavailable")
    with pytest.raises(crx.MarkUnavailable):
        c.trade(q, wait=30)
    t = c.trade(q, wait=30)
    assert t.status == "sending" and (t.rfq_id, t.quote_id) == (RFQ, QID)
    (body,) = accepts(session)
    assert "sig" in body


def first_trade_with_no_final_answer(make_client, venue, session, clock):
    """Return a client and its quote after one trade call that ends with no final answer.

    The gateway takes the signed accept and the read times out. The second RFQ answers each accept 409.
    """
    faulty_accept(venue, session, "timeout")
    session.routes[("POST", f"/rfqs/{RFQ2}/accept")] = (409, {"code": "round_closed", "error": "closed"})
    venue.script = [("sending", None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, settles=WEEK)
    c.trade(q, wait=30)
    return c, q


def test_no_second_quote_while_the_first_has_no_final_answer(make_client, venue, session, clock):
    """The first trade then reads ``open``: the second quote raises EarlierOutcome once."""
    c, q = first_trade_with_no_final_answer(make_client, venue, session, clock)
    second = len(accepts_on(session, RFQ2))
    with pytest.raises(crx.TradeUnknown) as ei:
        c.trade(other_rfq(q), wait=30)
    assert ei.value.details["rfq_id"] == RFQ and "nothing signed" in str(ei.value)
    venue.script = [("open", TXH)]
    with pytest.raises(crx.EarlierOutcome) as ei:
        c.trade(other_rfq(q), wait=30)
    assert ei.value.details == {"rfq_id": RFQ, "status": "open"}
    assert len(accepts_on(session, RFQ2)) == second
    # The earlier outcome blocks the second quote only until it is read.
    with pytest.raises(crx.QuoteExpired):
        c.trade(other_rfq(q), wait=30)
    assert len(accepts_on(session, RFQ2)) > second
    assert len(signed_posts(session)) == 1


def test_no_second_quote_while_the_first_has_no_final_answer_then_refused(make_client, venue, session, clock):
    """The first trade then reads ``refused``: the second quote goes out with no EarlierOutcome."""
    c, q = first_trade_with_no_final_answer(make_client, venue, session, clock)
    second = len(accepts_on(session, RFQ2))
    with pytest.raises(crx.TradeUnknown) as ei:
        c.trade(other_rfq(q), wait=30)
    assert ei.value.details["rfq_id"] == RFQ and "nothing signed" in str(ei.value)
    venue.script = [("refused", None)]
    assert len(accepts_on(session, RFQ2)) == second
    with pytest.raises(crx.QuoteExpired):
        c.trade(other_rfq(q), wait=30)
    assert len(accepts_on(session, RFQ2)) > second
    assert len(signed_posts(session)) == 1


class Money:
    """Serve GET /balance: nonce 3 and no withdraw until ``shown``, then the item at ``word`` and nonce 4."""

    def __init__(self, account, g):
        self.account, self.g, self.shown, self.word = account, g, False, "accepted"

    def __call__(self, req):
        if not self.shown:
            return balance_body(self.account)
        return balance_body(self.account, nonce="4", withdraw=item_row(self.word, self.g.item, LANDED))


def withdraw_posts(session):
    return [x["body"] for x in session.calls if x["method"] == "POST" and x["path"] == "/withdraw"]


def test_withdraw_retry_after_a_fault_signs_nothing_new(make_client, session, health, account):
    Chain(session)
    g = gate(session, health, account)
    session.routes[("POST", "/withdraw")] = fault_after(g.answer, "5xx")
    money = Money(account, g)
    session.routes[("GET", "/balance")] = money
    c = make_client(clock=Clock(time.time()))
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    money.shown = True
    out = c.withdraw(1000)
    assert (out.status, out.nonce, out.item, out.tx) == ("accepted", 3, g.item, LANDED)
    posts = withdraw_posts(session)
    assert len(posts) == 1 and posts[0]["nonce"] == "3"
