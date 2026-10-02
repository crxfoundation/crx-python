"""quote() and trade() against a scripted gateway and chain."""

import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import requests
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from eth_utils import keccak

import crx
from crx import _eip712 as e7
from crx._http import rest_message

from .conftest import BASE, CHAIN_ID, RPC, Clock, Resp, open_market

RFQ = "0x" + "11" * 32
HEAD = b"\x22" * 24  # the random head of the taker's leg id; the quote end follows it
QID = "0x" + "44" * 32
QID2 = "0x" + "66" * 32
TXH = "0x" + "55" * 32


def rnd():
    return "0x" + os.urandom(32).hex()


def typed_hash(td):
    """The EIP-712 digest of ``td``, by eth_account."""
    m = encode_typed_data(full_message=td)
    return keccak(b"\x19" + m.version + m.header + m.body)


def recovers(t, sig):
    """The signer of ``sig`` over the Trade that template ``t`` serves."""
    return Account._recover_hash(typed_hash(t["typed_data"]), signature=sig)


class Venue:
    """One RFQ round on the gateway: its answers, built from what the client sent.

    POST /rfqs answers the open fields: ``leg_id`` (24 random bytes, then the quote end, u64 big
    endian) and ``quote_expiry_max``. With ``waited`` and a request that asks to wait, the answer
    also carries the taker view with the winner. The winner row carries ``trade_template`` =
    {typed_data}: the gateway's Trade from the request's terms and the row's rate, a random pairC
    and ownSalt, ownLegId = the RFQ's leg_id, ownNonce from its counter. POST /accept
    ``{quote_id}`` answers 409 trade_stale with a fresh template; ``{quote_id, sig}`` opens the
    round when ``sig`` recovers to the seat over the template's Trade.
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
        self.premium = None         # the template's premiumBps; None: the request's
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
        self.stale_quote_id = None  # the quote_id a trade_stale answer names; None: the one posted; "": none
        # (trade_status, trade_tx) served one per poll once the Trade is taken; the last repeats.
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
        """A new round on the same RFQ id: nothing taken, no template."""
        self.sig, self.draft, self.draft_qid = None, None, None


def accepts(session):
    return [x["body"] for x in session.calls if x["method"] == "POST" and x["path"] == f"/rfqs/{RFQ}/accept"]


def signed_posts(session):
    return [b for b in accepts(session) if "sig" in b]


def floor_file(c, account):
    return c._state_dir / f"side-nonce-{account.address.lower()}"


def trade_polls(session):
    return [x for x in session.calls if x["method"] == "GET" and x["path"] == f"/rfqs/{RFQ}"]


def rfq_posts(session):
    return [x for x in session.calls if x["method"] == "POST" and x["path"] == "/rfqs"]


SETUP_RPC = {"eth_chainId", "eth_getCode"}  # the chain check before a signature; no tx


def sent_nothing(session):
    """No tx out and no arm tx asked for: only the setup RPC reads, no /arm-tx call."""
    return set(session.rpc_methods()) <= SETUP_RPC and not any(p.endswith("/arm-tx") for p in session.paths())


@pytest.fixture
def clock():
    return Clock(float(int(time.time())))


@pytest.fixture
def venue(session, health, markets, clock):
    return Venue(session, health, markets, clock)


# ---------- one call: quote, then trade signs this seat's own Trade on the accept ----------


def test_quote_then_trade_signs_one_trade(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert (q.rfq_id, q.quote_id, q.rate, q.side, q.pair, q.notional) == (
        RFQ, QID, Decimal("18.700000"), "buy", "USD/MXN", Decimal("25000"))
    assert q.raw["trade_template"] == venue.draft
    b = venue.rfq_body
    assert (b["pair"], b["notional"], b["chain"], "wait" in b) == ("USDMXN", "25000", "avax-fuji", False)
    assert accepts(session) == [] and trade_polls(session) == []  # quote() never accepts; the answer named the winner

    t = c.trade(q)
    assert (t.status, t.tx, t.quote_id) == ("open", TXH, QID)
    assert t.raw == {"rfq_id": RFQ, "quote_id": QID, "client_quote_id": None}
    (body,) = accepts(session)
    assert set(body) == {"quote_id", "sig"} and body["quote_id"] == QID
    assert recovers(venue.template, body["sig"]) == account.address
    assert sent_nothing(session)
    assert floor_file(c, account).read_text() == venue.template["typed_data"]["message"]["ownNonce"]


@pytest.mark.parametrize("side,word", [("buy", "buy"), ("sell", "sell")])
def test_trade_signs_the_readable_trade(make_client, venue, session, clock, account, side, word):
    c = make_client(clock=clock)
    assert c.trade(c.quote("USD/MXN", side, 25_000)).status == "open"
    td = venue.template["typed_data"]
    msg = td["message"]
    assert td["primaryType"] == "Trade" and set(td) == {"types", "primaryType", "domain", "message"}
    assert msg["side"] == word and msg["pair"] == "USD/MXN" and msg["rateE6"] == "18700000"
    assert msg["summary"].startswith(f"{word} 25 000 USD vs MXN at 18.7 MXN per USD, matures ")
    assert msg["summary"].endswith(", upfront none")
    assert msg["ownLegId"] == venue.leg and e7.leg_id_tail(msg["ownLegId"]) == venue.qe
    assert typed_hash(td) == e7.trade_digest(venue.sep, msg)
    sig = signed_posts(session)[0]["sig"]
    assert recovers(venue.template, sig) == account.address
    assert int(sig[66:130], 16) <= e7.SECP256K1_N // 2 and sig[-2:] in ("1b", "1c")


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


def test_every_signed_call_carries_valid_headers(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    c.trade(c.quote("USDMXN", "BUY", "25000"))
    signed = [x for x in session.calls if "x-crx-sig" in x["headers"]]
    assert signed and all(x["path"] not in ("/health", "/markets") for x in signed)
    stamps = []
    for x in signed:
        h = x["headers"]
        assert {k for k in h if k.startswith("x-crx-")} == {"x-crx-address", "x-crx-ts", "x-crx-sig"}
        assert h["x-crx-address"] == account.address.lower()
        msg = rest_message(x["method"], x["path"], h["x-crx-address"], h["x-crx-address"], int(h["x-crx-ts"]), x["raw"])
        assert Account.recover_message(encode_defunct(text=msg), signature=h["x-crx-sig"]) == account.address
        stamps.append(int(h["x-crx-ts"]))
    assert stamps == sorted(set(stamps))  # each call stamped once, strictly later


# ---------- the trade status after the accept ----------


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


# ---------- the accept: retries, stale templates, refusals ----------


def test_the_accept_deadline_is_the_rfqs_closes_at_not_the_quote_end(make_client, venue, session, clock):
    # closes_at: 20 s after the open. The quote end (expires_at) is 570 s out: it never sets the deadline.
    venue.open_edit = {"closes_at": int(clock() * 1000) + 20_000}
    venue.row_edit = {"expires_at": int(clock() * 1000) + 570_000}
    posted = []

    def rejected(req):
        posted.append(clock())
        return 409, {"code": "rejected", "error": "rejected"}
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = rejected
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    closes = venue.open_edit["closes_at"] / 1000
    assert q.closes_at == datetime.fromtimestamp(closes, timezone.utc) and q.expires_at > q.closes_at
    with pytest.raises(crx.QuoteExpired):
        c.trade(q)
    assert len(posted) > 1 and max(posted) < closes and max(posted) + 3 >= closes


def test_quote_closes_at_is_none_when_the_open_answer_has_none(make_client, venue, session, clock):
    venue.open_edit = {"closes_at": None}
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert q.closes_at is None and c.trade(q).status == "open"


def test_rejected_accept_retries_then_quote_expired(make_client, venue, session, clock):
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (
        409, {"code": "rejected", "error": "rejected", "details": {"reject_code": "rj_0011223344556677"}})
    c = make_client(clock=clock)
    with pytest.raises(crx.QuoteExpired) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.code == "quote_expired" and ei.value.gateway_code == "rejected"
    assert sent_nothing(session)
    bodies = accepts(session)
    assert len(bodies) > 1 and all(b == bodies[0] for b in bodies)  # the same signed body, posted again


def test_own_round_open(make_client, venue, session, clock):
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (
        409, {"code": "own_round_open", "error": "open", "details": {"rfq_id": RFQ, "until": 123}})
    c = make_client(clock=clock)
    with pytest.raises(crx.OwnRoundOpen) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.details["until"] == 123 and len(accepts(session)) == 1


def test_trade_stale_signs_the_fresh_template(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    first = q.raw["trade_template"]
    venue.stale = 1
    assert c.trade(q).status == "open"
    a, b = accepts(session)
    assert set(a) == set(b) == {"quote_id", "sig"} and a["sig"] != b["sig"]
    assert recovers(first, a["sig"]) == account.address
    assert recovers(venue.template, b["sig"]) == account.address
    own, fresh = first["typed_data"]["message"], venue.template["typed_data"]["message"]
    assert fresh["ownLegId"] == own["ownLegId"] == venue.leg
    assert int(fresh["ownNonce"]) > int(own["ownNonce"])
    assert floor_file(c, account).read_text() == fresh["ownNonce"]


@pytest.mark.parametrize("late,posts", [(False, 3), (True, 1)])
def test_trade_stale_three_posts_at_most(make_client, venue, session, clock, late, posts):
    # Stale on every post: 3 signed posts, then a new quote. Past the RFQ's closes_at: no second post.
    venue.stale = 99
    if late:
        venue.on_stale = lambda: clock.sleep(121)  # the RFQ closes 120 s after the open
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    with pytest.raises(crx.QuoteExpired) as ei:
        c.trade(q)
    assert ei.value.gateway_code == "trade_stale"
    bodies = accepts(session)
    assert len(bodies) == posts and all(set(b) == {"quote_id", "sig"} for b in bodies)
    assert len({b["sig"] for b in bodies}) == posts and sent_nothing(session)


@pytest.mark.parametrize("names,signed_first,err", [
    ("other", False, crx.RefusedToSign),   # the template asked for names another quote: nothing signed
    ("other", True, crx.TradeUnknown),     # after a signed post: never "nothing happened"
    ("missing", False, crx.RefusedToSign),
    (None, False, None),                   # positive control: the quote posted
    (None, True, None),
])
def test_trade_stale_names_the_quote(make_client, venue, session, clock, account, names, signed_first, err):
    venue.stale_quote_id = {"other": QID2, "missing": "", None: None}[names]
    if signed_first:
        venue.stale = 1
    else:
        venue.winner = False
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, wait=3)
    if err is None:
        assert c.trade(q).status == "open"
        assert recovers(venue.template, accepts(session)[-1]["sig"]) == account.address
    else:
        with pytest.raises(err, match="another quote"):
            c.trade(q)
        assert len(accepts(session)) == 1
        assert ("sig" in accepts(session)[0]) is signed_first


def test_quote_not_the_winner_asks_for_its_template(make_client, venue, session, clock, account):
    venue.winner = False
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, wait=3)
    assert q.quote_id == QID and "trade_template" not in q.raw
    assert c.trade(q).status == "open"
    probe, signed = accepts(session)
    assert probe == {"quote_id": QID} and set(signed) == {"quote_id", "sig"}
    assert recovers(venue.template, signed["sig"]) == account.address


def test_template_below_the_floor_is_asked_again(make_client, venue, session, clock, account):
    # A template made before this seat's last signature: ask for a newer one, sign that.
    c = make_client(clock=clock)
    c.trade(c.quote("USD/MXN", "buy", 25_000))
    floor = int(venue.template["typed_data"]["message"]["ownNonce"])
    venue.reset()
    venue.nonces = [floor]
    q = c.quote("USD/MXN", "buy", 25_000)
    assert int(q.raw["trade_template"]["typed_data"]["message"]["ownNonce"]) == floor
    before = len(accepts(session))
    assert c.trade(q).status == "open"
    ask, signed = accepts(session)[before:]
    assert ask == {"quote_id": QID} and set(signed) == {"quote_id", "sig"}
    assert int(venue.template["typed_data"]["message"]["ownNonce"]) > floor
    assert recovers(venue.template, signed["sig"]) == account.address


def test_nonce_floor_refuses_replay(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    c.trade(c.quote("USD/MXN", "buy", 25_000))
    floor = venue.template["typed_data"]["message"]["ownNonce"]
    venue.reset()
    venue.msg_edit = {"ownNonce": floor}  # the gateway keeps serving the nonce this seat signed
    q = c.quote("USD/MXN", "buy", 25_000)
    before = len(accepts(session))
    with pytest.raises(crx.RefusedToSign, match="^refused to sign: ownNonce is not above the last one this seat signed$"):
        c.trade(q)
    # Asked for a newer template while a signed post still fits; nothing signed.
    assert accepts(session)[before:] == [{"quote_id": QID}] * 2
    assert floor_file(c, account).read_text() == floor


@pytest.mark.parametrize("answer,err", [
    ((409, "round_closed"), crx.QuoteExpired),
    ((409, "own_round_open"), crx.OwnRoundOpen),
    ((422, "insufficient_collateral"), crx.TradeUnknown),
    ((400, "invalid_signature"), crx.TradeUnknown),
    ((409, "trade_stale"), crx.TradeUnknown),  # its fresh template cannot be read
])
def test_refusal_after_the_trade_is_signed(make_client, venue, session, clock, answer, err):
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (answer[0], {"code": answer[1], "error": answer[1],
                                                                   "details": {"trade_template": "?"}})
    c = make_client(clock=clock)
    with pytest.raises(err) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    if err is crx.TradeUnknown:
        assert "may still open" in str(ei.value) and ei.value.details == {"rfq_id": RFQ}
    assert len(accepts(session)) == 1 and sent_nothing(session)


def test_stale_template_that_fails_the_check_after_a_signature(make_client, venue, session, clock):
    # The first Trade is signed and posted; the fresh one serves another rate: never "nothing happened".
    venue.stale = 1
    venue.on_stale = lambda: venue.msg_edit.update(rateE6="18800000")
    c = make_client(clock=clock)
    with pytest.raises(crx.TradeUnknown, match="differs at message.rateE6"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert len(signed_posts(session)) == 1


@pytest.mark.parametrize("fault", ["5xx", "network"])
def test_signed_accept_no_answer_reads_status(make_client, venue, session, clock, fault):
    # No answer to the signed accept: the gateway may hold the Trade, so the status decides.
    def accept(req):
        venue.accept(req)
        if fault == "network":
            raise requests.ConnectionError("reset by peer")
        return (503, {"code": "upstream", "error": "down"})
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = accept
    c = make_client(clock=clock)
    t = c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert (t.status, t.raw) == ("open", {}) and len(accepts(session)) == 1


def test_network_fault_before_a_signature_raises_as_is(make_client, venue, session, clock, account):
    # The template ask failed: nothing was signed, so the fault is not TradeUnknown.
    venue.winner = False

    def accept(req):
        raise requests.ConnectionError("reset by peer")
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = accept
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, wait=3)
    with pytest.raises(crx.NetworkError) as ei:
        c.trade(q)
    assert not isinstance(ei.value, crx.TradeUnknown)
    assert signed_posts(session) == [] and not floor_file(c, account).exists()


@pytest.mark.parametrize("code", ["upstream", None])  # None: a 5xx with no JSON body
def test_other_5xx_on_the_signed_accept_reads_status(make_client, venue, session, clock, code):
    def accept(req):
        if "sig" in req["body"]:
            venue.sig = req["body"]["sig"]
            return (503, {"code": code, "error": "down"} if code else "<html>down</html>")
        return venue.accept(req)
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = accept
    c = make_client(clock=clock)
    assert c.trade(c.quote("USD/MXN", "buy", 25_000)).status == "open"


# ---------- the accept's band decline: typed, nothing reserved, never "may still open" ----------

DECLINE = {
    "rate_out_of_band": (422, {"pair": "USD/MXN", "rate": "18.700000", "mark": "17.900000", "band_bps": 250},
                         crx.RateOutOfBand),
    "mark_unavailable": (503, {"pair": "USD/MXN"}, crx.MarkUnavailable),
    "position_matured": (409, {}, crx.PositionMatured),
}


def declines(venue, session, code, signed_only=True):
    """The accept answers the gateway's band decline ``code``: on a signed post, or on any post."""
    status, details, _ = DECLINE[code]

    def accept(req):
        if signed_only and "sig" not in req["body"]:
            return venue.accept(req)
        return (status, {"code": code, "error": f"declined: {code}", "details": details})
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = accept


