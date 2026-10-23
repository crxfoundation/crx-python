"""EIP-712 typed data, exact amounts and the signers."""

import copy

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

import crx
from crx import _eip712 as e7
from crx._http import rest_message
from crx.signer import LocalSigner, sign_login, sign_typed

from .conftest import rnd

CORE = "0x072000d0eb830f767dc5128ed28139bb731b1521"


def test_scaled6_refuses_extra_precision():
    assert e7.scaled6("18.123456") == 18_123_456
    with pytest.raises(ValueError):
        e7.scaled6("18.1234567")


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


@pytest.mark.parametrize("value,bits,want", [
    (7, 128, 7_000_000), ("340282366920938463463374607431768.211455", 128, 2**128 - 1),
])
def test_e6_exact(value, bits, want):
    assert e7.e6(value, bits) == want


@pytest.mark.parametrize("value,bits,why", [
    (5.41, 128, "float"), ("1.0000001", 128, "more than 6"), ("-1", 128, "unsigned"),
    ("340282366920938463463374607431768.211456", 128, "2\\^128"), ("٣", 128, "unsigned"),
])
def test_e6_refuses(value, bits, why):
    with pytest.raises(ValueError, match=why):
        e7.e6(value, bits)


class HashOnly:
    """A signer that signs only raw hashes, as a hosted key service does."""

    def __init__(self, account):
        self.a, self.address, self.hashes = account, account.address, []

    def sign_hash(self, digest):
        self.hashes.append(digest)
        return self.a.unsafe_sign_hash(digest).signature

    def sign_message(self, message):
        return self.a.sign_message(encode_defunct(primitive=message)).signature


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


def test_typed_digest_refuses_a_domain_without_its_members():
    td = e7.typed_data("Terms", 1, "0x" + "44" * 20, {})
    del td["domain"]["chainId"]
    with pytest.raises(ValueError, match="the domain does not have its members"):
        e7.typed_digest(td)
