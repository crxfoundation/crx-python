"""The hand-built digests match eth-account's own EIP-712 encoder and the live domain; signers; session login."""

import copy
import os

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from eth_utils import keccak

from crx import _eip712 as e7
from crx._http import Gateway, rest_message

from .conftest import BASE, FakeSession

CORE = "0x0f6fba28791dfd909bd023e63bc072081610eeea"
DOMAIN = {"name": "CRX", "version": "rulebook-1.0", "chainId": 43113, "verifyingContract": CORE}


def typed_hash(primary, types, message):
    m = encode_typed_data(full_message={
        "types": {"EIP712Domain": [
            {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}], **types},
        "primaryType": primary, "domain": DOMAIN, "message": message})
    return keccak(b"\x19" + m.version + m.header + m.body)


def rnd():
    return "0x" + os.urandom(32).hex()


def test_domain_matches_live_health(health):
    c = next(c for c in health["chains"] if c["key"] == "avax-fuji")
    assert e7.h0x(e7.domain_separator(c["chain_id"], c["core"])) == c["domain"]


def test_trade_digest_matches_eth_account():
    msg = e7.trade_message("USD/MXN", -1, 25_000_000_000, 18_712_345, -3, 1_790_000_000, rnd(), rnd(),
                           "1789000000123", rnd())
    td = e7.typed_data("Trade", 43113, CORE, msg)
    types = {"Trade": td["types"]["Trade"]}
    assert e7.trade_digest(e7.domain_separator(43113, CORE), msg) == typed_hash("Trade", types, msg) == full(td)


def test_quote_digest_matches_eth_account():
    a = Account.create()
    leg_id = e7.leg_id_for(os.urandom(24), 1_790_000_600)
    words = [a.address, e7.hx(leg_id), e7.pair_id("USD/MXN"), -1, 25_000_000_000, 18_712_345, -3, 1_790_000_000,
             e7.maker_nonce(leg_id)]
    salt, ref = rnd(), e7.h0x(e7.taker_ref(Account.create().address, rnd()))
    td = e7.typed_data("Quote", 43113, CORE, e7.quote_message(words, salt, ref))
    sep = e7.domain_separator(43113, CORE)
    assert e7.quote_digest(sep, words, salt, ref) == full(td) == e7.typed_digest(td)


def test_withdraw_digest_matches_eth_account():
    a = Account.create()
    w = {"account": a.address.lower(), "amount": "1000000000", "nonce": "3", "deadline": 1_789_000_000}
    types = {"WithdrawIntent": [
        {"name": "account", "type": "address"}, {"name": "amount", "type": "uint256"},
        {"name": "nonce", "type": "uint64"}, {"name": "deadline", "type": "uint64"}]}
    msg = {"account": a.address, "amount": 10**9, "nonce": 3, "deadline": 1_789_000_000}
    assert e7.withdraw_digest(e7.domain_separator(43113, CORE), w) == typed_hash("WithdrawIntent", types, msg)


def test_rest_headers_recover_to_seat():
    a = Account.create()
    gw = Gateway("https://gateway.test", a, session=None)
    raw = b'{"chain":"avax-fuji","amount":"1000"}'
    h = gw.headers("POST", "/deposit", raw)
    seat = a.address.lower()
    assert set(h) == {"x-crx-address", "x-crx-ts", "x-crx-sig"} and h["x-crx-address"] == seat
    msg = rest_message("POST", "/deposit", seat, seat, int(h["x-crx-ts"]), raw)
    assert msg.endswith("Body: 0x" + keccak(raw).hex())
    assert msg.splitlines() == ["CRX-REST-LOGIN", "Audience: crx-gateway", "Method: POST", "Path: /deposit",
                                f"Custody: {seat}", f"Signer: {seat}", f"Timestamp: {h['x-crx-ts']}",
                                "Body: 0x" + keccak(raw).hex()]
    assert Account.recover_message(encode_defunct(text=msg), signature=h["x-crx-sig"]).lower() == seat


def test_scaled6_refuses_extra_precision():
    import pytest
    assert e7.scaled6("18.123456") == 18_123_456
    with pytest.raises(ValueError):
        e7.scaled6("18.1234567")


# ---------- typed data: the SDK's own objects hash to the digests it rebuilds ----------

import pytest  # noqa: E402

import crx  # noqa: E402
from crx.signer import LocalSigner, as_signer, recover, sign_hash_only, sign_login, sign_typed  # noqa: E402


def full(td):
    m = encode_typed_data(full_message=td)
    return keccak(b"\x19" + m.version + m.header + m.body)


def test_withdraw_typed_data_is_its_digest():
    sep = e7.domain_separator(43113, CORE)
    a = Account.create()
    w = {"account": a.address.lower(), "amount": 10**9, "nonce": 3, "deadline": 1_789_000_000}
    td = e7.typed_data("WithdrawIntent", 43113, CORE, e7.withdraw_message(w))
    assert set(td["message"]) == {"account", "amount", "nonce", "deadline"}
    assert full(td) == e7.typed_digest(td) == e7.withdraw_digest(sep, w)


