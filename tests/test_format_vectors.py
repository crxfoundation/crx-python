"""The signed and hidden formats against the shared format vectors: the seven types, the Quote,
the maker nonce, the taker ref, Terms ids, item commitments, consents and the withdraw intent."""

import hashlib
import json
from pathlib import Path

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from crx import _eip712 as e7
from crx.signer import LocalSigner, recover, sign_typed

from .conftest import FIX

PATH = FIX / "format-vectors-v5.json"
SHA256 = "4d7c40e3aa2c67c07fc9d15c0fece8cb5fc61f96c312f4176e3001c1b83b4621"
SPEC = Path.home() / "crx-scratch" / "readable-taker-terms-2026-10-01" / "spec"
VEC = json.loads(PATH.read_text())
TRADE_VEC = json.loads((FIX / "trade-vectors.json").read_text())
DOM = VEC["domain"]
SEP = e7.domain_separator(DOM["chainId"], DOM["verifyingContract"])
KEYS = VEC["keys"]
WITHDRAW_TYPEHASH_WITH_RECIPIENT = "0xc29b1c58edaab795013cfef1b8229080263aa099b26eb15ec60898253495a999"


def signer(name):
    return LocalSigner(Account.from_key(KEYS[name]["private_key"]))


def words(leg):
    """A vector leg's hidden words, in ``leg_fields`` order."""
    return [to_checksum_address(leg["seat"]), e7.hx(leg["leg_id"]), e7.hx(leg["pair"]), int(leg["side"]),
            int(leg["notional"]), int(leg["rate"]), int(leg["premium_bps"]), int(leg["expiry"]), int(leg["nonce"])]


def own_typed(primary, message):
    return e7.typed_data(primary, DOM["chainId"], DOM["verifyingContract"], message)


# ---------- the file ----------

def test_fixture_is_the_pinned_file():
    assert hashlib.sha256(PATH.read_bytes()).hexdigest() == SHA256


def test_fixture_is_the_spec_copy():
    if not SPEC.is_dir():
        pytest.skip("no spec folder on this machine")
    assert PATH.read_bytes() == (SPEC / "format-vectors-v5.json").read_bytes()


def test_switches_are_the_vector_defaults():
    assert VEC["switches"] == {"drop_quote_expiry": True, "drop_withdraw_recipient": True, "derive_maker_nonce": True}
    assert (e7.DROP_QUOTE_EXPIRY, e7.DROP_WITHDRAW_RECIPIENT, e7.DERIVE_MAKER_NONCE) == (True, True, True)


# ---------- the seven types ----------

def test_the_seven_types_and_no_other():
    assert set(VEC["types"]) == set(e7.structs()) == {
        "Trade", "Quote", "Terms", "AllocationConsent", "FailoverConsent", "AllocationAcceptance", "WithdrawIntent"}


@pytest.mark.parametrize("name", list(VEC["types"]))
def test_type_string_and_typehash(name):
    t = VEC["types"][name]
    assert e7.type_string(name) == t["type_string"]
    assert e7.h0x(e7.typehash(name)) == t["typehash"] == e7.h0x(keccak(text=t["type_string"]))


def test_domain_separator():
    assert e7.h0x(SEP) == VEC["domain_separator"]


# ---------- hidden words ----------

def test_hidden_word_lists():
    h = VEC["hidden_words"]
    assert h["leg"] == [n for n, _ in e7.leg_fields()]
    assert h["allocation_item"] == [n for n, _ in e7.ALLOCATION_ITEM_FIELDS]
    assert h["closeout_item"] == [n for n, _ in e7.CLOSEOUT_ITEM_FIELDS]


def test_quote_members_are_the_leg_words_salt_taker_ref():
    assert [n for n, _ in e7.quote_fields()] == VEC["hidden_words"]["leg"] + ["salt", "takerRef"]


# ---------- the Quote ----------

QUOTES = VEC["quotes"]


@pytest.mark.parametrize("q", QUOTES, ids=lambda q: q["id"])
def test_quote_struct_hash_digest_message(q):
    w, salt, ref = words(q["maker_leg"]), q["maker_salt"], q["taker_ref"]["value"]
    assert e7.h0x(e7.quote_struct_hash(w, salt, ref)) == q["struct_hash"]
    assert e7.h0x(e7.quote_digest(SEP, w, salt, ref)) == q["digest"]
    msg = e7.quote_message(w, salt, ref)
    assert msg == q["typed_data"]["message"] and list(msg) == list(q["typed_data"]["message"])
    td = own_typed("Quote", msg)
    assert td == q["typed_data"] and json.dumps(td) == json.dumps(q["typed_data"])
    assert e7.h0x(e7.typed_digest(td)) == q["digest"]