@pytest.mark.parametrize("code", list(DECLINE))
def test_accept_decline_is_typed(make_client, venue, session, clock, code):
    declines(venue, session, code)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    polls, start = len(trade_polls(session)), clock()
    with pytest.raises(DECLINE[code][2]) as ei:
        c.trade(q)
    e = ei.value
    assert isinstance(e, crx.Declined) and not isinstance(e, crx.TradeUnknown)
    assert (e.code, e.status, e.gateway_code, e.details) == (code, DECLINE[code][0], code, DECLINE[code][1])
    assert len(trade_polls(session)) == polls and clock() == start  # no status polls: nothing was reserved
    assert len(signed_posts(session)) == 1 and sent_nothing(session)


@pytest.mark.parametrize("code", list(DECLINE))
def test_decline_on_the_unsigned_ask_is_typed(make_client, venue, session, clock, code):
    venue.winner = False  # the row carries no template: the SDK asks for one first
    declines(venue, session, code, signed_only=False)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, wait=1)
    with pytest.raises(DECLINE[code][2]):
        c.trade(q)
    assert accepts(session) == [{"quote_id": QID}]


# ---------- what the SDK checks before it signs (SPEC §5.2): one refusal each ----------


def put(path, value):
    """An edit of the served typed_data at ``path`` (``message.rateE6``, ``primaryType``...)."""
    def edit(td):
        *head, last = path.split(".")
        node = td
        for k in head:
            node = node[k]
        node[last] = value(node[last]) if callable(value) else value
    return edit


