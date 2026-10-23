"""A binding quote over its life: its leg, the gateway's refusals, confirm() and drop_quote()."""

from decimal import Decimal

import pytest

import crx
from crx import _eip712 as e7

from .conftest import Clock
from .test_maker import OTHER, QE_MAX, RFQ, T0, TRADE, TXH, Gateway, new_maker, posts, refusal, rfq_obj, tail


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def maker(session, health, tmp_path, clock):
    return new_maker(session, health, tmp_path, clock)


@pytest.fixture
def gw(session, clock):
    return Gateway(session, clock)


def test_a_held_leg_that_ends_before_the_rfqs_quote_expiry_max_is_refused(maker, gw, session):
    # The chain and the gateway take a maker leg only when its quote end is the RFQ's quote_expiry_max.
    maker.send_quote(rfq_obj(), "5.41")
    with pytest.raises(crx.RefusedToSign, match="the leg id's quote end is not the RFQ's quote_expiry_max"):
        maker.send_quote(rfq_obj(quote_expiry_max=QE_MAX + 10), "5.41")
    assert len(posts(session)) == 1


def test_a_leg_refusal_is_typed_and_sets_the_leg_of_the_next_quote(maker, gw, session, clock):
    gw.answer = refusal("leg_id_taken")
    with pytest.raises(crx.LegIdTaken) as e:
        maker.send_quote(rfq_obj(), "5.41")
    assert (e.value.code, e.value.status) == ("leg_id_taken", 409) and maker._legs == {}
    q = maker.send_quote(rfq_obj(), "5.41")
    assert q.leg_id != gw.bodies[0]["leg_id"] and tail(q.leg_id) == QE_MAX
    live = e7.leg_id_for(b"\x01" * 24, QE_MAX)
    gw.live = live
    with pytest.raises(crx.LegLive) as e:
        maker.send_quote(rfq_obj(), "5.41")
    assert (e.value.code, e.value.status, e.value.leg_id) == ("leg_live", 409, live)
    q = maker.send_quote(rfq_obj(), "5.40")
    assert q.leg_id == live == gw.bodies[-1]["leg_id"] and gw.bodies[-2]["leg_id"] != live
    # No quote goes on a leg with under 60 s of life left.
    stale = e7.leg_id_for(b"\x02" * 24, int(clock()) + 59)
    gw.live = stale
    session.routes[("DELETE", f"/rfqs/{RFQ}/quotes/{stale}")] = lambda req: gw.delete(stale)
    with pytest.raises(crx.LegLive) as e:
        maker.send_quote(rfq_obj(), "5.41")
    assert e.value.leg_id == stale and maker._legs == {}
    assert maker.drop_quote(rfq_obj(), leg_id=e.value.leg_id).leg_id == stale
    assert maker.send_quote(rfq_obj(), "5.41").leg_id not in (stale, live)


ECHOES = [{"leg_hash": "0x" + "00" * 32}, {"rfq_id": OTHER}, {"quote_id": None}]


def test_send_quote_refuses_an_echo_of_another_quote(maker, gw, session):
    for n, edit in enumerate(ECHOES, 1):
        session.routes[("POST", f"/rfqs/{RFQ}/quotes")] = lambda req, edit=edit: dict(gw.post(req), **edit)
        with pytest.raises(crx.BadAnswer):
            maker.send_quote(rfq_obj(), "5.41")
        assert len(gw.bodies) == n, edit


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
    assert maker.confirm(sent).status == "open"
