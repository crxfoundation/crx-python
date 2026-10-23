"""quote() and trade() against a scripted gateway and chain."""

import copy
import dataclasses
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from eth_utils import keccak

import crx
from .conftest import WEEK  # noqa: F401
from crx import _eip712 as e7
from crx._http import rest_message

from .conftest import BASE, CHAIN_ID, RPC, Clock, Resp, open_market, rnd
from .test_maker import T0, Gateway, new_maker, rfq_obj

RFQ = "0x" + "11" * 32
HEAD = b"\x22" * 24  # the random head of the taker's leg id, before the quote end
QID = "0x" + "44" * 32
QID2 = "0x" + "66" * 32
TXH = "0x" + "55" * 32


def typed_hash(td):
    """Return the EIP-712 digest of ``td``, computed by eth_account."""
    m = encode_typed_data(full_message=td)
    return keccak(b"\x19" + m.version + m.header + m.body)


def recovers(t, sig):
    """Return the signer of ``sig`` over the Trade in template ``t``."""
    return Account._recover_hash(typed_hash(t["typed_data"]), signature=sig)


class Venue:
    """Script one RFQ round on the gateway, with answers built from what the client sent.

    The winner row carries a ``trade_template`` built from the request's terms and the row's rate.
    POST /accept with ``{quote_id}`` answers 409 trade_stale with a fresh template. With
    ``{quote_id, sig}`` it opens the round when ``sig`` recovers to the seat.
    """

    def __init__(self, session, health, markets, clock, pair="USD/MXN"):
        self.s, self.clock = session, clock
        self.chain = next(c for c in health["chains"] if c["key"] == "avax-fuji")
        self.sep = e7.domain_separator(CHAIN_ID, self.chain["core"])
        self.rfq_body = None
        self.qe = None              # quote_expiry_max, unix s
        self.leg = None             # the taker's leg id on the open answer
        self.qe_ahead = 300         # s: the quote end past the open
        self.tail_delta = 0         # s: the leg id's tail less quote_expiry_max
        self.open_edit = {}         # open answer members replaced; None removes one
        self.rate = "18.700000"     # the winner row's rate
        self.rates = {}             # quote_id -> rate, for rows other than the winner
        self.row_edit = {}
        self.premium = None         # the template's premiumBps (None: the request's)
        self.msg_edit = {}          # message members served in place of the gateway's (a value, or a callable)
        self.typed_edit = None      # callable(typed_data): changes the served typed_data
        self.template_edit = None   # callable(template) -> the template served
        self.winner = True          # the gateway named its pick: the view carries `quote`
        self.waited = True          # POST /rfqs with wait answers after the window, with the taker view
        self.waited_view = None     # callable: the view a waited answer carries; None: view()
        self.draft = None           # the last template served
        self.draft_qid = None       # the quote the draft is for
        self.template = None        # the template signed and taken
        self.sig = None             # the signature taken
        self.counter = 0            # the gateway's ownNonce counter for this seat
        self.nonces = []            # nonces the next templates take, in order
        self.stale = 0              # the next N signed posts answer 409 trade_stale with a fresh template
        self.on_stale = None        # called before each fresh template that `stale` forces
        self.stale_quote_id = None  # quote_id in a trade_stale answer (None: the one posted, "": none)
        # (trade_status, trade_tx) served one per poll after the Trade is taken, the last repeating.
        self.script = [("sending", None), ("open", TXH)]
        session.routes[("GET", "/markets")] = open_market(markets, pair)
        session.routes[("POST", "/rfqs")] = self.open_rfq
        session.routes[("GET", f"/rfqs/{RFQ}")] = self.view
        session.routes[("POST", f"/rfqs/{RFQ}/accept")] = self.accept

    def open_rfq(self, req):
        b = self.rfq_body = req["body"]
        now = int(self.clock())
        self.qe = now + self.qe_ahead
        self.leg = e7.leg_id_for(HEAD, self.qe + self.tail_delta)
        opened = {"rfq_id": RFQ, "leg_id": self.leg, "side": 1 if b["side"] == "buy" else -1, "expiry": b["expiry"],
                  "closes_at": now * 1000 + 120_000, "quote_expiry_max": self.qe, "status": "open", "kind": "open",
                  "client_rfq_id": b["client_rfq_id"]}
        for k, v in self.open_edit.items():
            if v is None:
                opened.pop(k, None)
            else:
                opened[k] = v
        if not (self.waited and b.get("wait", True)):  # no `wait`: the gateway answers after the window
            return opened
        v = self.view(req) if self.waited_view is None else self.waited_view()
        return {**opened, **v}

    def row(self):
        r = {"quote_id": QID, "rate": self.rate, "expires_at": int(self.clock() * 1000) + 60_000, "status": "quoted"}
        r.update(self.row_edit)
        return r

    def pick(self):
        n = self.nonces.pop(0) if self.nonces else max(self.counter + 1, int(self.clock() * 1000))
        self.counter = max(self.counter, n)
        return n

    def make_template(self, quote_id=QID, rate=None):
        b = self.rfq_body
        premium = b.get("premium_bps", 0) if self.premium is None else self.premium
        msg = e7.trade_message(
            b["pair"][:3] + "/" + b["pair"][3:], 1 if b["side"] == "buy" else -1, e7.e6(b["notional"]),
            e7.e6(rate or self.rates.get(quote_id, self.rate), 64), premium, b["expiry"] // 1000, rnd(), self.leg,
            str(self.pick()), rnd())
        for k, v in self.msg_edit.items():
            msg[k] = v(msg[k]) if callable(v) else v
        td = e7.typed_data("Trade", CHAIN_ID, self.chain["core"], msg)
        if self.typed_edit:
            self.typed_edit(td)
        t = {"typed_data": td}
        if self.template_edit:
            t = self.template_edit(t)
        self.draft, self.draft_qid = t, quote_id
        return t

    def view(self, req=None):
        q = self.row()
        if self.winner and self.sig is None:
            if self.draft is None or self.draft_qid != QID:
                self.make_template()
            q["trade_template"] = self.draft
        v = {"quote": q if self.winner else None, "quotes": [self.row()]}
        if self.sig is not None:
            word, tx = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            v.update(trade_status=word, trade_tx=tx)
        return v

    def accept(self, req):
        body, seat = req["body"], req["headers"]["x-crx-address"]
        qid = body["quote_id"]
        if "sig" not in body or self.draft is None or self.draft_qid != qid or self.stale > 0:
            if "sig" in body and self.stale > 0:
                self.stale -= 1
                if self.on_stale:
                    self.on_stale()
            self.make_template(qid)
            return self.stale_answer(qid)
        if Account._recover_hash(typed_hash(self.draft["typed_data"]), signature=body["sig"]).lower() != seat:
            return (400, {"code": "invalid_signature", "error": "the signature does not recover to the seat"})
        self.sig, self.template = body["sig"], self.draft
        return {"rfq_id": RFQ, "quote_id": qid, "client_quote_id": None}

    def stale_answer(self, qid):
        named = qid if self.stale_quote_id is None else self.stale_quote_id
        details = {"trade_template": self.draft} | ({"quote_id": named} if named else {})
        return (409, {"code": "trade_stale", "error": "the trade template moved; sign this one", "details": details})

    def reset(self):
        """Start a new round on the same RFQ id, with nothing taken."""
        self.sig, self.draft, self.draft_qid = None, None, None


