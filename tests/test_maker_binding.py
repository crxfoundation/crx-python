"""A binding quote over its life: the leg it rides on, a later quote, the gateway's leg refusals,
the answer's echo, confirm() up to the trade, drop_quote(), and the quickstart's maker calls."""

from decimal import Decimal

import pytest
import requests

import crx
from crx import _eip712 as e7
from crx import _maker

from .conftest import BASE, RPC, Clock
from .test_maker import (
    KEY, OTHER, QE_MAX, RFQ, T0, TRADE, TXH, Gateway, frame, new_maker, opened, posts, refusal, rfq_obj, tail, tape,
)


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def maker(session, health, tmp_path, clock):
    return new_maker(session, health, tmp_path, clock)


@pytest.fixture
def gw(session, clock):
    return Gateway(session, clock)


def gets(session):
    return [c["path"] for c in session.calls if c["method"] == "GET"]


# ---------- the leg ----------

def test_a_later_quote_keeps_the_leg_and_takes_a_new_salt(maker, gw):
    a = maker.send_quote(rfq_obj(), "5.41")
    b = maker.send_quote(rfq_obj(), "5.40")
    first, second = gw.bodies
    assert first["leg_id"] == second["leg_id"] == a.leg_id == b.leg_id
    assert first["salt"] != second["salt"] and first["sig"] != second["sig"] and a.quote_id != b.quote_id
    assert [r["status"] for r in gw.rows] == ["dropped", "quoted"]


def test_each_rfq_gets_its_own_leg(maker, gw, session, clock):
    other = Gateway(session, clock, rfq_id=OTHER)
    a = maker.send_quote(rfq_obj(), "5.41")
    b = maker.send_quote(rfq_obj(rfq_id=OTHER), "5.41")
    assert a.leg_id != b.leg_id and maker._legs == {RFQ: a.leg_id, OTHER: b.leg_id}
    assert len(gw.bodies) == len(other.bodies) == 1
    clock.t = QE_MAX + 1  # past both legs' quote end: the next quote forgets them
    with pytest.raises(crx.QuoteLost):
        maker.send_quote(rfq_obj(), "5.41")
    assert maker._legs == {}


def test_a_held_leg_that_ends_past_the_rfqs_quote_expiry_max_is_refused(maker, gw, session):
    maker.send_quote(rfq_obj(), "5.41")
    with pytest.raises(crx.RefusedToSign, match="the quote end is past the RFQ's quote_expiry_max"):
        maker.send_quote(rfq_obj(quote_expiry_max=QE_MAX - 10), "5.41")
    assert len(posts(session)) == 1


def test_leg_id_taken_is_typed_and_the_next_quote_takes_a_new_leg(maker, gw):
    gw.answer = refusal("leg_id_taken")
    with pytest.raises(crx.LegIdTaken) as e:
        maker.send_quote(rfq_obj(), "5.41")
    assert (e.value.code, e.value.status) == ("leg_id_taken", 409) and maker._legs == {}
    q = maker.send_quote(rfq_obj(), "5.41")
    assert q.leg_id != gw.bodies[0]["leg_id"] and tail(q.leg_id) == QE_MAX


def test_leg_live_names_the_leg_and_the_next_quote_goes_on_it(maker, gw):
    live = e7.leg_id_for(b"\x01" * 24, QE_MAX)
    gw.live = live
    with pytest.raises(crx.LegLive) as e:
        maker.send_quote(rfq_obj(), "5.41")
    assert (e.value.code, e.value.status, e.value.leg_id) == ("leg_live", 409, live)
    q = maker.send_quote(rfq_obj(), "5.40")
    assert q.leg_id == live == gw.bodies[1]["leg_id"] and gw.bodies[0]["leg_id"] != live


