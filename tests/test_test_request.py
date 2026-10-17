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
        "expiry": (NOW + 18_000) * 1000, "closes_at": (NOW + 120) * 1000,
        "request_expires_at": (NOW + 10_800) * 1000,
        "quote_expiry_max": NOW + 570, "taker_ref": REF,
        "test_request": True, "reference_rate": "18.123456",
    }, **edit)


def test_request_posts_only_chain_and_returns_maker_terms(make_client, session):
    c = make_client(clock=Clock(NOW))
    session.routes[("POST", "/test-requests")] = request_wire()
    r = c.test_request()
    assert isinstance(r, crx.Rfq)
    assert (r.rfq_id, r.pair, r.side, r.notional) == (RID, "USD/MXN", "sell", Decimal(100))
    assert r.test_request is True and r.reference_rate == Decimal("18.123456")
    assert r.expiry.timestamp() == NOW + 18_000
    assert r.closes_at.timestamp() == NOW + 120
    assert r.request_expires_at.timestamp() == NOW + 10_800
    assert r.quote_expiry_max.timestamp() == NOW + 570
    call = session.calls[-1]
    assert (call["path"], call["body"]) == ("/test-requests", {"chain": c.chain_key})
    assert call["headers"]["x-crx-address"] == c.address
    assert c.test_request().rfq_id == RID
    assert not session.rpc_calls


BAD = [
    {"chain": "ethereum"}, {"notional": "1000"}, {"notional": 100}, {"notional": "NaN"},
    {"pair": "USD/BRL"}, {"side": 1}, {"side": -1.0}, {"side": True},
    {"kind": "close"}, {"kind": {}}, {"test_request": 1}, {"test_request": False},
    {"premium_bps": 1}, {"premium_bps": False}, {"rfq_id": "bad"}, {"taker_ref": None},
    {"expiry": (NOW + 20_000) * 1000}, {"expiry": str((NOW + 18_000) * 1000)},
    {"closes_at": NOW * 1000}, {"quote_expiry_max": (NOW + 600) * 1000},
    {"quote_expiry_max": NOW}, {"reference_rate": "NaN"}, {"reference_rate": "Infinity"},
    {"reference_rate": "0"}, {"reference_rate": 18.1}, {"reference_rate": None},
    {"request_expires_at": None}, {"request_expires_at": True},
    {"request_expires_at": str((NOW + 10_800) * 1000)},
    {"request_expires_at": NOW * 1000},
    {"request_expires_at": (NOW + 10_831) * 1000, "expiry": (NOW + 18_031) * 1000},
    {"closes_at": (NOW + 151) * 1000},
    {"request_expires_at": (NOW + 60) * 1000, "expiry": (NOW + 7_260) * 1000},
    {"quote_expiry_max": NOW + 631},
    {"reference_rate": "18.1234567"}, {"status": "cancelled"}, {"client_rfq_id": "own"},
]


@pytest.mark.parametrize("edit", BAD)
def test_request_rejects_bad_terms(make_client, session, edit):
    session.routes[("POST", "/test-requests")] = request_wire(**edit)
    with pytest.raises(crx.BadAnswer):
        make_client(clock=Clock(NOW)).test_request()
    assert not session.rpc_calls
    assert not any(call["path"].endswith("/quotes") for call in session.calls)


@pytest.mark.parametrize("edit", [{"chain": "ethereum"}, {"notional": "1000"},
                                  {"pair": "USD/BRL"}, {"side": -1.0}, {"kind": "close"}])
def test_send_quote_rechecks_test_terms_before_signing(make_client, session, edit):
    session.routes[("POST", "/test-requests")] = request_wire()
    c = make_client(clock=Clock(NOW))
    r = c.test_request()
    r.raw.update(edit)
    with pytest.raises(crx.RefusedToSign):
        c.send_quote(r, r.reference_rate)
    assert not session.rpc_calls
    assert len(session.calls) == 1


def test_request_propagates_disabled_response(make_client, session):
    session.routes[("POST", "/test-requests")] = (503, {"code": "service_unavailable", "error": "unavailable"})
    with pytest.raises(crx.ServiceUnavailable):
        make_client().test_request()
    assert len(session.calls) == 1


def test_request_accepts_compact_wire_pair(make_client, session):
    session.routes[("POST", "/test-requests")] = request_wire(pair="USDMXN")
    assert make_client(clock=Clock(NOW)).test_request().pair == "USD/MXN"


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


def test_two_hour_old_request_renews_rfq_but_keeps_request_and_settlement_deadlines(make_client, session):
    clock = Clock(NOW)
    c = make_client(clock=clock)
    session.routes[("POST", "/test-requests")] = request_wire()
    first = c.test_request()
    clock.t += 7200
    session.routes[("POST", "/test-requests")] = request_wire(
        rfq_id="0x" + "c3" * 32, taker_ref="0x" + "d4" * 32,
        closes_at=(NOW + 7320) * 1000, quote_expiry_max=NOW + 7770)
    renewed = c.test_request()
    assert renewed.rfq_id != first.rfq_id
    assert renewed.raw["taker_ref"] != first.raw["taker_ref"]
    assert renewed.request_expires_at == first.request_expires_at
    assert renewed.expiry == first.expiry
    assert renewed.closes_at.timestamp() == NOW + 7320
    assert renewed.quote_expiry_max.timestamp() == NOW + 7770


@pytest.mark.parametrize("status", ["accepted", "opened"])
def test_accepted_repeat_returns_original_request_but_cannot_be_quoted(make_client, session, status):
    c = make_client(clock=Clock(NOW + 200))
    session.routes[("POST", "/test-requests")] = request_wire(status=status)
    request = c.test_request()
    assert request.status == status and request.rfq_id == RID
    with pytest.raises(crx.RefusedToSign):
        c.send_quote(request, request.reference_rate)
    assert len(session.calls) == 1 and not session.rpc_calls
