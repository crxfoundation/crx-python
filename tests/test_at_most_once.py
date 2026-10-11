"""trade() and withdraw() at most once: a signed accept or intent is never signed again, and no other quote
is taken, while the first has no final answer. Faults after the gateway took the post: a timeout, a 5xx,
a dropped connection."""

import dataclasses
import time

import pytest
import requests

import crx

from .conftest import Clock
from .test_money import LANDED, Chain, balance_body, gate, item_row
from .test_quote_trade import QID, RFQ, TXH, accepts, clock, signed_posts, venue  # noqa: F401 (fixtures)

RFQ2 = "0x" + "12" * 32
FAULTS = ["timeout", "5xx", "dropped"]


def fault_after(take, fault):
    """A route that runs ``take`` (the gateway takes the post), then answers with ``fault``."""
    def route(req):
        take(req)
        if fault == "timeout":
            raise requests.ReadTimeout("read timed out")
        if fault == "dropped":
            raise requests.ConnectionError("connection reset by peer")
        return (502, "<html>bad gateway</html>")
    return route


def faulty_accept(venue, session, fault):
    """The signed accept is taken, then ``fault``; the unsigned ask is answered as usual."""
    hit = fault_after(venue.accept, fault)
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = lambda req: hit(req) if "sig" in req["body"] else venue.accept(req)


def other_rfq(q):
    """The same terms on another RFQ: a second quote."""
    return dataclasses.replace(q, rfq_id=RFQ2, rfq={**q.rfq, "rfq_id": RFQ2})


def accepts_on(session, rfq_id):
    return [x for x in session.calls if x["method"] == "POST" and x["path"] == f"/rfqs/{rfq_id}/accept"]


# ---------- trade ----------


@pytest.mark.parametrize("fault", FAULTS)
def test_trade_retry_after_a_fault_signs_and_posts_nothing(make_client, venue, session, clock, fault):
    faulty_accept(venue, session, fault)
    venue.script = [("sending", None)]  # no final answer within the wait
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert c.trade(q).status == "sending"
    assert len(signed_posts(session)) == 1
    t = c.trade(q)  # the retry
    assert t.status == "sending" and (t.rfq_id, t.quote_id) == (RFQ, QID)
    assert len(accepts(session)) == 1 and len(signed_posts(session)) == 1


