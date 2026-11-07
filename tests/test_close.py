"""close_trade(), close_requests() and answer_close() against a scripted gateway that serves the gateway's
close templates."""

from decimal import Decimal

import copy

import pytest

import crx
from crx import _close
from crx import _eip712 as e7
from crx.signer import recover

from .conftest import BASE, RPC, Clock, fixture

FV = fixture("format-vectors-v5.json")
EXITING, INCOMING = FV["keys"]["taker"], FV["keys"]["other"]  # anvil test keys 0 and 1: never fund them
VEC = fixture("close-vectors.json")
AG = fixture("close-agreement-vectors.json")  # one close with the counterparty, as crx-api served it
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
    """Return the consent template the gateway serves ``role`` on an elective round. ``side`` is the exiting seat's."""
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
    td = e7.typed_data("AllocationConsent", CHAIN_ID, CORE, _close.consent_message(w, commitment, SALT))
    t = {"novation_id": "0x" + "34" * 32, "rfq_id": RFQ, "chain": "avax-fuji", "commitment": commitment,
         "incoming_c": INC_C, "item": item, "old": old, "role": role, "digest_kind": "allocation_consent",
         "digest": e7.h0x(e7.typed_digest(td)), "typed_data": td, "signed": False, "trade_id": TRADE}
    if path is not None:
        t["path"] = path
    return t


def closed_position(side=1):
    return {"trade_id": TRADE, "rfq_id": "0x" + "30" * 32, "pair": PAIR, "side": side, "notional": "100",
            "rate": "18.5", "settlement": SETTLE * 1000, "status": "open"}


def client(session, tmp_path, key, clock, core=None):
    c = crx.Client(key=key["private_key"], base_url=BASE, rpc_url=RPC, state_dir=tmp_path / key["address"],
                   session=session, core=core)
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


def test_close_vectors_are_the_gateway_words():
    """The SDK's word rules equal crx-api's on the dumped rows: side ids and close quote ids."""
    for s in VEC["side_ids"]:
        assert e7.h0x(_close.side_id(s["incoming_c"])) == s["side_id"]
    for q in VEC["close_quote_ids"]:
        assert _close.close_quote_id(q["rfq_id"], q["maker"].lower(), q["rate_e6"], q["client_quote_id"]) == q["quote_id"]


# ── a close with the counterparty, as crx-api served it ──────────────────────

AG_RFQ = AG["template"]["remaining"]["rfq_id"]
AG_TRADE = AG["template"]["remaining"]["trade_id"]
AG_CORE = AG["health"]["chains"][0]["core"]
AG_T0 = AG["tape_remaining"][-5]["ts"] / 1000  # the trade.close_requested frame


def tape_route(rows):
    head = max(r["seq"] for r in rows)
    return lambda req: {"seq": head, "trades": [r for r in rows if r["seq"] > int(req["query"]["after"][0])]}


def counterparty_routes(session, template=None):
    session.routes.update({
        ("GET", "/health"): AG["health"],
        ("GET", "/trades"): tape_route(AG["tape_remaining"]),
        ("GET", "/positions"): AG["positions"]["remaining"],
        ("POST", f"/rfqs/{AG_RFQ}/close/answer"): AG["answer"]["body"],
        ("GET", f"/rfqs/{AG_RFQ}/close"): template or AG["template"]["remaining"],
        ("POST", f"/rfqs/{AG_RFQ}/close/sign"): AG["sign"]["remaining"]["body"],
        ("POST", f"/rfqs/{AG_RFQ}/close/decline"): {"rfq_id": AG_RFQ, "status": "declined"},
        ("GET", f"/rfqs/{AG_RFQ}"): {"status": "armed"},
        ("GET", f"/rfqs/{AG_RFQ}/close/arm-tx"): AG["arm_tx"][-1],
    })


def test_answer_close_signs_the_gateways_plain_close_and_reads_closed(session, tmp_path):
    """The counterparty reads the request, answers, and signs the gateway's own plain close: the same
    signature the gateway took. The trade ends for both seats."""
    c = client(session, tmp_path, INCOMING, Clock(AG_T0), core=AG_CORE)
    counterparty_routes(session)
    assert AG["template"]["remaining"]["digest"] == AG["template"]["exiting"]["digest"]
    req = next(iter(c.close_requests(wait=0)))
    assert (req.rfq_id, req.raw["trade_id"], req.closes_at.timestamp() * 1000) == (
        AG_RFQ, AG_TRADE, AG["tape_remaining"][-5]["data"]["answer_by"])
    out = c.answer_close(req, "156.5", wait=150, client_quote_id="rt-answer-1")
    assert (out.status, out.final, out.counterparty, out.trade_id) == ("closed", True, True, AG_TRADE)
    assert out.item == AG["arm_tx"][-1]["item"]
    sent = {x["path"]: x["body"] for x in session.calls if x["method"] == "POST"}
    assert sent[f"/rfqs/{AG_RFQ}/close/answer"] == AG["answer"]["sent"]
    assert sent[f"/rfqs/{AG_RFQ}/close/sign"] == AG["sign"]["remaining"]["sent"]


