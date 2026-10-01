"""The Trade struct against the shared vectors: summary, struct hash, digest, signature, refusals."""

import copy
import hashlib
import json

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

import crx
from crx import _eip712 as e7
from crx.signer import LocalSigner, recover, sign_typed

from .conftest import FIX

PATH = FIX / "trade-vectors.json"
SHA256 = "ae1e6d75239ca5da98f0c24de6f88525756aa6547a453ba1ecb7d8af3df86eb8"
VEC = json.loads(PATH.read_text())
BY_ID = {v["id"]: v for v in VEC["positive"] + VEC["format_edges"] + VEC["negative"]}
SIGNED = VEC["positive"] + VEC["format_edges"]
DOM = VEC["domain"]
SEP = e7.domain_separator(DOM["chainId"], DOM["verifyingContract"])
TAKER = VEC["keys"]["taker"]


def terms(v):
    """The taker leg's readable terms: pair, side, notional, rate, premium, band, maturity."""
    t = v["inputs"]["taker_leg"]
    return (v["inputs"]["pair_text"], int(t["side"]), int(t["notional"]), int(t["rate"]), int(t["premium_bps"]),
            int(t["im_bps"]), int(t["expiry"]))


def message(v):
    m = BY_ID["V1"]["typed_data"]["message"] if "typed_data" not in v else v["typed_data"]["message"]
    return e7.trade_message(*terms(v), m["pairC"], m["ownLegId"], m["quoteExpiry"], m["ownNonce"], m["ownSalt"],
                            m["wrapsHash"])


def typed_hash(td):
    m = encode_typed_data(full_message=td)
    return keccak(b"\x19" + m.version + m.header + m.body)


def test_fixture_is_the_pinned_file():
    assert hashlib.sha256(PATH.read_bytes()).hexdigest() == SHA256
    assert (len(VEC["positive"]), len(VEC["format_edges"]), len(VEC["negative"])) == (6, 16, 17)


def test_type_string_and_constants():
    assert e7.TRADE_TYPE == VEC["type_string"] and e7.h0x(e7.TRADE_TYPEHASH) == VEC["typehash"]
    assert e7.SIDE_TYPE == VEC["side_type_string_unchanged"] and e7.h0x(e7.SIDE_TYPEHASH) == VEC["side_typehash_unchanged"]
    c = VEC["constants"]
    assert e7.h0x(keccak(text="buy")) == c["keccak_buy"] and e7.h0x(keccak(text="sell")) == c["keccak_sell"]
    assert e7.MAX_MATURITY == int(c["max_maturity"]) and e7.SUMMARY_MAX_LEN == int(c["summary_max_len"])
    assert e7.h0x(SEP) == VEC["domain_separator"]
    assert e7.domain_json(DOM["chainId"], DOM["verifyingContract"]) == DOM


@pytest.mark.parametrize("v", SIGNED, ids=lambda v: v["id"])
def test_summary_struct_hash_digest(v):
    msg = message(v)
    assert msg["summary"] == e7.trade_summary(*terms(v)) == v["summary"]
    assert len(msg["summary"]) == v["summary_len"] <= e7.SUMMARY_MAX_LEN
    td = e7.typed_data("Trade", DOM["chainId"], DOM["verifyingContract"], msg)
    assert td == v["typed_data"] and json.dumps(td) == json.dumps(v["typed_data"])  # values and key order
    assert e7.h0x(e7.trade_struct_hash(msg)) == v["struct_hash"]
    assert e7.h0x(e7.trade_digest(SEP, msg)) == v["digest"]
    assert e7.h0x(e7.typed_digest(td)) == e7.h0x(typed_hash(td)) == v["digest"]
    m = v["typed_data"]["message"]
    t = {"pair_c": m["pairC"], "own_leg_id": m["ownLegId"], "quote_expiry": m["quoteExpiry"],
         "own_nonce": m["ownNonce"], "own_salt": m["ownSalt"], "wraps_hash": m["wrapsHash"]}
    assert e7.h0x(e7.side_digest(SEP, t)) == v["old_side_digest"]


