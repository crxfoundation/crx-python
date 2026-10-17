"""One post per signed body. A refusal goes back to the caller with the gateway's reason and the time it
names. No second quote or withdraw is signed or posted unless the caller asks for it. The two re-sends the
SDK keeps carry the same body, once, where the gateway answers that body as it answered the first."""

import time

import pytest

import crx

from .conftest import Clock
from .test_at_most_once import Money, fault_after, withdraw_posts
from .test_binding_quote import drops
from .test_money import Chain, balance_body, gate
from .test_quote_trade import (  # noqa: F401 (fixtures)
    QID, QID2, RFQ, Custodian, SessionGate, accepts, clock, custodian_client, sent_nothing, signed_posts, venue,
)
from .test_solana import BOUND, sclient, solnet, solsession  # noqa: F401 (fixtures)

REJECTED = {"error": "rejected: rj_0011223344556677", "code": "rejected", "outcome": "trade_not_opened",
            "message": "Trade not opened. Ask for a new quote.", "details": {"reject_code": "rj_0011223344556677"}}
# The gateway's 409 while it has not read the latest settlement: its line names the next request time.
UNREAD = {"error": "conflict: the latest settlement is not read yet; retry shortly", "code": "conflict",
          "outcome": "unavailable", "message": "Balances are updating. Next request after 17:05 UTC."}
WAIT = {"Retry-After": "240"}


# ---------- the accept ----------


@pytest.mark.parametrize("signed", [True, False], ids=["signed", "template-ask"])
def test_a_409_rejected_accept_is_posted_once(make_client, venue, session, clock, signed):
    if not signed:
        venue.winner = False  # the row carries no template: the first post asks for it, with no sig
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (409, REJECTED)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, wait=3)
    t0 = clock()
    with pytest.raises(crx.QuoteExpired) as ei:
        c.trade(q)
    e = ei.value
    assert len(accepts(session)) == 1 and len(signed_posts(session)) == int(signed)
    assert clock() == t0 and sent_nothing(session)
    assert (e.status, e.gateway_code) == (409, "rejected")
    assert e.details["message"] == REJECTED["message"] and e.details["reject_code"] == "rj_0011223344556677"


def test_an_accept_refusal_carries_the_time_the_gateway_names(make_client, venue, session, clock):
    body = {"error": "service unavailable", "code": "service_unavailable", "outcome": "unavailable",
            "message": "Service temporarily unavailable: trade not executed. Next request after 17:05 UTC."}
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (503, body, {"Retry-After": "90"})
    c = make_client(clock=clock)
    with pytest.raises(crx.ServiceUnavailable) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    d = ei.value.details
    assert (d["retry_after_secs"], d["time_utc"], d["message"]) == (90, "17:05 UTC", body["message"])
    assert len(accepts(session)) == 1 and sent_nothing(session)


def test_a_dropped_quote_signs_nothing_for_the_best(make_client, venue, session, clock, account):
    # The best live quote is better than the dropped one: the SDK still signs nothing for it.
    drops(venue, session, {"quote_id": QID2, "rate": "18.650000"})
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    with pytest.raises(crx.QuoteDropped) as ei:
        c.trade(q)
    e = ei.value
    assert (e.best.quote_id, str(e.best.rate), e.best.rfq_id) == (QID2, "18.650000", RFQ)
    assert [b["quote_id"] for b in accepts(session)] == [QID] and len(signed_posts(session)) == 1
    t = c.trade(e.best)  # the caller's own call
    assert (t.status, t.quote_id) == ("open", QID2)
    assert [b["quote_id"] for b in accepts(session)] == [QID, QID2] and sent_nothing(session)


# ---------- the withdraw ----------


