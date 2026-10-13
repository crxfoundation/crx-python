"""The mainnet network: off by default, chain 1 only, gateway api.crxfx.com, RPC from config."""

import time

import pytest
from eth_utils import is_checksum_address, is_hex_address

import crx
from crx import _eip712 as e7
from crx.client import MAINNET_CORE

from .conftest import BASE, RPC

# The pinned mainnet core. While the source holds no address there, a stand-in core takes the row's place.
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


def test_mainnet_is_listed_with_its_gateway_and_no_rpc():
    net = crx.NETWORKS["mainnet"]
    assert net["chain"] == "ethereum"
    assert net["base_url"] == "https://api.crxfx.com" and net["rpc_url"] is None


def test_mainnet_core_is_the_one_constant_and_an_eip55_address_once_filled():
    row = crx.NETWORKS["mainnet"]["core"]
    assert row.lower() == CORE
    assert not FILLED or (row is MAINNET_CORE and is_checksum_address(MAINNET_CORE))


def test_readme_and_changelog_name_the_mainnet_core():
    from pathlib import Path
    root = Path(__file__).parent.parent
    assert f"Mainnet (chain 1): `{MAINNET_CORE}`." in (root / "README.md").read_text()
    log = (root / "CHANGELOG.md").read_text()
    entry = log[log.index(f"## {crx.__version__}\n"):]
    assert f"Ethereum mainnet (chain 1) pins core `{MAINNET_CORE}`." in entry[:entry.index("\n## ", 1)]


@pytest.mark.parametrize("core", ["0x" if FILLED else MAINNET_CORE, "<core>", "", None, "0x" + "9e" * 19, "9e" * 20,
                                  "0x" + "00" * 20, "0xC0c0C0C0c0C0c0C0c0C0c0C0c0C0c0C0c0C0c0C0"])
def test_mainnet_with_no_core_address_signs_nothing(session, tmp_path, account, monkeypatch, core):
    # A row core that is no address pins nothing: every signing call refuses, also when /health serves a core.
    monkeypatch.setitem(crx.NETWORKS["mainnet"], "core", core)
    session.routes[("GET", "/health")] = mainnet_health()
    session.rpc.update({"eth_chainId": "0x1", "eth_getCode": lambda p: "0x6080" if p[0].lower() == CORE else "0x"})
    c = main(session, tmp_path, account, allow_mainnet=True)
    with pytest.raises(crx.ConfigError, match="^network 'mainnet' pins no chain id or core; nothing signed. Pass core="):
        c._chain_info()
    with pytest.raises(crx.ConfigError, match="pins no chain id or core; nothing signed"):
        c.deposit(1000)
    assert session.paths("POST") == [] and "eth_sendRawTransaction" not in session.rpc_methods()


def test_mainnet_core_override_still_signs_with_no_core_address_in_the_row(session, tmp_path, account, monkeypatch):
    monkeypatch.setitem(crx.NETWORKS["mainnet"], "core", "<core>")
    session.routes[("GET", "/health")] = mainnet_health()
    c = main(session, tmp_path, account, allow_mainnet=True, core=CORE)
    assert c._chain_info()["core"] == CORE and c._sep == e7.domain_separator(1, CORE)


def test_mainnet_health_on_another_core_refuses(session, tmp_path, account):
    # The core move: /health names a core other than the pin, with a domain that matches it. Nothing signed.
    other = "0x" + "c0" * 20
    h = mainnet_health()
    h["chains"][0].update(core=other, domain=e7.h0x(e7.domain_separator(1, other)))
    session.routes[("GET", "/health")] = h
    c = main(session, tmp_path, account, allow_mainnet=True)
    with pytest.raises(crx.RefusedToSign, match=f"^core moved: /health names {other}, the SDK pins {CORE} on ethereum"):
        c._chain_info()


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


def test_mainnet_has_no_default_rpc(session, tmp_path, account):
    with pytest.raises(crx.ConfigError) as ei:
        main(session, tmp_path, account, allow_mainnet=True, rpc_url=None)
    assert "rpc_url" in str(ei.value)


def test_mainnet_default_gateway(session, tmp_path, account):
    c = main(session, tmp_path, account, allow_mainnet=True, base_url=None)
    assert c._gw.base_url == "https://api.crxfx.com"


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


@pytest.mark.parametrize("held, have", [(0, "0.000000"), (999_999_999, "999.999999")])
def test_mainnet_deposit_short_of_usdc_sends_nothing(session, tmp_path, account, held, have):
    # Mainnet has no mint: a short wallet with the default mint=True gets the shortfall, and no approve.
    from eth_utils import to_checksum_address

    from .conftest import Clock
    from .test_money import Chain
    token, raw = mainnet_health()["chains"][0]["base_token"], 1000 * 10**6
    session.routes[("GET", "/health")] = mainnet_health()
    session.rpc.update({"eth_chainId": "0x1", "eth_getCode": lambda p: "0x6080" if p[0].lower() == CORE else "0x"})
    chain = Chain(session, held=held)
    session.routes[("POST", "/deposit")] = {"amount_raw": str(raw), "transactions": [
        {"to": token, "chain_id": 1, "data": e7.calldata(
            "approve(address,uint256)", ["address", "uint256"], [to_checksum_address(CORE), raw])},
        {"to": CORE, "chain_id": 1, "data": e7.calldata("deposit(uint256)", ["uint256"], [raw])}]}
    c = main(session, tmp_path, account, allow_mainnet=True)
    clock = Clock(time.time())
    c._clock, c._sleep = clock, clock.sleep
    with pytest.raises(crx.TxFailed, match=rf"^not enough USDC: you have {have}, need 1000\.000000$"):
        c.deposit(1000)
    assert chain.sent == [] and not {"eth_estimateGas", "eth_sendRawTransaction"} & set(session.rpc_methods())
    approve = e7.h0x(e7.selector("approve(address,uint256)"))
    assert not [p for m, p in session.rpc_calls if m == "eth_call" and p[0]["data"].startswith(approve)]