def edited(template, edit):
    """Return a copy of ``template`` with ``edit`` set; a key ``item.<name>`` sets the item's ``<name>``."""
    t = copy.deepcopy(template)
    for k, v in edit.items():
        (t["item"] if k.startswith("item.") else t)[k.removeprefix("item.")] = v
    return t


@pytest.mark.parametrize("edit,why", [
    ({"path": "move"}, "path is not the remaining seat's"),
    ({"item.close_rate": "156400000"}, "close rate is not 156.5"),
    ({"item.incoming_side": "0x" + "16" * 32}, "incoming_side is not its remaining_side"),
    ({"item.notional": "1500000000000"}, "carries leg words"),
])
def test_answer_close_declines_a_plain_close_off_its_answer(session, tmp_path, edit, why):
    t = edited(AG["template"]["remaining"], edit)
    c = client(session, tmp_path, INCOMING, Clock(AG_T0), core=AG_CORE)
    counterparty_routes(session, t)
    with pytest.raises(crx.RefusedToSign, match=why):
        c.answer_close(next(iter(c.close_requests(wait=0))), "156.5", wait=150, client_quote_id="rt-answer-1")
    assert f"/rfqs/{AG_RFQ}/close/sign" not in [x["path"] for x in session.calls]
    assert any(x["path"] == f"/rfqs/{AG_RFQ}/close/decline" for x in session.calls)


def test_a_late_answer_reads_quote_expired(session, tmp_path):
    """Past answer_by the gateway refuses 409 quote_window_closed; the SDK raises QuoteExpired and signs nothing."""
    c = client(session, tmp_path, INCOMING, Clock(AG_T0), core=AG_CORE)
    counterparty_routes(session)
    session.routes[("POST", f"/rfqs/{AG_RFQ}/close/answer")] = (409, {
        "code": "quote_window_closed", "message": "Too late to quote.",
        "error": f"close rfq {AG_RFQ} took answers until 1793700597371 (unix ms): it takes no more"})
    with pytest.raises(crx.QuoteExpired, match="last answer"):
        c.answer_close(next(iter(c.close_requests(wait=0))), "156.5", wait=150)
    assert [x["path"] for x in session.calls if x["method"] == "POST"] == [f"/rfqs/{AG_RFQ}/close/answer"]


def test_close_trade_signs_the_gateways_plain_close_and_reads_closed(session, tmp_path):
    """The exiting seat takes the counterparty's answer and signs the gateway's own plain close: the same
    signature the gateway took."""
    c = client(session, tmp_path, EXITING, Clock(AG_T0), core=AG_CORE)
    session.routes.update({
        ("GET", "/health"): AG["health"],
        ("GET", "/positions"): AG["positions"]["exiting"],
        ("POST", "/rfqs"): AG["open"]["body"],
        ("GET", f"/rfqs/{AG_RFQ}"): list(AG["views_exiting"]),
        ("POST", f"/rfqs/{AG_RFQ}/accept"): AG["accept"]["body"],
        ("GET", f"/rfqs/{AG_RFQ}/close"): AG["template"]["exiting"],
        ("POST", f"/rfqs/{AG_RFQ}/close/sign"): AG["sign"]["exiting"]["body"],
        ("GET", f"/rfqs/{AG_RFQ}/close/arm-tx"): list(AG["arm_tx"]),
    })
    out = c.close_trade(AG_TRADE, wait=75)
    remaining = AG["template"]["exiting"]["item"]["incoming"]
    assert (out.status, out.final, out.counterparty, out.to, out.rate) == ("closed", True, True, remaining,
                                                                           Decimal("156.5"))
    sent = {x["path"]: x["body"] for x in session.calls if x["method"] == "POST"}
    assert sent["/rfqs"]["position_id"] == AG["open"]["sent"]["position_id"]
    assert sent[f"/rfqs/{AG_RFQ}/accept"] == AG["accept"]["sent"]
    assert sent[f"/rfqs/{AG_RFQ}/close/sign"] == AG["sign"]["exiting"]["sent"]
