import copy
import os
import pickle

import pytest
from eth_account import Account

import crx
from crx.errors import from_gateway

from .conftest import BASE, RPC


@pytest.mark.parametrize(
    "status,body,cls,code",
    [
        (409, {"code": "market_paused", "error": "paused", "details": {"pair": "USD/JPY"}}, crx.MarketPaused, "market_paused"),
        (400, {"code": "notional_below_minimum", "error": "min", "details": {"min": "10000"}}, crx.BelowMin, "below_min"),
        (400, {"code": "pool_min_notional", "error": "min"}, crx.BelowMin, "below_min"),
        (400, {"code": "notional_above_maximum", "error": "max"}, crx.AboveMax, "above_max"),
        (409, {"code": "pool_cap", "error": "cap"}, crx.AboveMax, "above_max"),
        (409, {"code": "rejected", "error": "no", "details": {"reject_code": "rj_0011223344556677"}}, crx.Rejected, "rejected"),
        (409, {"code": "own_round_open", "error": "open", "details": {"until": 1}}, crx.OwnRoundOpen, "own_round_open"),
        (403, {"code": "not_whitelisted", "error": "no"}, crx.NotWhitelisted, "not_whitelisted"),
        (400, {"code": "seat_not_ready", "error": "no"}, crx.SeatNotReady, "seat_not_ready"),
        (422, {"code": "insufficient_collateral", "error": "no"}, crx.InsufficientCollateral, "insufficient_collateral"),
        (410, {"code": "quote_expired", "error": "gone"}, crx.QuoteExpired, "quote_expired"),
        (410, {"error": "gone"}, crx.QuoteExpired, "quote_expired"),
        (429, {"code": "rate_limited", "error": "slow", "details": {"retry_after_secs": 2}}, crx.RateLimited, "rate_limited"),
        (401, {"code": "unauthorized", "error": "who"}, crx.AuthError, "unauthorized"),
        (502, {"code": "upstream", "error": "down"}, crx.ServerError, "server_error"),
        (503, "gateway down", crx.ServerError, "server_error"),
    ],
)
def test_gateway_codes_map_to_typed_errors(status, body, cls, code):
    e = from_gateway(status, body, body if isinstance(body, str) else "")
    assert type(e) is cls
    assert e.code == code
    assert e.status == status
    assert isinstance(e, crx.CrxError)


def test_unknown_code_passes_through():
    e = from_gateway(409, {"code": "brand_new", "error": "x\x1b[31m"})
    assert type(e) is crx.CrxError and e.code == "brand_new" and e.gateway_code == "brand_new"
    assert "\x1b" not in str(e)


def test_key_from_env(monkeypatch, account):
    monkeypatch.setenv("CRX_WALLET_PK", account.key.hex())
    c = crx.Client(base_url=BASE, rpc_url=RPC)
    assert c.address == account.address.lower()


def test_no_key_is_read_only(monkeypatch):
    monkeypatch.delenv("CRX_WALLET_PK", raising=False)
    monkeypatch.delenv("CRX_WALLET_PK_FILE", raising=False)
    c = crx.Client(base_url=BASE, rpc_url=RPC)
    assert c.address is None
    with pytest.raises(crx.ConfigError):
        c.balance()


def test_key_file_must_be_private(tmp_path, account):
    f = tmp_path / "seat.key"
    f.write_text(account.key.hex() + "\n")
    os.chmod(f, 0o644)
    with pytest.raises(crx.ConfigError, match="chmod 600"):
        crx.Client(key_file=f)
    os.chmod(f, 0o600)
    assert crx.Client(key_file=f).address == account.address.lower()


def test_key_file_from_env(monkeypatch, tmp_path, account):
    f = tmp_path / "seat.key"
    f.write_text(account.key.hex())
    os.chmod(f, 0o600)
    monkeypatch.delenv("CRX_WALLET_PK", raising=False)
    monkeypatch.setenv("CRX_WALLET_PK_FILE", str(f))
    assert crx.Client().address == account.address.lower()


def test_bad_key_never_echoed():
    secret = "0x" + "zz" * 32
    with pytest.raises(crx.ConfigError) as ei:
        crx.Client(key=secret)
    assert "zz" not in str(ei.value) and "zz" not in repr(ei.value)
    assert ei.value.__cause__ is None and ei.value.__context__ is None