def accepts(session):
    return [x["body"] for x in session.calls if x["method"] == "POST" and x["path"] == f"/rfqs/{RFQ}/accept"]


def signed_posts(session):
    return [b for b in accepts(session) if "sig" in b]


def floor_file(c, account):
    return c._state_dir / f"side-nonce-{account.address.lower()}"


def trade_polls(session):
    return [x for x in session.calls if x["method"] == "GET" and x["path"] == f"/rfqs/{RFQ}"]


SETUP_RPC = {"eth_chainId", "eth_getCode"}  # the chain check before a signature, with no tx


def sent_nothing(session):
    """Return True when no tx went out and no /arm-tx was asked for."""
    return set(session.rpc_methods()) <= SETUP_RPC and not any(p.endswith("/arm-tx") for p in session.paths())


@pytest.fixture
def clock():
    return Clock(float(int(time.time())))


@pytest.fixture
def venue(session, health, markets, clock):
    return Venue(session, health, markets, clock)


def test_quote_then_trade_signs_one_trade(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, settles=WEEK)
    assert (q.rfq_id, q.quote_id, q.rate, q.side, q.pair, q.notional) == (
        RFQ, QID, Decimal("18.700000"), "buy", "USD/MXN", Decimal("25000"))
    assert q.raw["trade_template"] == venue.draft
    b = venue.rfq_body
    assert (b["pair"], b["notional"], b["chain"], "wait" in b) == ("USDMXN", "25000", "avax-fuji", False)
    assert accepts(session) == [] and trade_polls(session) == []  # the open answer already named the winner

    t = c.trade(q)
    assert (t.status, t.tx, t.quote_id) == ("open", TXH, QID)
    assert t.raw == {"rfq_id": RFQ, "quote_id": QID, "client_quote_id": None}
    (body,) = accepts(session)
    assert set(body) == {"quote_id", "sig"} and body["quote_id"] == QID
    assert recovers(venue.template, body["sig"]) == account.address
    assert sent_nothing(session)
    assert floor_file(c, account).read_text() == venue.template["typed_data"]["message"]["ownNonce"]


