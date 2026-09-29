"""quote() and trade() against a scripted gateway and chain."""

import os
import time
from decimal import Decimal

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

import crx
from crx import _eip712 as e7

from .conftest import CHAIN_ID, Clock, open_market

RFQ = "0x" + "11" * 32
LEG = "0x" + "22" * 32
JOIN = "0x" + "33" * 32
QID = "0x" + "44" * 32
TXH = "0x" + "55" * 32
WRAPS_HASH = e7.h0x(keccak(encode(["bytes[]"], [[]])))


def rnd():
    return "0x" + os.urandom(32).hex()


class Venue:
    """One RFQ round: the gateway's answers, built from what the client sent."""

    def __init__(self, session, health, markets, clock, pair="USD/MXN"):
        self.s, self.clock = session, clock
        self.chain = next(c for c in health["chains"] if c["key"] == "avax-fuji")
        self.sep = e7.domain_separator(CHAIN_ID, self.chain["core"])
        self.rfq_body = None
        self.accept_body = None
        self.template = None
        self.side_sig = None
        self.row_edit = {}
        self.template_edit = {}
        # (trade_status, trade_tx) served one per poll once the Side is in; the last repeats.
        self.script = [("sending", None), ("open", TXH)]
        session.routes[("GET", "/markets")] = open_market(markets, pair)
        session.routes[("POST", "/rfqs")] = self.open_rfq
        session.routes[("GET", f"/rfqs/{RFQ}")] = [{"quote": None, "quotes": []}, self.view]
        session.routes[("POST", f"/rfqs/{RFQ}/accept")] = self.accept
        session.routes[("POST", f"/rfqs/{RFQ}/side")] = self.side

    def open_rfq(self, req):
        self.rfq_body = req["body"]
        self.qe_ms = int(self.clock() * 1000) + 3_600_000
        return {"rfq_id": RFQ, "leg_id": LEG, "quote_expiry": self.qe_ms, "im_bps": req["body"]["im_bps"],
                "side": 1 if req["body"]["side"] == "buy" else -1, "expiry": req["body"]["expiry"],
                "status": "open", "kind": "open", "join_ref": None}

    def row(self):
        b = self.rfq_body
        r = {"quote_id": QID, "rfq_id": RFQ, "rate": "18.700000", "expires_at": int(self.clock() * 1000) + 60_000,
             "leg_id": LEG, "join_ref": JOIN, "side": 1 if b["side"] == "buy" else -1,
             "pair_id": e7.h0x(e7.pair_id(b["pair"][:3] + "/" + b["pair"][3:])), "instrument_id": 1,
             "notional": f"{Decimal(b['notional']):.6f}", "premium_bps": 0, "expiry": b["expiry"],
             "status": "live", "house": False}
        r.update(self.row_edit)
        return r

    def view(self, req):
        v = {"quote": self.row(), "quotes": [self.row()]}
        if self.side_sig is not None:
            word, tx = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            v.update(trade_status=word, trade_tx=tx)
        return v

    def accept(self, req):
        self.accept_body = leg = req["body"]["leg"]
        own_nonce = int(self.template_edit.get("own_nonce", int(self.clock() * 1000)))
        qe = int(self.template_edit.get("quote_expiry", int(self.clock()) + 300))
        salt, c_maker = rnd(), keccak(os.urandom(32))
        c_taker = e7.half_commitment(e7.arm_words(leg, own_nonce, qe), salt)
        t = {"own_leg_id": leg["leg_id"], "own_nonce": str(own_nonce), "quote_expiry": qe, "own_salt": salt,
             "c_taker": e7.h0x(c_taker), "c_maker": e7.h0x(c_maker),
             "pair_c": e7.h0x(e7.pair_commitment(c_taker, c_maker)), "wraps_hash": WRAPS_HASH}
        t.update(self.template_edit)
        t["digest"] = e7.h0x(e7.side_digest(self.sep, t))
        self.template = t
        return {"status": "accepted", "leg_hash": e7.h0x(e7.leg_digest(self.sep, leg)), "side": t}

    def side(self, req):
        self.side_sig = req["body"]["sig"]
        return {"status": "signed"}


@pytest.fixture
def clock():
    return Clock(time.time())


@pytest.fixture
def venue(session, health, markets, clock):
    return Venue(session, health, markets, clock)


