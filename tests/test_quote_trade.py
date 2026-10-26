"""quote() and trade() against a scripted gateway and chain."""

import dataclasses
import re
import time
from decimal import Decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

import crx
from .conftest import WEEK  # noqa: F401
from crx import _eip712 as e7

from .conftest import CHAIN_ID, Clock, open_market, rnd

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

    def __init__(self, session, health, markets, clock, pair="USD/MXN", rfq=RFQ):
        self.s, self.clock, self.rfq = session, clock, rfq
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
        session.routes[("GET", f"/rfqs/{rfq}")] = self.view
        session.routes[("POST", f"/rfqs/{rfq}/accept")] = self.accept

    def open_rfq(self, req):
        b = self.rfq_body = req["body"]
        now = int(self.clock())
        self.qe = now + self.qe_ahead
        self.leg = e7.leg_id_for(HEAD, self.qe + self.tail_delta)
        opened = {"rfq_id": self.rfq, "leg_id": self.leg, "side": 1 if b["side"] == "buy" else -1, "expiry": b["expiry"],
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
        return {"rfq_id": self.rfq, "quote_id": qid, "client_quote_id": None}

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