def test_reject_sends_nothing_and_a_later_accept_signs_nothing(make_client, venue, session, clock, monkeypatch):
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, settles=WEEK)
    calls = len(session.calls)
    assert c.reject(q) is None
    assert len(session.calls) == calls
    signed = []
    monkeypatch.setattr(e7, "typed_digest", lambda *a, **k: signed.append(1))
    for quote in (q, dataclasses.replace(q, quote_id=QID2)):  # any quote on the rejected RFQ
        with pytest.raises(crx.QuoteRejected) as ei:
            c.accept(quote)
        assert (ei.value.code, ei.value.details) == ("quote_rejected", {"rfq_id": RFQ})
    assert accepts(session) == [] and signed == [] and len(session.calls) == calls
    c.reject(q)  # a repeat is a no-op


def test_trade_refuses_a_maker_quote(session, health, tmp_path):
    clk = Clock(T0)
    Gateway(session, clk)
    maker = new_maker(session, health, tmp_path, clk)
    q = maker.send_quote(rfq_obj(), "5.41")
    with pytest.raises(crx.BadRequest, match="accept\\(\\) takes the Quote that quote\\(\\) returned"):
        maker.trade(q)


def test_reject_after_an_accept_raises_and_the_accept_stands(make_client, venue, session, clock):
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, settles=WEEK)
    assert c.accept(q).status == "open"
    with pytest.raises(crx.BadRequest, match="the accept stands"):
        c.reject(q)
    assert c.accept(q).status == "open" and len(signed_posts(session)) == 1


def test_nonce_floor_refuses_replay(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    c.trade(c.quote("USD/MXN", "buy", 25_000, settles=WEEK))
    floor = venue.template["typed_data"]["message"]["ownNonce"]
    venue.reset()
    venue.msg_edit = {"ownNonce": floor}  # the gateway keeps serving the nonce this seat signed
    c = make_client(clock=clock)  # a fresh client on the reused RFQ id
    q = c.quote("USD/MXN", "buy", 25_000, settles=WEEK)
    before = len(accepts(session))
    with pytest.raises(crx.RefusedToSign, match="^refused to sign: ownNonce is not above the last one this seat signed$"):
        c.trade(q)
    # It asks for a newer template twice and signs nothing.
    assert accepts(session)[before:] == [{"quote_id": QID}] * 2
    assert floor_file(c, account).read_text() == floor


def test_the_accept_deadline_is_the_rfqs_closes_at_not_the_quote_end(make_client, venue, session, clock):
    # closes_at is 20 s after the open, and the quote end is 570 s out.
    # Each signed post answers trade_stale 15 s later, so only two posts fit before closes_at.
    venue.open_edit = {"closes_at": int(clock() * 1000) + 20_000}
    venue.row_edit = {"expires_at": int(clock() * 1000) + 570_000}
    venue.stale = 99
    posted = []
    venue.on_stale = lambda: posted.append(clock()) or clock.sleep(15)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, settles=WEEK)
    closes = venue.open_edit["closes_at"] / 1000
    assert q.closes_at == datetime.fromtimestamp(closes, timezone.utc) and q.expires_at > q.closes_at
    with pytest.raises(crx.QuoteExpired) as ei:
        c.trade(q)
    assert ei.value.gateway_code == "trade_stale"
    assert len(posted) == len(signed_posts(session)) == 2 and max(posted) < closes


