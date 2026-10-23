"""Signed and hidden formats against the shared format vectors."""

import hashlib
import json

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from crx import _eip712 as e7
from crx.signer import LocalSigner, recover, sign_typed

from .conftest import FIX

PATH = FIX / "format-vectors-v5.json"
SHA256 = "4d7c40e3aa2c67c07fc9d15c0fece8cb5fc61f96c312f4176e3001c1b83b4621"
VEC = json.loads(PATH.read_text())
TRADE_VEC = json.loads((FIX / "trade-vectors.json").read_text())
DOM = VEC["domain"]
SEP = e7.domain_separator(DOM["chainId"], DOM["verifyingContract"])
KEYS = VEC["keys"]
WITHDRAW_TYPEHASH_WITH_RECIPIENT = "0xc29b1c58edaab795013cfef1b8229080263aa099b26eb15ec60898253495a999"


def signer(name):
    return LocalSigner(Account.from_key(KEYS[name]["private_key"]))


def words(leg):
    """Return a vector leg's hidden words in ``leg_fields`` order."""
    return [to_checksum_address(leg["seat"]), e7.hx(leg["leg_id"]), e7.hx(leg["pair"]), int(leg["side"]),
            int(leg["notional"]), int(leg["rate"]), int(leg["premium_bps"]), int(leg["expiry"]), int(leg["nonce"])]


def own_typed(primary, message):
    return e7.typed_data(primary, DOM["chainId"], DOM["verifyingContract"], message)


def test_the_seven_types_and_no_other():
    assert hashlib.sha256(PATH.read_bytes()).hexdigest() == SHA256
    assert set(VEC["types"]) == set(e7.structs()) == {
        "Trade", "Quote", "Terms", "AllocationConsent", "FailoverConsent", "AllocationAcceptance", "WithdrawIntent"}
    for name, t in VEC["types"].items():
        assert e7.type_string(name) == t["type_string"]
        assert e7.h0x(e7.typehash(name)) == t["typehash"] == e7.h0x(keccak(text=t["type_string"]))


QUOTES = VEC["quotes"]


def test_quote_struct_hash_digest_message():
    for q in QUOTES:
        w, salt, ref = words(q["maker_leg"]), q["maker_salt"], q["taker_ref"]["value"]
        assert e7.h0x(e7.quote_struct_hash(w, salt, ref)) == q["struct_hash"]
        assert e7.h0x(e7.quote_digest(SEP, w, salt, ref)) == q["digest"]
        msg = e7.quote_message(w, salt, ref)
        assert msg == q["typed_data"]["message"] and list(msg) == list(q["typed_data"]["message"])
        td = own_typed("Quote", msg)
        assert td == q["typed_data"] and json.dumps(td) == json.dumps(q["typed_data"])
        assert e7.h0x(e7.typed_digest(td)) == q["digest"]
        leg, r = q["maker_leg"], q["taker_ref"]
        assert e7.maker_nonce(leg["leg_id"]) == int(leg["nonce"])
        assert e7.leg_id_tail(leg["leg_id"]) == int(q["quote_expiry"])
        assert e7.h0x(e7.taker_ref(r["taker_seat"], r["taker_leg_id"])) == r["value"]
        assert e7.h0x(e7.half_commitment(w, salt)) == q["c_maker"]


def test_maker_nonce_refuses_a_leg_id_of_another_length():
    for bad in ("0x" + "11" * 31, "0x" + "11" * 33, "11" * 32):
        with pytest.raises(ValueError):
            e7.maker_nonce(bad)


def test_terms_id():
    for t in VEC["terms"]:
        assert e7.h0x(e7.terms_id(t["inputs"])) == t["terms_id"]


ALLOC, CLOSE = VEC["allocation"], VEC["closeout"]


@pytest.mark.parametrize("part,fields,hidden", [
    (ALLOC["consent"], e7.ALLOCATION_ITEM_FIELDS, ALLOC), (ALLOC["acceptance"], e7.ALLOCATION_ITEM_FIELDS, ALLOC),
    (CLOSE["failover_consent"], e7.CLOSEOUT_ITEM_FIELDS, CLOSE),
], ids=["allocation-consent", "allocation-acceptance", "failover-consent"])
def test_consent_commitment_digest_and_signature(part, fields, hidden):
    td = part["typed_data"]
    assert e7.h0x(e7.hidden_commitment(fields, hidden["item"], hidden["salt"])) == hidden["commitment"]
    assert td["message"]["commitment"] == hidden["commitment"]
    assert own_typed(td["primaryType"], td["message"]) == td
    assert e7.h0x(e7.struct_hash(td["primaryType"], td["message"])) == part["struct_hash"]
    digest = e7.typed_digest(td)
    assert e7.h0x(digest) == part["digest"]
    who = part["signer"]
    assert sign_typed(signer(who), td, digest, KEYS[who]["address"]) == part["signature"]


W1 = VEC["withdraw"]


def test_withdraw_digest_message_signature_and_item_id():
    td, m = W1["typed_data"], W1["typed_data"]["message"]
    assert e7.withdraw_message(m) == m and own_typed("WithdrawIntent", e7.withdraw_message(m)) == td
    digest = e7.withdraw_digest(SEP, m)
    assert e7.h0x(digest) == W1["digest"] == e7.h0x(e7.typed_digest(td))
    assert e7.h0x(e7.struct_hash("WithdrawIntent", m)) == W1["struct_hash"]
    assert sign_typed(signer("taker"), td, digest, KEYS["taker"]["address"]) == W1["signature"]
    want = keccak(encode(["uint256", "address", "uint256", "uint64", "uint64"],
                         [5, to_checksum_address(m["account"]), int(m["amount"]), int(m["nonce"]), int(m["deadline"])]))
    assert e7.withdraw_item(m) == want


OLD_NEW = TRADE_VEC["quote_old_new"]


def test_withdraw_recipient_switch_off(monkeypatch):
    monkeypatch.setattr(e7, "DROP_WITHDRAW_RECIPIENT", False)
    assert e7.type_string("WithdrawIntent") == (
        "WithdrawIntent(address account,uint256 amount,address recipient,uint64 nonce,uint64 deadline)")
    assert e7.h0x(e7.typehash("WithdrawIntent")) == WITHDRAW_TYPEHASH_WITH_RECIPIENT
    monkeypatch.undo()
    assert e7.h0x(e7.typehash("WithdrawIntent")) == VEC["types"]["WithdrawIntent"]["typehash"]

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
    assert recover(e7.hx(old["digest"]), e7.hx(old["signature"])) == seat
    assert recover(digest, e7.hx(old["signature"])) != seat
