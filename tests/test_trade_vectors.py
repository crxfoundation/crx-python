"""The Trade struct against the shared vectors: summary, struct hash, digest, signature, the client's
checks before it signs, and the refusals a client sees."""

import copy
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

import crx
from crx import _eip712 as e7
from crx._bind import Binder
from crx._http import Gateway
from crx.signer import LocalSigner, recover, sign_typed

from .conftest import BASE, FIX, FakeSession

PATH = FIX / "trade-vectors.json"
SHA256 = "d784e894f128490c218f8fa6205bb2056262c281e12ef60e8c10d33771de5a6b"
TEMPLATE_PATH = FIX / "template-sample-v5.json"
TEMPLATE_SHA256 = "aec95090f2cb48fe5435d26198a57cabdbde632078471a24ef2678daeee50272"
SPEC = Path.home() / "crx-scratch" / "readable-taker-terms-2026-10-01" / "spec"
VEC = json.loads(PATH.read_text())
TEMPLATE = json.loads(TEMPLATE_PATH.read_text())
BY_ID = {v["id"]: v for v in VEC["positive"] + VEC["format_edges"] + VEC["negative"]}
SIGNED = VEC["positive"] + VEC["format_edges"]
DOM = VEC["domain"]
SEP = e7.domain_separator(DOM["chainId"], DOM["verifyingContract"])
TAKER = VEC["keys"]["taker"]
V1 = BY_ID["V1"]


def terms(v):
    """The taker leg's readable terms: pair, side, notional, rate, premium, maturity."""
    t = v["inputs"]["taker_leg"]
    return (v["inputs"]["pair_text"], int(t["side"]), int(t["notional"]), int(t["rate"]), int(t["premium_bps"]),
            int(t["expiry"]))


def words(leg):
    """A vector leg's hidden words, in ``leg_fields`` order."""
    return [to_checksum_address(leg["seat"]), e7.hx(leg["leg_id"]), e7.hx(leg["pair"]), int(leg["side"]),
            int(leg["notional"]), int(leg["rate"]), int(leg["premium_bps"]), int(leg["expiry"]), int(leg["nonce"])]


def pair_c(inputs):
    """pairC from the two halves: keccak(0x03 ‖ C_taker ‖ C_maker)."""
    c_taker = e7.half_commitment(words(inputs["taker_leg"]), inputs["taker_salt"])
    c_maker = e7.half_commitment(words(inputs["maker_leg"]), inputs["maker_salt"])
    return c_taker, c_maker, e7.pair_commitment(c_taker, c_maker)


def message(v, inputs=None):
    """The SDK's Trade message of a vector's inputs: own leg id, nonce and salt from the taker leg."""
    i = inputs or v["inputs"]
    t = i["taker_leg"]
    return e7.trade_message(*terms({"inputs": i}), e7.h0x(pair_c(i)[2]), t["leg_id"], t["nonce"], i["taker_salt"])


def typed_hash(td):
    m = encode_typed_data(full_message=td)
    return keccak(b"\x19" + m.version + m.header + m.body)


def e6_text(v: int) -> str:
    return f"{v // 10**6}.{v % 10**6:06d}"


def ask_of(v, leg_id=None):
    """The taker's own ask behind a vector: what it asked, the quote's rate, the RFQ view's quote end."""
    t, i = v["inputs"]["taker_leg"], v["inputs"]
    prem = int(t["premium_bps"])
    return {"pair": i["pair_text"], "side": int(t["side"]), "notional": e6_text(int(t["notional"])),
            "expiry": int(t["expiry"]) * 1000, "premium_bps": prem, "rate": Decimal(e6_text(int(t["rate"]))),
            "quote_expiry_max": int(i["rfq_quote_expiry_max"]), "max_premium_bps": abs(prem) or None,
            "leg_id": leg_id or t["leg_id"]}


def binder(tmp_path, now):
    session = FakeSession()
    gw = Gateway(BASE, None, session)
    b = Binder(gw, Account.from_key(TAKER["private_key"]), {"chain_id": DOM["chainId"],
               "core": DOM["verifyingContract"]}, SEP, tmp_path / "state", sleep=lambda s: None, clock=lambda: now)
    return b, session


