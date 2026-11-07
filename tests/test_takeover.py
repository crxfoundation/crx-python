"""The unlisted takeover helper: bid, award, failover form check, then the signature or the decline.

Every form here is built by hand with eth_abi, keccak and eth_account, apart from the module under test.
"""

import pytest
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

import crx
from crx import _eip712 as e7
from crx import _takeover as tko

from .conftest import Clock, fixture

FV = fixture("format-vectors-v5.json")
HEALTH = fixture("health.json")["chains"][0]
CORE = HEALTH["core"]
SEP = HEALTH["domain"]
RFQ = "0x" + "c1" * 32
QID = "0x" + "c2" * 32
T0 = 1_800_000_000
SETTLE = T0 + 90 * 86_400
PAIR = "USD/BRL"
NOTIONAL = 1_000_000_000_000  # USD 1M at 6 dp
SIDE = -1  # the bidder's own side, the closed-out seat's direction
RATE = "5.401"
RATE_E6 = 5_401_000
MARK = 5_400_000
ENTRY = 5_300_000
FEE = NOTIONAL * (RATE_E6 - MARK) // MARK  # seller: P above the mark; USD is the base currency
CAP = "2000000"
OTHER = "0x" + "77" * 20

TERMS = ("Terms(bytes32 takerSide,bytes32 makerSide,uint256 notional,int16 premiumBps,uint64 nonce,bytes32 pair,"
         "int8 side,uint40 settlement,uint64 rate)")
FAILOVER_TYPES = [("oldId", "bytes32"), ("closedOutSide", "bytes32"), ("remainingSide", "bytes32"),
                  ("incomingSide", "bytes32"), ("incomingC", "bytes32"), ("nonce", "uint64"), ("openNonce", "uint64"),
                  ("deadline", "uint64"), ("commitment", "bytes32")]
DOMAIN_TYPES = [("name", "string"), ("version", "string"), ("chainId", "uint256"), ("verifyingContract", "address")]


def h(b: bytes) -> str:
    return "0x" + bytes(b).hex()


def b32(word: str) -> bytes:
    return bytes.fromhex(word[2:])


def typed(primary, fields, message):
    return {
        "types": {"EIP712Domain": [{"name": n, "type": t} for n, t in DOMAIN_TYPES],
                  primary: [{"name": n, "type": t} for n, t in fields]},
        "primaryType": primary,
        "domain": {"name": "CRX", "version": "rulebook-1.0", "chainId": 43113, "verifyingContract": CORE},
        "message": message,
    }


def digest_of(td) -> bytes:
    """The EIP-712 digest by eth_account."""
    m = encode_typed_data(full_message=td)
    return keccak(b"\x19" + m.version + m.header + m.body)


def old_leg(side=SIDE, notional=NOTIONAL, pair=PAIR, settle=SETTLE):
    return {"notional": str(notional), "premium_bps": 0, "nonce": 77, "pair": h(keccak(text=pair)), "side": side,
            "settlement": settle, "rate": str(ENTRY)}


def old_id_of(taker: bytes, maker: bytes, old: dict) -> bytes:
    return keccak(encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "int16", "uint64", "bytes32", "int8", "uint40", "uint64"],
        [keccak(text=TERMS), taker, maker, int(old["notional"]), old["premium_bps"], old["nonce"], b32(old["pair"]),
         old["side"], old["settlement"], int(old["rate"])]))


STRONG_C = bytes(range(1, 33))
STRONG_SALT = bytes(range(100, 132))
CLOSED, REMAINING = keccak(b"closed-out side"), keccak(b"remaining side")