@pytest.mark.parametrize("primary", ["AllocationConsent", "AllocationAcceptance", "FailoverConsent", "Terms"])
def test_close_consent_typed_data(primary):
    msg = {n: (rnd() if t == "bytes32" else str(1 + i % 100)) for i, (n, t) in enumerate(e7.fields_of(primary))}
    td = e7.typed_data(primary, 43113, CORE, msg)
    assert full(td) == e7.typed_digest(td)
    assert e7.typehash(primary) == keccak(text=primary + "(" + ",".join(
        f"{t} {n}" for n, t in e7.fields_of(primary)) + ")")


def a_trade_td():
    return e7.typed_data("Trade", 43113, CORE, e7.trade_message(
        "USD/BRL", 1, 5_000_000_000_000, 5_410_000, 0, 1_798_732_800, rnd(), rnd(), 1, rnd()))


def test_unknown_or_malformed_typed_data_has_no_digest():
    td = a_trade_td()
    for edit in (lambda d: d.update(primaryType="Side"), lambda d: d.update(primaryType="Leg"),
                 lambda d: d["types"]["Trade"].reverse(), lambda d: d["message"].pop("ownSalt"),
                 lambda d: d["message"].update(quoteExpiry="1"), lambda d: d["message"].update(wrapsHash=rnd()),
                 lambda d: d["domain"].update(chainId=True), lambda d: d.update(extra=1)):
        bad = copy.deepcopy(td)
        edit(bad)
        with pytest.raises(ValueError):
            e7.typed_digest(bad)


# ---------- exact amounts, /v1 sides ----------

@pytest.mark.parametrize("value,bits,want", [
    ("5000000", 128, 5_000_000_000_000), ("0.999999", 128, 999_999), ("25000.000000", 128, 25_000_000_000),
    ("340282366920938463463374607431768.211455", 128, 2**128 - 1), (7, 128, 7_000_000),
    ("18446744073709.551615", 64, 2**64 - 1),
])
def test_e6_exact(value, bits, want):
    assert e7.e6(value, bits) == want


@pytest.mark.parametrize("value,bits,why", [
    (5.41, 128, "float"), (True, 128, "unsigned"), ("1.0000001", 128, "more than 6"), ("-1", 128, "unsigned"),
    ("1e3", 128, "unsigned"), (" 1", 128, "unsigned"), ("340282366920938463463374607431768.211456", 128, "2\\^128"),
    ("18446744073709.551616", 64, "2\\^64"), ("٣", 128, "unsigned"),
])
def test_e6_refuses(value, bits, why):
    with pytest.raises(ValueError, match=why):
        e7.e6(value, bits)


def test_scaled6_rounds_where_e6_is_exact():
    # scaled6 rounds past 28 digits; the Trade path uses e6.
    big = "340282366920938463463374607431768.211455"
    assert e7.e6(big) == 2**128 - 1 != int(__import__("decimal").Decimal(big).scaleb(6))


@pytest.mark.parametrize("side,pair,want", [("buy_usd", "USD/BRL", "buy"), ("sell_usd", "USD/MXN", "sell")])
def test_v1_side_when_base_is_usd(side, pair, want):
    assert e7.v1_side(side, pair) == want


@pytest.mark.parametrize("side,pair", [("buy_usd", "EUR/USD"), ("sell_usd", "usd/brl"), ("buy", "USD/BRL")])
def test_v1_side_refused(side, pair):
    with pytest.raises(ValueError):
        e7.v1_side(side, pair)


# ---------- signers ----------

class HashOnly:
    """A KMS-like signer: signs a hash, never typed data."""

    def __init__(self, account):
        self.a, self.address, self.hashes = account, account.address, []

    def sign_hash(self, digest):
        self.hashes.append(digest)
        return self.a.unsafe_sign_hash(digest).signature

    def sign_message(self, message):
        return self.a.sign_message(encode_defunct(primitive=message)).signature


def test_hash_only_path_rebuilds_then_signs_the_digest():
    a = Account.create()
    w = {"account": a.address.lower(), "amount": 10**9, "nonce": 3, "deadline": 9}
    td = e7.typed_data("WithdrawIntent", 43113, CORE, e7.withdraw_message(w))
    digest = e7.withdraw_digest(e7.domain_separator(43113, CORE), w)
    s = HashOnly(a)
    sig = sign_typed(s, td, digest, a.address)
    assert s.hashes == [digest] and recover(digest, e7.hx(sig)) == a.address.lower()
    assert sig == sign_typed(LocalSigner(a), td, digest, a.address)  # same digest, same bytes
    with pytest.raises(crx.RefusedToSign, match="not this typed data"):
        sign_hash_only(s, td, b"\x00" * 32)
    assert len(s.hashes) == 1


