"""close_trade() and take_over() against a scripted gateway that serves the gateway's close templates."""

from decimal import Decimal

import pytest

import crx
from crx import _close
from crx import _eip712 as e7
from crx._maker import rfq_of
from crx.signer import recover

from .conftest import BASE, RPC, Clock, fixture

FV = fixture("format-vectors-v5.json")
EXITING, INCOMING = FV["keys"]["taker"], FV["keys"]["other"]  # anvil test keys 0 and 1: never fund them
VEC = fixture("close-vectors.json")
CORE, CHAIN_ID = "0x072000d0eb830f767dc5128ed28139bb731b1521", 43113
T0 = 1_792_800_000
TRADE, RFQ = "0x" + "31" * 32, "0x" + "33" * 32
EX, REM, INC_C, SALT = "0x" + "16" * 32, "0x" + "17" * 32, "0x" + "18" * 32, "0x" + "19" * 32
PAIR, NOTIONAL, ENTRY, SETTLE = "USD/MXN", 100_000_000, 18_500_000, 1_793_000_000


def old_terms(side):
    return {"notional": str(NOTIONAL), "premium_bps": 0, "nonce": 4242, "pair": e7.h0x(e7.pair_id(PAIR)),
            "side": side, "settlement": SETTLE, "rate": str(ENTRY)}


def terms_id(old, taker, maker):
    return e7.h0x(_close._terms_id(_close._old_terms(old), e7.hx(taker), e7.hx(maker)))


def camel(item, fields):
    return {n: item[_close._snake(n)] for n, _ in fields}


def allocation(role, incoming, *, rate=18_612_345, side=1, taker=True, deadline=T0 + 570, path=None, **edits):
    """Return the close template the gateway serves ``role`` on an elective round. ``side`` is the exiting seat's."""
    old = old_terms(side if taker else -side)
    item = {"old_id": terms_id(old, EX, REM) if taker else terms_id(old, REM, EX), "exiting_side": EX,
            "remaining_side": REM, "incoming": incoming, "incoming_side": e7.h0x(_close.side_id(INC_C)),
            "close_rate": str(rate), "spread": "0", "nonce": "7", "deadline": deadline,
            "pair_id": e7.h0x(e7.pair_id(PAIR)), "side": side, "notional": str(NOTIONAL), "settlement": SETTLE,
            "salt": SALT}
    if path == "close":
        item.update(pair_id="0x" + "00" * 32, side=0, notional="0", settlement=0)
    item.update(edits)
    w = camel(item, e7.ALLOCATION_ITEM_FIELDS)
    commitment = e7.h0x(e7.hidden_commitment(e7.ALLOCATION_ITEM_FIELDS, w, SALT))
    if role != "incoming":
        primary, kind, msg = "AllocationConsent", "allocation_consent", _close.consent_message(w, commitment, SALT)
    else:
        primary, kind = "AllocationAcceptance", "allocation_acceptance"
        msg = _close.acceptance_message(w, INC_C, commitment)
    td = e7.typed_data(primary, CHAIN_ID, CORE, msg)
    t = {"novation_id": "0x" + "34" * 32, "rfq_id": RFQ, "chain": "avax-fuji", "commitment": commitment,
         "incoming_c": INC_C, "item": item, "old": old, "role": role, "digest_kind": kind,
         "digest": e7.h0x(e7.typed_digest(td)), "typed_data": td, "signed": False}
    if role == "exiting":
        t["trade_id"] = TRADE
    if path is not None:
        t["path"] = path
    return t


def closed_position(side=1):
    return {"trade_id": TRADE, "rfq_id": "0x" + "30" * 32, "pair": PAIR, "side": side, "notional": "100",
            "rate": "18.5", "settlement": SETTLE * 1000, "status": "open"}


def client(session, tmp_path, key, clock):
    c = crx.Client(key=key["private_key"], base_url=BASE, rpc_url=RPC, state_dir=tmp_path / key["address"],
                   session=session)
    c._clock, c._sleep = clock, clock.sleep
    return c


def signed_by(digest_of, seat, answer):
    """Return a sign route that records the body and checks the signature recovers to ``seat``."""
    def route(req):
        assert recover(digest_of(), e7.hx(req["body"]["sig"])) == seat.lower()
        return answer
    return route


