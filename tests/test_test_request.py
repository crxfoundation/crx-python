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
