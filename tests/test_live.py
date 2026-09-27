"""Opt-in live checks against the gateway. Read-only: no accept, no transaction.

Run: CRX_LIVE=1 pytest -m live
The quote check uses a fresh throwaway key. It is not whitelisted, so the
gateway refuses it, or the market is closed. Either answer is typed.
"""

import os

import pytest
from eth_account import Account

import crx

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("CRX_LIVE") != "1", reason="live checks are opt-in: set CRX_LIVE=1"),
]


@pytest.fixture
def client():
    return crx.Client(key=Account.create().key.hex(), network="testnet")


def test_health(client):
    h = client.health()
    assert h.get("healthy") is True or h.get("status") == "ok"
    c = client._chain_info()  # checks the domain against the core
    assert c["chain_id"] == 43113


def test_markets(client):
    ms = {m.pair: m for m in client.markets()}
    for pair in ("USD/MXN", "USD/BRL", "USD/PHP"):
        assert pair in ms and not ms[pair].paused, pair


def test_rpc_matches_chain(client):
    assert client._chain_ready()["chain_id"] == 43113


def test_throwaway_quote_is_refused_typed(client):
    with pytest.raises(crx.CrxError) as ei:
        client.quote("USD/MXN", "buy", 25_000, wait=10)
    assert ei.value.code in {"market_closed", "not_whitelisted", "unauthorized", "no_quotes", "seat_not_ready"}


def test_throwaway_balance_is_typed(client):
    try:
        b = client.balance()
    except crx.CrxError as e:
        assert e.code in {"not_whitelisted", "unauthorized", "seat_not_ready", "bad_request"}
    else:
        assert b.account == client.address