UNSIGNED_ASKS = [  # answer to the unsigned ask (None: trade_stale with the template of another quote), error, words
    ({"rfq_id": RFQ, "quote_id": QID}, crx.TradeUnknown, "holds an accept"),  # the accept the gateway already holds
    ((503, {"code": "mark_unavailable", "error": "declined: mark_unavailable", "details": {"pair": "USD/MXN"}}),
     crx.MarkUnavailable, None),
    (None, crx.RefusedToSign, "another quote"),
]


def test_an_answer_to_the_unsigned_ask_signs_nothing(make_client, venue, session, clock):
    venue.winner, venue.stale_quote_id = False, QID2  # the row carries no template: the SDK asks for one first
    for answer, err, words in UNSIGNED_ASKS:
        session.routes[("POST", f"/rfqs/{RFQ}/accept")] = answer or venue.accept
        before = len(accepts(session))
        c = make_client(clock=clock)
        q = c.quote("USD/MXN", "buy", 25_000, wait=1, settles=WEEK)
        with pytest.raises(err, match=words):
            c.trade(q)
        assert accepts(session)[before:] == [{"quote_id": QID}], err


SIGNED_REFUSALS = [
    ((409, "round_closed"), crx.QuoteExpired),
    ((422, "insufficient_collateral"), crx.TradeUnknown),
    ((409, "trade_stale"), crx.TradeUnknown),  # its fresh template cannot be read
]


def test_refusal_after_the_trade_is_signed(make_client, venue, session, clock):
    for (status, code), err in SIGNED_REFUSALS:
        venue.reset()
        session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (status, {"code": code, "error": code,
                                                                   "details": {"trade_template": "?"}})
        before = len(accepts(session))
        c = make_client(clock=clock)
        with pytest.raises(err) as ei:
            c.trade(c.quote("USD/MXN", "buy", 25_000, settles=WEEK))
        if err is crx.TradeUnknown:
            assert "may still open" in str(ei.value) and ei.value.details == {"rfq_id": RFQ}, code
        assert len(accepts(session)) == before + 1 and sent_nothing(session), code


DOWN_BODY = {"code": "service_unavailable", "outcome": "unavailable", "error": "service temporarily unavailable",
             "message": "Service temporarily unavailable: trade not executed."}


SEAT_BODY = {"code": "seat_cannot_sign", "outcome": "not_permitted", "error": "this address cannot sign trades",
             "message": "This address cannot sign trades."}
OWN_ERRORS = [  # status, body, error, a type it is, a type it is not
    (503, DOWN_BODY, crx.ServiceUnavailable, crx.ServerError, ()),
    (403, SEAT_BODY, crx.SeatCannotSign, crx.CrxError, crx.AuthError),
]


def test_gateway_codes_map_to_their_own_errors():
    for status, body, err, kind, other in OWN_ERRORS:
        e = crx.errors.from_gateway(status, body)
        assert type(e) is err and isinstance(e, kind) and not isinstance(e, other), err
        assert (e.code, e.status, e.gateway_code) == (body["code"], status, body["code"]), err
        assert err.__name__ in crx.__all__, err
    assert type(crx.errors.from_gateway(503, dict(DOWN_BODY, code="upstream"))) is crx.ServerError
    assert crx.RelayUnavailable is crx.ServiceUnavailable and "RelayUnavailable" not in crx.__all__


def put(path, value):
    """Return an edit that sets the served typed_data at a dotted ``path``."""
    def edit(td):
        *head, last = path.split(".")
        node = td
        for k in head:
            node = node[k]
        node[last] = value(node[last]) if callable(value) else value
    return edit


def refused_unsigned(session, c, account):
    """Return True when no signed accept was posted and no nonce floor written."""
    return signed_posts(session) == [] and not floor_file(c, account).exists() and sent_nothing(session)


