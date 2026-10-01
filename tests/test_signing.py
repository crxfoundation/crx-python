"""The hand-built digests match eth-account's own EIP-712 encoder and the live domain."""

import copy
import os

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from eth_utils import keccak

from crx import _eip712 as e7
from crx._http import Gateway, rest_message

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


def test_leg_digest_matches_eth_account():
    a = Account.create()
    leg = {"seat": a.address.lower(), "leg_id": rnd(), "join_ref": rnd(), "pair_id": e7.h0x(e7.pair_id("USD/MXN")),
           "instrument_id": 1, "side": -1, "notional": "25000.000000", "rate": "18.712345", "im_bps": 100,
           "premium_bps": -3, "expiry": 1_790_000_000_000, "nonce": "123456789", "quote_expiry": 1_789_000_000_000}
    types = {"Leg": [
        {"name": "seat", "type": "address"}, {"name": "legId", "type": "bytes32"}, {"name": "joinRef", "type": "bytes32"},
        {"name": "pair", "type": "bytes32"}, {"name": "instrumentId", "type": "uint8"}, {"name": "side", "type": "int8"},
        {"name": "notional", "type": "uint256"}, {"name": "rate", "type": "uint64"}, {"name": "imBps", "type": "uint16"},
        {"name": "premiumBps", "type": "int16"}, {"name": "expiry", "type": "uint40"}, {"name": "nonce", "type": "uint64"},
        {"name": "quoteExpiry", "type": "uint64"}]}
    msg = {"seat": a.address, "legId": e7.hx(leg["leg_id"]), "joinRef": e7.hx(leg["join_ref"]),
           "pair": e7.hx(leg["pair_id"]), "instrumentId": 1, "side": -1, "notional": 25_000_000_000,
           "rate": 18_712_345, "imBps": 100, "premiumBps": -3, "expiry": 1_790_000_000, "nonce": 123456789,
           "quoteExpiry": 1_789_000_000}
    sep = e7.domain_separator(43113, CORE)
    assert e7.leg_digest(sep, leg) == typed_hash("Leg", types, msg)


def test_side_digest_matches_eth_account():
    t = {"pair_c": rnd(), "own_leg_id": rnd(), "quote_expiry": 1_789_000_300, "own_nonce": 1_789_000_000_123,
         "own_salt": rnd(), "wraps_hash": rnd()}
    types = {"Side": [
        {"name": "pairC", "type": "bytes32"}, {"name": "ownLegId", "type": "bytes32"},
        {"name": "quoteExpiry", "type": "uint64"}, {"name": "ownNonce", "type": "uint64"},
        {"name": "ownSalt", "type": "bytes32"}, {"name": "wrapsHash", "type": "bytes32"}]}
    msg = {"pairC": e7.hx(t["pair_c"]), "ownLegId": e7.hx(t["own_leg_id"]), "quoteExpiry": t["quote_expiry"],
           "ownNonce": t["own_nonce"], "ownSalt": e7.hx(t["own_salt"]), "wrapsHash": e7.hx(t["wraps_hash"])}
    assert e7.side_digest(e7.domain_separator(43113, CORE), t) == typed_hash("Side", types, msg)


def test_withdraw_digest_matches_eth_account():
    a = Account.create()
    w = {"account": a.address.lower(), "amount": "1000000000", "recipient": a.address.lower(), "nonce": "3",
         "deadline": 1_789_000_000}
    types = {"WithdrawIntent": [
        {"name": "account", "type": "address"}, {"name": "amount", "type": "uint256"},
        {"name": "recipient", "type": "address"}, {"name": "nonce", "type": "uint64"},
        {"name": "deadline", "type": "uint64"}]}
    msg = {"account": a.address, "amount": 10**9, "recipient": a.address, "nonce": 3, "deadline": 1_789_000_000}
    assert e7.withdraw_digest(e7.domain_separator(43113, CORE), w) == typed_hash("WithdrawIntent", types, msg)


def test_rest_headers_recover_to_seat():
    a = Account.create()
    gw = Gateway("https://gateway.test", a, session=None)
    raw = b'{"chain":"avax-fuji","amount":"1000"}'
    h = gw.headers("POST", "/deposit", raw)
    seat = a.address.lower()
    assert h["x-crx-address"] == seat and h["x-crx-signer"] == seat
    msg = rest_message("POST", "/deposit", seat, seat, int(h["x-crx-ts"]), h["x-crx-nonce"], raw)
    assert msg.endswith("Body: 0x" + keccak(raw).hex())
    assert msg.splitlines()[:4] == ["CRX-REST-LOGIN", "Audience: crx-gateway", "Method: POST", "Path: /deposit"]
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