def failover_form(seat, *, rate_e6=RATE_E6, side=SIDE, notional=NOTIONAL, mark=MARK, spread=None, subsidy=0,
           deadline=T0 + 1000, close_time=T0 - 360, incoming_c=STRONG_C, salt=STRONG_SALT, incoming_side=None,
           old=None, old_id=None, open_nonce=None, nonce=3, remaining=REMAINING):
    """Return a failover_consent form whose commitment, digest and typed data hash over its own words."""
    old = old or old_leg(notional=notional)
    taker, maker = (CLOSED, remaining) if side == old["side"] else (remaining, CLOSED)
    oid = old_id or old_id_of(taker, maker, old)
    onc = open_nonce if open_nonce is not None else int.from_bytes(
        keccak(b"crx/alloc-open-nonce/v1" + oid + nonce.to_bytes(8, "big"))[:8], "big")
    if spread is None:
        d = max(mark - rate_e6, 0) if side == 1 else max(rate_e6 - mark, 0)
        spread = notional * d // mark
    iside = incoming_side or keccak(b"CRX/side/v1" + incoming_c)
    feed = keccak(b"feed")
    words = [oid, CLOSED, remaining, to_checksum_address(seat), iside, feed, close_time, spread, nonce, deadline, onc,
             subsidy]
    kinds = ["bytes32", "bytes32", "bytes32", "address", "bytes32", "bytes32", "uint64", "uint256", "uint64", "uint64",
             "uint64", "uint256"]
    commitment = keccak(encode(kinds, words) + salt)
    msg = {"oldId": h(oid), "closedOutSide": h(CLOSED), "remainingSide": h(remaining), "incomingSide": h(iside),
           "incomingC": h(incoming_c), "nonce": nonce, "openNonce": onc, "deadline": deadline,
           "commitment": h(commitment)}
    td = typed("FailoverConsent", FAILOVER_TYPES, msg)
    return {
        "rfq_id": RFQ, "novation_id": "0x" + "c3" * 32, "role": "incoming", "digest_kind": "failover_consent",
        "digest": h(digest_of(td)), "typed_data": td, "signed": False, "chain": "avax-fuji", "domain_separator": SEP,
        "commitment": h(commitment), "incoming_c": h(incoming_c),
        "item": {"old_id": h(oid), "closed_out_side": h(CLOSED), "remaining_side": h(remaining), "incoming": seat,
                 "incoming_side": h(iside), "feed_id": h(feed), "close_time": close_time, "spread": str(spread),
                 "nonce": str(nonce), "deadline": deadline, "open_nonce": str(onc), "subsidy_max_usd": str(subsidy),
                 "salt": h(salt)},
        "old": old,
        "pricing": {"mark": str(mark), "notional": str(notional), "usd_is_base": True, "entry_rate": str(ENTRY),
                    "quote_rate": str(rate_e6), "side": side},
        "status": {"state": "signing", "deadline": deadline, "signed": False, "queued": False},
        "state": "signing",
    }


def view(status="open", quotes=(), **edit):
    """The close round as the bidder reads it: its own side, no position id."""
    d = {"rfq_id": RFQ, "kind": "close", "chain": "avax-fuji", "pair": PAIR, "side": SIDE, "notional": "1000000",
         "expiry": SETTLE * 1000, "premium_bps": 0, "closes_at": (T0 + 60) * 1000, "status": status,
         "quotes": list(quotes)}
    d.update(edit)
    return d


class Gate:
    """Script the close-round doors on ``session``; record each signature and decline."""

    def __init__(self, session, form=None, rfq=None):
        self.signed, self.declined = [], []
        s = session
        s.routes[("GET", f"/rfqs/{RFQ}")] = rfq if rfq is not None else view()
        s.routes[("POST", f"/rfqs/{RFQ}/quotes")] = lambda req: {
            "quote_id": QID, "rfq_id": RFQ, "rate": req["body"]["rate"],
            "client_quote_id": req["body"]["client_quote_id"]}
        if form is not None:
            s.routes[("GET", f"/rfqs/{RFQ}/close")] = form
        s.routes[("POST", f"/rfqs/{RFQ}/close/sign")] = self._sign
        s.routes[("POST", f"/rfqs/{RFQ}/close/decline")] = self._decline

    def _sign(self, req):
        self.signed.append(req["body"]["sig"])
        return {"rfq_id": RFQ, "state": "armed"}

    def _decline(self, req):
        self.declined.append(req["body"])
        return {"rfq_id": RFQ, "quote_id": QID, "status": "declined"}


NO_FORM = (404, {"code": "not_found", "error": "no close template on rfq for this account"})


@pytest.fixture
def client(make_client):
    return make_client(clock=Clock(T0))


def recover(digest, sig) -> str:
    return Account._recover_hash(b32(digest), signature=sig).lower()


# ── the happy paths ──────────────────────────────────────────────────────────