SERVED_EDITS = {
    "primaryType": put("primaryType", "Side"),
    "types": put("types", lambda t: {**t, "Trade": t["Trade"][::-1]}),  # reordered
    "typed_data": lambda td: td.update(metadata={"note": "x"}),
    "domain": lambda td: td["domain"].pop("version"),
    "message.notionalE6": put("message.notionalE6", "25e9"),
}


def test_served_typed_data_must_equal_own(make_client, venue, session, clock, account):
    for at, edit in SERVED_EDITS.items():
        venue.reset()
        venue.typed_edit = edit
        c = make_client(clock=clock)
        with pytest.raises(crx.RefusedToSign,
                           match=f"^refused to sign: the served typed_data differs at {re.escape(at)}$"):
            c.trade(c.quote("USD/MXN", "buy", 25_000, settles=WEEK))
        assert refused_unsigned(session, c, account), at


TEMPLATE_EDITS = [  # Venue member, edit, words
    ("template_edit", lambda t: {}, r"^refused to sign: the trade template is not \{typed_data\}$"),  # no typed_data
    ("typed_edit", lambda td: td.pop("message"), "^refused to sign: the template cannot be read$"),
]


def test_template_holds_exactly_a_readable_typed_data(make_client, venue, session, clock, account):
    for member, edit, words in TEMPLATE_EDITS:
        venue.reset()
        setattr(venue, member, edit)
        c = make_client(clock=clock)
        with pytest.raises(crx.RefusedToSign, match=words):
            c.trade(c.quote("USD/MXN", "buy", 25_000, settles=WEEK))
        assert refused_unsigned(session, c, account), member
        setattr(venue, member, None)


QUOTE_ENDS = [
    (0, crx.QuoteExpired, "^the quote end has passed; not opened; request a new quote$"),
    (631, crx.RefusedToSign, "^refused to sign: the quote end is more than 630 s ahead$"),
]


def test_quote_end_is_ahead_by_at_most_630_s(make_client, venue, session, clock, account):
    for ahead, err, words in QUOTE_ENDS:
        venue.reset()
        venue.qe_ahead = ahead
        c = make_client(clock=clock)
        with pytest.raises(err, match=words) as ei:
            c.trade(c.quote("USD/MXN", "buy", 25_000, settles=WEEK))
        assert not isinstance(ei.value, crx.TradeUnknown), ahead
        assert refused_unsigned(session, c, account), ahead


def with_cap(markets, cap, pair="USD/MXN"):
    """Return /markets with ``pair``'s ``max_premium_bps`` set to ``cap``, or removed when None."""
    m = open_market(markets, pair)
    for row in m["markets"]:
        if row["pair"] == pair:
            for ch in row["chains"]:
                if cap is None:
                    ch.pop("max_premium_bps", None)
                else:
                    ch["max_premium_bps"] = cap
    return m


PREMIUM_REFUSALS = [
    (201, 200, r"^refused to sign: the premium 201 bps is above the cap \(200 bps\)$"),
    (5, None, "^refused to sign: the premium is not 0 and the market serves no premium cap$"),
]


def test_premium_beyond_the_served_cap_is_refused(make_client, venue, session, markets, clock, account):
    for premium, cap, words in PREMIUM_REFUSALS:
        venue.reset()
        session.routes[("GET", "/markets")] = with_cap(markets, cap)
        c = make_client(clock=clock)
        q = c.quote("USD/MXN", "buy", 25_000, premium_bps=premium, settles=WEEK)
        with pytest.raises(crx.RefusedToSign, match=words):
            c.trade(q)
        assert refused_unsigned(session, c, account), premium


def fuji_row(markets, **edit):
    """Return /markets with USD/MXN's avax-fuji row edited, where None removes a key."""
    m = open_market(markets, "USD/MXN")
    for row in m["markets"]:
        if row["pair"] == "USD/MXN":
            (ch,) = [c for c in row["chains"] if c["chain"] == "avax-fuji"]
            for k, v in edit.items():
                if v is None:
                    ch.pop(k, None)
                else:
                    ch[k] = v
    return m


def test_a_market_row_paused_true_is_market_paused(make_client, venue, session, markets, clock):
    session.routes[("GET", "/markets")] = fuji_row(markets, paused=True)
    c = make_client(clock=clock)
    assert c.market("USD/MXN").paused is True
    with pytest.raises(crx.MarketPaused, match="^USD/MXN is paused$") as ei:
        c.quote("USD/MXN", "buy", 25_000, settles=WEEK)
    assert ei.value.details == {"pair": "USD/MXN"} and session.paths("POST") == []


