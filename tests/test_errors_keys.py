import copy
import os
import pickle
import re
from pathlib import Path

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
        (409, {"code": "withdraw_in_progress", "error": "one withdraw at a time"}, crx.CrxError, "withdraw_in_progress"),
        (410, {"code": "quote_expired", "error": "gone"}, crx.QuoteExpired, "quote_expired"),
        (410, {"error": "gone"}, crx.QuoteExpired, "quote_expired"),
        (429, {"code": "rate_limited", "error": "slow", "details": {"retry_after_secs": 2}}, crx.RateLimited, "rate_limited"),
        (409, {"code": "quote_dropped", "error": "dropped", "details": {"best": None}}, crx.QuoteDropped, "quote_dropped"),
        (409, {"code": "quote_not_yours", "error": "not yours"}, crx.QuoteNotYours, "quote_not_yours"),
        (409, {"error": "quote_not_yours"}, crx.QuoteNotYours, "quote_not_yours"),
        (409, {"code": "leg_id_taken", "error": "held"}, crx.LegIdTaken, "leg_id_taken"),
        (409, {"code": "leg_live", "error": "live", "details": {"leg_id": "0x01"}}, crx.LegLive, "leg_live"),
        (409, {"code": "quote_fills_full", "error": "full"}, crx.QuoteFillsFull, "quote_fills_full"),
        (409, {"code": "already_accepted", "error": "taken", "details": {"trade_id": None}}, crx.AlreadyAccepted, "already_accepted"),
        (404, {"code": "unknown_or_ended", "error": "no leg"}, crx.UnknownOrEnded, "unknown_or_ended"),
        (401, {"code": "unauthorized", "error": "who"}, crx.AuthError, "unauthorized"),
        (400, {"code": "viewer_invalid", "error": "no"}, crx.BadRequest, "bad_request"),
        (400, {"code": "quote_format_outdated", "error": "outdated"}, crx.QuoteFormatOutdated, "quote_format_outdated"),
        (403, {"code": "viewer_is_maker", "error": "no"}, crx.BadRequest, "bad_request"),
        (503, {"code": "viewers_unavailable", "error": "off"}, crx.ServerError, "server_error"),
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


def test_no_fold_or_crank_words():
    root = Path(__file__).parent.parent
    files = [root / "README.md", *(root / "examples").glob("*.py"), *(root / "src" / "crx").glob("*.py")]
    hits = [f"{f.name}:{i}" for f in files for i, line in enumerate(f.read_text().splitlines(), 1)
            if re.search(r"\b(fold|folds|crank)\b", line, re.I)]
    assert hits == []


@pytest.mark.parametrize("details,headers,wait", [
    ({"retry_after_secs": 2}, None, 2.0), ({}, {"Retry-After": "9"}, 9.0),
    ({}, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, 0.0), ({"retry_after_secs": "x"}, None, None),
])
def test_rate_limited_retry_after(details, headers, wait):
    e = from_gateway(429, {"code": "rate_limited", "error": "slow", "details": details}, "",
                     retry_after=(headers or {}).get("Retry-After"))
    assert type(e) is crx.RateLimited and e.retry_after == wait


def test_quote_format_outdated_is_a_bad_request():
    assert "QuoteFormatOutdated" in crx.__all__ and issubclass(crx.QuoteFormatOutdated, crx.BadRequest)
    body = {"code": "quote_format_outdated", "error": "the quote is signed in an older format",
            "details": {"typehash": "0xe0bb"}}
    e = from_gateway(400, body)
    assert type(e) is crx.QuoteFormatOutdated and isinstance(e, crx.BadRequest)
    assert (e.code, e.gateway_code, e.status) == ("quote_format_outdated", "quote_format_outdated", 400)
    assert str(e) == "the quote is signed in an older format" and e.details == {"typehash": "0xe0bb"}
    assert type(from_gateway(400, {"error": "quote_format_outdated"})) is crx.QuoteFormatOutdated


def test_leg_live_and_already_accepted_read_their_details():
    e = from_gateway(409, {"code": "leg_live", "error": "live", "details": {"leg_id": "0xAB"}})
    assert e.leg_id == "0xab" and from_gateway(409, {"code": "leg_live", "error": "live"}).leg_id is None
    e = from_gateway(409, {"code": "already_accepted", "error": "x", "details": {"trade_id": "0xCD"}})
    assert e.trade_id == "0xcd"
    assert from_gateway(409, {"code": "already_accepted", "error": "x", "details": {"trade_id": None}}).trade_id is None


def test_market_closed_is_market_paused():
    assert crx.MarketClosed is crx.MarketPaused
    e = from_gateway(409, {"code": "market_paused", "error": "USD/JPY is paused", "details": {"pair": "USD/JPY"}})
    assert isinstance(e, crx.MarketClosed) and e.code == "market_paused"


def test_no_market_closed_code():
    root = Path(__file__).parent.parent
    files = [root / "README.md", *(root / "examples").glob("*.py"), *(root / "src" / "crx").glob("*.py")]
    assert [f.name for f in files if "market_closed" in f.read_text()] == []


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
    with pytest.raises(crx.ConfigError) as ei:
        crx.Client(network="devnet")
    assert "known: testnet, mainnet" in str(ei.value) and "fuji" not in str(ei.value)


@pytest.mark.parametrize("kw", [{}, {"network": "testnet"}, {"network": "fuji"}])
def test_testnet_and_fuji_alias(kw, account):
    c = crx.Client(key=account.key.hex(), **kw)
    assert c.network == "testnet" and c.chain_key == "avax-fuji"
    assert "network='testnet'" in repr(c)
    assert list(crx.NETWORKS) == ["testnet", "mainnet"]


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
