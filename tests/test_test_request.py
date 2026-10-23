"""Test requests use fixed terms and reject malformed gateway responses."""

from decimal import Decimal

import pytest

import crx
from .conftest import Clock

NOW = 1_790_000_000
RID = "0x" + "a1" * 32
REF = "0x" + "b2" * 32


def request_wire(**edit):
    return dict({
        "rfq_id": RID, "chain": "avax-fuji", "pair": "USD/MXN", "side": -1,
        "notional": "100", "premium_bps": 0, "kind": "open", "status": "open",
        "expiry": (NOW + 4_500) * 1000, "closes_at": (NOW + 120) * 1000,
        "request_expires_at": (NOW + 900) * 1000,
        "quote_expiry_max": NOW + 570, "taker_ref": REF,
        "test_request": True, "reference_rate": "18.123456",
    }, **edit)


BAD = [
    {"chain": "ethereum"}, {"notional": "1000"}, {"premium_bps": 1}, {"taker_ref": None},
    {"expiry": str((NOW + 4_500) * 1000)}, {"quote_expiry_max": (NOW + 600) * 1000},
    {"reference_rate": "NaN"}, {"reference_rate": "0"}, {"status": "cancelled"}, {"client_rfq_id": "own"},
]


def test_request_posts_only_chain_and_returns_only_the_fixed_maker_terms(make_client, session):
    c = make_client(clock=Clock(NOW))
    session.routes[("POST", "/test-requests")] = request_wire()
    r = c.test_request()
    assert isinstance(r, crx.Rfq)
    assert (r.rfq_id, r.pair, r.side, r.notional) == (RID, "USD/MXN", "sell", Decimal(100))
    assert r.test_request is True and r.reference_rate == Decimal("18.123456")
    assert r.expiry.timestamp() == NOW + 4_500
    assert r.closes_at.timestamp() == NOW + 120
    assert r.request_expires_at.timestamp() == NOW + 900
    assert r.quote_expiry_max.timestamp() == NOW + 570
    call = session.calls[-1]
    assert (call["path"], call["body"]) == ("/test-requests", {"chain": c.chain_key})
    assert call["headers"]["x-crx-address"] == c.address
    assert c.test_request().rfq_id == RID
    for edit in BAD:
        session.routes[("POST", "/test-requests")] = request_wire(**edit)
        with pytest.raises(crx.BadAnswer):
            make_client(clock=Clock(NOW)).test_request()
        assert not session.rpc_calls, edit
    assert not any(call["path"].endswith("/quotes") for call in session.calls)


def test_request_on_a_network_with_no_test_requests_is_test_requests_off(make_client, session):
    session.routes[("POST", "/test-requests")] = (404, {"code": "not_found", "error": "not found",
                                                        "message": "Not found. Check the id."})
    with pytest.raises(crx.CrxError) as ei:
        make_client().test_request()
    e = ei.value
    assert type(e) is crx.CrxError and (e.code, e.status, e.gateway_code) == ("test_requests_off", 404, "not_found")
    assert str(e) == "test requests are not offered on avax-fuji"
    assert e.details["message"] == "Not found. Check the id. (not found)" and session.paths("POST") == ["/test-requests"]


@pytest.mark.parametrize("edit", [{"notional": "1000"}])
def test_send_quote_rechecks_test_terms_before_signing(make_client, session, edit):
    session.routes[("POST", "/test-requests")] = request_wire()
    c = make_client(clock=Clock(NOW))
    r = c.test_request()
    r.raw.update(edit)
    with pytest.raises(crx.RefusedToSign):
        c.send_quote(r, r.reference_rate)
    assert not session.rpc_calls
    assert len(session.calls) == 1


def test_marked_stream_rfq_can_be_quoted_without_reference_rate(session, health, tmp_path):
    from crx import _maker
    from .test_maker import Gateway, new_maker

    clock = Clock(NOW)
    maker = new_maker(session, health, tmp_path, clock)
    wire = request_wire()
    venue = Gateway(session, clock, **wire)
    wire.pop("reference_rate")
    wire.pop("status")
    wire.pop("kind")
    quoted = maker.send_quote(_maker.rfq_of(wire), "18.123456")
    assert quoted.notional == Decimal(100)
    assert len(venue.bodies) == 1