def test_market_on_another_chain_only_is_not_offered(make_client, session, markets):
    m = open_market(markets, "USD/MXN")
    for row in m["markets"]:
        if row["pair"] == "USD/MXN":
            row["chains"] = [dict(row["chains"][0], chain="ethereum")]
    session.routes[("GET", "/markets")] = m
    c = make_client()
    assert next(x for x in c.markets() if x.pair == "USD/MXN").paused is True
    with pytest.raises(crx.MarketPaused, match="^USD/MXN is not offered on avax-fuji$"):
        c.market("usdmxn")
    with pytest.raises(crx.MarketPaused):
        c.quote("USD/MXN", "buy", 25_000, settles=WEEK)
    assert session.paths("POST") == []


def test_no_quotes(make_client, venue, session, clock):
    venue.waited = False
    session.routes[("GET", f"/rfqs/{RFQ}")] = {"quote": None, "quotes": []}
    c = make_client(clock=clock)
    with pytest.raises(crx.NoQuotes):
        c.quote("USD/MXN", "buy", 25_000, wait=5, settles=WEEK)


def test_open_answer_without_its_leg_is_bad_answer(make_client, venue, session, clock):
    venue.open_edit = {"quote_expiry_max": None}
    c = make_client(clock=clock)
    with pytest.raises(crx.BadAnswer, match="^/rfqs sent an answer this SDK cannot read$"):
        c.quote("USD/MXN", "buy", 25_000, settles=WEEK)
    assert accepts(session) == [] and trade_polls(session) == []


EXPIRY_MS = 1_900_000_000_000


def test_ask_quote_is_the_winner_and_trades(make_client, venue, session, clock):
    session.routes[("GET", f"/rfqs/{RFQ}")] = [{"quote": None, "quotes": []}, venue.view]
    c = make_client(clock=clock)
    q = c.ask("USD/MXN", "buy", 25_000, expiry=EXPIRY_MS).quote()
    assert (q.rfq_id, q.quote_id, q.rate, q.side, q.pair) == (RFQ, QID, Decimal("18.700000"), "buy", "USD/MXN")
    assert (q.rfq["leg_id"], q.rfq["quote_expiry_max"], q.expiry_ms) == (venue.leg, venue.qe, EXPIRY_MS)
    assert len(trade_polls(session)) == 2  # no winner named, then the winner
    assert accepts(session) == []
    t = c.trade(q)
    assert (t.status, t.tx) == ("open", TXH) and sent_nothing(session)


QUOTE_CHECKS = [  # args, kw, error
    (("USD/XX", "buy", 25_000), {}, crx.BadRequest),
    (("USD/MXN", "long", 25_000), {}, crx.BadRequest),
    (("USD/MXN", "buy", -1), {}, crx.BadRequest),
    (("USD/MXN", "buy", "1.0000001"), {}, crx.BadRequest),
    (("USD/MXN", "buy", 5_000), {}, crx.BelowMin),
    (("USD/MXN", "buy", 25_000), {"client_rfq_id": ""}, crx.BadRequest),
    (("USD/MXN", "buy", 25_000), {"premium_bps": 10_001}, crx.BadRequest),
    (("USD/MXN", "buy", 25_000), {"expiry": datetime(2027, 1, 4)}, crx.BadRequest),
    (("USD/MXN", "buy", 25_000), {"expiry": "tomorrow"}, crx.BadRequest),
]


def test_ask_makes_the_checks_of_quote(make_client, session, markets):
    session.routes[("GET", "/markets")] = open_market(markets, "USD/MXN")
    c = make_client()
    for args, kw, err in QUOTE_CHECKS:
        said = []
        for call in (c.quote, c.ask):
            with pytest.raises(err) as ei:
                call(*args, **kw)
            said.append((type(ei.value), str(ei.value), ei.value.details))
        assert said[0] == said[1] and said[0][0] is err, (args, kw)
    assert session.paths("POST") == []