def test_close_trade_signs_the_served_consent_and_reads_closed(session, tmp_path):
    clock = Clock(T0)
    c = client(session, tmp_path, EXITING, clock)
    t = allocation("exiting", INCOMING["address"])
    session.routes.update({
        ("GET", "/positions"): {"positions": [closed_position()]},
        ("POST", "/rfqs"): {"rfq_id": RFQ, "position_id": TRADE, "kind": "close", "status": "open"},
        ("GET", f"/rfqs/{RFQ}"): [
            {"status": "open"},
            {"status": "quoted", "quote": {"quote_id": "0x" + "44" * 32, "rate": "18.612345", "status": "quoted"}},
            {"status": "signing"}, {"status": "consenting"}],
        ("POST", f"/rfqs/{RFQ}/accept"): {"rfq_id": RFQ, "status": "signing"},
        ("GET", f"/rfqs/{RFQ}/close"): t,
        ("POST", f"/rfqs/{RFQ}/close/sign"): signed_by(lambda: e7.hx(t["digest"]), EXITING["address"],
                                                       {"rfq_id": RFQ, "state": "armed"}),
        ("GET", f"/rfqs/{RFQ}/close/arm-tx"): [{"status": "sending", "item": None, "tx": None},
                                               {"status": "closed", "item": "0x" + "35" * 32,
                                                "tx": "0x" + "36" * 32, "final": True}],
    })
    out = c.close_trade(TRADE.upper().replace("0X", "0x"), wait=600)
    assert (out.status, out.to, out.rate, out.final) == ("closed", INCOMING["address"].lower(), Decimal("18.612345"), True)
    opened = next(r for r in session.calls if r["path"] == "/rfqs")
    assert opened["body"]["kind"] == "close" and opened["body"]["position_id"] == TRADE
    assert next(r for r in session.calls if r["path"].endswith("/accept"))["body"] == {"quote_id": "0x" + "44" * 32}


@pytest.mark.parametrize("edit,why", [
    ({"close_rate": "18700000"}, "close rate"),
    ({"spread": "1"}, "spread"),
    ({"incoming": EXITING["address"]}, "incoming seat"),
])
def test_take_over_declines_an_allocation_off_its_quote(session, tmp_path, edit, why):
    clock = Clock(T0)
    c = client(session, tmp_path, INCOMING, clock)
    seat = INCOMING["address"].lower()
    r = rfq_of({"rfq_id": RFQ, "kind": "close", "chain": "avax-fuji", "pair": "USDMXN", "side": 1,
                "notional": "100", "expiry": SETTLE * 1000}, 5)
    qid = _close.close_quote_id(RFQ, seat, 18_612_345, "take-1")
    edit = dict(edit)
    t = allocation("incoming", edit.pop("incoming", seat), **edit)
    session.routes.update({
        ("POST", f"/rfqs/{RFQ}/quotes"): {"quote_id": qid, "rate": "18.612345", "status": "quoted"},
        ("GET", "/trades"): {"seq": 6, "trades": [{"type": "rfq.awarded", "seq": 6, "data": {
            "rfq_id": RFQ, "quote_id": qid, "role": "incoming", "digest_kind": "allocation_acceptance"}}]},
        ("GET", f"/rfqs/{RFQ}/close"): t,
        ("POST", f"/rfqs/{RFQ}/close/decline"): {"rfq_id": RFQ, "status": "declined"},
    })
    with pytest.raises(crx.RefusedToSign, match=why):
        c.take_over(r, "18.612345", wait=60, client_quote_id="take-1")
    assert ("POST", f"/rfqs/{RFQ}/close/sign") not in [(x["method"], x["path"]) for x in session.calls]
    decline = next(x for x in session.calls if x["path"].endswith("/close/decline"))
    assert decline["body"] == {"quote_id": qid, "digest": t["digest"]}


