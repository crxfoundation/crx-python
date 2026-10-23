"""trade() and withdraw() sign at most once while the first call has no final answer."""

import time

import pytest
import requests

import crx

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


RETRIES = [  # fault after the gateway takes the signed accept, error of the first call
    ("timeout", None),
    ("mark_unavailable", crx.MarkUnavailable),  # the gateway may keep the round after this answer
]


def test_trade_retry_after_a_fault_signs_and_posts_nothing(make_client, venue, session, clock):
    venue.script = [("sending", None)]  # no final answer within the wait
    for fault, err in RETRIES:
        venue.reset()
        faulty_accept(venue, session, fault)
        before = len(accepts(session))
        c = make_client(clock=clock)
        q = c.quote("USD/MXN", "buy", 25_000)
        if err is None:
            assert c.trade(q, wait=30).status == "sending"
        else:
            with pytest.raises(err):
                c.trade(q, wait=30)
        t = c.trade(q, wait=30)
        assert t.status == "sending" and (t.rfq_id, t.quote_id) == (RFQ, QID), fault
        (body,) = accepts(session)[before:]
        assert "sig" in body, fault


FIRST_ENDS = [  # the first trade's final word after its call ended, error of the second quote until it is read
    ("open", crx.EarlierOutcome),
    ("refused", None),
]


def test_no_second_quote_while_the_first_has_no_final_answer(make_client, venue, session, clock):
    faulty_accept(venue, session, "timeout")
    session.routes[("POST", f"/rfqs/{RFQ2}/accept")] = (409, {"code": "round_closed", "error": "closed"})
    for word, err in FIRST_ENDS:
        venue.reset()
        venue.script = [("sending", None)]
        c = make_client(clock=clock)
        q = c.quote("USD/MXN", "buy", 25_000)
        c.trade(q, wait=30)
        second = len(accepts_on(session, RFQ2))
        with pytest.raises(crx.TradeUnknown) as ei:
            c.trade(other_rfq(q), wait=30)
        assert ei.value.details["rfq_id"] == RFQ and "nothing signed" in str(ei.value), word
        venue.script = [(word, TXH if word == "open" else None)]
        if err is not None:
            with pytest.raises(err) as ei:
                c.trade(other_rfq(q), wait=30)
            assert ei.value.details == {"rfq_id": RFQ, "status": word}
        assert len(accepts_on(session, RFQ2)) == second, word
        # The earlier outcome blocks the second quote only until it is read.
        with pytest.raises(crx.QuoteExpired):
            c.trade(other_rfq(q), wait=30)
        assert len(accepts_on(session, RFQ2)) > second, word
    assert len(signed_posts(session)) == len(FIRST_ENDS)


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