FUJI_CORE = "0x072000d0eb830f767dc5128ed28139bb731b1521"
OTHER_CORE = "0x" + "c0" * 20


def served_on(health, core, chain_id=CHAIN_ID):
    """Return /health with avax-fuji on ``core`` and the matching domain for ``chain_id``."""
    h = copy.deepcopy(health)
    fuji = next(c for c in h["chains"] if c["key"] == "avax-fuji")
    fuji.update(core=core, chain_id=chain_id, domain=e7.h0x(e7.domain_separator(chain_id, core)))
    return h


MOVED = [  # /health served, words, details
    # The domain matches the other core, so only the pin catches the move.
    (lambda h: served_on(h, OTHER_CORE),
     f"^core moved: /health names {OTHER_CORE}, the SDK pins {FUJI_CORE} on avax-fuji; nothing signed",
     {"chain": "avax-fuji", "served_core": OTHER_CORE, "pinned_core": FUJI_CORE}),
    (lambda h: {**h, "chains": [dict(c, domain="0x" + "00" * 32) for c in h["chains"]]},
     "^domain moved: /health does not match the core; nothing signed$", None),
]


def test_health_that_moved_refuses_the_trade(make_client, venue, session, health, clock):
    for served, words, details in MOVED:
        venue.reset()
        session.routes[("GET", "/health")] = served(health)
        c = make_client(clock=clock)
        with pytest.raises(crx.RefusedToSign, match=words) as ei:
            c.trade(c.quote("USD/MXN", "buy", 25_000, settles=WEEK))
        assert details is None or ei.value.details == details, words
        assert accepts(session) == [] and sent_nothing(session), words


CHAIN_PINS = [  # /health chain id, RPC chain id, call on make_client, words
    (1, hex(CHAIN_ID), lambda mk: mk()._chain_info(), "testnets only"),
    (CHAIN_ID, "0x1", lambda mk: mk().deposit(1000), "RPC"),
    (CHAIN_ID, hex(CHAIN_ID), lambda mk: mk(core=""), "core is not a wallet address"),
]


def test_another_chain_id_or_core_refuses(make_client, session, health):
    for chain_id, rpc_id, call, words in CHAIN_PINS:
        session.routes[("GET", "/health")] = served_on(health, FUJI_CORE, chain_id=chain_id)
        session.rpc["eth_chainId"] = rpc_id
        with pytest.raises(crx.ConfigError, match=words):
            call(make_client)
        assert session.paths("POST") == [], words


def own_check(tmp_path, account, health, clock, **edit):
    """Run Binder.check_trade on a matching template after ``edit`` changes the ask."""
    from crx._bind import Binder
    chain = next(c for c in health["chains"] if c["key"] == "avax-fuji")
    sep = e7.domain_separator(CHAIN_ID, chain["core"])
    qe = int(clock()) + 300
    leg = e7.leg_id_for(HEAD, qe)
    ask = {"pair": "USD/MXN", "side": 1, "notional": "25000", "expiry": int(clock() * 1000) + 30 * 86_400_000,
           "premium_bps": 0, "rate": Decimal("18.7"), "quote_expiry_max": qe, "max_premium_bps": 200, "leg_id": leg}
    msg = e7.trade_message("USD/MXN", 1, 25_000 * 10**6, 18_700_000, 0, ask["expiry"] // 1000, rnd(), leg,
                           str(int(clock() * 1000)), rnd())
    t = {"typed_data": e7.typed_data("Trade", CHAIN_ID, chain["core"], msg)}
    ask.update(edit)
    b = Binder(None, account, dict(chain, chain_id=CHAIN_ID), sep, tmp_path, clock=clock)
    return b.check_trade(t, ask)


OWN_REFUSALS = [
    ({"pair": "usd/mxn"}, "pair is not AAA/BBB"),
    ({"side": 2}, "side is not buy or sell"),
    ({"notional": "340282366920938463463374607431768.211456"}, "notional is not a 6-decimal amount below 2\\^128"),
    ({"rate": Decimal("18446744073709.551616")}, "rate is not a 6-decimal amount below 2\\^64"),
    ({"expiry": -1}, "expiry is not unix ms"),
]