def test_leg_live_on_a_leg_near_its_end_is_dropped_by_its_id(maker, gw, session, clock):
    # Under 60 s of life left: no quote goes on it. The error's leg_id drops it; the next quote takes a new leg.
    stale = e7.leg_id_for(b"\x02" * 24, int(clock()) + 59)
    gw.live = stale
    session.routes[("DELETE", f"/rfqs/{RFQ}/quotes/{stale}")] = lambda req: gw.delete(stale)
    with pytest.raises(crx.LegLive) as e:
        maker.send_quote(rfq_obj(), "5.41")
    assert e.value.leg_id == stale and maker._legs == {}
    assert maker.drop_quote(rfq_obj(), leg_id=e.value.leg_id).leg_id == stale
    assert maker.send_quote(rfq_obj(), "5.41").leg_id not in (stale, gw.bodies[0]["leg_id"])


def test_quote_fills_full_is_typed(maker, gw):
    gw.answer = refusal("quote_fills_full")
    with pytest.raises(crx.QuoteFillsFull) as e:
        maker.send_quote(rfq_obj(), "5.41")
    assert (e.value.code, e.value.status) == ("quote_fills_full", 409)


@pytest.mark.parametrize("edit", [{"leg_hash": "0x" + "00" * 32}, {"leg_id": "0x" + "00" * 32}, {"rate": "5.42"},
                                  {"rfq_id": OTHER}, {"quote_id": None}])
def test_send_quote_refuses_an_echo_of_another_quote(maker, gw, session, edit):
    session.routes[("POST", f"/rfqs/{RFQ}/quotes")] = lambda req: dict(gw.post(req), **edit)
    with pytest.raises(crx.BadAnswer):
        maker.send_quote(rfq_obj(), "5.41")


def test_a_quote_that_may_rest_after_a_network_error_is_dropped_by_its_rfq(maker, gw, session):
    # The post reached the gateway and its answer was lost: the client still holds the leg.
    def lost_answer(req):
        gw.post(req)
        raise requests.ConnectionError("reset")
    session.routes[("POST", f"/rfqs/{RFQ}/quotes")] = lost_answer
    with pytest.raises(crx.NetworkError):
        maker.send_quote(rfq_obj(), "5.41")
    leg = gw.bodies[0]["leg_id"]
    assert gw.rows[0]["status"] == "quoted" and maker._legs == {RFQ: leg}
    assert maker.drop_quote(rfq_obj()).leg_id == leg and gw.rows[0]["status"] == "dropped"


# ---------- confirm ----------

@pytest.fixture
def sent(maker, gw):
    return maker.send_quote(rfq_obj(), "5.41")


def test_confirm_waits_for_the_accept_and_signs_nothing(maker, gw, sent, session):
    n = len(session.calls)
    gw.accept_at = 2
    t = maker.confirm(sent)
    assert (t.status, t.tx, t.rfq_id, t.quote_id, t.side, t.rate, t.notional) == (
        "open", TXH, RFQ, sent.quote_id, "sell", Decimal("5.41"), Decimal("5000000.000000"))
    assert {(c["method"], c["path"]) for c in session.calls[n:]} == {("GET", f"/rfqs/{RFQ}")}
    assert len(posts(session)) == 1
    assert set(session.rpc_methods()) <= {"eth_chainId", "eth_getCode"}


def test_confirm_reads_the_view_every_third_poll(maker, gw, sent, clock):
    gw.accept_at = 4
    start = clock()
    assert maker.confirm(sent, timeout=60).status == "open"
    assert 9 <= clock() - start <= 12  # three reads 3 s apart before the accept shows


def test_confirm_rides_out_a_gateway_that_does_not_answer(maker, gw, sent, session):
    gw.accept()
    session.routes[("GET", f"/rfqs/{RFQ}")] = [
        (429, {"code": "rate_limited", "error": "slow down"}), (503, {"code": "upstream", "error": "down"}), gw.view]
    assert maker.confirm(sent).status == "open"