def test_quote_then_trade_binds(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert (q.rfq_id, q.quote_id, q.rate, q.side, q.pair) == (RFQ, QID, Decimal("18.700000"), "buy", "USD/MXN")
    assert venue.rfq_body["pair"] == "USDMXN" and venue.rfq_body["notional"] == "25000"
    assert venue.rfq_body["chain"] == "avax-fuji" and venue.rfq_body["im_bps"] == 100
    assert "/rfqs/" + RFQ + "/accept" not in session.paths()  # quote() never accepts

    t = c.trade(q)
    assert (t.status, t.tx) == ("open", TXH)
    leg = venue.accept_body
    assert leg["seat"] == account.address.lower() and leg["leg_id"] == LEG and leg["join_ref"] == JOIN
    body = next(x for x in session.calls if x["path"].endswith("/accept"))["body"]
    assert Account._recover_hash(e7.leg_digest(venue.sep, leg), signature=body["sig"]) == account.address
    assert Account._recover_hash(e7.side_digest(venue.sep, venue.template), signature=venue.side_sig) == account.address
    assert sent_nothing(session)
    floor = (c._state_dir / f"side-nonce-{account.address.lower()}").read_text()
    assert floor == venue.template["own_nonce"]


def trade_polls(session):
    return [x for x in session.calls if x["method"] == "GET" and x["path"] == f"/rfqs/{RFQ}"]


SETUP_RPC = {"eth_chainId", "eth_getCode"}  # the chain check before a signature; no tx


def sent_nothing(session):
    """No tx out and no arm tx asked for: only the setup RPC reads, no /arm-tx call."""
    return set(session.rpc_methods()) <= SETUP_RPC and not any(p.endswith("/arm-tx") for p in session.paths())


def test_sent_nothing_sees_a_send(make_client, session, health):
    # Positive control: the same fakes flag a tx and an /arm-tx read.
    from .test_money import Chain, deposit_route
    Chain(session, held=10**12)
    deposit_route(session, health, None, 1000 * 10**6, approve=False)
    session.routes[("GET", "/balance")] = (503, {"code": "upstream", "error": "down"})
    c = make_client(clock=Clock(time.time()))
    assert sent_nothing(session)
    c.deposit(1000)
    assert not sent_nothing(session) and "eth_sendRawTransaction" in session.rpc_methods()
    session.rpc_calls.clear()
    assert sent_nothing(session)
    c._gw.raw_request("GET", f"/rfqs/{RFQ}/arm-tx")
    assert not sent_nothing(session)


@pytest.mark.parametrize("script,word,tx", [
    ([("sending", None), ("sending", None), ("open", TXH)], "open", TXH),
    ([("sending", None), ("pending", TXH)], "pending", TXH),
    ([("sending", None), ("refused", None)], "refused", None),
])
def test_trade_sends_nothing_and_reads_status(make_client, venue, session, clock, script, word, tx):
    venue.script = list(script)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    before = len(trade_polls(session))
    t = c.trade(q)
    assert (t.status, t.tx) == (word, tx)
    assert sent_nothing(session)
    assert len(trade_polls(session)) - before == (31 if word == "pending" else len(script))


def test_trade_refused_expired(make_client, venue, session, clock):
    # The relay's tx never landed inside the quote's life: refused, no tx; the tape names why.
    venue.script = [("sending", None), ("refused", None)]
    session.routes[("GET", "/trades")] = {"seq": 2, "trades": [{"type": "trade.refused", "seq": 2, "ts": 1, "data": {
        "rfq_id": RFQ, "reason": "expired", "arm_seq": None}}]}
    c = make_client(clock=clock)
    t = c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert (t.status, t.tx) == ("refused", None) and sent_nothing(session)
    (e,) = c.trades()
    assert (e.type, e.data["reason"], e.data["arm_seq"]) == ("trade.refused", "expired", None)


@pytest.mark.parametrize("word", ["open", "refused"])
def test_trade_polls_until_final(make_client, venue, session, clock, word):
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    before = len(trade_polls(session))
    words = iter(["sending", "pending", word])
    session.routes[("GET", f"/rfqs/{RFQ}")] = lambda req: dict(venue.view(req), trade_status=next(words))
    t0 = clock()
    assert c.trade(q).status == word
    assert len(trade_polls(session)) - before == 3 and clock() - t0 >= 2


@pytest.mark.parametrize("word", ["sending", "pending"])
def test_trade_waits_30_s_on_testnet(make_client, venue, session, clock, word):
    venue.script = [(word, None)]
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    before = len(trade_polls(session))
    t0 = clock()
    t = c.trade(q)
    assert (t.status, t.tx) == (word, None)
    assert len(trade_polls(session)) - before == 31 and 30 <= clock() - t0 <= 31


@pytest.mark.parametrize("code,err", [((409, "round_closed"), crx.QuoteExpired),
                                      ((422, "insufficient_collateral"), crx.TradeUnknown)])
def test_side_refusal(make_client, venue, session, clock, code, err):
    session.routes[("POST", f"/rfqs/{RFQ}/side")] = (code[0], {"code": code[1], "error": code[1]})
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    before = len(trade_polls(session))
    with pytest.raises(err):
        c.trade(q)
    assert sent_nothing(session) and len(trade_polls(session)) == before


def test_side_no_answer_still_reads_status(make_client, venue, session, clock):
    # A 5xx on the Side post: the gateway may hold it, so the status decides.
    def side(req):
        venue.side(req)
        return (503, {"code": "upstream", "error": "down"})
    session.routes[("POST", f"/rfqs/{RFQ}/side")] = side
    c = make_client(clock=clock)
    assert c.trade(c.quote("USD/MXN", "buy", 25_000)).status == "open"


@pytest.mark.parametrize("view", [
    lambda v, req: {k: x for k, x in v.view(req).items() if k != "trade_status"},  # no trade_status yet
    lambda v, req: (503, {"code": "upstream", "error": "down"}),
    lambda v, req: dict(v.view(req), trade_status=None),
])
def test_trade_no_status_is_pending(make_client, venue, session, clock, view):
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    session.routes[("GET", f"/rfqs/{RFQ}")] = lambda req: view(venue, req)
    assert c.trade(q).status == "pending"


def test_accept_without_side_template_sends_nothing(make_client, venue, session, clock):
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = {"status": "accepted"}
    c = make_client(clock=clock)
    with pytest.raises(crx.RefusedToSign, match="Side template"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert f"/rfqs/{RFQ}/side" not in session.paths("POST")
    assert sent_nothing(session)


def test_every_signed_call_carries_valid_headers(make_client, venue, session, clock, account):
    from eth_account.messages import encode_defunct
    from crx._http import rest_message
    c = make_client(clock=clock)
    c.trade(c.quote("USDMXN", "BUY", "25000"))
    signed = [x for x in session.calls if "x-crx-sig" in x["headers"]]
    assert signed and all(x["path"] not in ("/health", "/markets") for x in signed)
    for x in signed:
        h = x["headers"]
        msg = rest_message(x["method"], x["path"], h["x-crx-address"], h["x-crx-signer"], int(h["x-crx-ts"]),
                           h["x-crx-nonce"], x["raw"])
        assert Account.recover_message(encode_defunct(text=msg), signature=h["x-crx-sig"]) == account.address


def test_wrong_c_taker_signs_nothing(make_client, venue, session, clock):
    venue.template_edit = {"c_taker": rnd()}
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    with pytest.raises(crx.RefusedToSign, match="c_taker"):
        c.trade(q)
    assert f"/rfqs/{RFQ}/side" not in session.paths("POST")
    assert sent_nothing(session)


def test_quote_with_other_terms_is_refused_before_accept(make_client, venue, session, clock):
    venue.row_edit = {"notional": "250000.000000"}
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    with pytest.raises(crx.RefusedToSign, match="notional"):
        c.trade(q)
    assert f"/rfqs/{RFQ}/accept" not in session.paths("POST")


def test_side_nonce_floor_refuses_replay(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    c.trade(c.quote("USD/MXN", "buy", 25_000))
    floor = int(venue.template["own_nonce"])
    venue.side_sig = None
    venue.template_edit = {"own_nonce": str(floor)}
    session.routes[("GET", f"/rfqs/{RFQ}")] = venue.view
    with pytest.raises(crx.RefusedToSign, match="own_nonce"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))


def test_side_window_too_far_refused(make_client, venue, clock):
    venue.template_edit = {"quote_expiry": int(clock()) + 3600}
    c = make_client(clock=clock)
    with pytest.raises(crx.RefusedToSign, match="quote_expiry"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))


def test_rejected_accept_retries_then_quote_expired(make_client, venue, session, clock):
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (
        409, {"code": "rejected", "error": "rejected", "details": {"reject_code": "rj_0011223344556677"}})
    c = make_client(clock=clock)
    with pytest.raises(crx.QuoteExpired) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.code == "quote_expired" and ei.value.gateway_code == "rejected"
    assert sent_nothing(session)
    assert session.paths("POST").count(f"/rfqs/{RFQ}/accept") > 1


def test_own_round_open(make_client, venue, session, clock):
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (
        409, {"code": "own_round_open", "error": "open", "details": {"rfq_id": RFQ, "until": 123}})
    c = make_client(clock=clock)
    with pytest.raises(crx.OwnRoundOpen) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.details["until"] == 123


def test_no_quotes(make_client, venue, session, clock):
    session.routes[("GET", f"/rfqs/{RFQ}")] = {"quote": None, "quotes": []}
    c = make_client(clock=clock)
    with pytest.raises(crx.NoQuotes):
        c.quote("USD/MXN", "buy", 25_000, wait=5)


def test_best_live_quote_when_no_pick(make_client, venue, session, clock):
    session.routes[("GET", f"/rfqs/{RFQ}")] = lambda req: {"quote": None, "quotes": [venue.row()]}
    c = make_client(clock=clock)
    assert c.quote("USD/MXN", "buy", 25_000, wait=3).quote_id == QID


def test_closed_market_still_opens_the_rfq(make_client, venue, session, markets, clock):
    session.routes[("GET", "/markets")] = markets  # recorded /markets: weekend, every session closed
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert q.quote_id == QID
    assert session.paths("POST") == ["/rfqs"]


def test_closed_market_gateway_refusal_is_market_closed(make_client, session, markets):
    session.routes[("POST", "/rfqs")] = (409, {"code": "market_closed", "error": "USD/MXN is closed",
                                               "details": {"opens_at": 1790200000000}})
    with pytest.raises(crx.MarketClosed) as ei:
        make_client().quote("USD/MXN", "buy", 25_000)
    assert ei.value.code == "market_closed" and ei.value.details["opens_at"] == 1790200000000
    assert session.paths("POST") == ["/rfqs"]


def test_closed_market_no_maker_is_no_quotes(make_client, venue, session, markets, clock):
    session.routes[("GET", "/markets")] = markets
    session.routes[("GET", f"/rfqs/{RFQ}")] = {"quote": None, "quotes": []}
    c = make_client(clock=clock)
    with pytest.raises(crx.NoQuotes):
        c.quote("USD/MXN", "buy", 25_000, wait=3)


def test_paused_pair(make_client, session):
    with pytest.raises(crx.MarketPaused):
        make_client().quote("USD/JPY", "buy", 25_000)
    assert session.paths("POST") == []


def test_below_min(make_client, session, markets):
    session.routes[("GET", "/markets")] = open_market(markets, "USD/MXN")
    with pytest.raises(crx.BelowMin) as ei:
        make_client().quote("USD/MXN", "buy", 5_000)
    assert ei.value.details["min"] == "10000"
    assert session.paths("POST") == []


def test_gateway_below_min_maps(make_client, session, markets):
    session.routes[("GET", "/markets")] = open_market(markets, "USD/MXN")
    session.routes[("POST", "/rfqs")] = (400, {"code": "pool_min_notional", "error": "pool min 25000"})
    with pytest.raises(crx.BelowMin):
        make_client().quote("USD/MXN", "buy", 10_000)


@pytest.mark.parametrize("args", [("USD/XXX", "buy", 25_000), ("USD/MXN", "long", 25_000),
                                  ("USD/MXN", "buy", -1), ("USD/MXN", "buy", "1.0000001")])
def test_bad_inputs(make_client, session, markets, args):
    session.routes[("GET", "/markets")] = open_market(markets, "USD/MXN")
    with pytest.raises(crx.BadRequest):
        make_client().quote(*args)
    assert session.paths("POST") == []


def test_naive_expiry_refused(make_client, session, markets):
    from datetime import datetime
    session.routes[("GET", "/markets")] = open_market(markets, "USD/MXN")
    with pytest.raises(crx.BadRequest, match="timezone"):
        make_client().quote("USD/MXN", "buy", 25_000, expiry=datetime(2027, 1, 4))


def test_domain_moved_refuses(make_client, session, health):
    h = {**health, "chains": [dict(c, domain="0x" + "00" * 32) for c in health["chains"]]}
    session.routes[("GET", "/health")] = h
    with pytest.raises(crx.RefusedToSign, match="domain"):
        make_client().deposit(1000)


def test_rpc_on_wrong_chain_refused(make_client, session):
    session.rpc["eth_chainId"] = "0x1"
    with pytest.raises(crx.ConfigError, match="RPC"):
        make_client().deposit(1000)
    assert session.paths("POST") == []