@pytest.mark.parametrize("q", QUOTES, ids=lambda q: q["id"])
def test_quote_leg_id_nonce_tail_taker_ref_commitment(q):
    leg, ref = q["maker_leg"], q["taker_ref"]
    assert e7.maker_nonce(leg["leg_id"]) == int(leg["nonce"])
    assert e7.leg_id_tail(leg["leg_id"]) == int(q["quote_expiry"])
    assert e7.h0x(e7.taker_ref(ref["taker_seat"], ref["taker_leg_id"])) == ref["value"]
    assert e7.h0x(e7.half_commitment(words(leg), q["maker_salt"])) == q["c_maker"]


@pytest.mark.parametrize("q", QUOTES, ids=lambda q: q["id"])
def test_quote_signature(q):
    seat = q["maker_leg"]["seat"]
    assert seat == KEYS["other"]["address"]
    td = own_typed("Quote", e7.quote_message(words(q["maker_leg"]), q["maker_salt"], q["taker_ref"]["value"]))
    assert sign_typed(signer("other"), td, e7.hx(q["digest"]), seat) == q["signature"]


# ---------- the maker nonce ----------

@pytest.mark.parametrize("d", VEC["maker_nonce"]["cases"], ids=lambda d: d["id"])
def test_maker_nonce(d):
    assert e7.maker_nonce(d["leg_id"]) == int(d["nonce"])
    assert int(d["nonce"]) == int.from_bytes(e7.hx(d["leg_id"])[16:24], "big")


def test_maker_nonce_d0_reads_bytes_16_to_24():
    d0 = VEC["maker_nonce"]["cases"][0]
    assert d0["id"] == "D0" and e7.maker_nonce(d0["leg_id"]) == 0x0102030405060708


@pytest.mark.parametrize("bad", ["0x" + "11" * 31, "0x" + "11" * 33, "11" * 32])
def test_maker_nonce_refuses_a_leg_id_of_another_length(bad):
    with pytest.raises(ValueError):
        e7.maker_nonce(bad)


def test_nd1_quote_over_another_nonce_does_not_recover_the_seat():
    n, q = VEC["maker_nonce"]["negative"], QUOTES[0]
    assert n["leg_id"] == q["maker_leg"]["leg_id"] and e7.maker_nonce(n["leg_id"]) == int(n["derived_nonce"])
    w = words(q["maker_leg"])
    sdk_digest = e7.quote_digest(SEP, w, q["maker_salt"], q["taker_ref"]["value"])
    assert e7.h0x(sdk_digest) == n["gateway_digest"] == q["digest"]
    w[8] = int(n["signed_nonce"])
    assert e7.h0x(e7.quote_digest(SEP, w, q["maker_salt"], q["taker_ref"]["value"])) == n["signed_digest"]
    assert recover(e7.hx(n["signed_digest"]), e7.hx(n["signature"])) == n["maker_seat"]
    assert recover(sdk_digest, e7.hx(n["signature"])) == n["recovered_from_gateway_digest"] != n["maker_seat"]


# ---------- the taker ref ----------

def test_taker_ref_is_the_packed_keccak():
    ref = QUOTES[0]["taker_ref"]
    packed = b"CRX/takerRef/v1" + e7.hx(ref["taker_seat"]) + e7.hx(ref["taker_leg_id"])
    assert len(packed) == 67 and e7.h0x(keccak(packed)) == ref["value"]
    assert e7.h0x(e7.taker_ref(ref["taker_seat"].upper().replace("0X", "0x"), ref["taker_leg_id"])) == ref["value"]


# ---------- Terms ids ----------

@pytest.mark.parametrize("t", VEC["terms"], ids=lambda t: t["id"])
def test_terms_id(t):
    assert e7.h0x(e7.terms_id(t["inputs"])) == t["terms_id"]


# ---------- allocation and close-out ----------

ALLOC, CLOSE = VEC["allocation"], VEC["closeout"]


def test_allocation_commitment():
    assert e7.h0x(e7.hidden_commitment(e7.ALLOCATION_ITEM_FIELDS, ALLOC["item"], ALLOC["salt"])) == ALLOC["commitment"]


def test_closeout_commitment():
    assert e7.h0x(e7.hidden_commitment(e7.CLOSEOUT_ITEM_FIELDS, CLOSE["item"], CLOSE["salt"])) == CLOSE["commitment"]


def test_commitment_is_keccak_of_words_then_salt():
    item = dict(ALLOC["item"])
    other_salt = "0x" + "06" * 32
    assert e7.hidden_commitment(e7.ALLOCATION_ITEM_FIELDS, item, other_salt) != e7.hx(ALLOC["commitment"])
    item["side"] = "1"
    assert e7.hidden_commitment(e7.ALLOCATION_ITEM_FIELDS, item, ALLOC["salt"]) != e7.hx(ALLOC["commitment"])