def test_leg_typed_data_is_the_leg_digest():
    a = Account.create()
    leg = {"seat": a.address.lower(), "leg_id": rnd(), "join_ref": rnd(), "pair_id": e7.h0x(e7.pair_id("USD/MXN")),
           "instrument_id": 1, "side": -1, "notional": "25000.000000", "rate": "18.712345", "im_bps": 100,
           "premium_bps": -3, "expiry": 1_790_000_000_000, "nonce": "123456789", "quote_expiry": 1_789_000_000_000}
    td = e7.typed_data("Leg", 43113, CORE, e7.leg_message(leg))
    sep = e7.domain_separator(43113, CORE)
    assert full(td) == e7.typed_digest(td) == e7.leg_digest(sep, leg)


def test_side_and_withdraw_typed_data_are_their_digests():
    sep = e7.domain_separator(43113, CORE)
    t = {"pair_c": rnd(), "own_leg_id": rnd(), "quote_expiry": 1_789_000_300, "own_nonce": "1789000000123",
         "own_salt": rnd(), "wraps_hash": rnd()}
    td = e7.typed_data("Side", 43113, CORE, e7.side_message(t))
    assert full(td) == e7.typed_digest(td) == e7.side_digest(sep, t)
    a = Account.create()
    w = {"account": a.address.lower(), "amount": 10**9, "recipient": a.address.lower(), "nonce": 3,
         "deadline": 1_789_000_000}
    td = e7.typed_data("WithdrawIntent", 43113, CORE, e7.withdraw_message(w))
    assert full(td) == e7.typed_digest(td) == e7.withdraw_digest(sep, w)


@pytest.mark.parametrize("primary", ["AllocationConsent", "AllocationAcceptance", "FailoverConsent"])
def test_close_consent_typed_data(primary):
    msg = {n: (rnd() if t == "bytes32" else str(1_789_000_000 + i)) for i, (n, t) in enumerate(e7.fields_of(primary))}
    td = e7.typed_data(primary, 43113, CORE, msg)
    assert full(td) == e7.typed_digest(td)
    assert e7.h0x(keccak(text=e7.STRUCTS[primary])) == e7.h0x(keccak(text=primary + "(" + ",".join(
        f"{t} {n}" for n, t in e7.fields_of(primary)) + ")"))


def test_unknown_or_malformed_typed_data_has_no_digest():
    td = e7.typed_data("Side", 43113, CORE, e7.side_message(
        {"pair_c": rnd(), "own_leg_id": rnd(), "quote_expiry": 1, "own_nonce": 1, "own_salt": rnd(), "wraps_hash": rnd()}))
    for edit in (lambda d: d.update(primaryType="Quote"), lambda d: d["types"]["Side"].reverse(),
                 lambda d: d["message"].pop("ownSalt"), lambda d: d["domain"].update(chainId=True),
                 lambda d: d.update(extra=1)):
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
    # The old helper rounds past 28 digits; the Trade path uses e6.
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
    w = {"account": a.address.lower(), "amount": 10**9, "recipient": a.address.lower(), "nonce": 3, "deadline": 9}
    td = e7.typed_data("WithdrawIntent", 43113, CORE, e7.withdraw_message(w))
    digest = e7.withdraw_digest(e7.domain_separator(43113, CORE), w)
    s = HashOnly(a)
    sig = sign_typed(s, td, digest, a.address)
    assert s.hashes == [digest] and recover(digest, e7.hx(sig)) == a.address.lower()
    assert sig == sign_typed(LocalSigner(a), td, digest, a.address)  # same digest, same bytes
    with pytest.raises(crx.RefusedToSign, match="not this typed data"):
        sign_hash_only(s, td, b"\x00" * 32)
    assert len(s.hashes) == 1


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

    msg = rest_message("GET", "/balance", a.address.lower(), a.address.lower(), 1, "n", b"")
    sig = sign_login(HighS(a), msg)
    assert int(sig[66:130], 16) <= e7.SECP256K1_N // 2 and sig[-2:] in ("1b", "1c")
    assert Account.recover_message(encode_defunct(text=msg), signature=sig) == a.address