def test_take_over_bids_waits_and_signs_the_failover_form(client, session):
    form = failover_form(client.address)
    g = Gate(session, form=[NO_FORM, NO_FORM, form])
    out = tko.take_over(client, RFQ, RATE, max_notional=CAP, poll=0.5)
    assert out == tko.Signed(RFQ, "failover_consent", form["digest"], "armed")
    assert len(g.signed) == 1 and not g.declined
    assert form["digest"] == h(digest_of(form["typed_data"]))
    assert recover(form["digest"], g.signed[0]) == client.address
    quote = next(c for c in session.calls if c["path"] == f"/rfqs/{RFQ}/quotes")
    assert set(quote["body"]) == {"rate", "client_quote_id"}, "a close bid is a price only"
    assert quote["body"]["rate"] == RATE and quote["body"]["client_quote_id"].startswith("tko-")
    assert session.paths().count(f"/rfqs/{RFQ}/close") == 3
    assert FEE == int(form["item"]["spread"]) > 0
    assert not [n for n in crx.__all__ if "takeover" in n.lower().replace("_", "")]


def test_a_form_already_signed_posts_nothing(client, session):
    form = dict(failover_form(client.address), signed=True)
    g = Gate(session, form=form)
    out = tko.take_over(client, RFQ, RATE, max_notional=CAP)
    assert out.already and not g.signed and not g.declined


@pytest.mark.parametrize("edit, match", [
    ({"kind": "open"}, "not a close round"),
    ({"chain": "base"}, "this client signs for"),
])
def test_the_bid_refuses_another_round(client, session, edit, match):
    Gate(session, rfq=view(**edit))
    with pytest.raises(crx.BadRequest, match=match):
        tko.bid(client, RFQ, RATE)
    assert f"/rfqs/{RFQ}/quotes" not in session.paths("POST")


def test_the_bid_refuses_a_notional_over_the_cap(client, session):
    Gate(session)
    with pytest.raises(crx.BadRequest, match="over max_notional"):
        tko.bid(client, RFQ, RATE, max_notional="999999.999999")
    assert f"/rfqs/{RFQ}/quotes" not in session.paths("POST")


# ── each word off the bid: decline, raise, no signature ──────────────────────


