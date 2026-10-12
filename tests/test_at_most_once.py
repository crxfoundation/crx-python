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


def test_past_the_quote_end_no_trade_status_and_no_position_is_no_trade(make_client, venue, session, clock):
    # The view names no trade status (nothing held or landed) and no position is listed: no trade.
    faulty_accept(venue, session, "timeout")
    venue.script = [("sending", None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    c.trade(q)
    with pytest.raises(crx.TradeUnknown):
        c.trade(other_rfq(q))
    venue.sig = None  # the view serves no trade status from here on
    session.routes[("GET", "/positions")] = {"positions": []}
    clock.t = q.rfq["quote_expiry_max"] + 121
    t = c.trade(q)
    assert t.status == "refused" and len(accepts(session)) == 1


@pytest.mark.parametrize("word", ["sending", "pending"])
def test_past_the_quote_end_a_live_status_with_no_position_stays_unknown(make_client, venue, session, clock, word):
    # A view still sending or pending is no final answer, even with no position listed.
    faulty_accept(venue, session, "timeout")
    venue.script = [(word, None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert c.trade(q).status == word
    session.routes[("GET", "/positions")] = {"positions": []}
    clock.t = q.rfq["quote_expiry_max"] + 121
    with pytest.raises(crx.TradeUnknown):
        c.trade(other_rfq(q))
    assert accepts_on(session, RFQ2) == []


def test_past_the_quote_end_an_unreadable_view_and_no_position_stays_unknown(make_client, venue, session, clock):
    # positions() lists open positions only: no listing is no answer while the view cannot be read.
    faulty_accept(venue, session, "dropped")
    venue.script = [("sending", None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    c.trade(q)
    session.routes[("GET", f"/rfqs/{RFQ}")] = (429, {"code": "rate_limited", "error": "slow down"})
    session.routes[("GET", "/positions")] = {"positions": []}
    clock.t = q.rfq["quote_expiry_max"] + 121
    with pytest.raises(crx.TradeUnknown):
        c.trade(other_rfq(q))
    assert accepts_on(session, RFQ2) == []


def test_a_pending_trade_blocks_the_second_quote_until_it_opens(make_client, venue, session, clock):
    # pending: the arm landed and its verdict is due. Not final: no second quote until open or refused.
    venue.script = [("pending", None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert c.trade(q).status == "pending"
    with pytest.raises(crx.TradeUnknown):
        c.trade(other_rfq(q))
    venue.script = [("open", TXH)]
    with pytest.raises(crx.EarlierOutcome) as ei:
        c.trade(other_rfq(q))
    assert ei.value.details == {"rfq_id": RFQ, "status": "open"}
    assert accepts_on(session, RFQ2) == [] and len(signed_posts(session)) == 1


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


def test_withdraw_nonce_passed_with_no_item_reads_the_row_at_its_nonce(make_client, session, health, account):
    # A row keyed on its arm tx names no item: once the nonce passed, the row at the intent's nonce is its own.
    Chain(session)
    g = gate(session, health, account)
    session.routes[("POST", "/withdraw")] = fault_after(g.answer, "timeout")
    money = Money(account, g)
    session.routes[("GET", "/balance")] = money
    c = make_client(clock=Clock(time.time()))
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    session.routes[("GET", "/balance")] = balance_body(account, nonce="4", withdraw=item_row("accepted", None, LANDED))
    out = c.withdraw(1000)
    assert (out.status, out.nonce) == ("accepted", 3) and len(withdraw_posts(session)) == 1


def test_withdraw_nonce_passed_by_another_row_is_no_new_intent(make_client, session, health, account):
    # The nonce passed and the newest row is another withdraw: the held intent was paid or can never be.
    Chain(session)
    g = gate(session, health, account)
    session.routes[("POST", "/withdraw")] = fault_after(g.answer, "timeout")
    session.routes[("GET", "/balance")] = Money(account, g)
    c = make_client(clock=Clock(time.time()))
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    other = dict(item_row("accepted", "0x" + "ee" * 32, LANDED), nonce="4")
    session.routes[("GET", "/balance")] = balance_body(account, nonce="5", withdraw=other)
    with pytest.raises(crx.EarlierOutcome, match="read balance"):
        c.withdraw(1000)
    assert len(withdraw_posts(session)) == 1


def test_withdraw_repeat_post_refused_ends_the_hold(make_client, session, health, account):
    # The first post never arrived; the repeat of the same intent is refused: nothing is held, the next call signs.
    Chain(session)
    g = gate(session, health, account)
    money = Money(account, g)
    lost = fault_after(lambda req: None, "dropped")
    refusal = (422, {"code": "unprocessable_entity", "error": "amount above free", "outcome": "insufficient_collateral"})

    def post(req):
        n = len(withdraw_posts(session))
        return lost(req) if n == 1 else refusal if n == 2 else g.answer(req)
    session.routes[("POST", "/withdraw")] = post
    session.routes[("GET", "/balance")] = money
    clock = Clock(time.time())
    c = make_client(clock=clock)
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    clock.t += 300  # past the in-flight window of the first post
    with pytest.raises(crx.CrxError) as ei:
        c.withdraw(1000)
    assert ei.value.status == 422 and not isinstance(ei.value, crx.WithdrawUnknown)
    c.withdraw(500)  # a new intent: the hold ended
    first, again, third = withdraw_posts(session)
    assert first == again and third["amount"] == "500"


def test_a_final_trade_answer_is_never_read_again(make_client, venue, session, clock):
    # A told "open" stays "open": a later failed read never turns it into "refused".
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert c.trade(q).status == "open"
    session.routes[("GET", f"/rfqs/{RFQ}")] = (404, {"code": "not_found", "error": "no such rfq"})
    session.routes[("GET", "/positions")] = {"positions": []}
    clock.t = q.rfq["quote_expiry_max"] + 121
    t = c.trade(q)
    assert (t.status, t.tx) == ("open", TXH) and len(accepts(session)) == 1


def test_withdraw_returned_pending_then_paid_is_told_once(make_client, session, health, account):
    # The first call returned "pending"; the next call of the same amount is a new withdraw, not that one's "paid".
    Chain(session)
    g = gate(session, health, account)
    money = Money(account, g)
    session.routes[("POST", "/withdraw")] = g.answer
    session.routes[("GET", "/balance")] = money
    c = make_client(clock=Clock(time.time()))
    assert c.withdraw(1000).status == "pending"  # the item never shows within the wait
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    money.shown, money.word = True, "paid"
    with pytest.raises(crx.EarlierOutcome) as ei:
        c.withdraw(1000)
    assert ei.value.details["status"] == "paid" and len(withdraw_posts(session)) == 1
    c.withdraw(1000)  # told: a new intent at the next nonce
    first, second = withdraw_posts(session)
    assert (first["nonce"], second["nonce"]) == ("3", "4")


def test_withdraw_nonce_passed_row_of_another_amount_is_not_the_intent(make_client, session, health, account):
    # A row with no item at the intent's nonce but another amount is not the held intent.
    Chain(session)
    g = gate(session, health, account)
    session.routes[("POST", "/withdraw")] = fault_after(lambda req: None, "dropped")
    session.routes[("GET", "/balance")] = Money(account, g)
    c = make_client(clock=Clock(time.time()))
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    row = dict(item_row("paid", None, LANDED), amount="250.000000")
    session.routes[("GET", "/balance")] = balance_body(account, nonce="4", withdraw=row)
    with pytest.raises(crx.EarlierOutcome, match="read balance"):
        c.withdraw(1000)


def test_withdraw_no_repeat_post_near_the_deadline(make_client, session, health, account):
    Chain(session)
    g = gate(session, health, account)
    session.routes[("POST", "/withdraw")] = fault_after(lambda req: None, "dropped")
    session.routes[("GET", "/balance")] = Money(account, g)
    clock = Clock(time.time())
    c = make_client(clock=clock)
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    clock.t = c._held_withdraw["deadline"] - 200
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    assert len(withdraw_posts(session)) == 1


def test_withdraw_no_repeat_post_while_the_first_may_be_in_flight(make_client, session, health, account):
    # A slow first post can still be queued: a repeat refused at step 5 then would drop the hold.
    Chain(session)
    g = gate(session, health, account)
    session.routes[("POST", "/withdraw")] = fault_after(lambda req: None, "timeout")
    session.routes[("GET", "/balance")] = Money(account, g)
    clock = Clock(time.time())
    c = make_client(clock=clock)
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)  # within 300 s of the first post: read only
    assert len(withdraw_posts(session)) == 1
    session.routes[("POST", "/withdraw")] = (409, {"code": "withdraw_in_progress", "error": "in progress"})
    clock.t += 300
    with pytest.raises(crx.CrxError) as ei:
        c.withdraw(1000)  # nothing can be in flight now: the refusal ends the hold
    assert ei.value.status == 409 and c._held_withdraw is None and len(withdraw_posts(session)) == 2


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
    clock = Clock(time.time())
    c = make_client(clock=clock)
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    clock.t += 300  # past the in-flight window of the first post
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