def test_a_withdraw_409_is_posted_once_with_its_time(make_client, session, health, account):
    Chain(session)
    g = gate(session, health, account)
    session.routes[("POST", "/withdraw")] = (409, UNREAD, WAIT)
    session.routes[("GET", "/balance")] = balance_body(account)
    clock = Clock(time.time())
    c = make_client(clock=clock)
    t0 = clock()
    with pytest.raises(crx.CrxError) as ei:
        c.withdraw(1000)
    e = ei.value
    assert (e.code, e.status, str(e)) == ("conflict", 409, UNREAD["message"])
    assert (e.details["retry_after_secs"], e.details["time_utc"], e.details["error"]) == (240, "17:05 UTC",
                                                                                          UNREAD["error"])
    assert len(withdraw_posts(session)) == 1 and clock() == t0 and c._held_withdraw is None
    # The caller's next call signs its own intent and posts it once.
    session.routes[("POST", "/withdraw")] = g.answer
    session.routes[("GET", "/balance")] = [balance_body(account), g.view("accepted")]
    assert c.withdraw(1000).status == "accepted" and len(withdraw_posts(session)) == 2


def test_a_solana_withdraw_409_is_posted_once(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    solsession.routes[("GET", "/bind")] = dict(BOUND)
    solsession.routes[("GET", "/balance")] = {"account": c.address, "withdraw": {"nonce": "4", "live": True}}
    solsession.routes[("POST", "/withdraw")] = (409, UNREAD, WAIT)
    t0 = c._clock()
    with pytest.raises(crx.CrxError) as ei:
        c.withdraw("10")
    assert (ei.value.code, str(ei.value), ei.value.details["retry_after_secs"]) == ("conflict", UNREAD["message"], 240)
    assert len(withdraw_posts(solsession)) == 1 and c._clock() == t0


# ---------- the re-sends kept ----------


def test_the_kept_withdraw_repost_is_the_same_body_once_per_call(make_client, session, health, account):
    """The first post gets no answer and the gateway never read it. The caller's next withdraw() of the same
    amount posts the same signed body once. The gateway's refusal of that post comes back with its time."""
    Chain(session)
    g = gate(session, health, account)
    session.routes[("GET", "/balance")] = Money(account, g)
    session.routes[("POST", "/withdraw")] = fault_after(lambda req: None, "timeout")
    clock = Clock(time.time())
    c = make_client(clock=clock)
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    clock.t += 300
    session.routes[("POST", "/withdraw")] = (409, UNREAD, WAIT)
    with pytest.raises(crx.WithdrawUnknown) as ei:
        c.withdraw(1000)
    r = ei.value.details["refusal"]
    assert (r["code"], r["status"], r["message"]) == ("conflict", 409, UNREAD["message"])
    assert (r["retry_after_secs"], r["time_utc"]) == (240, "17:05 UTC")
    first, second = withdraw_posts(session)
    assert first == second  # the same signed intent: one signature
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)  # within 300 s of the last post: read only
    assert len(withdraw_posts(session)) == 2


def test_the_kept_401_resend_is_the_same_body_once(session, health, account, tmp_path):
    """A gateway that forgot the session token answers 401 before it reads the call. The SDK mints a token
    and sends the same bytes once more. A second 401 stands: no third send."""
    Chain(session)
    g = gate(session, health, account)
    session.routes[("GET", "/balance")] = [balance_body(account), g.view("accepted")]
    tokens = SessionGate(session, account.address.lower())
    sent, inner, forget = [], session.request, {"on": "first"}

    def posts():
        return [s for s in sent if s[1].endswith("/withdraw")]

    def record(method, url, headers=None, data=None, **kw):
        sent.append((method, url.split("?")[0], (headers or {}).get("x-crx-session"), data))
        if url.endswith("/withdraw") and (forget["on"] == "every" or len(posts()) == 1):
            tokens.restart()  # the gateway restarts as the post arrives
        return inner(method, url, headers=headers, data=data, **kw)
    session.request = record
    c = custodian_client(session, tmp_path, Custodian(account), Clock(time.time()))
    assert c.withdraw(1000).status == "accepted"
    first, second = posts()
    assert first[3] == second[3] and first[2] != second[2]  # the same bytes, a new token
    assert tokens.mints == 2 and len(withdraw_posts(session)) == 1  # the gateway read one post

    forget["on"] = "every"
    session.routes[("GET", "/balance")] = balance_body(account, nonce="4")
    with pytest.raises(crx.AuthError):
        c.withdraw(5)
    assert len(posts()) == 4 and len(withdraw_posts(session)) == 1
