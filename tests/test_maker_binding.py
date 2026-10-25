"""A binding quote over its life: its leg, the gateway's refusals, confirm() and drop_quote()."""

from decimal import Decimal

import pytest

import crx

from .conftest import Clock
from .test_maker import RFQ, T0, TXH, Gateway, new_maker, posts, rfq_obj


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def maker(session, health, tmp_path, clock):
    return new_maker(session, health, tmp_path, clock)


@pytest.fixture
def gw(session, clock):
    return Gateway(session, clock)


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