def refused_unsigned(session, c, account):
    """No signed accept posted and no nonce floor written."""
    return signed_posts(session) == [] and not floor_file(c, account).exists() and sent_nothing(session)


SERVED_EDITS = [
    ("domain.name", "CRX2"),
    ("domain.version", "rulebook-1.1"),
    ("domain.chainId", 43114),
    ("domain.verifyingContract", "0x" + "ab" * 20),
    ("primaryType", "Side"),
    ("message.summary", lambda s: s.replace("25 000", "250 000")),
    ("message.summary", lambda s: s.replace("25 000", "25,000")),
    ("message.pair", "USD/BRL"),
    ("message.side", "sell"),
    ("message.notionalE6", "250000000000"),
    ("message.rateE6", "18700001"),
    ("message.premiumBps", "1"),
    ("message.maturity", lambda m: str(int(m) + 1)),
]


@pytest.mark.parametrize("path,value", SERVED_EDITS, ids=[f"{p}-{i}" for i, (p, _) in enumerate(SERVED_EDITS)])
def test_served_typed_data_must_equal_own(make_client, venue, session, clock, account, path, value):
    venue.typed_edit = put(path, value)
    c = make_client(clock=clock)
    with pytest.raises(crx.RefusedToSign, match=f"^refused to sign: the served typed_data differs at {re.escape(path)}$"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert refused_unsigned(session, c, account)


EXTRA_MEMBER = {"name": "quoteExpiry", "type": "uint64"}


@pytest.mark.parametrize("types", [
    lambda t: {**t, "Trade": t["Trade"] + [EXTRA_MEMBER]},                    # the Trade type with an extra member
    lambda t: {**t, "Trade": t["Trade"][:9] + [EXTRA_MEMBER] + t["Trade"][9:]},  # quoteExpiry after ownLegId
    lambda t: {**t, "Trade": t["Trade"][1:]},                                 # a member short
    lambda t: {**t, "Trade": t["Trade"][::-1]},                               # reordered
    lambda t: {"Trade": t["Trade"]},                                          # no EIP712Domain
    lambda t: {**t, "Side": t["Trade"]},                                      # another struct beside it
], ids=["extra", "quote-expiry-after-leg", "short", "reordered", "no-domain", "other-struct"])
def test_trade_type_must_be_the_spec_string(make_client, venue, session, clock, account, types):
    venue.typed_edit = put("types", types)
    c = make_client(clock=clock)
    with pytest.raises(crx.RefusedToSign, match="^refused to sign: the served typed_data differs at types$"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert refused_unsigned(session, c, account)


def test_trade_type_string_is_the_spec_one(make_client, venue, session, clock):
    # The positive control of the type check: the template the SDK signs names the §1 string.
    c = make_client(clock=clock)
    assert c.trade(c.quote("USD/MXN", "buy", 25_000)).status == "open"
    fields = venue.template["typed_data"]["types"]["Trade"]
    assert "Trade(" + ",".join(f"{f['type']} {f['name']}" for f in fields) + ")" == (
        "Trade(string summary,string pair,string side,uint256 notionalE6,uint64 rateE6,int16 premiumBps,"
        "uint40 maturity,bytes32 pairC,bytes32 ownLegId,uint64 ownNonce,bytes32 ownSalt)")


@pytest.mark.parametrize("bad,at", [
    ("extra_member", "message"), ("missing_member", "message"), ("unparsable", "message.notionalE6"),
    ("extra_top", "typed_data"), ("missing_top", "typed_data"), ("extra_domain", "domain"),
    ("missing_domain", "domain"),
])
def test_served_message_shape_refused(make_client, venue, session, clock, account, bad, at):
    # Key sets are exact at the top level, in domain and in message.
    venue.typed_edit = {
        "extra_member": lambda td: td["message"].update(quoteExpiry="1"),
        "missing_member": lambda td: td["message"].pop("summary"),
        "unparsable": put("message.notionalE6", "25e9"),
        "extra_top": lambda td: td.update(metadata={"note": "x"}),
        "missing_top": lambda td: td.pop("primaryType"),
        "extra_domain": lambda td: td["domain"].update(salt="0x" + "00" * 32),
        "missing_domain": lambda td: td["domain"].pop("version"),
    }[bad]
    c = make_client(clock=clock)
    with pytest.raises(crx.RefusedToSign, match=f"differs at {re.escape(at)}$"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert refused_unsigned(session, c, account)


def test_served_values_compare_parsed(make_client, venue, session, clock, account):
    # The same values in another spelling: upper-case hex, integers as JSON numbers.
    def respell(td):
        m = td["message"]
        m["pairC"] = "0x" + m["pairC"][2:].upper()
        m["notionalE6"] = int(m["notionalE6"])
        td["domain"]["verifyingContract"] = "0x" + td["domain"]["verifyingContract"][2:].upper()
    venue.typed_edit = respell
    c = make_client(clock=clock)
    assert c.trade(c.quote("USD/MXN", "buy", 25_000)).status == "open"
    assert recovers(venue.template, venue.sig) == account.address


@pytest.mark.parametrize("template", [
    lambda t: dict(t, digest="0x" + "00" * 32),     # a loose hash beside typed_data
    lambda t: dict(t, c_maker="0x" + "00" * 32),
    lambda t: {},                                   # no typed_data
    lambda t: {"digest": "0x" + "00" * 32},
], ids=["digest", "c_maker", "empty", "digest-only"])
def test_template_holds_exactly_typed_data(make_client, venue, session, clock, account, template):
    venue.template_edit = template
    c = make_client(clock=clock)
    with pytest.raises(crx.RefusedToSign, match=r"^refused to sign: the trade template is not \{typed_data\}$"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert refused_unsigned(session, c, account)


def test_template_without_a_message_cannot_be_read(make_client, venue, session, clock, account):
    venue.typed_edit = lambda td: td.pop("message")
    c = make_client(clock=clock)
    with pytest.raises(crx.RefusedToSign, match="^refused to sign: the template cannot be read$"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert refused_unsigned(session, c, account)


def test_own_leg_id_must_be_the_open_answers(make_client, venue, session, clock, account):
    # Another head, the same quote end: only the leg id check sees it.
    venue.msg_edit = {"ownLegId": lambda v: e7.leg_id_for(b"\x99" * 24, e7.leg_id_tail(v))}
    c = make_client(clock=clock)
    with pytest.raises(crx.RefusedToSign, match="^refused to sign: ownLegId is not this RFQ's leg$"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert refused_unsigned(session, c, account)


@pytest.mark.parametrize("delta", [1, -1, 0])  # 0: the positive control
def test_own_leg_id_tail_must_be_quote_expiry_max(make_client, venue, session, clock, account, delta):
    # The open answer's leg_id (and so ownLegId) ends delta s off the quote_expiry_max it serves.
    venue.tail_delta = delta
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert q.rfq["leg_id"] == venue.leg and q.rfq["quote_expiry_max"] == venue.qe
    if delta:
        with pytest.raises(crx.RefusedToSign,
                           match="^refused to sign: the ownLegId tail is not the RFQ's quote_expiry_max$"):
            c.trade(q)
        assert refused_unsigned(session, c, account)
    else:
        assert c.trade(q).status == "open"


@pytest.mark.parametrize("ahead_ms,signs", [(86_400_001, False), (86_400_000, True)])
def test_own_nonce_at_most_24_h_ahead(make_client, venue, session, clock, account, ahead_ms, signs):
    venue.msg_edit = {"ownNonce": lambda v: str(int(clock() * 1000) + ahead_ms)}
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    served = str(int(clock() * 1000) + ahead_ms)
    assert q.raw["trade_template"]["typed_data"]["message"]["ownNonce"] == served
    if signs:
        assert c.trade(q).status == "open"
        assert floor_file(c, account).read_text() == served and recovers(venue.template, venue.sig) == account.address
    else:
        with pytest.raises(crx.RefusedToSign, match="^refused to sign: ownNonce is more than 24 h ahead$"):
            c.trade(q)
        assert refused_unsigned(session, c, account)


def test_own_nonce_at_or_below_the_floor_is_refused(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    floor = str(int(clock() * 1000) + 5_000)  # this seat already signed a later nonce on this machine
    floor_file(c, account).parent.mkdir(parents=True, exist_ok=True)
    floor_file(c, account).write_text(floor)
    venue.msg_edit = {"ownNonce": floor}
    with pytest.raises(crx.RefusedToSign, match="^refused to sign: ownNonce is not above the last one this seat signed$"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert signed_posts(session) == [] and floor_file(c, account).read_text() == floor


@pytest.mark.parametrize("ahead", [0, -60])
def test_quote_end_passed_is_quote_expired(make_client, venue, session, clock, account, ahead):
    venue.qe_ahead = ahead
    c = make_client(clock=clock)
    with pytest.raises(crx.QuoteExpired, match="^the quote end has passed; not opened; request a new quote$") as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert not isinstance(ei.value, crx.TradeUnknown)
    assert refused_unsigned(session, c, account)


@pytest.mark.parametrize("ahead,signs", [(631, False), (630, True), (1, True)])
def test_quote_end_at_most_630_s_ahead(make_client, venue, session, clock, account, ahead, signs):
    venue.qe_ahead = ahead
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    if signs:
        assert c.trade(q).status == "open"
    else:
        with pytest.raises(crx.RefusedToSign, match="^refused to sign: the quote end is more than 630 s ahead$"):
            c.trade(q)
        assert refused_unsigned(session, c, account)


# ---------- the premium: the caller's, inside the market's cap ----------


def with_cap(markets, cap, pair="USD/MXN"):
    """/markets with this chain's ``max_premium_bps`` on ``pair`` set; None removes it."""
    m = open_market(markets, pair)
    for row in m["markets"]:
        if row["pair"] == pair:
            for ch in row["chains"]:
                if cap is None:
                    ch.pop("max_premium_bps", None)
                else:
                    ch["max_premium_bps"] = cap
    return m


@pytest.mark.parametrize("premium,cap,up", [
    (5, 200, "taker pays 0.05 %"),
    (200, 200, "taker pays 2.00 %"),
    (-200, 200, "maker pays 2.00 %"),
    (0, None, "none"),
    (0, 200, "none"),
])
def test_premium_within_the_cap_signs(make_client, venue, session, markets, clock, account, premium, cap, up):
    session.routes[("GET", "/markets")] = with_cap(markets, cap)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, premium_bps=premium)
    assert q.rfq["max_premium_bps"] == cap and q.rfq["premium_bps"] == premium
    assert c.trade(q).status == "open"
    msg = venue.template["typed_data"]["message"]
    assert msg["premiumBps"] == str(premium) and msg["summary"].endswith(f", upfront {up}")
    assert recovers(venue.template, venue.sig) == account.address


def test_served_premium_other_than_the_ask_is_refused(make_client, venue, session, markets, clock, account):
    # The gateway builds its Trade with 6 bps; the caller asked 5: the summary already differs.
    session.routes[("GET", "/markets")] = with_cap(markets, 200)
    venue.premium = 6
    c = make_client(clock=clock)
    with pytest.raises(crx.RefusedToSign, match="differs at message.summary$"):
        c.trade(c.quote("USD/MXN", "buy", 25_000, premium_bps=5))
    assert refused_unsigned(session, c, account)


@pytest.mark.parametrize("premium", [201, -201])
def test_premium_beyond_the_cap_is_refused(make_client, venue, session, markets, clock, account, premium):
    session.routes[("GET", "/markets")] = with_cap(markets, 200)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, premium_bps=premium)
    with pytest.raises(crx.RefusedToSign, match=f"^refused to sign: the premium {premium} bps is above the cap "
                                                r"\(200 bps\)$"):
        c.trade(q)
    assert refused_unsigned(session, c, account)


@pytest.mark.parametrize("premium", [5, -5])
def test_a_premium_with_no_cap_served_is_refused(make_client, venue, session, markets, clock, account, premium):
    session.routes[("GET", "/markets")] = with_cap(markets, None)
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, premium_bps=premium)
    with pytest.raises(crx.RefusedToSign,
                       match="^refused to sign: the premium is not 0 and the market serves no premium cap$"):
        c.trade(q)
    assert refused_unsigned(session, c, account)


@pytest.mark.parametrize("premium", [10_001, -10_001, True, 1.5, "5", None])
def test_premium_out_of_range_is_refused_before_any_post(make_client, session, markets, premium):
    session.routes[("GET", "/markets")] = open_market(markets, "USD/MXN")
    c = make_client()
    for call in (c.quote, c.ask):
        with pytest.raises(crx.BadRequest, match="^premium_bps is an integer from -10000 to 10000$"):
            call("USD/MXN", "buy", 25_000, premium_bps=premium)
    assert session.paths("POST") == []


@pytest.mark.parametrize("premium", [0, 5, -5, 10_000, -10_000])
def test_premium_is_sent_only_when_not_zero(make_client, venue, session, clock, premium):
    make_client(clock=clock).quote("USD/MXN", "buy", 25_000, premium_bps=premium)
    [post] = rfq_posts(session)
    keys = {"chain", "pair", "side", "notional", "expiry", "client_rfq_id"}
    assert set(post["body"]) == (keys | {"premium_bps"} if premium else keys)
    assert post["body"].get("premium_bps") == (premium or None)


# ---------- markets: paused only when this chain's row says so, or there is none ----------


def fuji_row(markets, **edit):
    """/markets with USD/MXN's fuji row edited; a None value removes the key."""
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


def test_a_market_row_without_paused_is_open(make_client, venue, session, markets, clock):
    """A chain row with no ``paused`` key is open: markets() reads it as not paused and quote() posts the
    RFQ. Read as paused, the missing key would refuse every pair."""
    m = fuji_row(markets, paused=None)
    assert all("paused" not in ch for row in m["markets"] for ch in row["chains"])
    session.routes[("GET", "/markets")] = m
    c = make_client(clock=clock)
    assert c.market("USD/MXN").paused is False
    assert c.quote("USD/MXN", "buy", 25_000).quote_id == QID
    assert session.paths("POST") == ["/rfqs"]


def test_a_market_row_paused_false_is_open(make_client, venue, session, markets, clock):
    session.routes[("GET", "/markets")] = fuji_row(markets, paused=False)
    c = make_client(clock=clock)
    assert c.market("USD/MXN").paused is False
    assert c.quote("USD/MXN", "buy", 25_000).quote_id == QID


def test_a_market_row_paused_true_is_market_paused(make_client, venue, session, markets, clock):
    session.routes[("GET", "/markets")] = fuji_row(markets, paused=True)
    c = make_client(clock=clock)
    assert c.market("USD/MXN").paused is True
    with pytest.raises(crx.MarketPaused, match="^USD/MXN is paused$") as ei:
        c.quote("USD/MXN", "buy", 25_000)
    assert ei.value.details == {"pair": "USD/MXN"} and session.paths("POST") == []


def test_another_chains_paused_row_leaves_this_chain_open(make_client, venue, session, markets, clock):
    m = fuji_row(markets)
    for row in m["markets"]:
        if row["pair"] == "USD/MXN":
            row["chains"].append({"chain": "ethereum", "paused": True, "max_premium_bps": 200})
    session.routes[("GET", "/markets")] = m
    c = make_client(clock=clock)
    assert c.market("USD/MXN").paused is False
    assert c.quote("USD/MXN", "buy", 25_000).quote_id == QID


def without(markets: dict, pair: str) -> dict:
    m = open_market(markets, "USD/MXN")
    m["markets"] = [row for row in m["markets"] if row["pair"] != pair]
    return m


@pytest.mark.parametrize("pair", ["USD/MXN", "USD/XXX"])
def test_market_not_offered_is_market_paused(make_client, session, markets, pair):
    session.routes[("GET", "/markets")] = without(markets, "USD/MXN")
    with pytest.raises(crx.MarketPaused) as ei:
        make_client().market(pair)
    assert str(ei.value) == f"{pair} is not offered on avax-fuji" and ei.value.details == {"pair": pair}


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
        c.quote("USD/MXN", "buy", 25_000)
    assert session.paths("POST") == []


def test_quote_not_offered_sends_nothing(make_client, session, markets):
    session.routes[("GET", "/markets")] = without(markets, "USD/MXN")
    with pytest.raises(crx.MarketPaused) as ei:
        make_client().quote("USD/MXN", "buy", 25_000)
    assert ei.value.code == "market_paused" and ei.value.details == {"pair": "USD/MXN"}
    assert session.paths("POST") == []


def test_paused_pair(make_client, session):
    with pytest.raises(crx.MarketPaused):
        make_client().quote("USD/JPY", "buy", 25_000)
    assert session.paths("POST") == []


# ---------- the open answer ----------


@pytest.mark.parametrize("edit", [
    {"quote_expiry_max": None}, {"leg_id": None}, {"rfq_id": None},
    {"quote_expiry_max": "1790000300"}, {"quote_expiry_max": True}, {"quote_expiry_max": 1790000300.0},
    {"leg_id": "0x" + "22" * 31}, {"leg_id": "22" * 32},
], ids=["no-qe-max", "no-leg-id", "no-rfq-id", "qe-max-text", "qe-max-bool", "qe-max-float", "leg-id-short",
        "leg-id-no-0x"])
@pytest.mark.parametrize("call", ["quote", "ask"])
def test_open_answer_without_its_leg_is_bad_answer(make_client, venue, session, clock, edit, call):
    venue.open_edit = edit
    c = make_client(clock=clock)
    with pytest.raises(crx.BadAnswer, match="^/rfqs sent an answer this SDK cannot read$"):
        getattr(c, call)("USD/MXN", "buy", 25_000)
    assert accepts(session) == [] and trade_polls(session) == []


EXPIRY_MS = 1_900_000_000_000


def test_quote_terms_come_from_the_ask(make_client, venue, session, clock, account):
    # A row that carries copies of the terms: the Quote and the Trade take the ask's.
    venue.row_edit = {"notional": "250000.000000", "expiry": 1, "side": -1, "premium_bps": 9, "pair_id": "0x00"}
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, expiry=EXPIRY_MS)
    assert (q.notional, q.side, q.pair, q.expiry_ms) == (Decimal("25000"), "buy", "USD/MXN", EXPIRY_MS)
    assert q.expiry == datetime.fromtimestamp(EXPIRY_MS / 1000, timezone.utc)
    assert c.trade(q).status == "open"
    msg = venue.template["typed_data"]["message"]
    assert (msg["notionalE6"], msg["maturity"], msg["side"]) == ("25000000000", str(EXPIRY_MS // 1000), "buy")


def test_quote_row_without_copies_reads(make_client, venue, session, clock):
    q = make_client(clock=clock).quote("USD/MXN", "sell", "25000.5", expiry=EXPIRY_MS)
    assert set(q.raw) == {"quote_id", "rate", "expires_at", "status", "trade_template"}
    assert (q.notional, q.side, q.expiry_ms) == (Decimal("25000.5"), "sell", EXPIRY_MS)


@pytest.mark.parametrize("row", [{"rate": "x"}, {"rate": None}])
def test_quote_row_that_cannot_be_read_is_bad_answer(make_client, venue, session, clock, row):
    venue.waited = False
    session.routes[("GET", f"/rfqs/{RFQ}")] = lambda req: {"quote": None, "quotes": [dict(venue.row(), **row)]}
    with pytest.raises(crx.BadAnswer, match="the quote row cannot be read"):
        make_client(clock=clock).quote("USD/MXN", "buy", 25_000, wait=1)


# ---------- quote(): the winner ----------


def test_no_quotes(make_client, venue, session, clock):
    venue.waited = False
    session.routes[("GET", f"/rfqs/{RFQ}")] = {"quote": None, "quotes": []}
    c = make_client(clock=clock)
    with pytest.raises(crx.NoQuotes):
        c.quote("USD/MXN", "buy", 25_000, wait=5)


def test_best_live_quote_when_no_pick(make_client, venue, session, clock):
    venue.waited = False
    session.routes[("GET", f"/rfqs/{RFQ}")] = lambda req: {"quote": None, "quotes": [venue.row()]}
    c = make_client(clock=clock)
    assert c.quote("USD/MXN", "buy", 25_000, wait=3).quote_id == QID


def test_closed_market_still_opens_the_rfq(make_client, venue, session, markets, clock):
    session.routes[("GET", "/markets")] = markets  # recorded /markets: weekend, every session closed
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert q.quote_id == QID
    assert session.paths("POST") == ["/rfqs"]


def test_closed_market_no_maker_is_no_quotes(make_client, venue, session, markets, clock):
    venue.waited = False
    session.routes[("GET", "/markets")] = markets
    session.routes[("GET", f"/rfqs/{RFQ}")] = {"quote": None, "quotes": []}
    c = make_client(clock=clock)
    with pytest.raises(crx.NoQuotes):
        c.quote("USD/MXN", "buy", 25_000, wait=3)


def test_quote_sends_no_wait_and_takes_a_30_s_timeout(make_client, venue, session, clock):
    # The gateway answers after the window when the body has no `wait`.
    make_client(clock=clock).quote("USD/MXN", "buy", 25_000)
    [post] = rfq_posts(session)
    assert "wait" not in post["body"] and post["timeout"] == 30
    assert {x["timeout"] for x in session.calls if x is not post} == {10}


def test_quote_timeout_is_never_below_the_client_timeout(make_client, venue, session, clock):
    make_client(clock=clock, timeout=45).quote("USD/MXN", "buy", 25_000)
    assert rfq_posts(session)[0]["timeout"] == 45


def test_waited_answer_carries_the_winner_and_its_template(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert (q.rfq_id, q.quote_id, q.rate) == (RFQ, QID, Decimal("18.700000"))
    assert trade_polls(session) == []  # the answer named the winner: no poll
    assert (q.rfq["leg_id"], q.rfq["quote_expiry_max"]) == (venue.leg, venue.qe)
    assert q.raw["trade_template"] == venue.draft
    t = c.trade(q)
    assert (t.status, t.tx) == ("open", TXH) and sent_nothing(session)
    [body] = accepts(session)
    assert set(body) == {"quote_id", "sig"} and recovers(venue.draft, body["sig"]) == account.address


@pytest.mark.parametrize("expired", [False, True])
def test_waited_answer_without_a_quote_is_no_quotes(make_client, venue, session, clock, expired):
    venue.waited_view = lambda: {"quote": None, "quotes": [dict(venue.row(), expires_at=1)] if expired else []}
    c = make_client(clock=clock)
    start = clock()
    with pytest.raises(crx.NoQuotes) as ei:
        c.quote("USD/MXN", "buy", 25_000)
    assert ei.value.details == {"rfq_id": RFQ} and trade_polls(session) == [] and clock() == start


def test_waited_answer_without_a_pick_takes_the_best_live_quote(make_client, venue, session, clock):
    # The gateway's wait cap answered before it named a winner.
    venue.waited_view = lambda: {"quote": None, "quotes": [venue.row()]}
    q = make_client(clock=clock).quote("USD/MXN", "buy", 25_000)
    assert q.quote_id == QID and trade_polls(session) == []


def other_row(venue, status):
    """A better-priced row in `status`; None leaves the status out."""
    row = dict(venue.row(), quote_id=QID2, rate="18.500000", status=status)
    return {k: v for k, v in row.items() if v is not None}


@pytest.mark.parametrize("status", ["dropped", "declined", "live", None])
def test_pick_takes_only_a_quoted_row(make_client, venue, session, clock, status):
    venue.waited_view = lambda: {"quote": None, "quotes": [other_row(venue, status), venue.row()]}
    assert make_client(clock=clock).quote("USD/MXN", "buy", 25_000).quote_id == QID


@pytest.mark.parametrize("waited", [True, False])
@pytest.mark.parametrize("status", ["dropped", "declined"])
def test_dropped_or_declined_rows_never_win(make_client, venue, session, clock, status, waited):
    view = lambda: {"quote": None, "quotes": [other_row(venue, status)]}
    venue.waited, venue.waited_view = waited, view
    session.routes[("GET", f"/rfqs/{RFQ}")] = lambda req: view()
    with pytest.raises(crx.NoQuotes):
        make_client(clock=clock).quote("USD/MXN", "buy", 25_000, wait=3)


@pytest.mark.parametrize("edit", [{"expires_at": 1}, {"status": "expired"}, {"expires_at": "99999999999999"}])
def test_a_named_pick_must_be_live(make_client, venue, session, clock, edit):
    def view():
        pick = {k: v for k, v in dict(venue.row(), **edit).items() if v is not None}
        return {"quote": pick, "quotes": [pick]}
    venue.waited_view = view
    with pytest.raises(crx.NoQuotes):
        make_client(clock=clock).quote("USD/MXN", "buy", 25_000)


# ---------- an RFQ that ended: a cancel names its reason; rows are taken only while it is open ----------

BAND_MSG = "every quote was more than 2.50 % from the market price"


def ended_view(venue, status="cancelled", reason="rate_out_of_band", message=BAND_MSG):
    """The gateway's view of an RFQ the off-market screen cancelled: no pick, the rows still read quoted."""
    v = {"status": status, "quote": None, "quotes": [venue.row()]}
    v.update({k: x for k, x in (("reason", reason), ("message", message)) if x is not None})
    return v


def ask_or_quote(c, path):
    if path == "ask":
        return c.ask("USD/MXN", "buy", 25_000).quote(wait=3)
    return c.quote("USD/MXN", "buy", 25_000, wait=3)


def serve(venue, session, path, view):
    if path == "waited":
        venue.waited, venue.waited_view = True, view
    else:
        venue.waited = False
        session.routes[("GET", f"/rfqs/{RFQ}")] = lambda req: view()


@pytest.mark.parametrize("path", ["waited", "polled", "ask"])
def test_band_cancelled_rfq_raises_its_reason(make_client, venue, session, clock, path):
    serve(venue, session, path, lambda: ended_view(venue))
    c = make_client(clock=clock)
    start = clock()
    with pytest.raises(crx.RfqCancelled) as ei:
        ask_or_quote(c, path)
    e = ei.value
    assert isinstance(e, crx.NoQuotes) and e.code == "rfq_cancelled" and e.reason == "rate_out_of_band"
    assert str(e) == BAND_MSG
    assert e.details == {"rfq_id": RFQ, "status": "cancelled", "reason": "rate_out_of_band", "message": BAND_MSG}
    assert accepts(session) == [] and len(trade_polls(session)) == (0 if path == "waited" else 1) and clock() == start


def test_cancel_without_a_reason(make_client, venue, session, clock):
    serve(venue, session, "waited", lambda: ended_view(venue, reason=None, message=None))
    with pytest.raises(crx.RfqCancelled, match="no reason named") as ei:
        make_client(clock=clock).quote("USD/MXN", "buy", 25_000)
    assert ei.value.reason is None and accepts(session) == []


@pytest.mark.parametrize("status", ["expired", "accepted", "signing"])
@pytest.mark.parametrize("path", ["waited", "polled"])
def test_ended_rfq_is_no_quotes(make_client, venue, session, clock, status, path):
    serve(venue, session, path, lambda: ended_view(venue, status, None, None))
    with pytest.raises(crx.NoQuotes) as ei:
        ask_or_quote(make_client(clock=clock), path)
    assert type(ei.value) is crx.NoQuotes and ei.value.details == {"rfq_id": RFQ, "status": status}
    assert accepts(session) == []


@pytest.mark.parametrize("status", ["open", "quoted"])  # the positive control: the same rows, the RFQ open
def test_open_rfq_still_falls_back_to_a_quoted_row(make_client, venue, session, clock, status):
    serve(venue, session, "waited", lambda: ended_view(venue, status, None, None))
    assert make_client(clock=clock).quote("USD/MXN", "buy", 25_000).quote_id == QID


@pytest.mark.parametrize("live", [True, False])
@pytest.mark.parametrize("pick", ["expired", "declined", "accepted", None])
def test_a_pick_not_quoted_never_wins(make_client, venue, session, clock, pick, live):
    serve(venue, session, "waited", lambda: {"quote": other_row(venue, pick),
                                             "quotes": [venue.row() if live else other_row(venue, pick)]})
    c = make_client(clock=clock)
    if live:
        assert c.quote("USD/MXN", "buy", 25_000).quote_id == QID
    else:
        with pytest.raises(crx.NoQuotes):
            c.quote("USD/MXN", "buy", 25_000)


def test_answer_at_once_polls(make_client, venue, session, clock):
    # An open answer without `quotes`: the gateway answered at once, so the SDK polls.
    venue.waited = False
    session.routes[("GET", f"/rfqs/{RFQ}")] = [{"quote": None, "quotes": []}, venue.view]
    q = make_client(clock=clock).quote("USD/MXN", "buy", 25_000)
    assert q.quote_id == QID and len(trade_polls(session)) == 2 and "trade_template" in q.raw


# ---------- ask() ----------

OPEN_BODY = (b'{"chain":"avax-fuji","pair":"USDMXN","side":"buy","notional":"25000","expiry":1900000000000,'
             b'"client_rfq_id":"desk-1"%s}')


def test_quote_wire_body(make_client, venue, session, clock):
    make_client(clock=clock).quote("USD/MXN", "buy", 25_000, expiry=EXPIRY_MS, client_rfq_id="desk-1")
    [post] = rfq_posts(session)
    assert post["raw"] == OPEN_BODY % b"" and post["timeout"] == 30


def test_ask_sends_wait_false_and_returns_at_once(make_client, venue, session, clock):
    start = clock()
    a = make_client(clock=clock).ask("USD/MXN", "buy", 25_000, expiry=EXPIRY_MS, client_rfq_id="desk-1")
    [post] = rfq_posts(session)
    assert post["raw"] == OPEN_BODY % b',"wait":false' and post["timeout"] == 10
    assert trade_polls(session) == [] and clock() == start
    assert isinstance(a, crx.Ask)
    assert (a.rfq_id, a.pair, a.side, a.notional) == (RFQ, "USD/MXN", "buy", Decimal("25000"))
    assert a.expiry == datetime.fromtimestamp(EXPIRY_MS / 1000, timezone.utc)
    assert a.raw["leg_id"] == venue.leg and a.rfq["quote_expiry_max"] == venue.qe and "Client" not in repr(a)
    with pytest.raises(AttributeError):
        a.rfq_id = QID


def test_ask_sends_its_own_client_rfq_id_by_default(make_client, venue, session, clock):
    make_client(clock=clock).ask("USD/MXN", "buy", 25_000)
    [post] = rfq_posts(session)
    assert re.fullmatch(r"sdk-[0-9a-f]{12}", post["body"]["client_rfq_id"])


def test_ask_notional_is_the_notional_sent(make_client, venue, session, clock):
    a = make_client(clock=clock).ask("USD/MXN", "buy", "25000.00")
    [post] = rfq_posts(session)
    assert post["body"]["notional"] == "25000" and str(a.notional) == "25000"


def test_ask_is_public():
    import crx.models
    assert "Ask" in crx.__all__ and crx.Ask is crx.models.Ask


def test_ask_quote_is_the_winner_and_trades(make_client, venue, session, clock):
    session.routes[("GET", f"/rfqs/{RFQ}")] = [{"quote": None, "quotes": []}, venue.view]
    c = make_client(clock=clock)
    q = c.ask("USD/MXN", "buy", 25_000).quote()
    assert (q.rfq_id, q.quote_id, q.rate, q.side, q.pair) == (RFQ, QID, Decimal("18.700000"), "buy", "USD/MXN")
    assert (q.rfq["leg_id"], q.rfq["quote_expiry_max"]) == (venue.leg, venue.qe)
    assert len(trade_polls(session)) == 2  # no winner named, then the winner
    assert accepts(session) == []  # nothing accepted
    t = c.trade(q)
    assert (t.status, t.tx) == ("open", TXH) and sent_nothing(session)


def test_ask_quote_is_the_quote_that_quote_returns(make_client, venue, session, clock):
    c, t0 = make_client(clock=clock), clock()
    session.routes[("GET", f"/rfqs/{RFQ}")] = [{"quote": None, "quotes": []}, venue.view]
    asked = c.ask("USD/MXN", "buy", 25_000, expiry=EXPIRY_MS).quote()
    venue.reset()
    venue.waited = False
    session.routes[("GET", f"/rfqs/{RFQ}")] = [{"quote": None, "quotes": []}, venue.view]
    clock.t = t0
    one_call = c.quote("USD/MXN", "buy", 25_000, expiry=EXPIRY_MS)  # an open answer without `quotes`: it polls
    assert asked == one_call and asked.rfq == one_call.rfq


def test_ask_quote_without_a_quote_is_no_quotes(make_client, venue, session, clock):
    session.routes[("GET", f"/rfqs/{RFQ}")] = {"quote": None, "quotes": []}
    a = make_client(clock=clock).ask("USD/MXN", "buy", 25_000)
    start = clock()
    with pytest.raises(crx.NoQuotes) as ei:
        a.quote(wait=5)
    assert ei.value.details == {"rfq_id": RFQ} and clock() - start == pytest.approx(5)


def test_ask_quote_without_a_pick_takes_the_best_live_quote_at_the_end(make_client, venue, session, clock):
    session.routes[("GET", f"/rfqs/{RFQ}")] = lambda req: {"quote": None, "quotes": [venue.row()]}
    a = make_client(clock=clock).ask("USD/MXN", "buy", 25_000)
    start = clock()
    assert a.quote(wait=3).quote_id == QID and clock() - start == pytest.approx(3)


def test_ask_gateway_refusal_raises_at_the_ask(make_client, session, markets):
    session.routes[("GET", "/markets")] = open_market(markets, "USD/MXN")
    session.routes[("POST", "/rfqs")] = (400, {"code": "pool_min_notional", "error": "pool min 25000"})
    with pytest.raises(crx.BelowMin, match="pool min 25000"):
        make_client().ask("USD/MXN", "buy", 10_000)


@pytest.mark.parametrize("answer, err", [
    ((429, {"code": "rate_limited", "error": "slow down"}), crx.RateLimited),
    ((401, {"code": "unauthorized", "error": "not entitled"}), crx.AuthError),
    ((503, {"code": "upstream", "error": "down"}), crx.ServerError),
])
def test_ask_quote_gateway_refusal_raises_its_class(make_client, venue, session, clock, answer, err):
    a = make_client(clock=clock).ask("USD/MXN", "buy", 25_000)
    session.routes[("GET", f"/rfqs/{RFQ}")] = answer
    with pytest.raises(err):
        a.quote()


def naive_expiry():
    return datetime(2027, 1, 4)


@pytest.mark.parametrize("args, kw, err", [
    (("USD/XX", "buy", 25_000), {}, crx.BadRequest),
    (("USD/MXN", "long", 25_000), {}, crx.BadRequest),
    (("USD/MXN", "buy", -1), {}, crx.BadRequest),
    (("USD/MXN", "buy", "1.0000001"), {}, crx.BadRequest),
    (("USD/MXN", "buy", 5_000), {}, crx.BelowMin),
    (("USD/JPY", "buy", 25_000), {}, crx.MarketPaused),
    (("USD/MXN", "buy", 25_000), {"client_rfq_id": ""}, crx.BadRequest),
    (("USD/MXN", "buy", 25_000), {"premium_bps": 10_001}, crx.BadRequest),
    (("USD/MXN", "buy", 25_000), {"expiry": naive_expiry()}, crx.BadRequest),
    (("USD/MXN", "buy", 25_000), {"expiry": "tomorrow"}, crx.BadRequest),
])
def test_ask_makes_the_checks_of_quote(make_client, session, markets, args, kw, err):
    session.routes[("GET", "/markets")] = open_market(markets, "USD/MXN")
    c = make_client()
    said = []
    for call in (c.quote, c.ask):
        with pytest.raises(err) as ei:
            call(*args, **kw)
        said.append((type(ei.value), str(ei.value), ei.value.details))
    assert said[0] == said[1] and said[0][0] is err
    assert session.paths("POST") == []


@pytest.mark.parametrize("call", ["quote", "ask"])
def test_no_im_bps_argument(make_client, session, markets, call):
    session.routes[("GET", "/markets")] = open_market(markets, "USD/MXN")
    with pytest.raises(TypeError):
        getattr(make_client(), call)("USD/MXN", "buy", 25_000, im_bps=100)
    assert session.calls == []


def test_ask_needs_the_seat_key(make_client, session, monkeypatch):
    for name in ("CRX_WALLET_PK", "CRX_WALLET_PK_FILE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(crx.ConfigError):
        make_client(key=False).ask("USD/MXN", "buy", 25_000)
    assert session.calls == []


# ---------- the request's own checks ----------


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


@pytest.mark.parametrize("args", [("USD/XX", "buy", 25_000), ("USD/MXN", "long", 25_000),
                                  ("USD/MXN", "buy", -1), ("USD/MXN", "buy", "1.0000001")])
def test_bad_inputs(make_client, session, markets, args):
    session.routes[("GET", "/markets")] = open_market(markets, "USD/MXN")
    with pytest.raises(crx.BadRequest):
        make_client().quote(*args)
    assert session.paths("POST") == []


def test_min_notional_comes_from_markets(make_client, venue, session, markets, clock):
    m = open_market(markets, "USD/MXN")
    for row in m["markets"]:
        row["notional"]["min"] = "5000"
    session.routes[("GET", "/markets")] = m
    c = make_client(clock=clock)
    with pytest.raises(crx.BelowMin) as ei:
        c.quote("USD/MXN", "buy", "4999.999999")
    assert ei.value.details == {"min": "5000"} and session.paths("POST") == []
    c.quote("USD/MXN", "buy", 5_000)
    assert session.paths("POST") == ["/rfqs"] and venue.rfq_body["notional"] == "5000"


@pytest.mark.parametrize("cid", ["", "x" * 129, "a\nb", "tab\there", "café", "del\x7f", 123, b"id"])
def test_client_rfq_id_refused_before_any_call(make_client, session, cid):
    with pytest.raises(crx.BadRequest) as ei:
        make_client().quote("USD/MXN", "buy", 25_000, client_rfq_id=cid)
    assert str(ei.value) == "client_rfq_id must be 1 to 128 printable characters"
    assert ei.value.details == {"field": "client_rfq_id", "max": 128}
    assert session.calls == []


@pytest.mark.parametrize("cid", ["x" * 128, " ~!desk 42~ ", "A"])
def test_client_rfq_id_printable_ascii_is_sent(make_client, venue, clock, cid):
    make_client(clock=clock).quote("USD/MXN", "buy", 25_000, client_rfq_id=cid)
    assert venue.rfq_body["client_rfq_id"] == cid


def test_default_client_rfq_id_fits(make_client, venue, clock):
    make_client(clock=clock).quote("USD/MXN", "buy", 25_000)
    cid = venue.rfq_body["client_rfq_id"]
    assert 1 <= len(cid) <= 128 and all(0x20 <= ord(ch) <= 0x7E for ch in cid)


def test_naive_expiry_refused(make_client, session, markets):
    session.routes[("GET", "/markets")] = open_market(markets, "USD/MXN")
    with pytest.raises(crx.BadRequest, match="timezone"):
        make_client().quote("USD/MXN", "buy", 25_000, expiry=datetime(2027, 1, 4))


def test_domain_moved_refuses(make_client, session, health):
    h = {**health, "chains": [dict(c, domain="0x" + "00" * 32) for c in health["chains"]]}
    session.routes[("GET", "/health")] = h
    with pytest.raises(crx.RefusedToSign, match="domain"):
        make_client().deposit(1000)


def test_domain_moved_refuses_the_trade(make_client, venue, session, health, clock, account):
    session.routes[("GET", "/health")] = {**health, "chains": [dict(c, domain="0x" + "00" * 32)
                                                               for c in health["chains"]]}
    c = make_client(clock=clock)
    with pytest.raises(crx.RefusedToSign, match="^domain moved: /health does not match the core; nothing signed$"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert accepts(session) == []


def test_rpc_on_wrong_chain_refused(make_client, session):
    session.rpc["eth_chainId"] = "0x1"
    with pytest.raises(crx.ConfigError, match="RPC"):
        make_client().deposit(1000)
    assert session.paths("POST") == []


@pytest.mark.parametrize("now, at", [
    ("2026-10-01T08:00:00", "2026-10-01T08:05:00"),
    ("2026-10-01T08:04:59", "2026-10-01T08:05:00"),
    ("2026-10-01T08:05:00", "2026-10-01T09:05:00"),
    ("2026-10-01T23:58:30", "2026-10-02T00:05:00"),
])
def test_next_check_is_the_next_05_past_the_hour_on_testnet(make_client, session, now, at):
    t = datetime.fromisoformat(now).replace(tzinfo=timezone.utc).timestamp()
    got = make_client(key=False, clock=Clock(t)).next_check()
    assert got == datetime.fromisoformat(at).replace(tzinfo=timezone.utc) and got.tzinfo is not None
    assert session.calls == []


# ---------- this seat's own Trade: the terms it builds from its ask ----------


def own_check(tmp_path, account, health, clock, **edit):
    """Binder.check_trade on a template that agrees with the ask, the ask then edited by ``edit``."""
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
    return b.check_trade(t, ask), msg, sep


def test_own_trade_check_passes_and_keeps_the_nonce(tmp_path, account, health, clock):
    # The positive control of the refusals below.
    (digest, td), msg, sep = own_check(tmp_path, account, health, clock)
    assert digest == e7.trade_digest(sep, msg) == typed_hash(td) and td["message"] == msg
    assert (tmp_path / f"side-nonce-{account.address.lower()}").read_text() == msg["ownNonce"]


@pytest.mark.parametrize("edit,why", [
    ({"pair": "usd/mxn"}, "pair is not AAA/BBB"),
    ({"pair": "USDMXN"}, "pair is not AAA/BBB"),
    ({"side": 2}, "side is not buy or sell"),
    ({"side": True}, "side is not buy or sell"),
    ({"notional": "340282366920938463463374607431768.211456"}, "notional is not a 6-decimal amount below 2\\^128"),
    ({"notional": "1.0000001"}, "notional is not a 6-decimal amount below 2\\^128"),
    ({"rate": Decimal("18446744073709.551616")}, "rate is not a 6-decimal amount below 2\\^64"),
    ({"expiry": -1}, "expiry is not unix ms"),
    ({"expiry": "1900000000000"}, "expiry is not unix ms"),
    ({"expiry": 253402300800_000}, "maturity is out of range"),
])
def test_own_trade_refusals(tmp_path, account, health, clock, edit, why):
    with pytest.raises(crx.RefusedToSign, match=why):
        own_check(tmp_path, account, health, clock, **edit)
    assert not (tmp_path / f"side-nonce-{account.address.lower()}").exists()  # no nonce kept, nothing signed


# ---------- a custodian signer: typed data and EIP-191; one login per 8 h ----------


def mangle(sig: bytes, form: str) -> bytes:
    """A signature as an MPC custodian may return it: ``high_s`` (s' = n - s, v flipped), ``v01``, ``v29``."""
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
    """Signs the typed data it is handed, and EIP-191 messages. Keeps what it was asked to sign."""

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


class HashCustodian:
    """A KMS or raw-MPC signer: signs a 32-byte digest only."""

    def __init__(self, account):
        self.a, self.address, self.hashes, self.messages = account, account.address, [], []

    def sign_hash(self, digest):
        self.hashes.append(digest)
        return self.a.unsafe_sign_hash(digest).signature

    def sign_message(self, message):
        self.messages.append(message)
        return self.a.sign_message(encode_defunct(primitive=message)).signature


class SessionGate:
    """The gateway's session bridge: POST /session mints a token from a signed envelope; a token
    stands in for the envelope. ``on`` False: the bridge is off (POST /session answers 404)."""

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


def low_s(sig: str) -> bool:
    return int(sig[66:130], 16) <= e7.SECP256K1_N // 2 and sig[-2:] in ("1b", "1c")


@pytest.mark.parametrize("form", ["plain", "high_s", "v01"])
def test_custodian_signs_own_typed_data(venue, session, clock, account, tmp_path, form):
    gate = SessionGate(session, account.address.lower())
    s = Custodian(account, form)
    c = custodian_client(session, tmp_path, s, clock)
    assert c.trade(c.quote("USD/MXN", "buy", 25_000)).status == "open"
    t = venue.template
    assert [td["primaryType"] for td in s.typed] == ["Trade"]
    assert s.typed[0] == t["typed_data"]  # its own object, equal by value to the served one
    sig = venue.sig
    assert low_s(sig) and recovers(t, sig) == account.address  # normalized before it was sent
    assert len(s.messages) == gate.mints == 1  # one login signature for the whole trade
    assert gate.envelopes() == [("POST", BASE + "/session")]
    assert all(h.get("x-crx-session") for m, u, h in gate.sent if u.split("?")[0] not in (
        BASE + "/health", BASE + "/markets", BASE + "/session"))


def test_custodian_is_handed_its_own_object(venue, session, clock, account, tmp_path):
    # A served member in another spelling: the custodian signs the SDK's own spelling.
    venue.typed_edit = lambda td: td["message"].update(notionalE6=int(td["message"]["notionalE6"]))
    SessionGate(session, account.address.lower())
    s = Custodian(account)
    c = custodian_client(session, tmp_path, s, clock)
    assert c.trade(c.quote("USD/MXN", "buy", 25_000)).status == "open"
    (own,) = s.typed
    assert own["message"]["notionalE6"] == "25000000000" and own != venue.template["typed_data"]
    assert typed_hash(own) == typed_hash(venue.template["typed_data"])


def test_hash_only_signer_signs_the_rebuilt_digest(venue, session, clock, account, tmp_path):
    SessionGate(session, account.address.lower())
    s = HashCustodian(account)
    c = custodian_client(session, tmp_path, s, clock)
    assert c.trade(c.quote("USD/MXN", "buy", 25_000)).status == "open"
    (digest,) = s.hashes
    assert bytes(digest) == typed_hash(venue.template["typed_data"])
    assert recovers(venue.template, venue.sig) == account.address


@pytest.mark.parametrize("form,why", [("v29", "no usable signature"), ("other_key", "does not recover to the seat")])
def test_custodian_bad_signature_is_not_sent(venue, session, clock, account, tmp_path, form, why):
    SessionGate(session, account.address.lower())
    s = Custodian(account, "plain", key=Account.create()) if form == "other_key" else Custodian(account, form)
    if form == "v29":
        s.sign_message = lambda m: account.sign_message(encode_defunct(primitive=m)).signature  # login works
    c = custodian_client(session, tmp_path, s, clock)
    with pytest.raises(crx.RefusedToSign, match=why):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert signed_posts(session) == []


def test_custodian_withdraw_signs_typed_intent(session, health, account, tmp_path):
    from .test_money import Chain, balance_body, gate
    Chain(session)
    g = gate(session, health, account)
    session.routes[("GET", "/balance")] = [balance_body(account), g.view("accepted")]
    SessionGate(session, account.address.lower())
    s = Custodian(account, "high_s")
    out = custodian_client(session, tmp_path, s, Clock(time.time())).withdraw(1000)
    assert out.status == "accepted" and g.signer == account.address
    (td,) = s.typed
    assert td["primaryType"] == "WithdrawIntent" and td["message"]["amount"] == "1000000000"
    assert set(td["message"]) == {"account", "amount", "nonce", "deadline"}
    assert td["domain"] == e7.domain_json(CHAIN_ID, next(c for c in health["chains"] if c["key"] == "avax-fuji")["core"])


@pytest.mark.parametrize("call", ["deposit", "send_quote", "confirm"])
def test_custodian_calls_that_need_a_local_key(session, account, tmp_path, call):
    c = custodian_client(session, tmp_path, Custodian(account))
    with pytest.raises(crx.ConfigError, match="needs a local key"):
        getattr(c, call)(*{"deposit": (1000,), "send_quote": (None, 1), "confirm": (None,)}[call])
    assert session.calls == []


def test_signer_and_key_are_exclusive(session, account, tmp_path):
    with pytest.raises(crx.ConfigError, match="one only"):
        crx.Client(key=account.key.hex(), signer=Custodian(account), base_url=BASE, rpc_url=RPC, session=session)


def balance_route(session, account):
    from .test_money import balance_body
    session.routes[("GET", "/balance")] = lambda req: balance_body(account)


def test_one_login_per_8_hours(session, account, tmp_path):
    balance_route(session, account)
    gate = SessionGate(session, account.address.lower())
    s = Custodian(account)
    clock = Clock(time.time())
    c = custodian_client(session, tmp_path, s, clock)
    for _ in range(3):
        c.balance()
    assert (gate.mints, len(s.messages), len(gate.tokened())) == (1, 1, 3)
    clock.t += 8 * 3600 - 61  # inside the token's life, less the margin
    c.balance()
    assert gate.mints == 1
    clock.t += 2
    c.balance()
    assert (gate.mints, len(s.messages)) == (2, 2)


def test_gateway_restart_mints_once_more(session, account, tmp_path):
    balance_route(session, account)
    gate = SessionGate(session, account.address.lower())
    s = Custodian(account)
    c = custodian_client(session, tmp_path, s, Clock(time.time()))
    c.balance()
    gate.restart()  # every token forgotten: the next call answers 401, then mints and goes again
    c.balance()
    assert (gate.mints, len(s.messages)) == (2, 2)
    assert [u.split("?")[0] for m, u, h in gate.sent if m in ("GET", "POST")] == [
        BASE + "/session", BASE + "/balance", BASE + "/balance", BASE + "/session", BASE + "/balance"]


def test_token_refused_twice_is_auth_error(session, account, tmp_path):
    balance_route(session, account)
    gate = SessionGate(session, account.address.lower())
    gate.tokens = type("Never", (set,), {"__contains__": lambda self, k: False})()
    c = custodian_client(session, tmp_path, Custodian(account), Clock(time.time()))
    with pytest.raises(crx.AuthError):
        c.balance()
    assert gate.mints == 2  # one mint more, then the 401 stands


def test_sessions_off_signs_every_call(session, account, tmp_path):
    balance_route(session, account)
    gate = SessionGate(session, account.address.lower(), on=False)
    s = Custodian(account)
    c = custodian_client(session, tmp_path, s, Clock(time.time()))
    for _ in range(3):
        c.balance()
    assert gate.tokened() == [] and len(gate.envelopes()) == 4  # one mint asked once, then three signed reads
    assert session.paths("POST").count("/session") == 1 and len(s.messages) == 4


def test_viewer_grants_and_viewer_mode_use_the_envelope(session, account, tmp_path):
    other = Account.create().address.lower()
    session.routes[("PUT", f"/viewers/{other}")] = {"viewer": other, "granted_by": account.address.lower()}
    gate = SessionGate(session, account.address.lower())
    c = custodian_client(session, tmp_path, Custodian(account), Clock(time.time()))
    c.add_viewer(other)
    assert gate.mints == 0 and gate.envelopes() == [("PUT", BASE + f"/viewers/{other}")]
    owner = Account.create()
    balance_route(session, owner)
    v = custodian_client(session, tmp_path, Custodian(account), Clock(time.time()), account=owner.address)
    v.balance()
    assert gate.mints == 0 and gate.tokened() == []


def test_local_key_never_mints(make_client, venue, session, clock, account):
    gate = SessionGate(session, account.address.lower())
    c = make_client(clock=clock)
    assert c.trade(c.quote("USD/MXN", "buy", 25_000)).status == "open"
    assert gate.mints == 0 and gate.tokened() == [] and "/session" not in session.paths()