def just_after_own_nonce(v):
    return int(v["inputs"]["taker_leg"]["nonce"]) / 1000 + 0.2


# ---------- the files ----------

@pytest.mark.parametrize("path,sha", [(PATH, SHA256), (TEMPLATE_PATH, TEMPLATE_SHA256)], ids=lambda x: str(x)[-24:])
def test_fixture_is_the_pinned_file(path, sha):
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha


@pytest.mark.parametrize("ours,spec", [(PATH, "trade-vectors-v5.json"), (TEMPLATE_PATH, "template-sample-v5.json")],
                         ids=["trade", "template"])
def test_fixture_is_the_spec_copy(ours, spec):
    if not SPEC.is_dir():
        pytest.skip("no spec folder on this machine")
    assert ours.read_bytes() == (SPEC / spec).read_bytes()


def test_vector_counts_and_switches():
    assert (len(VEC["positive"]), len(VEC["format_edges"]), len(VEC["negative"])) == (5, 14, 19)
    assert VEC["switches"] == {"drop_quote_expiry": e7.DROP_QUOTE_EXPIRY,
                               "drop_withdraw_recipient": e7.DROP_WITHDRAW_RECIPIENT,
                               "derive_maker_nonce": e7.DERIVE_MAKER_NONCE} == {
        "drop_quote_expiry": True, "drop_withdraw_recipient": True, "derive_maker_nonce": True}


def test_type_string_and_constants():
    assert e7.type_string("Trade") == VEC["type_string"] and e7.h0x(e7.typehash("Trade")) == VEC["typehash"]
    c = VEC["constants"]
    assert e7.h0x(keccak(text="buy")) == c["keccak_buy"] and e7.h0x(keccak(text="sell")) == c["keccak_sell"]
    assert e7.MAX_MATURITY == int(c["max_maturity"]) and e7.SUMMARY_MAX_LEN == int(c["summary_max_len"]) == 162
    assert e7.h0x(SEP) == VEC["domain_separator"]
    assert e7.domain_json(DOM["chainId"], DOM["verifyingContract"]) == DOM


# ---------- the signed vectors: V1 to V5 and the format edges ----------

@pytest.mark.parametrize("v", SIGNED, ids=lambda v: v["id"])
def test_pair_commitment(v):
    c_taker, c_maker, pc = pair_c(v["inputs"])
    d = v["derived"]
    assert (e7.h0x(c_taker), e7.h0x(c_maker), e7.h0x(pc)) == (d["c_taker"], d["c_maker"], d["pair_c"])
    assert e7.h0x(e7.pair_id(v["inputs"]["pair_text"])) == d["pair_id"] == v["inputs"]["taker_leg"]["pair"]


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


@pytest.mark.parametrize("v", SIGNED, ids=lambda v: v["id"])
def test_signature(v):
    td = e7.typed_data("Trade", DOM["chainId"], DOM["verifyingContract"], message(v))
    sig = sign_typed(LocalSigner(Account.from_key(TAKER["private_key"])), td, e7.hx(v["digest"]), TAKER["address"])
    assert sig == v["signature"]
    assert recover(e7.hx(v["digest"]), e7.hx(sig)) == TAKER["address"]


def test_worst_case_summary_is_162_bytes():
    assert len(BY_ID["V5"]["summary"]) == BY_ID["V5"]["summary_len"] == 162 == e7.SUMMARY_MAX_LEN
    assert max(v["summary_len"] for v in SIGNED) == 162


# ---------- the client's checks before it signs (SPEC v5 §5.2) ----------

@pytest.mark.parametrize("v", [v for v in SIGNED if v["id"] != "V5"], ids=lambda v: v["id"])
def test_client_signs_each_vector_from_its_own_ask(v, tmp_path):
    end = int(v["inputs"]["quote_expiry"])
    b, session = binder(tmp_path, end - 300)
    t = {"typed_data": copy.deepcopy(v["typed_data"])}
    digest, td = b.check_trade(t, ask_of(v))
    assert e7.h0x(digest) == v["digest"] and td == v["typed_data"]
    assert b.last_signed() == int(v["inputs"]["taker_leg"]["nonce"])
    b2, _ = binder(tmp_path / "again", end - 300)
    assert b2.sign_template(t, ask_of(v)) == v["signature"]
    assert session.calls == []


