"""The mainnet network: off by default, chain 1 only, the api.crxfx.com gateway and a configured RPC."""

import pytest
from eth_utils import is_hex_address

import crx
from crx import _eip712 as e7
from crx.client import MAINNET_CORE

from .conftest import BASE, RPC

# While the source pins no mainnet core address, a stand-in core takes its place.
FILLED = is_hex_address(MAINNET_CORE)
CORE = (MAINNET_CORE if FILLED else "0x" + "9e" * 20).lower()


def mainnet_health():
    return {"status": "ok", "chains": [{
        "key": "ethereum", "chain_id": 1, "core": CORE, "base_token": "0x" + "a0" * 20,
        "domain": e7.h0x(e7.domain_separator(1, CORE)),
    }]}


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    for k in ("CRX_ALLOW_MAINNET", "CRX_BASE", "CRX_RPC"):
        monkeypatch.delenv(k, raising=False)
    if not FILLED:
        monkeypatch.setitem(crx.NETWORKS["mainnet"], "core", CORE)


def main(session, tmp_path, account, **kw):
    args = dict(key=account.key.hex(), network="mainnet", base_url=BASE, rpc_url=RPC,
                state_dir=tmp_path / "state", session=session)
    args.update(kw)
    return crx.Client(**args)


OTHER = "0x" + "c0" * 20

REFUSALS = [  # pinned core, /health chain edit, error, words
    # The refusal holds even when /health serves a core.
    (None, {}, crx.ConfigError, "^network 'mainnet' pins no chain id or core; nothing signed. Pass core="),
    (CORE, {"chain_id": 43113, "domain": e7.h0x(e7.domain_separator(43113, CORE))}, crx.ConfigError, "chain 1"),
    # The domain matches the other core, so only the pin catches the move.
    (CORE, {"core": OTHER, "domain": e7.h0x(e7.domain_separator(1, OTHER))}, crx.RefusedToSign,
     f"^core moved: /health names {OTHER}, the SDK pins {CORE} on ethereum"),
]


def test_mainnet_off_its_pins_signs_nothing(session, tmp_path, account, monkeypatch):
    session.rpc.update({"eth_chainId": "0x1", "eth_getCode": lambda p: "0x6080" if p[0].lower() == CORE else "0x"})
    for core, edit, err, words in REFUSALS:
        monkeypatch.setitem(crx.NETWORKS["mainnet"], "core", core)
        h = mainnet_health()
        h["chains"][0].update(edit)
        session.routes[("GET", "/health")] = h
        c = main(session, tmp_path, account, allow_mainnet=True)
        with pytest.raises(err, match=words):
            c._chain_info()
        with pytest.raises(err, match=words):
            c.deposit(1000)
    assert session.paths("POST") == [] and "eth_sendRawTransaction" not in session.rpc_methods()


OFF = [  # Client arguments, words
    ({}, "allow_mainnet"),
    ({"allow_mainnet": True, "rpc_url": None}, "rpc_url"),  # no default RPC
]


def test_mainnet_is_off_by_default_and_has_no_default_rpc(session, tmp_path, account):
    for kw, words in OFF:
        with pytest.raises(crx.ConfigError) as ei:
            main(session, tmp_path, account, **kw)
        assert words in str(ei.value), kw
    assert session.calls == []


def test_mainnet_opt_in_by_env(session, tmp_path, account, monkeypatch):
    monkeypatch.setenv("CRX_ALLOW_MAINNET", "1")
    assert main(session, tmp_path, account).chain_key == "ethereum"
    monkeypatch.setenv("CRX_ALLOW_MAINNET", "yes")
    with pytest.raises(crx.ConfigError):
        main(session, tmp_path, account)
