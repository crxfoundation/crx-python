"""The hand-built digests match eth-account's own EIP-712 encoder and the live domain."""

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