def test_take_over_signs_the_gateway_close_out_template(session, tmp_path):
    """The gateway's own close-out template (crx-api c3696350) passes every check and signs."""
    v = VEC["forced"][0]
    t = v["template"]
    clock = Clock(int(t["item"]["deadline"]) - 1000)
    c = client(session, tmp_path, INCOMING, clock)
    seat = INCOMING["address"].lower()
    rate = int(t["pricing"]["quote_rate"])
    r = rfq_of({"rfq_id": t["rfq_id"], "kind": "close", "chain": "avax-fuji", "pair": "USDMXN",
                "side": v["c_side"], "notional": "100", "expiry": SETTLE * 1000}, 5)
    qid = _close.close_quote_id(t["rfq_id"], seat, rate, "take-2")
    rows = [{"type": "rfq.awarded", "seq": 6, "data": {"rfq_id": t["rfq_id"], "quote_id": qid,
                                                       "digest_kind": "failover_consent"}},
            {"type": "trade.novated", "seq": 7, "data": {"trade_id": "0x" + "41" * 32,
                                                         "close_rfq_id": t["rfq_id"]}}]
    rid = t["rfq_id"]
    session.routes.update({
        ("POST", f"/rfqs/{rid}/quotes"): {"quote_id": qid},
        ("GET", "/trades"): lambda req: {"seq": 7, "trades": [x for x in rows
                                                              if x["seq"] > int(req["query"]["after"][0])]},
        ("GET", f"/rfqs/{rid}/close"): t,
        ("POST", f"/rfqs/{rid}/close/sign"): signed_by(lambda: e7.hx(t["digest"]), seat,
                                                      {"rfq_id": rid, "state": "armed"}),
        ("GET", f"/rfqs/{rid}"): {"status": "armed"},
    })
    out = c.take_over(r, Decimal(rate).scaleb(-6), wait=600, client_quote_id="take-2")
    assert (out.status, out.trade_id, out.side) == ("open", "0x" + "41" * 32, "buy")


def test_close_vectors_are_the_gateway_words():
    """The SDK's word rules equal crx-api's on the dumped rows: side ids, open nonces, quote ids, fees."""
    for s in VEC["side_ids"]:
        assert e7.h0x(_close.side_id(s["incoming_c"])) == s["side_id"]
    for o in VEC["open_nonces"]:
        assert str(_close.open_nonce(o["old_id"], o["nonce"])) == o["open_nonce"]
    for q in VEC["close_quote_ids"]:
        assert _close.close_quote_id(q["rfq_id"], q["maker"].lower(), q["rate_e6"], q["client_quote_id"]) == q["quote_id"]
    for f in VEC["forced_spreads"]:
        got = _close.forced_spread(int(f["notional"]), int(f["mark"]), int(f["rate"]), f["side"], f["usd_is_base"],
                                   int(f["entry"]))
        assert str(got) == f["spread"]


def test_answer_close_signs_the_same_consent_as_the_exiting_seat(session, tmp_path):
    """The counterparty's answer wins, and both seats sign one AllocationConsent: the trade ends for both."""
    clock = Clock(T0)
    seat = INCOMING["address"].lower()
    plain = dict(path="close", incoming_side=REM, incoming_c="0x" + "00" * 32)
    t = allocation("remaining", seat, **plain)
    assert t["digest"] == allocation("exiting", seat, **plain)["digest"]
    c = client(session, tmp_path, INCOMING, clock)
    qid = _close.close_quote_id(RFQ, seat, 18_612_345, "answer-1")
    rows = [{"type": "trade.close_requested", "seq": 4, "ts": T0 * 1000, "data": {
        "rfq_id": RFQ, "trade_id": TRADE, "quote_expiry": (T0 + 120) * 1000, "pair": "USD/MXN", "notional": "100",
        "expiry": SETTLE * 1000}},
            {"type": "rfq.awarded", "seq": 6, "data": {"rfq_id": RFQ, "quote_id": qid,
                                                       "digest_kind": "allocation_consent"}},
            {"type": "trade.closed", "seq": 8, "data": {"trade_id": TRADE, "rfq_id": RFQ, "ended": True}}]
    session.routes.update({
        ("GET", "/trades"): lambda req: {"seq": 8, "trades": [x for x in rows if x["seq"] > int(req["query"]["after"][0])]},
        ("GET", "/positions"): {"positions": [closed_position(side=-1)]},
        ("POST", f"/rfqs/{RFQ}/close/answer"): {"quote_id": qid, "status": "quoted"},
        ("GET", f"/rfqs/{RFQ}/close"): t,
        ("POST", f"/rfqs/{RFQ}/close/sign"): signed_by(lambda: e7.hx(t["digest"]), seat, {"state": "armed"}),
        ("GET", f"/rfqs/{RFQ}"): {"status": "armed"},
        ("GET", f"/rfqs/{RFQ}/close/arm-tx"): {"status": "pending"},
    })
    req = next(iter(c.close_requests(wait=0)))
    out = c.answer_close(req, "18.612345", wait=600, client_quote_id="answer-1")
    assert (out.status, out.counterparty, out.trade_id) == ("closed", True, TRADE)
