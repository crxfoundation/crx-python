"""One post per signed body: a refusal goes back to the caller."""

import pytest

import crx

from .test_at_most_once import withdraw_posts
from .test_quote_trade import QID, QID2, RFQ, accepts, clock, sent_nothing, signed_posts, venue  # noqa: F401 (fixtures)
from .test_solana import BOUND, sclient, solnet, solsession  # noqa: F401 (fixtures)

REJECTED = {"error": "rejected: rj_0011223344556677", "code": "rejected", "outcome": "trade_not_opened",
            "message": "Trade not opened. Ask for a new quote.", "details": {"reject_code": "rj_0011223344556677"}}
# The gateway's 409 before it reads the latest settlement names the next request time.
UNREAD = {"error": "conflict: the latest settlement is not read yet; retry shortly", "code": "conflict",
          "outcome": "unavailable", "message": "Balances are updating. Next request after 17:05 UTC."}
WAIT = {"Retry-After": "240"}


def drops(venue, session, best_edit):
    """Answer the accept on QID with 409 quote_dropped, naming the best quote that ``best_edit`` shapes."""
    def accept(req):
        body = req["body"]
        if body["quote_id"] == QID:
            best = None
            if best_edit is not None:
                best = dict(venue.row(), **best_edit)
                venue.rates[best["quote_id"]] = best["rate"]
                best["trade_template"] = venue.make_template(best["quote_id"])
            return (409, {"code": "quote_dropped", "error": "the maker dropped this quote", "details": {"best": best}})
        return venue.accept(req)
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = accept


def test_a_409_rejected_accept_is_posted_once(make_client, venue, session, clock):
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (409, REJECTED)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, wait=3)
    t0 = clock()
    with pytest.raises(crx.QuoteExpired) as ei:
        c.trade(q)
    e = ei.value
    assert len(accepts(session)) == 1 and len(signed_posts(session)) == 1
    assert clock() == t0 and sent_nothing(session)
    assert (e.status, e.gateway_code) == (409, "rejected")
    assert e.details["message"] == REJECTED["message"] and e.details["reject_code"] == "rj_0011223344556677"


def test_a_dropped_quote_signs_nothing_for_the_best(make_client, venue, session, clock, account):
    # No best, or the dropped quote itself, is no best.
    for best_edit in (None, {"quote_id": QID}):
        venue.reset()
        drops(venue, session, best_edit)
        before = len(accepts(session))
        c = make_client(clock=clock)
        with pytest.raises(crx.QuoteDropped) as ei:
            c.trade(c.quote("USD/MXN", "buy", 25_000))
        assert ei.value.best is None and len(accepts(session)) == before + 1 and sent_nothing(session), best_edit
    before = len(accepts(session))
    # Even a better best quote gets no signature from the SDK.
    venue.reset()
    drops(venue, session, {"quote_id": QID2, "rate": "18.650000"})
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    with pytest.raises(crx.QuoteDropped) as ei:
        c.trade(q)
    e = ei.value
    assert (e.best.quote_id, str(e.best.rate), e.best.rfq_id) == (QID2, "18.650000", RFQ)
    assert [b["quote_id"] for b in accepts(session)[before:]] == [QID] and len(signed_posts(session)) == before + 1
    t = c.trade(e.best)
    assert (t.status, t.quote_id) == ("open", QID2)
    assert [b["quote_id"] for b in accepts(session)[before:]] == [QID, QID2] and sent_nothing(session)


def test_a_solana_withdraw_409_is_posted_once(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    solsession.routes[("GET", "/bind")] = dict(BOUND)
    solsession.routes[("GET", "/balance")] = {"account": c.address, "free": "100.000000",
                                              "withdraw": {"nonce": "4", "live": True}}
    solsession.routes[("POST", "/withdraw")] = (409, UNREAD, WAIT)
    t0 = c._clock()
    with pytest.raises(crx.CrxError) as ei:
        c.withdraw("10", wait=0)
    assert (ei.value.code, str(ei.value), ei.value.details["retry_after_secs"]) == (
        "conflict", f"{UNREAD['message']} ({UNREAD['error']})", 240)
    assert len(withdraw_posts(solsession)) == 1 and c._clock() == t0