@pytest.mark.parametrize("v", SIGNED, ids=lambda v: v["id"])
def test_signature(v):
    signer = LocalSigner(Account.from_key(TAKER["private_key"]))
    sig = sign_typed(signer, v["typed_data"], e7.hx(v["digest"]), TAKER["address"])
    assert sig == v["signature"]
    assert recover(e7.hx(v["digest"]), e7.hx(sig)) == TAKER["address"]


def test_worst_case_is_207_bytes():
    assert len(BY_ID["V5"]["summary"]) == 207 == e7.SUMMARY_MAX_LEN


# ---------- refusals a client sees ----------

@pytest.mark.parametrize("vid,why", [
    ("N1a", "side"), ("N1b", "side"), ("N1c", "side"), ("N1d", "side"),
    ("N2a", "maturity"), ("N2b", "maturity"),
    ("N3a", "pair"), ("N3b", "pair"), ("N3c", "pair"),
])
def test_format_refusals(vid, why):
    v = BY_ID[vid]
    assert v["check"] in (1, 2, 3)
    with pytest.raises(ValueError, match=why):
        e7.trade_summary(*terms(v))
    with pytest.raises(ValueError, match=why):
        message(v)


def test_n4_pair_text_is_not_the_leg_pair():
    v = BY_ID["N4"]
    assert e7.pair_text_ok(v["inputs"]["pair_text"])
    assert e7.h0x(e7.pair_id(v["inputs"]["pair_text"])) == v["keccak_pair_text"] != v["taker_pair"]


@pytest.mark.parametrize("vid", ["N5a", "N5b", "N5c", "N5d", "N5e"])
def test_n5_signatures_do_not_recover_to_the_taker(vid):
    v = BY_ID[vid]
    assert v["guest_digest"] == BY_ID["V1"]["digest"]
    assert recover(e7.hx(v["guest_digest"]), e7.hx(v["signature"])) == v["recovered_from_guest_digest"] != TAKER["address"]


@pytest.mark.parametrize("vid,field", [("N5b", "message.summary"), ("N5c", "message.summary"),
                                       ("N5e", "domain.chainId")])
def test_n5_served_object_differs_from_own(vid, field):
    v, own = BY_ID[vid], BY_ID["V1"]["typed_data"]
    served = copy.deepcopy(own)
    w = v["signed_what"]
    if w["kind"] == "summary":
        served["message"]["summary"] = w["summary"]
    else:
        served["domain"]["chainId"] = w["chainId"]
    assert e7.h0x(typed_hash(served)) == v["signed_digest"]
    assert e7.typed_mismatch(served, own) == field


def test_n5d_custodian_signing_with_another_key_is_refused():
    v = BY_ID["N5d"]

    class Other:
        address = TAKER["address"]

        def sign_typed_data(self, td):
            return v["signature"]

        def sign_message(self, m):
            raise AssertionError

    with pytest.raises(crx.RefusedToSign, match="does not recover to the seat"):
        sign_typed(Other(), BY_ID["V1"]["typed_data"], e7.hx(v["guest_digest"]), TAKER["address"])


def test_n5f_normalizes_to_v1():
    assert e7.h0x(e7.normalize_sig(BY_ID["N5f"]["signature"])) == BY_ID["V1"]["signature"]


def test_v_0_and_1_normalize_to_v1():
    # V1 and its high-s mate N5f carry opposite v: with 27 taken off, they cover v = 0 and v = 1.
    good, mate = e7.hx(BY_ID["V1"]["signature"]), e7.hx(BY_ID["N5f"]["signature"])
    raws = [good[:64] + bytes([good[64] - 27]), mate[:64] + bytes([mate[64] - 27])]
    assert sorted(r[64] for r in raws) == [0, 1]
    assert [e7.normalize_sig(r) for r in raws] == [good, good]


def test_n5g_v29_refused():
    with pytest.raises(ValueError, match="v is not"):
        e7.normalize_sig(BY_ID["N5g"]["signature"])


@pytest.mark.parametrize("bad", [b"\x00" * 64, b"\x00" * 66, "0x" + "00" * 65, 7])
def test_normalizer_refuses_malformed(bad):
    with pytest.raises(ValueError):
        e7.normalize_sig(bad)