def _cases():
    return [
        ("item.incoming", lambda s: failover_form(OTHER)),
        ("item.incoming_side", lambda s: failover_form(s, incoming_side=keccak(b"another side"))),
        ("item.remaining_side", lambda s: failover_form(s, remaining=CLOSED)),
        ("item.subsidy_max_usd", lambda s: failover_form(s, subsidy=500_000)),
        ("item.deadline", lambda s: failover_form(s, deadline=T0 + 3600)),
        ("item.deadline", lambda s: failover_form(s, deadline=T0)),
        ("item.close_time", lambda s: failover_form(s, close_time=T0 - 7200)),
        ("pricing.quote_rate", lambda s: failover_form(s, rate_e6=RATE_E6 + 1)),
        ("pricing.side", lambda s: failover_form(s, side=-SIDE)),
        ("pricing.notional", lambda s: failover_form(s, notional=NOTIONAL * 2)),
        ("item.salt", lambda s: failover_form(s, salt=b"\x0c" * 32)),
        ("item.salt", lambda s: failover_form(s, salt=bytes(range(8)) * 4)),
        ("incoming_c", lambda s: failover_form(s, incoming_c=b"\x0d" * 32)),
        ("item.old_id", lambda s: failover_form(s, old_id=keccak(b"another leg"))),
        ("item.open_nonce", lambda s: failover_form(s, open_nonce=1)),
        ("item.spread", lambda s: failover_form(s, spread=FEE - 1)),
        ("item.spread", lambda s: failover_form(s, spread=FEE + 1)),
        ("item.spread", lambda s: failover_form(s, mark=4_000_000, spread=2 * NOTIONAL // 100)),  # the cap cuts the fee
        ("old.pair", lambda s: failover_form(s, old=old_leg(pair="USD/MXN"))),
        ("old.settlement", lambda s: failover_form(s, old=old_leg(settle=SETTLE + 1))),
        ("commitment", lambda s: dict(failover_form(s), commitment="0x" + "ee" * 32)),
        ("digest", lambda s: dict(failover_form(s), digest="0x" + "ee" * 32)),
        ("domain_separator", lambda s: dict(failover_form(s), domain_separator="0x" + "ee" * 32)),
        ("chain", lambda s: dict(failover_form(s), chain="base")),
        ("rfq_id", lambda s: dict(failover_form(s), rfq_id="0x" + "ee" * 32)),
        ("role", lambda s: dict(failover_form(s), role="other")),
        ("digest_kind", lambda s: dict(failover_form(s), digest_kind="allocation_consent")),
        ("digest_kind", lambda s: dict(failover_form(s), digest_kind="allocation_acceptance")),
        ("digest_kind", lambda s: dict(failover_form(s), digest_kind="allocation_acceptance", signed=True)),
        ("pricing", lambda s: {k: v for k, v in failover_form(s).items() if k != "pricing"}),
        ("pricing.usd_is_base", lambda s: _repriced(failover_form(s), usd_is_base=False)),
        ("old", lambda s: {k: v for k, v in failover_form(s).items() if k != "old"}),
        ("typed_data.message.nonce", lambda s: _retyped(failover_form(s))),
        ("item.spread", lambda s: _reitem(failover_form(s), spread="1.5")),
    ]


def _retyped(form):
    td = form["typed_data"]
    form["typed_data"] = dict(td, message=dict(td["message"], nonce=99))
    return form


def _repriced(form, **edit):
    form["pricing"] = dict(form["pricing"], **edit)
    return form


def _reitem(form, **edit):
    form["item"] = dict(form["item"], **edit)
    return form


@pytest.mark.parametrize("word, build", _cases(), ids=[f"{i}-{w}" for i, (w, _) in enumerate(_cases())])
def test_a_form_off_the_bid_is_declined_and_not_signed(client, session, word, build):
    g = Gate(session, form=build(client.address))
    with pytest.raises(crx.RefusedToSign) as e:
        tko.take_over(client, RFQ, RATE, max_notional=CAP)
    assert e.value.details == {"rfq_id": RFQ, "word": word, "declined": True}
    assert g.declined == [{"quote_id": QID}] and not g.signed


def test_a_notional_over_the_cap_at_the_award_is_declined(client, session):
    g = Gate(session, form=failover_form(client.address))
    b = tko.bid(client, RFQ, RATE)
    with pytest.raises(crx.RefusedToSign) as e:
        tko.sign_award(client, RFQ, bid=b, max_notional="999999")
    assert e.value.details["word"] == "max_notional" and g.declined and not g.signed


def test_a_failed_decline_still_raises(client, session):
    g = Gate(session, form=failover_form(OTHER))
    session.routes[("POST", f"/rfqs/{RFQ}/close/decline")] = (409, {"code": "conflict", "error": "nothing declined"})
    with pytest.raises(crx.RefusedToSign) as e:
        tko.take_over(client, RFQ, RATE, max_notional=CAP)
    assert e.value.details["declined"] is False and not g.signed


def test_a_signer_failure_is_declined(client, session, monkeypatch):
    g = Gate(session, form=failover_form(client.address))
    monkeypatch.setattr(client._signer, "sign_typed_data", lambda td: b"\x01" * 65)
    with pytest.raises(crx.RefusedToSign) as e:
        tko.take_over(client, RFQ, RATE, max_notional=CAP)
    assert e.value.details["word"] == "signature" and g.declined and not g.signed


# ── the wait ─────────────────────────────────────────────────────────────────


def test_no_award_before_the_wait_ends_raises_timeout(client, session):
    g = Gate(session, form=NO_FORM)
    t0 = client._clock()
    with pytest.raises(crx.QuoteLost) as e:
        tko.take_over(client, RFQ, RATE, max_notional=CAP, wait=3, poll=1)
    assert e.value.reason == "timeout" and client._clock() - t0 == 3
    assert not g.signed and not g.declined


def test_the_default_wait_runs_to_the_window_end_plus_the_sign_grace(client, session):
    Gate(session, form=NO_FORM)
    t0 = client._clock()
    with pytest.raises(crx.QuoteLost):
        tko.take_over(client, RFQ, RATE, max_notional=CAP, poll=30)
    assert client._clock() - t0 == 60 + tko.SIGN_GRACE_S


def test_a_round_that_ends_unawarded_raises(client, session):
    g = Gate(session, form=NO_FORM, rfq=[view(), view(), view(status="expired")])
    with pytest.raises(crx.QuoteLost) as e:
        tko.take_over(client, RFQ, RATE, max_notional=CAP, poll=0.5)
    assert e.value.reason == "expired" and not g.signed and not g.declined


def test_a_declined_bid_raises(client, session):
    mine = {"quote_id": QID, "rate": RATE, "status": "declined"}
    Gate(session, form=NO_FORM, rfq=[view(), view(), view(quotes=[mine])])
    with pytest.raises(crx.QuoteLost) as e:
        tko.take_over(client, RFQ, RATE, max_notional=CAP, poll=0.5)
    assert e.value.details["status"] == "declined"


# ── the pinned vectors ───────────────────────────────────────────────────────


def test_the_failover_digest_matches_the_format_vectors():
    """format-vectors-v5.json closeout C1, computed by eth_account and viem; signed by keys.other."""
    c = FV["closeout"]
    fc = c["failover_consent"]
    dom = fc["typed_data"]["domain"]
    assert h(e7.hidden_commitment(e7.CLOSEOUT_ITEM_FIELDS, c["item"], c["salt"])) == c["commitment"]
    td = e7.typed_data("FailoverConsent", dom["chainId"], dom["verifyingContract"], fc["typed_data"]["message"])
    assert h(e7.typed_digest(td)) == fc["digest"] == h(digest_of(fc["typed_data"]))
    assert e7.typed_mismatch(fc["typed_data"], td) is None
    assert recover(fc["digest"], fc["signature"]) == FV["keys"]["other"]["address"].lower()
    assert keccak(text=e7.type_string("FailoverConsent")) == keccak(
        text="FailoverConsent(bytes32 oldId,bytes32 closedOutSide,bytes32 remainingSide,bytes32 incomingSide,"
             "bytes32 incomingC,uint64 nonce,uint64 openNonce,uint64 deadline,bytes32 commitment)")


def test_the_fee_and_cap_match_the_gateways_vectors():
    """The gateway's fee and cap vectors: USD/JPY 10M at mark 150, and EUR/USD 1M at mark 1.08."""
    n = 10_000_000_000_000
    assert tko.fee_usd6(n, 150_000_000, 149_850_000, 1, True) == 10_000_000_000
    assert tko.fee_usd6(n, 150_000_000, 150_150_000, -1, True) == 10_000_000_000
    assert tko.fee_usd6(n, 150_000_000, 149_850_000, -1, True) == 0
    assert tko.fee_usd6(n, 0, 100_000_000, -1, True) == 0
    assert tko.fee_usd6(n, 150_000_000, 100_000_000, 1, True) == 3_333_333_333_333
    assert tko.cap_usd6(n, 150_000_000, True) == 200_000_000_000
    assert tko.fee_usd6(1_000_000_000_000, 1_080_000, 1_079_000, 1, False) == 1_000_000_000
    assert tko.cap_usd6(1_000_000_000_000, 1_050_000, False) == 21_000_000_000
    assert tko.cap_usd6(2**128 - 1, 2**128 - 1, True) == 2 * ((2**128 - 1) // 10_000)
    assert tko.fee_usd6(2**128 - 1, 2**128 - 1, 0, 1, False) == 2**128 - 1


def test_the_side_id_and_open_nonce_formulas():
    c = bytes([0xE4]) * 32
    assert h(tko.incoming_side(c)) == "0x0b4cbadda91877bab7fe06ac35c8e18f89f0dc75b2bd904432803680893d4ad6"
    oid = b"\x11" * 32
    want = int.from_bytes(keccak(b"crx/alloc-open-nonce/v1" + oid + (9).to_bytes(8, "big"))[:8], "big")
    assert tko.open_nonce(oid, 9) == want


# ── the sign window ──────────────────────────────────────────────────────────


def test_the_reads_and_the_post_time_out_inside_the_sign_window(client, session):
    form = failover_form(client.address)
    Gate(session, form=form)
    tko.take_over(client, RFQ, RATE, max_notional=CAP)
    window = (f"/rfqs/{RFQ}/close", f"/rfqs/{RFQ}/close/sign")
    for c in [c for c in session.calls if c["path"] in window]:
        assert c["timeout"] == tko.HTTP_TIMEOUT_S == 2.0
