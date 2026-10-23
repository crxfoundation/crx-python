import copy
import os
import pickle

import pytest

import crx
from crx.errors import from_gateway

from .conftest import BASE, RPC


CODES = [
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
    (409, {"code": "withdraw_in_progress", "error": "one withdraw at a time"}, crx.WithdrawInProgress,
     "withdraw_in_progress"),
    (409, {"code": "trade_in_flight", "error": "a trade is opening"}, crx.TradeInFlight, "trade_in_flight"),
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
    (409, {"code": "brand_new", "error": "x\x1b[31m"}, crx.CrxError, "brand_new"),
]


def test_gateway_codes_map_to_typed_errors():
    for status, body, cls, code in CODES:
        e = from_gateway(status, body, body if isinstance(body, str) else "")
        assert (type(e), e.code, e.status) == (cls, code, status), body
        assert isinstance(e, crx.CrxError)


def test_no_key_is_read_only(monkeypatch):
    monkeypatch.delenv("CRX_WALLET_PK", raising=False)
    monkeypatch.delenv("CRX_WALLET_PK_FILE", raising=False)
    c = crx.Client(base_url=BASE, rpc_url=RPC)
    assert c.address is None
    with pytest.raises(crx.ConfigError):
        c.balance()


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


def _frames_text(exc):
    """Return the error text and the SDK frames of its traceback, with their locals."""
    import traceback
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename == __file__:
        tb = tb.tb_next
    assert tb is not None, "the error did not come from the SDK"
    text = "".join(traceback.format_list(traceback.StackSummary.extract(traceback.walk_tb(tb), capture_locals=True)))
    return text + repr(exc.args) + str(exc)


INIT_REFUSALS = [  # Client() argument, words of the refusal
    (dict(network="nowhere"), "unknown network"),
    (dict(signer=object()), "one only"),
    (dict(network="mainnet"), "mainnet is off"),
    (dict(account="0x12"), "account is not a wallet address"),
] + [(dict(keepalive=bad), "keepalive is None, or 1 to 10 s") for bad in (0.5, 11, True, "5")]


def test_client_init_refusals_hold_no_key(session, tmp_path, account, monkeypatch):
    """Each Client() refusal raises ConfigError, and no SDK frame, local or error text holds the key."""
    monkeypatch.delenv("CRX_ALLOW_MAINNET", raising=False)
    monkeypatch.chdir(tmp_path)
    key = account.key.hex()
    for kw, words in INIT_REFUSALS:
        with pytest.raises(crx.ConfigError, match=words) as ei:
            crx.Client(key=key, base_url=BASE, rpc_url=RPC, session=session, state_dir=tmp_path / "s", **kw)
        assert key.removeprefix("0x") not in _frames_text(ei.value), kw