def test_key_never_in_repr_or_pickle(account):
    c = crx.Client(key=account.key.hex(), base_url=BASE, rpc_url=RPC)
    k = account.key.hex().removeprefix("0x")
    assert k not in repr(c) and k not in str(c)
    assert "secret-path-key" not in repr(c._rpc) and "gateway.test" not in repr(c)
    with pytest.raises(TypeError):
        pickle.dumps(c)
    with pytest.raises(TypeError):
        copy.deepcopy(c)


@pytest.mark.parametrize("bad", ["0x" + "1" * 63, "1" * 65, b"\x01" * 31])
def test_key_length_is_exact(bad):
    with pytest.raises(crx.ConfigError):
        crx.Client(key=bad, base_url=BASE, rpc_url=RPC)


def test_key_leaves_frame_locals(account):
    import traceback
    k = account.key.hex()
    cases = [(k, {"network": "mainnet"}), (k, {"base_url": "http://gateway.test"}), (k, {"key_file": "x"}),
             (k[:-1], {}), (k + "1", {}), ("0x" + "zz" * 32, {}), (b"\x01" * 31, {})]
    for bad, kw in cases:
        try:
            crx.Client(key=bad, base_url=kw.pop("base_url", BASE), rpc_url=RPC, **kw)
        except crx.ConfigError as e:
            frames = [f for f, _ in traceback.walk_tb(e.__traceback__)][1:]  # the SDK's frames, not this test's
            assert frames and all(f.f_locals.get("key") is None for f in frames), kw
        else:
            raise AssertionError("no ConfigError")


def test_non_utf8_key_file_has_no_context(tmp_path):
    f = tmp_path / "seat.key"
    f.write_bytes(b"\xff\xfe" + b"\x01" * 30)
    os.chmod(f, 0o600)
    with pytest.raises(crx.ConfigError) as ei:
        crx.Client(key_file=f)
    assert ei.value.__context__ is None


def test_parts_refuse_pickle(make_client):
    c = make_client()
    for part in (c._gw, c._rpc, c._binder()):
        with pytest.raises(TypeError):
            pickle.dumps(part)


def test_both_key_and_file_refused(tmp_path, account):
    with pytest.raises(crx.ConfigError):
        crx.Client(key=account.key.hex(), key_file=tmp_path / "x")


def test_unknown_network():
    with pytest.raises(crx.ConfigError):
        crx.Client(network="mainnet")


def test_rpc_url_never_in_errors(make_client, session):
    import requests

    def down(*a, **k):
        raise requests.ConnectionError("https://rpc.test/secret-path-key refused")

    session.post = down
    c = make_client()
    with pytest.raises(crx.NetworkError) as ei:
        c._rpc("eth_chainId")
    assert "secret-path-key" not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__context__ is None


def test_rpc_non_json_is_bad_answer(make_client, session):
    import requests

    class NotJson:
        def json(self):
            raise requests.exceptions.JSONDecodeError("x", "doc", 0)

    session.post = lambda *a, **k: NotJson()
    with pytest.raises(crx.BadAnswer):
        make_client()._rpc("eth_chainId")


def test_bad_rpc_url_is_network(account):
    c = crx.Client(key=account.key.hex(), base_url=BASE, rpc_url="not a url/secret-path-key")
    with pytest.raises(crx.NetworkError) as ei:
        c._rpc("eth_chainId")
    assert "secret-path-key" not in str(ei.value) and ei.value.__context__ is None


def test_new_account_helper_not_needed():
    # A throwaway key works like any other.
    a = Account.create()
    assert crx.Client(key=a.key.hex(), base_url=BASE, rpc_url=RPC).address == a.address.lower()


@pytest.mark.parametrize("url", ["http://gateway.test", "ftp://x", "https://user:pw@gateway.test"])
def test_gateway_url_must_be_https(url):
    with pytest.raises(crx.ConfigError):
        crx.Client(base_url=url, rpc_url=RPC)


def test_localhost_http_allowed():
    crx.Client(base_url="http://localhost:8080", rpc_url=RPC)


def test_redirect_is_not_followed(make_client, session):
    session.routes[("GET", "/balance")] = (307, {"error": "moved"})
    with pytest.raises(crx.CrxError) as ei:
        make_client().balance()
    assert ei.value.status == 307
