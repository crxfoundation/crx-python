"""The Trade struct against the shared trade vectors."""

import copy
import json
from decimal import Decimal

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
VEC = json.loads(PATH.read_text())
BY_ID = {v["id"]: v for v in VEC["positive"] + VEC["format_edges"] + VEC["negative"]}
SIGNED = VEC["positive"] + VEC["format_edges"]
DOM = VEC["domain"]
SEP = e7.domain_separator(DOM["chainId"], DOM["verifyingContract"])
TAKER = VEC["keys"]["taker"]
V1 = BY_ID["V1"]


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


def e6_text(v: int) -> str:
    return f"{v // 10**6}.{v % 10**6:06d}"


def ask_of(v, leg_id=None):
    """Return the taker's ask behind a vector, with the quote's rate and the RFQ's quote end."""
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
               "core": DOM["verifyingContract"]}, SEP, tmp_path / "state", clock=lambda: now)
    return b, session


def just_after_own_nonce(v):
    return int(v["inputs"]["taker_leg"]["nonce"]) / 1000 + 0.2


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


def test_v5_own_nonce_far_ahead_is_refused(tmp_path):
    v = BY_ID["V5"]
    b, session = binder(tmp_path, int(v["inputs"]["quote_expiry"]) - 300)
    with pytest.raises(crx.RefusedToSign, match="ownNonce is more than 24 h ahead"):
        b.check_trade({"typed_data": copy.deepcopy(v["typed_data"])}, ask_of(v))
    assert not b.state_path.exists() and session.calls == []


N6 = BY_ID["N6"]


def n6_typed_data():
    return e7.typed_data("Trade", DOM["chainId"], DOM["verifyingContract"], message(N6))


def test_n6_tail_off_by_one_second_is_refused(tmp_path):
    b, session = binder(tmp_path, just_after_own_nonce(V1))
    ask = ask_of(V1)
    ask["leg_id"] = N6["set"]["taker_leg_id"]
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