def test_own_trade_refusals(tmp_path, account, health, clock):
    for edit, why in OWN_REFUSALS:
        with pytest.raises(crx.RefusedToSign, match=why):
            own_check(tmp_path, account, health, clock, **edit)
        assert not (tmp_path / f"side-nonce-{account.address.lower()}").exists(), why  # no nonce kept, nothing signed


def mangle(sig: bytes, form: str) -> bytes:
    """Return ``sig`` in a form a custodian may send: ``high_s`` (s' = n - s, v flipped), ``v01`` or ``v29``."""
    sig = bytes(sig)
    if form == "high_s":
        s = e7.SECP256K1_N - int.from_bytes(sig[32:64], "big")
        return sig[:32] + s.to_bytes(32, "big") + bytes([55 - sig[64]])
    if form == "v01":
        return sig[:64] + bytes([sig[64] - 27])
    if form == "v29":
        return sig[:64] + bytes([29])
    return sig


class Custodian:
    """A custodian signer that records the typed data and EIP-191 messages it signs."""

    def __init__(self, account, form="plain", key=None):
        self.a, self.form, self.key = account, form, key or account
        self.address = account.address  # checksummed, as a custodian API names it
        self.typed, self.messages = [], []

    def sign_typed_data(self, td):
        self.typed.append(td)
        return "0x" + mangle(self.key.sign_typed_data(full_message=td).signature, self.form).hex()

    def sign_message(self, message):
        self.messages.append(message)
        return mangle(self.a.sign_message(encode_defunct(primitive=message)).signature, self.form)


class SessionGate:
    """Script the gateway's session tokens: POST /session mints a token from a signed envelope.

    With ``on`` False, POST /session answers 404.
    """

    def __init__(self, session, seat, on=True):
        self.seat, self.tokens, self.mints, self.sent = seat, set(), 0, []
        self.inner = session.request
        session.request = self.request
        if on:
            session.routes[("POST", "/session")] = self.mint

    def mint(self, req):
        h = req["headers"]
        msg = rest_message("POST", "/session", h["x-crx-address"], h.get("x-crx-signer", h["x-crx-address"]),
                           int(h["x-crx-ts"]), b"")
        assert Account.recover_message(encode_defunct(text=msg), signature=h["x-crx-sig"]).lower() == self.seat
        self.mints += 1
        tok = os.urandom(32).hex()
        self.tokens.add(tok)
        return {"token": tok, "expires_at": 0, "custody": self.seat, "signer": self.seat, "ttl_ms": 8 * 3_600_000}

    def request(self, method, url, headers=None, **kw):
        h = dict(headers or {})
        self.sent.append((method, url, dict(h)))
        tok = h.get("x-crx-session")
        if tok is not None:
            assert not [k for k in h if k.startswith("x-crx-") and k != "x-crx-session"]  # no envelope beside it
            if tok not in self.tokens:
                return Resp(401, {"code": "unauthorized", "error": "the x-crx-session token is unknown or expired"})
            h["x-crx-address"] = self.seat
        return self.inner(method, url, headers=h, **kw)

    def restart(self):
        self.tokens.clear()

    def envelopes(self):
        return [(m, u) for m, u, h in self.sent if "x-crx-sig" in h]

    def tokened(self):
        return [(m, u) for m, u, h in self.sent if "x-crx-session" in h]


def custodian_client(session, tmp_path, signer, clock=None, **kw):
    c = crx.Client(signer=signer, base_url=BASE, rpc_url=RPC, state_dir=tmp_path / "state", session=session, **kw)
    if clock is not None:
        c._clock, c._sleep, c._gw._clock = clock, clock.sleep, clock
    return c


def test_custodian_bad_signature_or_local_key_call_is_not_sent(venue, session, clock, account, tmp_path):
    SessionGate(session, account.address.lower())
    s = Custodian(account, "v29")
    s.sign_message = lambda m: account.sign_message(encode_defunct(primitive=m)).signature  # login works
    c = custodian_client(session, tmp_path, s, clock)
    with pytest.raises(crx.ConfigError, match="needs a local key"):
        c.deposit(1000)
    assert session.calls == []
    with pytest.raises(crx.RefusedToSign, match="no usable signature"):
        c.trade(c.quote("USD/MXN", "buy", 25_000, settles=WEEK))
    assert signed_posts(session) == []