@pytest.mark.parametrize("status, reason", [
    ("accepted", "another_maker"),   # the taker accepted another maker's quote
    ("opened", "another_maker"),
    ("expired", "expired"),
    ("cancelled", "cancelled"),
])
def test_confirm_on_an_ended_rfq_says_why(maker, gw, sent, status, reason):
    gw.end(status)
    assert [r["status"] for r in gw.rows] == ["dropped"]
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert (e.value.reason, e.value.code, e.value.details["rfq_id"]) == (reason, "quote_lost", RFQ)
    assert str(e.value) == _maker.LOST[reason]


@pytest.mark.parametrize("status, own, reason", [
    ("quoted", "dropped", "dropped"),    # a live RFQ: the seat's drop, or a gateway restart
    ("quoted", "expired", "expired"),    # the quote's own life ended
    ("expired", "expired", "expired"),
])
def test_confirm_a_quote_that_ended_on_its_own_says_why(maker, gw, sent, status, own, reason):
    gw.status, gw.rows[0]["status"] = status, own
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert e.value.reason == reason


def test_lost_reads_the_rfq_status_before_the_quotes_own():
    def view(status, *rows):
        return {"status": status, "quotes": [{"quote_id": i, "status": s} for i, s in rows]}
    assert _maker.lost(view("accepted", ("a", "dropped")), "a") == "another_maker"
    assert _maker.lost(view("accepted", ("a", "dropped"), ("b", "accepted")), "a") == "dropped"
    assert _maker.lost(view("accepted", ("a", "dropped"), ("b", "accepted")), "b") is None
    assert _maker.lost(view("accepted"), "a") == "another_maker"
    assert _maker.lost(view("cancelled", ("a", "dropped")), "a") == "cancelled"
    assert _maker.lost(view("expired", ("a", "dropped")), "a") == "expired"
    assert _maker.lost(view("quoted", ("a", "dropped")), "a") == "dropped"
    assert _maker.lost(view("quoted", ("a", "dropped"), ("b", "quoted")), "a") == "dropped"
    assert _maker.lost(view("quoted", ("a", "quoted")), "a") is None


def test_confirm_no_accept_before_the_wait_is_timeout(maker, gw, sent, clock):
    start = clock()
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent, timeout=4)
    assert e.value.reason == "timeout" and 4 <= clock() - start <= 6
    assert str(e.value) == "no accept before the wait ended"


def test_confirm_by_default_waits_until_closes_at_and_5_s(maker, gw, sent, clock):
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert e.value.reason == "timeout" and T0 + 125 <= clock() <= T0 + 126


def test_confirm_the_accept_wins_over_an_ended_rfq(maker, gw, sent):
    gw.accept()
    assert gw.status == "accepted" and maker.confirm(sent).status == "open"


@pytest.mark.parametrize("accepted", [False, True])
def test_confirm_the_earlier_quote_on_a_leg_reads_dropped(maker, gw, sent, accepted):
    # Replaced by this seat's later quote: dropped, before and after the taker accepts the later one.
    later = maker.send_quote(rfq_obj(), "5.40")
    if accepted:
        gw.accept()
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert e.value.reason == "dropped"
    gw.accept_at = 1
    assert maker.confirm(later).rate == Decimal("5.40")


def test_confirm_takes_a_maker_quote_only(maker, session):
    with pytest.raises(crx.BadRequest, match="confirm\\(\\) takes the MakerQuote that send_quote\\(\\) returned"):
        maker.confirm(rfq_obj())
    assert session.calls == []


# ---------- drop_quote ----------