def test_v5_own_nonce_far_ahead_is_refused(tmp_path):
    v = BY_ID["V5"]
    b, session = binder(tmp_path, int(v["inputs"]["quote_expiry"]) - 300)
    with pytest.raises(crx.RefusedToSign, match="ownNonce is more than 24 h ahead"):
        b.check_trade({"typed_data": copy.deepcopy(v["typed_data"])}, ask_of(v))
    assert not b.state_path.exists() and session.calls == []


def test_v1_control_signs_and_matches(tmp_path):
    b, session = binder(tmp_path, just_after_own_nonce(V1))
    sig = b.sign_template({"typed_data": copy.deepcopy(V1["typed_data"])}, ask_of(V1))
    assert sig == V1["signature"]
    assert b.last_signed() == int(V1["typed_data"]["message"]["ownNonce"]) and session.calls == []


# ---------- N6: the taker leg id's tail is not the RFQ's quote end ----------

N6 = BY_ID["N6"]


def n6_typed_data():
    return e7.typed_data("Trade", DOM["chainId"], DOM["verifyingContract"], message(N6))


def test_n6_vector_is_an_honest_signature_over_its_own_trade():
    td = n6_typed_data()
    assert td["message"]["ownLegId"] == N6["set"]["taker_leg_id"] == N6["inputs"]["taker_leg"]["leg_id"]
    assert e7.leg_id_tail(td["message"]["ownLegId"]) == int(N6["inputs"]["rfq_quote_expiry_max"]) + 1
    assert e7.h0x(e7.typed_digest(td)) == N6["guest_digest"] == N6["signed_digest"]
    assert recover(e7.hx(N6["guest_digest"]), e7.hx(N6["signature"])) == TAKER["address"]
    assert N6["recovered_from_guest_digest"] == TAKER["address"]


@pytest.mark.parametrize("leg_id", [N6["set"]["taker_leg_id"], None], ids=["leg-id-served", "no-leg-id"])
def test_n6_tail_off_by_one_second_is_refused(tmp_path, leg_id):
    b, session = binder(tmp_path, just_after_own_nonce(V1))
    ask = ask_of(V1)
    ask["leg_id"] = leg_id
    t = {"typed_data": n6_typed_data()}
    assert t["typed_data"]["message"]["ownLegId"] != V1["typed_data"]["message"]["ownLegId"]
    with pytest.raises(crx.RefusedToSign, match="ownLegId tail is not the RFQ's quote_expiry_max"):
        b.check_trade(t, ask)
    with pytest.raises(crx.RefusedToSign, match="ownLegId tail is not the RFQ's quote_expiry_max"):
        b.sign_template(t, ask)
    assert not b.state_path.exists() and session.calls == []


def test_n6_with_v1_leg_id_in_the_ask_is_refused_as_another_leg(tmp_path):
    b, session = binder(tmp_path, just_after_own_nonce(V1))
    with pytest.raises(crx.RefusedToSign, match="ownLegId is not this RFQ's leg"):
        b.check_trade({"typed_data": n6_typed_data()}, ask_of(V1))
    assert not b.state_path.exists() and session.calls == []


# ---------- the template sample (PROPOSED until compared with a real gateway answer) ----------

def keys_sorted(x):
    if isinstance(x, dict):
        return list(x) == sorted(x) and all(keys_sorted(y) for y in x.values())
    return all(keys_sorted(y) for y in x) if isinstance(x, list) else True


def test_template_sample_is_v1_typed_data_alone():
    assert set(TEMPLATE) == {"typed_data"}
    assert TEMPLATE["typed_data"] == V1["typed_data"]
    assert keys_sorted(TEMPLATE)  # as serde_json emits them