@pytest.mark.parametrize("fault", FAULTS)
def test_trade_retry_reads_the_final_answer(make_client, venue, session, clock, fault):
    faulty_accept(venue, session, fault)
    venue.script = [("sending", None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    c.trade(q)
    venue.script = [("open", TXH)]
    t = c.trade(q)
    assert (t.status, t.tx) == ("open", TXH)
    assert len(accepts(session)) == 1


@pytest.mark.parametrize("fault", FAULTS)
def test_no_second_quote_while_the_first_has_no_final_answer(make_client, venue, session, clock, fault):
    faulty_accept(venue, session, fault)
    venue.script = [("sending", None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    c.trade(q)
    with pytest.raises(crx.TradeUnknown) as ei:
        c.trade(other_rfq(q))
    assert ei.value.details["rfq_id"] == RFQ and "nothing signed" in str(ei.value)
    assert accepts_on(session, RFQ2) == [] and len(signed_posts(session)) == 1


@pytest.mark.parametrize("fault", FAULTS)
def test_an_unread_final_answer_stops_the_second_quote_once(make_client, venue, session, clock, fault):
    faulty_accept(venue, session, fault)
    venue.script = [("sending", None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    c.trade(q)
    venue.script = [("open", TXH)]  # the first trade opened after the call ended
    with pytest.raises(crx.EarlierOutcome) as ei:
        c.trade(other_rfq(q))
    assert ei.value.details == {"rfq_id": RFQ, "status": "open"}
    assert accepts_on(session, RFQ2) == []
    # Read once: the next call proceeds to the second quote.
    session.routes[("POST", f"/rfqs/{RFQ2}/accept")] = (409, {"code": "round_closed", "error": "closed"})
    with pytest.raises(crx.QuoteExpired):
        c.trade(other_rfq(q))
    assert len(accepts_on(session, RFQ2)) >= 1


def test_a_refused_first_trade_lets_the_second_quote_go(make_client, venue, session, clock):
    faulty_accept(venue, session, "timeout")
    venue.script = [("sending", None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    c.trade(q)
    venue.script = [("refused", None)]
    session.routes[("POST", f"/rfqs/{RFQ2}/accept")] = (409, {"code": "round_closed", "error": "closed"})
    with pytest.raises(crx.QuoteExpired):
        c.trade(other_rfq(q))
    assert len(accepts_on(session, RFQ2)) >= 1


def test_past_the_quote_end_positions_give_the_final_answer(make_client, venue, session, clock):
    # The RFQ view keeps no final answer: past the quote end plus 120 s, no listed position means no trade.
    faulty_accept(venue, session, "timeout")
    venue.script = [("sending", None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    c.trade(q)
    with pytest.raises(crx.TradeUnknown):
        c.trade(other_rfq(q))
    session.routes[("GET", "/positions")] = {"positions": []}
    clock.t = q.rfq["quote_expiry_max"] + 121
    t = c.trade(q)
    assert t.status == "refused" and len(accepts(session)) == 1


def test_past_the_quote_end_an_unreadable_view_defers_to_positions(make_client, venue, session, clock):
    # No RFQ view past the quote end: a listed position is the trade, never "refused".
    faulty_accept(venue, session, "dropped")
    venue.script = [("sending", None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    c.trade(q)
    del session.routes[("GET", f"/rfqs/{RFQ}")]  # 404 from here on
    session.routes[("GET", "/positions")] = {"positions": [
        {"trade_id": "0x" + "ab" * 32, "rfq_id": RFQ.upper().replace("0X", "0x"), "pair": "USD/MXN", "side": 1,
         "notional": "25000", "rate": "18.7", "status": "open"}]}
    clock.t = q.rfq["quote_expiry_max"] + 121
    with pytest.raises(crx.EarlierOutcome) as ei:
        c.trade(other_rfq(q))
    assert ei.value.details == {"rfq_id": RFQ, "status": "open"}
    assert c.trade(q).status == "open" and len(accepts(session)) == 1


def test_mark_unavailable_after_the_signature_keeps_the_rfq(make_client, venue, session, clock):
    # The gateway can keep the round on this answer: a retry reads, it never posts the accept again.
    def accept(req):
        if "sig" in req["body"]:
            venue.accept(req)
            return (503, {"code": "mark_unavailable", "error": "no mark", "details": {"pair": "USD/MXN"}})
        return venue.accept(req)
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = accept
    venue.script = [("sending", None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    with pytest.raises(crx.MarkUnavailable):
        c.trade(q)
    assert c.trade(q).status == "sending"
    assert len(signed_posts(session)) == 1


def test_a_200_to_the_unsigned_ask_is_trade_unknown(make_client, venue, session, clock):
    # The gateway answers an RFQ it holds an accept on with that accept: never "nothing happened".
    venue.winner = False
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = {"rfq_id": RFQ, "quote_id": QID}
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, wait=1)
    with pytest.raises(crx.TradeUnknown, match="holds an accept"):
        c.trade(q)
    assert signed_posts(session) == []


# ---------- withdraw ----------


class Money:
    """GET /balance: before ``shown``, the seat's nonce 3 and no withdraw; after, the queued item at ``word``
    and the nonce at 4."""

    def __init__(self, account, g):
        self.account, self.g, self.shown, self.word = account, g, False, "accepted"

    def __call__(self, req):
        if not self.shown:
            return balance_body(self.account)
        return balance_body(self.account, nonce="4", withdraw=item_row(self.word, self.g.item, LANDED))


def withdraw_posts(session):
    return [x["body"] for x in session.calls if x["method"] == "POST" and x["path"] == "/withdraw"]


@pytest.mark.parametrize("fault", FAULTS)
def test_withdraw_retry_after_a_fault_signs_nothing_new(make_client, session, health, account, fault):
    Chain(session)
    g = gate(session, health, account)
    session.routes[("POST", "/withdraw")] = fault_after(g.answer, fault)
    money = Money(account, g)
    session.routes[("GET", "/balance")] = money
    c = make_client(clock=Clock(time.time()))
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)  # the item does not show within the wait
    money.shown = True  # the first intent was taken and paid; the seat's nonce moved to 4
    out = c.withdraw(1000)  # the retry
    assert (out.status, out.nonce, out.item, out.tx) == ("accepted", 3, g.item, LANDED)
    posts = withdraw_posts(session)
    assert len(posts) == 1 and posts[0]["nonce"] == "3"


@pytest.mark.parametrize("fault", FAULTS)
def test_withdraw_not_taken_posts_the_same_intent_again(make_client, session, health, account, fault):
    # The first post never reached the gateway: the retry posts the same signed intent, never a new one.
    Chain(session)
    g = gate(session, health, account)
    money = Money(account, g)
    lost = fault_after(lambda req: None, fault)

    def post(req):
        if not withdraw_posts(session)[:-1]:
            return lost(req)
        money.shown = True
        return g.answer(req)
    session.routes[("POST", "/withdraw")] = post
    session.routes[("GET", "/balance")] = money
    c = make_client(clock=Clock(time.time()))
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    out = c.withdraw(1000)
    assert out.status == "accepted"
    first, again = withdraw_posts(session)
    assert first == again  # the same nonce, deadline and signature


@pytest.mark.parametrize("fault", FAULTS)
def test_withdraw_of_another_amount_signs_nothing_while_the_first_is_open(make_client, session, health, account,
                                                                         fault):
    Chain(session)
    g = gate(session, health, account)
    session.routes[("POST", "/withdraw")] = fault_after(g.answer, fault)
    money = Money(account, g)
    session.routes[("GET", "/balance")] = money
    c = make_client(clock=Clock(time.time()))
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    with pytest.raises(crx.WithdrawUnknown, match="nothing signed"):
        c.withdraw(500)
    money.shown = True
    with pytest.raises(crx.EarlierOutcome) as ei:
        c.withdraw(500)
    assert ei.value.details["status"] == "accepted"
    assert len(withdraw_posts(session)) == 1


def test_withdraw_refused_answer_clears(make_client, session, health, account):
    # A refusal queues nothing: the next withdraw signs its own intent.
    Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account)
    session.routes[("POST", "/withdraw")] = (422, {"code": "unprocessable_entity", "error": "amount above free"})
    c = make_client(clock=Clock(time.time()))
    for _ in range(2):
        with pytest.raises(crx.CrxError) as ei:
            c.withdraw(1000)
        assert not isinstance(ei.value, (crx.WithdrawUnknown, crx.EarlierOutcome))
    assert len(withdraw_posts(session)) == 2
