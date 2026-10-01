"""The mainnet network: off by default, chain 1 only, URLs from config."""

import time

import pytest

import crx
from crx import _eip712 as e7

from .conftest import BASE, RPC

CORE = "0x00000000000000000000000000000000000c0e01"


def mainnet_health():
    return {"status": "ok", "chains": [{
        "key": "ethereum", "chain_id": 1, "core": CORE, "base_token": "0x" + "a0" * 20,
        "domain": e7.h0x(e7.domain_separator(1, CORE)),
    }]}


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    for k in ("CRX_ALLOW_MAINNET", "CRX_BASE", "CRX_RPC"):
        monkeypatch.delenv(k, raising=False)


def main(session, tmp_path, account, **kw):
    args = dict(key=account.key.hex(), network="mainnet", base_url=BASE, rpc_url=RPC,
                state_dir=tmp_path / "state", session=session)
    args.update(kw)
    return crx.Client(**args)


def test_mainnet_is_listed_without_urls():
    net = crx.NETWORKS["mainnet"]
    assert net["chain"] == "ethereum"
    assert net["base_url"] is None and net["rpc_url"] is None


def test_mainnet_is_off_by_default(session, tmp_path, account):
    with pytest.raises(crx.ConfigError) as ei:
        main(session, tmp_path, account)
    assert "allow_mainnet" in str(ei.value)
    assert session.calls == []


def test_mainnet_opt_in_by_argument(session, tmp_path, account):
    c = main(session, tmp_path, account, allow_mainnet=True)
    assert c.network == "mainnet" and c.chain_key == "ethereum"


def test_mainnet_opt_in_by_env(session, tmp_path, account, monkeypatch):
    monkeypatch.setenv("CRX_ALLOW_MAINNET", "1")
    assert main(session, tmp_path, account).chain_key == "ethereum"
    monkeypatch.setenv("CRX_ALLOW_MAINNET", "yes")
    with pytest.raises(crx.ConfigError):
        main(session, tmp_path, account)


@pytest.mark.parametrize("missing", ["base_url", "rpc_url"])
def test_mainnet_has_no_default_urls(session, tmp_path, account, missing):
    with pytest.raises(crx.ConfigError) as ei:
        main(session, tmp_path, account, allow_mainnet=True, **{missing: None})
    assert missing in str(ei.value)


def test_mainnet_urls_from_env(session, tmp_path, account, monkeypatch):
    monkeypatch.setenv("CRX_BASE", BASE)
    monkeypatch.setenv("CRX_RPC", RPC)
    c = main(session, tmp_path, account, allow_mainnet=True, base_url=None, rpc_url=None)
    assert c._gw.base_url == BASE


def test_mainnet_reads_chain_1(session, tmp_path, account):
    session.routes[("GET", "/health")] = mainnet_health()
    c = main(session, tmp_path, account, allow_mainnet=True)
    chain = c._chain_info()
    assert chain["chain_id"] == 1 and chain["core"] == CORE
    assert c._sep == e7.domain_separator(1, CORE)


def test_mainnet_refuses_a_testnet_chain(session, tmp_path, account):
    h = mainnet_health()
    fuji_domain = e7.h0x(e7.domain_separator(43113, CORE))
    h["chains"][0].update(chain_id=43113, domain=fuji_domain)
    session.routes[("GET", "/health")] = h
    c = main(session, tmp_path, account, allow_mainnet=True)
    with pytest.raises(crx.ConfigError) as ei:
        c._chain_info()
    assert "chain 1" in str(ei.value)


def test_testnet_still_refuses_chain_1(make_client, session, health):
    h = dict(health)
    fuji = next(c for c in h["chains"] if c["key"] == "avax-fuji")
    h["chains"] = [dict(fuji, chain_id=1, domain=e7.h0x(e7.domain_separator(1, fuji["core"])))]
    session.routes[("GET", "/health")] = h
    with pytest.raises(crx.ConfigError) as ei:
        make_client()._chain_info()
    assert "testnets only" in str(ei.value)


def test_allow_mainnet_leaves_testnet_as_is(make_client):
    c = make_client(allow_mainnet=True)
    assert c.network == "testnet" and c.chain_key == "avax-fuji"


@pytest.mark.parametrize("network,wait", [("mainnet", 90), ("testnet", 30)])
def test_withdraw_wait_cap_per_network(session, tmp_path, account, health, network, wait):
    # Mainnet waits 90 s past sending, testnet 30 s: each run is the other's control.
    from .conftest import CHAIN_ID, Clock
    from .test_money import Gate, balance_body
    if network == "mainnet":
        session.routes[("GET", "/health")] = mainnet_health()
        session.rpc.update({"eth_chainId": "0x1", "eth_getCode": lambda p: "0x6080" if p[0].lower() == CORE else "0x"})
        chain_id, core, key = 1, CORE, "ethereum"
    else:
        chain_id, key = CHAIN_ID, "avax-fuji"
        core = next(c for c in health["chains"] if c["key"] == key)["core"]
    g = Gate(session, account, chain_id, core)
    session.routes[("GET", "/balance")] = [dict(balance_body(account), chain=key), g.view("sending", chain=key)]
    c = main(session, tmp_path, account, allow_mainnet=True, network=network)
    clock = Clock(time.time())
    c._clock, c._sleep = clock, clock.sleep
    t0 = clock()
    out = c.withdraw(1000)
    assert out.status == "sending" and clock() - t0 == wait and g.signer == account.address
    assert sum(1 for x in session.calls if x["path"] == "/balance") == 1 + wait + 1
    assert crx.NETWORKS[network]["settle_wait"] == wait


@pytest.mark.parametrize("now, at", [("08:30:00", "08:35:00"), ("08:35:00", "09:35:00"), ("08:40:00", "09:35:00")])
def test_mainnet_next_check_is_35_past_the_hour(session, tmp_path, account, now, at):
    from datetime import datetime, timezone
    c = main(session, tmp_path, account, allow_mainnet=True)
    c._clock = lambda: datetime.fromisoformat(f"2026-10-01T{now}+00:00").timestamp()
    assert c.next_check() == datetime.fromisoformat(f"2026-10-01T{at}+00:00")