def test_template_sample_is_accepted_with_v1_ask(tmp_path):
    b, session = binder(tmp_path, just_after_own_nonce(V1))
    digest, td = b.check_trade(copy.deepcopy(TEMPLATE), ask_of(V1))
    assert e7.h0x(digest) == V1["digest"] and td == V1["typed_data"]
    assert sign_typed(b.signer, td, digest, b.seat) == V1["signature"] and session.calls == []


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
    m = V1["typed_data"]["message"]
    with pytest.raises(ValueError, match=why):
        e7.trade_message(*terms(v), m["pairC"], m["ownLegId"], m["ownNonce"], m["ownSalt"])


def test_n4_pair_text_is_not_the_leg_pair():
    v = BY_ID["N4"]
    assert e7.pair_text_ok(v["inputs"]["pair_text"])
    assert e7.h0x(e7.pair_id(v["inputs"]["pair_text"])) == v["keccak_pair_text"] != v["taker_pair"]


@pytest.mark.parametrize("vid", ["N5a", "N5h"])
def test_retired_formats_do_not_recover_to_the_taker(vid):
    """N5a: the retired Side signature. N5h: the Trade with quoteExpiry and wrapsHash."""
    v = BY_ID[vid]
    sdk_digest = e7.h0x(e7.trade_digest(SEP, message(V1)))
    assert v["guest_digest"] == sdk_digest == V1["digest"] != v["signed_digest"]
    assert recover(e7.hx(v["signed_digest"]), e7.hx(v["signature"])) == TAKER["address"]  # an honest old signature
    who = recover(e7.hx(sdk_digest), e7.hx(v["signature"]))
    assert who == v["recovered_from_guest_digest"] != TAKER["address"]


@pytest.mark.parametrize("vid", ["N5b", "N5c", "N5d", "N5e"])
def test_n5_signatures_do_not_recover_to_the_taker(vid):
    v = BY_ID[vid]
    assert v["guest_digest"] == V1["digest"]
    assert recover(e7.hx(v["guest_digest"]), e7.hx(v["signature"])) == v["recovered_from_guest_digest"] != TAKER["address"]


@pytest.mark.parametrize("vid,field", [("N5b", "message.summary"), ("N5c", "message.summary"),
                                       ("N5e", "domain.chainId")])
def test_n5_served_object_differs_from_own(vid, field):
    v, own = BY_ID[vid], V1["typed_data"]
    served = copy.deepcopy(own)
    w = v["signed_what"]
    if w["kind"] == "summary":
        served["message"]["summary"] = w["summary"]
    else:
        served["domain"]["chainId"] = w["chainId"]
    assert e7.h0x(typed_hash(served)) == v["signed_digest"]
    assert e7.typed_mismatch(served, own) == field


@pytest.mark.parametrize("vid,field", [("N5b", "message.summary"), ("N5e", "domain.chainId")])
def test_n5_served_object_is_refused_before_signing(tmp_path, vid, field):
    w = BY_ID[vid]["signed_what"]
    served = copy.deepcopy(V1["typed_data"])
    if w["kind"] == "summary":
        served["message"]["summary"] = w["summary"]
    else:
        served["domain"]["chainId"] = w["chainId"]
    b, session = binder(tmp_path, just_after_own_nonce(V1))
    with pytest.raises(crx.RefusedToSign, match=f"differs at {field}"):
        b.check_trade({"typed_data": served}, ask_of(V1))
    assert not b.state_path.exists() and session.calls == []


def test_n5d_custodian_signing_with_another_key_is_refused():
    v = BY_ID["N5d"]

    class Other:
        address = TAKER["address"]

        def sign_typed_data(self, td):
            return v["signature"]

        def sign_message(self, m):
            raise AssertionError

    with pytest.raises(crx.RefusedToSign, match="does not recover to the seat"):
        sign_typed(Other(), V1["typed_data"], e7.hx(v["guest_digest"]), TAKER["address"])


def test_n5f_normalizes_to_v1():
    assert e7.h0x(e7.normalize_sig(BY_ID["N5f"]["signature"])) == V1["signature"]


def test_v_0_and_1_normalize_to_v1():
    # V1 and its high-s mate N5f carry opposite v: with 27 taken off, they cover v = 0 and v = 1.
    good, mate = e7.hx(V1["signature"]), e7.hx(BY_ID["N5f"]["signature"])
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
