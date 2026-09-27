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
        self.sent_raw = None
        self.row_edit = {}
        self.template_edit = {}
        self.tx_edit = {}
        session.routes[("GET", "/markets")] = open_market(markets, pair)
        session.routes[("POST", "/rfqs")] = self.open_rfq
        session.routes[("GET", f"/rfqs/{RFQ}")] = [{"quote": None, "quotes": []}, self.view]
        session.routes[("POST", f"/rfqs/{RFQ}/accept")] = self.accept
        session.routes[("POST", f"/rfqs/{RFQ}/side")] = self.side
        session.routes[("GET", f"/rfqs/{RFQ}/arm-tx")] = self.arm_tx
        session.rpc["eth_call"] = "0x"
        session.rpc["eth_sendRawTransaction"] = self.send_raw
        session.rpc["eth_getTransactionReceipt"] = lambda p: {"status": "0x1", "blockNumber": "0x3e9", "logs": []} \
            if self.sent_raw and p[0] == TXH else None

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
        return {"quote": self.row(), "quotes": [self.row()]}

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

    def arm_tx(self, req):
        if self.sent_raw:
            return {"status": "sent", "sent": TXH}
        if not self.side_sig:
            return (409, {"code": "conflict", "error": "a signature is missing"})
        t = self.template
        own = e7.hx(self.side_sig)
        sigs = own + b"\x01" * 64 + b"\x1b"
        public = encode(["bytes32", "bytes32", "uint64"], [e7.hx(t["c_taker"]), e7.hx(t["c_maker"]), int(t["quote_expiry"])])
        env = encode(["bytes32", "bytes", "bytes32", "bytes[]", "bytes"],
                     [e7.hx(t["pair_c"]), public, e7.hx(t["wraps_hash"]), [], encode(["bytes"], [sigs])])
        data = e7.h0x(e7.selector("armOpenPair(uint8,bytes)") + encode(["uint8", "bytes"], [9, env]))
        tx = {"to": self.chain["core"], "chain_id": CHAIN_ID, "data": data, "gas": 900_000}
        tx.update(self.tx_edit)
        self.served = tx
        return {"status": "ready", "tx": tx}

    def send_raw(self, params):
        self.sent_raw = params[0]
        return TXH


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
    assert (t.status, t.tx) == ("bound", TXH)
    leg = venue.accept_body
    assert leg["seat"] == account.address.lower() and leg["leg_id"] == LEG and leg["join_ref"] == JOIN
    body = next(x for x in session.calls if x["path"].endswith("/accept"))["body"]
    assert Account._recover_hash(e7.leg_digest(venue.sep, leg), signature=body["sig"]) == account.address
    assert Account._recover_hash(e7.side_digest(venue.sep, venue.template), signature=venue.side_sig) == account.address
    assert Account.recover_transaction(venue.sent_raw) == account.address
    floor = (c._state_dir / f"side-nonce-{account.address.lower()}").read_text()
    assert floor == venue.template["own_nonce"]


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
    assert "eth_sendRawTransaction" not in session.rpc_methods()


def test_quote_with_other_terms_is_refused_before_accept(make_client, venue, session, clock):
    venue.row_edit = {"notional": "250000.000000"}
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    with pytest.raises(crx.RefusedToSign, match="notional"):
        c.trade(q)
    assert f"/rfqs/{RFQ}/accept" not in session.paths("POST")


@pytest.mark.parametrize("edit", [{"to": "0x" + "99" * 20}, {"chain_id": 1}, {"data": "0xzz"}, {"gas": "lots"},
                                  {"gas": 10**9}])
def test_foreign_arm_tx_is_not_sent(make_client, venue, session, clock, edit):
    # The Side is already signed: the maker may still land the arm, so the answer is TradeUnknown.
    venue.tx_edit = edit
    c = make_client(clock=clock)
    with pytest.raises(crx.TradeUnknown, match="armOpenPair"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert "eth_sendRawTransaction" not in session.rpc_methods()


def test_sent_arm_is_the_checked_bytes(make_client, venue, session, clock):
    import rlp
    venue.tx_edit = {"to": venue.chain["core"].upper().replace("0X", "0x")}  # same core, other spelling
    c = make_client(clock=clock)
    c.trade(c.quote("USD/MXN", "buy", 25_000))
    nonce, price, gas, to, value, data, v, r, s = rlp.decode(e7.hx(venue.sent_raw))
    assert e7.h0x(to) == venue.chain["core"] and data == e7.hx(venue.served["data"]) and value == b""
    assert int.from_bytes(gas, "big") == venue.served["gas"]


def test_side_nonce_floor_refuses_replay(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    c.trade(c.quote("USD/MXN", "buy", 25_000))
    floor = int(venue.template["own_nonce"])
    venue.sent_raw, venue.side_sig = None, None
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
    assert "eth_sendRawTransaction" not in session.rpc_methods()
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


def test_market_closed_sends_nothing(make_client, session):
    c = make_client()  # recorded /markets: weekend, every session closed
    with pytest.raises(crx.MarketClosed) as ei:
        c.quote("USD/MXN", "buy", 25_000)
    assert ei.value.code == "market_closed" and isinstance(ei.value.details["opens_at"], int)
    assert session.paths("POST") == []


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
