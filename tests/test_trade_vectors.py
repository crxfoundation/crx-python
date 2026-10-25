"""The Trade struct against the shared trade vectors."""

import json

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

from crx import _eip712 as e7
from crx.signer import LocalSigner, recover, sign_typed

from .conftest import FIX

PATH = FIX / "trade-vectors.json"
VEC = json.loads(PATH.read_text())
SIGNED = VEC["positive"] + VEC["format_edges"]
DOM = VEC["domain"]
SEP = e7.domain_separator(DOM["chainId"], DOM["verifyingContract"])
TAKER = VEC["keys"]["taker"]


def terms(v):
    """Return the taker leg's readable terms: pair, side, notional, rate, premium and maturity."""
    t = v["inputs"]["taker_leg"]
    return (v["inputs"]["pair_text"], int(t["side"]), int(t["notional"]), int(t["rate"]), int(t["premium_bps"]),
            int(t["expiry"]))


def words(leg):
    """Return a vector leg's hidden words in ``leg_fields`` order."""
    return [to_checksum_address(leg["seat"]), e7.hx(leg["leg_id"]), e7.hx(leg["pair"]), int(leg["side"]),
            int(leg["notional"]), int(leg["rate"]), int(leg["premium_bps"]), int(leg["expiry"]), int(leg["nonce"])]


def pair_c(inputs):
    """Return both half commitments and pairC = keccak(0x03 ‖ C_taker ‖ C_maker)."""
    c_taker = e7.half_commitment(words(inputs["taker_leg"]), inputs["taker_salt"])
    c_maker = e7.half_commitment(words(inputs["maker_leg"]), inputs["maker_salt"])
    return c_taker, c_maker, e7.pair_commitment(c_taker, c_maker)


def message(v, inputs=None):
    """Build the SDK's Trade message from a vector's inputs, with the taker leg's id, nonce and salt."""
    i = inputs or v["inputs"]
    t = i["taker_leg"]
    return e7.trade_message(*terms({"inputs": i}), e7.h0x(pair_c(i)[2]), t["leg_id"], t["nonce"], i["taker_salt"])


def typed_hash(td):
    m = encode_typed_data(full_message=td)
    return keccak(b"\x19" + m.version + m.header + m.body)


def test_signed_vectors():
    for v in SIGNED:
        c_taker, c_maker, pc = pair_c(v["inputs"])
        d = v["derived"]
        assert (e7.h0x(c_taker), e7.h0x(c_maker), e7.h0x(pc)) == (d["c_taker"], d["c_maker"], d["pair_c"])
        assert e7.h0x(e7.pair_id(v["inputs"]["pair_text"])) == d["pair_id"] == v["inputs"]["taker_leg"]["pair"]
        msg = message(v)
        assert msg["summary"] == e7.trade_summary(*terms(v)) == v["summary"]
        assert len(msg["summary"]) == v["summary_len"] <= e7.SUMMARY_MAX_LEN
        td = e7.typed_data("Trade", DOM["chainId"], DOM["verifyingContract"], msg)
        assert td == v["typed_data"] and json.dumps(td) == json.dumps(v["typed_data"])  # values and key order
        assert e7.h0x(e7.trade_struct_hash(msg)) == v["struct_hash"]
        assert e7.h0x(e7.trade_digest(SEP, msg)) == v["digest"]
        assert e7.h0x(e7.typed_digest(td)) == e7.h0x(typed_hash(td)) == v["digest"]
        sig = sign_typed(LocalSigner(Account.from_key(TAKER["private_key"])), td, e7.hx(v["digest"]), TAKER["address"])
        assert sig == v["signature"]
        assert recover(e7.hx(v["digest"]), e7.hx(sig)) == TAKER["address"]