@pytest.mark.parametrize("part,commitment", [
    (ALLOC["consent"], ALLOC["commitment"]), (ALLOC["acceptance"], ALLOC["commitment"]),
    (CLOSE["failover_consent"], CLOSE["commitment"]),
], ids=["allocation-consent", "allocation-acceptance", "failover-consent"])
def test_consent_digest_and_signature(part, commitment):
    td = part["typed_data"]
    assert td["message"]["commitment"] == commitment
    assert own_typed(td["primaryType"], td["message"]) == td
    assert e7.h0x(e7.struct_hash(td["primaryType"], td["message"])) == part["struct_hash"]
    digest = e7.typed_digest(td)
    assert e7.h0x(digest) == part["digest"]
    who = part["signer"]
    assert sign_typed(signer(who), td, digest, KEYS[who]["address"]) == part["signature"]


# ---------- the withdraw intent ----------

W1 = VEC["withdraw"]


def test_withdraw_digest_message_signature():
    td, m = W1["typed_data"], W1["typed_data"]["message"]
    assert e7.withdraw_message(m) == m and own_typed("WithdrawIntent", e7.withdraw_message(m)) == td
    digest = e7.withdraw_digest(SEP, m)
    assert e7.h0x(digest) == W1["digest"] == e7.h0x(e7.typed_digest(td))
    assert e7.h0x(e7.struct_hash("WithdrawIntent", m)) == W1["struct_hash"]
    assert sign_typed(signer("taker"), td, digest, KEYS["taker"]["address"]) == W1["signature"]


def test_withdraw_item_id_is_kind_5_and_the_words():
    m = W1["typed_data"]["message"]
    want = keccak(encode(["uint256", "address", "uint256", "uint64", "uint64"],
                         [5, to_checksum_address(m["account"]), int(m["amount"]), int(m["nonce"]), int(m["deadline"])]))
    assert e7.withdraw_item(m) == want


# ---------- a Quote signed in the retired format ----------

OLD_NEW = TRADE_VEC["quote_old_new"]


def test_quote_old_new():
    new, old = OLD_NEW["new"], OLD_NEW["old"]
    seat = KEYS[OLD_NEW["signer"]]["address"]
    w, salt, ref = words(OLD_NEW["maker_leg"]), OLD_NEW["maker_salt"], OLD_NEW["taker_ref"]["value"]
    assert e7.h0x(e7.taker_ref(OLD_NEW["taker_ref"]["taker_seat"], OLD_NEW["taker_ref"]["taker_leg_id"])) == ref
    assert e7.type_string("Quote") == new["type_string"] and e7.h0x(e7.typehash("Quote")) == new["typehash"]
    assert e7.h0x(e7.typehash("Quote")) != old["typehash"]
    digest = e7.quote_digest(SEP, w, salt, ref)
    assert e7.h0x(digest) == new["digest"] and e7.h0x(e7.quote_struct_hash(w, salt, ref)) == new["struct_hash"]
    assert own_typed("Quote", e7.quote_message(w, salt, ref)) == new["typed_data"]
    assert sign_typed(signer(OLD_NEW["signer"]), new["typed_data"], digest, seat) == new["signature"]
    assert recover(e7.hx(old["digest"]), e7.hx(old["signature"])) == seat  # an honest old signature
    assert recover(digest, e7.hx(old["signature"])) != seat


# ---------- switches off: the member goes back in its place ----------

def test_withdraw_recipient_switch_off(monkeypatch):
    monkeypatch.setattr(e7, "DROP_WITHDRAW_RECIPIENT", False)
    assert e7.type_string("WithdrawIntent") == (
        "WithdrawIntent(address account,uint256 amount,address recipient,uint64 nonce,uint64 deadline)")
    assert e7.h0x(e7.typehash("WithdrawIntent")) == WITHDRAW_TYPEHASH_WITH_RECIPIENT
    monkeypatch.undo()
    assert e7.h0x(e7.typehash("WithdrawIntent")) == VEC["types"]["WithdrawIntent"]["typehash"]


def test_quote_expiry_switch_off(monkeypatch):
    trade, quote = VEC["types"]["Trade"]["type_string"], VEC["types"]["Quote"]["type_string"]
    monkeypatch.setattr(e7, "DROP_QUOTE_EXPIRY", False)
    assert e7.type_string("Trade") == trade.replace(
        "bytes32 ownLegId,uint64 ownNonce", "bytes32 ownLegId,uint64 quoteExpiry,uint64 ownNonce")
    assert e7.type_string("Quote") == quote.replace("uint64 nonce,bytes32 salt", "uint64 nonce,uint64 quoteExpiry,bytes32 salt")
    assert [n for n, _ in e7.leg_fields()][-1] == "quoteExpiry"
    monkeypatch.undo()
    assert e7.type_string("Trade") == trade and e7.type_string("Quote") == quote