def test_drop_quote_ends_the_leg_and_the_next_quote_takes_a_new_one(maker, gw, sent, session, clock):
    d = maker.drop_quote(sent)
    assert (d.rfq_id, d.leg_id, d.at.timestamp()) == (RFQ, sent.leg_id, T0)
    (call,) = [c for c in session.calls if c["method"] == "DELETE"]
    assert call["path"] == f"/rfqs/{RFQ}/quotes/{sent.leg_id}" and call["raw"] == b"" and "x-crx-sig" in call["headers"]
    clock.t += 5
    assert maker.drop_quote(sent).at == d.at  # a repeat answers the first drop's time
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert e.value.reason == "dropped"
    again = maker.send_quote(rfq_obj(), "5.41")
    assert again.leg_id != sent.leg_id and gw.rows[-1]["status"] == "quoted"


def test_drop_after_the_accept_is_already_accepted(maker, gw, sent):
    gw.accept()
    with pytest.raises(crx.AlreadyAccepted) as e:
        maker.drop_quote(sent)
    assert (e.value.code, e.value.status, e.value.trade_id) == ("already_accepted", 409, TRADE)
    assert maker._legs == {RFQ: sent.leg_id}
    assert maker.confirm(sent).status == "open"  # the trade stands


def test_drop_on_an_ended_rfq_is_unknown_or_ended(maker, gw, sent):
    gw.ended = True
    with pytest.raises(crx.UnknownOrEnded) as e:
        maker.drop_quote(sent)
    assert (e.value.code, e.value.status) == ("unknown_or_ended", 404) and maker._legs == {}


def test_drop_by_rfq_and_leg_id(maker, gw, sent, session, clock):
    assert maker.drop_quote(rfq_obj(), leg_id=sent.leg_id.upper().replace("0X", "0x")).leg_id == sent.leg_id
    other = Gateway(session, clock)
    q = maker.send_quote(rfq_obj(), "5.41")
    assert maker.drop_quote(rfq_obj()).leg_id == q.leg_id  # the leg this client quoted on
    assert other.dropped and maker._legs == {}


@pytest.mark.parametrize("answer", [{"dropped": False}, {"dropped": True, "leg_id": "0x" + "00" * 32}, {}])
def test_drop_refuses_an_answer_for_another_leg(maker, gw, sent, session, answer):
    session.routes[("DELETE", f"/rfqs/{RFQ}/quotes/{sent.leg_id}")] = answer
    with pytest.raises(crx.BadAnswer, match="the drop answer does not name this leg"):
        maker.drop_quote(sent)
    assert maker._legs == {RFQ: sent.leg_id}


@pytest.mark.parametrize("bad, kw", [("not a quote", {}), (frame(), {}), ("rfq", {}), ("rfq", {"leg_id": "0x12"})],
                         ids=["text", "frame", "rfq_no_leg_held", "bad_leg_id"])
def test_drop_refuses_before_any_call(maker, session, bad, kw):
    with pytest.raises(crx.BadRequest):
        maker.drop_quote(rfq_obj() if bad == "rfq" else bad, **kw)
    assert session.calls == []


def test_a_viewer_cannot_drop(session, tmp_path):
    c = crx.Client(key=KEY, base_url=BASE, rpc_url=RPC, state_dir=tmp_path, session=session,
                   account="0x5b38da6a701c568545dcfcb03fcb875f56beddc4")
    with pytest.raises(crx.ConfigError):
        c.drop_quote(rfq_obj(), leg_id="0x" + "01" * 32)
    assert session.calls == []


# ---------- the quickstart's maker calls, off the tape ----------

def test_the_quickstart_maker_calls_run_off_the_tape(maker, gw, session):
    gw.accept_at = 1
    session.routes[("GET", "/trades")] = tape([opened(frame(), 1)], 1)
    for rfq in maker.rfqs(wait=60):
        q = maker.send_quote(rfq, "5.41")
        t = maker.confirm(q, timeout=60)
        break
    assert (t.status, t.tx, q.rfq_id, q.side) == ("open", TXH, RFQ, "sell")
    assert set(gw.bodies[0]) == {"rate", "leg_id", "salt", "sig"}
    assert [c["method"] for c in session.calls if c["path"].startswith("/rfqs")].count("POST") == 1