def test_hash_only_path_refuses_a_digest_of_other_typed_data():
    a = Account.create()
    td = a_trade_td()
    other = copy.deepcopy(td)
    other["message"]["rateE6"] = "5410001"
    s = HashOnly(a)
    with pytest.raises(crx.RefusedToSign, match="the digest is not this typed data's"):
        sign_typed(s, td, e7.typed_digest(other), a.address)
    assert s.hashes == []
    sig = sign_typed(s, td, e7.typed_digest(td), a.address)
    assert s.hashes == [e7.typed_digest(td)] and sig == sign_typed(LocalSigner(a), td, e7.typed_digest(td), a.address)


def test_signer_shape_checked():
    with pytest.raises(crx.ConfigError):
        as_signer(object())

    class NoLogin:
        address = "0x" + "11" * 20

        def sign_typed_data(self, td):
            return b""
    with pytest.raises(crx.ConfigError):
        as_signer(NoLogin())


def test_login_signature_from_a_custodian_is_normalized():
    a = Account.create()

    class HighS(HashOnly):
        def sign_message(self, message):
            sig = bytes(super().sign_message(message))
            s = e7.SECP256K1_N - int.from_bytes(sig[32:64], "big")
            return sig[:32] + s.to_bytes(32, "big") + bytes([55 - sig[64] - 27])  # high s, v 0/1

    msg = rest_message("GET", "/balance", a.address.lower(), a.address.lower(), 1, b"")
    sig = sign_login(HighS(a), msg)
    assert int(sig[66:130], 16) <= e7.SECP256K1_N // 2 and sig[-2:] in ("1b", "1c")
    assert Account.recover_message(encode_defunct(text=msg), signature=sig) == a.address


# ---------- session login (custodian mode) ----------

TOKEN = "ab" * 32


def session_gateway(a, mint):
    s = FakeSession()
    s.routes[("POST", "/session")] = mint
    s.routes[("GET", "/balance")] = {"ok": True}
    return Gateway(BASE, HashOnly(a), s, login=True), s


def minted(a, ttl_ms=3_600_000, token=TOKEN):
    me = a.address.lower()
    return {"token": token, "custody": me, "signer": me, "ttl_ms": ttl_ms}


def test_session_login_signs_once_then_sends_the_token():
    a = Account.create()
    gw, s = session_gateway(a, minted(a))
    gw.request("GET", "/balance"), gw.request("GET", "/balance")
    mint, *reads = s.calls
    me = a.address.lower()
    assert (mint["method"], mint["path"], mint["raw"]) == ("POST", "/session", b"")
    assert set(mint["headers"]) == {"accept", "x-crx-address", "x-crx-ts", "x-crx-sig"}
    msg = rest_message("POST", "/session", me, me, int(mint["headers"]["x-crx-ts"]), b"")
    assert Account.recover_message(encode_defunct(text=msg), signature=mint["headers"]["x-crx-sig"]).lower() == me
    assert [r["headers"] for r in reads] == [{"accept": "application/json", "x-crx-session": TOKEN}] * 2


def test_session_refused_token_mints_once_more():
    a = Account.create()
    gw, s = session_gateway(a, [minted(a), minted(a, token="cd" * 32)])
    s.routes[("GET", "/balance")] = [(401, {"code": "unauthorized", "error": "token"}), {"ok": True}]
    assert gw.request("GET", "/balance") == {"ok": True}
    assert [c["path"] for c in s.calls] == ["/session", "/balance", "/session", "/balance"]
    assert s.calls[-1]["headers"]["x-crx-session"] == "cd" * 32


def test_session_off_signs_every_call():
    a = Account.create()
    gw, s = session_gateway(a, (404, {"code": "not_found", "error": "no"}))
    gw.request("GET", "/balance"), gw.request("GET", "/balance")
    assert [c["path"] for c in s.calls] == ["/session", "/balance", "/balance"]
    assert all(set(c["headers"]) == {"accept", "x-crx-address", "x-crx-ts", "x-crx-sig"} for c in s.calls)


@pytest.mark.parametrize("bad", [{"token": "zz"}, {"custody": "0x" + "11" * 20}, {"signer": "0x" + "11" * 20},
                                 {"ttl_ms": 0}, {"ttl_ms": True}])
def test_session_token_this_sdk_cannot_use_is_refused(bad):
    a = Account.create()
    gw, s = session_gateway(a, {**minted(a), **bad})
    with pytest.raises(crx.BadAnswer, match="/session"):
        gw.request("GET", "/balance")
    assert [c["path"] for c in s.calls] == ["/session"]


def test_viewer_never_takes_a_session_token():
    a = Account.create()
    gw, s = session_gateway(a, minted(a))
    gw.custody = Account.create().address.lower()
    gw.request("GET", "/balance")
    (call,) = s.calls
    assert call["path"] == "/balance" and call["headers"]["x-crx-signer"] == a.address.lower()
    assert call["headers"]["x-crx-address"] == gw.custody and "x-crx-session" not in call["headers"]
