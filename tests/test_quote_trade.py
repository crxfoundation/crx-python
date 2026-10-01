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
    """One RFQ round: the gateway's answers, built from what the client sent.

    ``mode`` "one_call": the winner row carries the taker's Side template and
    ``POST /accept {quote_id, sig}`` opens the round. "legacy": the gateway takes
    the leg body only, then ``POST /side``.
    """

    def __init__(self, session, health, markets, clock, pair="USD/MXN", mode="legacy"):
        self.s, self.clock, self.mode = session, clock, mode
        self.chain = next(c for c in health["chains"] if c["key"] == "avax-fuji")
        self.sep = e7.domain_separator(CHAIN_ID, self.chain["core"])
        self.rfq_body = None
        self.accept_body = None
        self.template = None
        self.side_sig = None
        self.row_edit = {}
        self.template_edit = {}
        self.winner = True      # the gateway named its pick: GET /rfqs/{id} carries `quote`
        self.waited = False     # POST /rfqs answers after the window: open fields + taker view + accept_by
        self.waited_view = None  # callable: the view a waited answer carries; None: view()
        self.draft = None       # one_call: the taker's Side template for the winner
        self.accepted = False
        self.counter = 0        # the gateway's Side nonce counter for this seat
        self.nonces = []        # one-shot nonces the next picks take, in order
        self.stale = 0          # the next N accept posts answer 409 side_stale with a fresh draft
        self.on_stale = None    # called before each fresh draft that `stale` forces
        self.stale_quote_id = None  # the quote_id a side_stale answer names; None: the one posted; "": none
        self.served_digest = None   # the digest the template names; None: the Side's own
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
        opened = {"rfq_id": RFQ, "leg_id": LEG, "quote_expiry": self.qe_ms, "im_bps": req["body"]["im_bps"],
                  "side": 1 if req["body"]["side"] == "buy" else -1, "expiry": req["body"]["expiry"],
                  "status": "open", "kind": "open", "join_ref": None}
        if not self.waited:
            return opened
        v = self.view(req) if self.waited_view is None else self.waited_view()
        q = v.get("quote")
        return {**opened, **v, "accept_by": q["expires_at"] if isinstance(q, dict) else None}

    def row(self):
        b = self.rfq_body
        r = {"quote_id": QID, "rfq_id": RFQ, "rate": "18.700000", "expires_at": int(self.clock() * 1000) + 60_000,
             "leg_id": LEG, "join_ref": JOIN, "side": 1 if b["side"] == "buy" else -1,
             "pair_id": e7.h0x(e7.pair_id(b["pair"][:3] + "/" + b["pair"][3:])), "instrument_id": 1,
             "notional": f"{Decimal(b['notional']):.6f}", "premium_bps": 0, "expiry": b["expiry"],
             "status": "quoted", "house": False}
        r.update(self.row_edit)
        return r

    def taker_arm(self, seat):
        """The taker half the gateway builds: its own seat and credential, the quote's joined terms."""
        q = self.row()
        return {"seat": seat, "leg_id": LEG, "join_ref": JOIN, "pair_id": q["pair_id"], "instrument_id": 1,
                "side": q["side"], "notional": q["notional"], "rate": q["rate"], "im_bps": self.rfq_body["im_bps"],
                "premium_bps": q["premium_bps"], "expiry": q["expiry"]}

    def pick(self):
        n = self.nonces.pop(0) if self.nonces else max(self.counter + 1, int(self.clock() * 1000))
        self.counter = max(self.counter, n)
        return n

    def make_template(self, arm):
        own_nonce = int(self.template_edit.get("own_nonce", self.pick()))
        qe = int(self.template_edit.get("quote_expiry", int(self.clock()) + 300))
        salt, c_maker = rnd(), keccak(os.urandom(32))
        c_taker = e7.half_commitment(e7.arm_words(arm, own_nonce, qe), salt)
        t = {"digest_kind": "side", "own_leg_id": arm["leg_id"], "own_nonce": str(own_nonce), "quote_expiry": qe,
             "own_salt": salt, "c_taker": e7.h0x(c_taker), "c_maker": e7.h0x(c_maker),
             "pair_c": e7.h0x(e7.pair_commitment(c_taker, c_maker)), "wraps_hash": WRAPS_HASH,
             "domain_separator": e7.h0x(self.sep)}
        t.update(self.template_edit)
        t["digest"] = self.served_digest or e7.h0x(e7.side_digest(self.sep, t))
        return t

    def view(self, req):
        q = self.row()
        if self.mode == "one_call" and self.winner and not self.accepted:
            if self.draft is None:
                self.draft = self.make_template(self.taker_arm(req["headers"]["x-crx-address"]))
            q["side_template"] = self.draft
        v = {"quote": q if self.winner else None, "quotes": [self.row()]}
        if self.side_sig is not None:
            word, tx = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            v.update(trade_status=word, trade_tx=tx)
        return v

    def accept(self, req):
        body = req["body"]
        if "leg" in body and "sig" in body:
            return self.accept_leg(body)
        if self.mode == "legacy":
            return (400, {"code": "bad_request", "error": "sig is required"})
        seat = req["headers"]["x-crx-address"]
        if self.draft is None or self.stale > 0 or body["quote_id"] != self.row()["quote_id"]:
            self.stale = max(self.stale - 1, 0)
            if self.on_stale:
                self.on_stale()
            self.draft = self.make_template(self.taker_arm(seat))
            return self.stale_answer(body)
        if "sig" not in body:
            return self.stale_answer(body)
        signer = Account._recover_hash(e7.hx(self.draft["digest"]), signature=body["sig"]).lower()
        if signer != seat:
            return (400, {"code": "invalid_signature", "error": f"sign the digest {self.draft['digest']}"})
        self.side_sig, self.template, self.accepted = body["sig"], self.draft, True
        return {"rfq_id": RFQ, "quote_id": body["quote_id"], "leg_id": LEG, "status": "accepted",
                "leg_hash": e7.h0x(e7.leg_digest(self.sep, dict(self.taker_arm(seat), nonce=self.draft["own_nonce"],
                                                                  quote_expiry=self.draft["quote_expiry"]))),
                "side": dict(self.draft, signed=True)}

    def stale_answer(self, body):
        named = body["quote_id"] if self.stale_quote_id is None else self.stale_quote_id
        details = {"side_template": self.draft} | ({"quote_id": named} if named else {})
        return (409, {"code": "side_stale", "error": "the Side template moved; sign this one", "details": details})

    def accept_leg(self, body):
        self.accept_body = leg = body["leg"]
        t = self.make_template(leg)
        self.template = t
        return {"status": "accepted", "leg_hash": e7.h0x(e7.leg_digest(self.sep, leg)), "side": t}

    def side(self, req):
        self.side_sig = req["body"]["sig"]
        return {"status": "signed"}

    def reset(self):
        """A new round on the same RFQ id: nothing accepted, no draft, no initial empty view."""
        self.side_sig, self.draft, self.accepted = None, None, False
        self.s.routes[("GET", f"/rfqs/{RFQ}")] = self.view


def accepts(session):
    return [x["body"] for x in session.calls if x["method"] == "POST" and x["path"] == f"/rfqs/{RFQ}/accept"]


@pytest.fixture
def clock():
    return Clock(time.time())


@pytest.fixture(params=["one_call", "legacy"])
def venue(request, session, health, markets, clock):
    return Venue(session, health, markets, clock, mode=request.param)


legacy_only = pytest.mark.parametrize("venue", ["legacy"], indirect=True)
one_call_only = pytest.mark.parametrize("venue", ["one_call"], indirect=True)


@legacy_only
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
    body = next(b for b in accepts(session) if "leg" in b)
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


@legacy_only
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


@legacy_only
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


@legacy_only
def test_accept_without_side_template_sends_nothing(make_client, venue, session, clock):
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (
        lambda req: {"status": "accepted"} if "leg" in req["body"] else venue.accept(req))
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
    venue.reset()
    venue.template_edit = {"own_nonce": str(floor)}
    q = c.quote("USD/MXN", "buy", 25_000)
    before, sides = len(accepts(session)), session.paths("POST").count(f"/rfqs/{RFQ}/side")
    with pytest.raises(crx.RefusedToSign, match="own_nonce"):
        c.trade(q)
    if venue.mode == "one_call":
        # Asked for a newer template while a signed post still fits; the gateway kept the old nonce; nothing signed.
        assert accepts(session)[before:] == [{"quote_id": QID}] * 2
    assert session.paths("POST").count(f"/rfqs/{RFQ}/side") == sides


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
    bodies = accepts(session)
    assert len(bodies) > 1 and all(b == bodies[0] for b in bodies)  # the same body, posted again


def test_own_round_open(make_client, venue, session, clock):
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (
        409, {"code": "own_round_open", "error": "open", "details": {"rfq_id": RFQ, "until": 123}})
    c = make_client(clock=clock)
    with pytest.raises(crx.OwnRoundOpen) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert ei.value.details["until"] == 123


# ---------- one call: the Side signature rides on the accept ----------


def recovers(venue, t, sig):
    return Account._recover_hash(e7.side_digest(venue.sep, t), signature=sig)


@one_call_only
def test_one_call_trade(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert q.raw["side_template"] == venue.draft
    t = c.trade(q)
    assert (t.status, t.tx) == ("open", TXH) and t.raw["status"] == "accepted"
    (body,) = accepts(session)
    assert set(body) == {"quote_id", "sig"} and body["quote_id"] == QID
    assert recovers(venue, venue.template, body["sig"]) == account.address
    assert f"/rfqs/{RFQ}/side" not in session.paths("POST")
    assert sent_nothing(session)
    floor = (c._state_dir / f"side-nonce-{account.address.lower()}").read_text()
    assert floor == venue.template["own_nonce"]


@one_call_only
def test_side_stale_signs_the_fresh_template(make_client, venue, session, clock, account):
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    first = q.raw["side_template"]
    venue.stale = 1
    assert c.trade(q).status == "open"
    a, b = accepts(session)
    assert set(a) == set(b) == {"quote_id", "sig"} and a["sig"] != b["sig"]
    assert recovers(venue, first, a["sig"]) == account.address
    assert recovers(venue, venue.template, b["sig"]) == account.address
    assert int(venue.template["own_nonce"]) > int(first["own_nonce"])
    assert (c._state_dir / f"side-nonce-{account.address.lower()}").read_text() == venue.template["own_nonce"]
    assert f"/rfqs/{RFQ}/side" not in session.paths("POST")


@one_call_only
@pytest.mark.parametrize("late,posts", [(False, 3), (True, 1)])
def test_side_stale_three_posts_at_most(make_client, venue, session, clock, late, posts):
    # Stale on every post: 3 signed posts, then a new quote. Past the quote's life: no second post.
    venue.stale = 99
    if late:
        venue.on_stale = lambda: clock.sleep(61)  # the quote row lives 60 s
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    with pytest.raises(crx.QuoteExpired) as ei:
        c.trade(q)
    assert ei.value.gateway_code == "side_stale"
    bodies = accepts(session)
    assert len(bodies) == posts and all(set(b) == {"quote_id", "sig"} for b in bodies)
    assert len({b["sig"] for b in bodies}) == posts
    assert f"/rfqs/{RFQ}/side" not in session.paths("POST") and sent_nothing(session)


@legacy_only
def test_leg_body_gateway_falls_back(make_client, venue, session, clock, account):
    # No side_template on the row: {quote_id} asks for one; 400 bad_request means the leg body, then /side.
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert "side_template" not in q.raw
    assert c.trade(q).status == "open"
    probe, legacy = accepts(session)
    assert probe == {"quote_id": QID}
    assert set(legacy) == {"quote_id", "leg", "sig"}
    assert Account._recover_hash(e7.leg_digest(venue.sep, legacy["leg"]), signature=legacy["sig"]) == account.address
    assert session.paths("POST").count(f"/rfqs/{RFQ}/side") == 1
    assert recovers(venue, venue.template, venue.side_sig) == account.address


@pytest.mark.parametrize("bad", ["pair_c", "digest", None])  # None: the positive control, same path
def test_template_pair_c_and_digest_must_rebuild(make_client, venue, session, clock, account, bad):
    # pair_c: c_taker rebuilds, pair_c is not keccak(0x03, c_taker, c_maker); the served digest is over it.
    # digest: every word rebuilds, the served digest is not the Side's.
    if bad == "pair_c":
        venue.template_edit = {"pair_c": rnd()}
    elif bad == "digest":
        venue.served_digest = rnd()
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    if bad:
        with pytest.raises(crx.RefusedToSign, match="pair_c" if bad == "pair_c" else "digest"):
            c.trade(q)
        assert not any("sig" in b and "leg" not in b for b in accepts(session))
        assert f"/rfqs/{RFQ}/side" not in session.paths("POST")
    else:
        assert c.trade(q).status == "open"
        assert recovers(venue, venue.template, venue.side_sig) == account.address
    assert sent_nothing(session)


@one_call_only
@pytest.mark.parametrize("names,signed_first,err", [
    ("other", False, crx.RefusedToSign),   # the template asked for names another quote: nothing signed
    ("other", True, crx.TradeUnknown),     # after a signed post: never "nothing happened"
    ("missing", False, crx.RefusedToSign),
    (None, False, None),                   # positive control: the quote posted
    (None, True, None),
])
def test_side_stale_names_the_quote(make_client, venue, session, clock, account, names, signed_first, err):
    venue.stale_quote_id = {"other": "0x" + "66" * 32, "missing": "", None: None}[names]
    if signed_first:
        venue.stale = 1
    else:
        venue.winner = False
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, wait=3)
    if err is None:
        assert c.trade(q).status == "open"
        assert recovers(venue, venue.template, accepts(session)[-1]["sig"]) == account.address
    else:
        with pytest.raises(err, match="another quote"):
            c.trade(q)
        assert len(accepts(session)) == 1
        assert ("sig" in accepts(session)[0]) is signed_first
    assert f"/rfqs/{RFQ}/side" not in session.paths("POST")


@one_call_only
def test_quote_not_the_winner_asks_for_its_template(make_client, venue, session, clock, account):
    venue.winner = False
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, wait=3)
    assert q.quote_id == QID and "side_template" not in q.raw
    assert c.trade(q).status == "open"
    probe, signed = accepts(session)
    assert probe == {"quote_id": QID} and set(signed) == {"quote_id", "sig"}
    assert recovers(venue, venue.template, signed["sig"]) == account.address
    assert f"/rfqs/{RFQ}/side" not in session.paths("POST")


@one_call_only
def test_template_below_the_floor_is_asked_again(make_client, venue, session, clock, account):
    # A template made before this seat's last signature: ask for a newer one, sign that.
    c = make_client(clock=clock)
    c.trade(c.quote("USD/MXN", "buy", 25_000))
    floor = int(venue.template["own_nonce"])
    venue.reset()
    venue.nonces, venue.stale = [floor], 1
    q = c.quote("USD/MXN", "buy", 25_000)
    assert int(q.raw["side_template"]["own_nonce"]) == floor
    before = len(accepts(session))
    assert c.trade(q).status == "open"
    ask, signed = accepts(session)[before:]
    assert ask == {"quote_id": QID} and set(signed) == {"quote_id", "sig"}
    assert int(venue.template["own_nonce"]) > floor
    assert recovers(venue, venue.template, signed["sig"]) == account.address


@one_call_only
@pytest.mark.parametrize("bad", [True, False])  # False: the positive control, same path
def test_template_c_taker_must_rebuild(make_client, venue, session, clock, account, bad):
    if bad:
        venue.template_edit = {"c_taker": rnd()}
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    state = c._state_dir / f"side-nonce-{account.address.lower()}"
    if bad:
        with pytest.raises(crx.RefusedToSign, match="c_taker"):
            c.trade(q)
        assert accepts(session) == [] and not state.exists()
    else:
        assert c.trade(q).status == "open"
        (body,) = accepts(session)
        assert recovers(venue, venue.template, body["sig"]) == account.address
    assert f"/rfqs/{RFQ}/side" not in session.paths("POST") and sent_nothing(session)


@one_call_only
@pytest.mark.parametrize("answer,err", [
    ((409, "round_closed"), crx.QuoteExpired),
    ((409, "own_round_open"), crx.OwnRoundOpen),
    ((422, "insufficient_collateral"), crx.TradeUnknown),
    ((400, "invalid_signature"), crx.TradeUnknown),
    ((409, "side_stale"), crx.TradeUnknown),  # its fresh template cannot be read
])
def test_refusal_after_the_side_is_signed(make_client, venue, session, clock, answer, err):
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = (answer[0], {"code": answer[1], "error": answer[1],
                                                                   "details": {"side_template": "?"}})
    c = make_client(clock=clock)
    with pytest.raises(err) as ei:
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    if err is crx.TradeUnknown:
        assert "may still open" in str(ei.value) and ei.value.details == {"rfq_id": RFQ}
    assert len(accepts(session)) == 1 and sent_nothing(session)


@one_call_only
def test_stale_template_that_fails_the_check_after_a_signature(make_client, venue, session, clock):
    # The first Side is signed and posted; the fresh one does not rebuild: never "nothing happened".
    venue.stale = 1
    venue.on_stale = lambda: venue.template_edit.update(c_taker=rnd())
    c = make_client(clock=clock)
    with pytest.raises(crx.TradeUnknown, match="c_taker"):
        c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert len(accepts(session)) == 1


@one_call_only
def test_signed_accept_no_answer_reads_status(make_client, venue, session, clock):
    # A 5xx on the signed accept: the gateway may hold the Side, so the status decides.
    def accept(req):
        venue.accept(req)
        return (503, {"code": "upstream", "error": "down"})
    session.routes[("POST", f"/rfqs/{RFQ}/accept")] = accept
    c = make_client(clock=clock)
    t = c.trade(c.quote("USD/MXN", "buy", 25_000))
    assert (t.status, t.raw) == ("open", {}) and len(accepts(session)) == 1


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


def test_closed_market_no_maker_is_no_quotes(make_client, venue, session, markets, clock):
    session.routes[("GET", "/markets")] = markets
    session.routes[("GET", f"/rfqs/{RFQ}")] = {"quote": None, "quotes": []}
    c = make_client(clock=clock)
    with pytest.raises(crx.NoQuotes):
        c.quote("USD/MXN", "buy", 25_000, wait=3)


def rfq_posts(session):
    return [x for x in session.calls if x["method"] == "POST" and x["path"] == "/rfqs"]


def test_quote_sends_wait_with_a_30_s_timeout(make_client, venue, session, clock):
    make_client(clock=clock).quote("USD/MXN", "buy", 25_000)
    [post] = rfq_posts(session)
    assert post["body"]["wait"] is True and post["timeout"] == 30
    assert {x["timeout"] for x in session.calls if x is not post} == {10}


def test_quote_timeout_is_never_below_the_client_timeout(make_client, venue, session, clock):
    make_client(clock=clock, timeout=45).quote("USD/MXN", "buy", 25_000)
    assert rfq_posts(session)[0]["timeout"] == 45


def test_waited_answer_carries_the_winner(make_client, venue, session, clock):
    venue.waited = True
    session.routes[("GET", f"/rfqs/{RFQ}")] = venue.view
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert (q.rfq_id, q.quote_id, q.rate) == (RFQ, QID, Decimal("18.700000"))
    assert trade_polls(session) == []  # the answer named the winner: no poll
    assert (q.rfq["leg_id"], q.rfq["quote_expiry"]) == (LEG, venue.qe_ms)
    t = c.trade(q)
    assert (t.status, t.tx) == ("open", TXH) and sent_nothing(session)


@one_call_only
def test_waited_answer_template_is_signed_in_one_call(make_client, venue, session, clock, account):
    venue.waited = True
    session.routes[("GET", f"/rfqs/{RFQ}")] = venue.view
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000)
    assert q.raw["side_template"] == venue.draft
    c.trade(q)
    [body] = accepts(session)
    assert set(body) == {"quote_id", "sig"} and f"/rfqs/{RFQ}/side" not in session.paths("POST")
    assert Account._recover_hash(e7.hx(venue.draft["digest"]), signature=body["sig"]) == account.address


@pytest.mark.parametrize("expired", [False, True])
def test_waited_answer_without_a_quote_is_no_quotes(make_client, venue, session, clock, expired):
    venue.waited = True
    venue.waited_view = lambda: {"quote": None, "quotes": [dict(venue.row(), expires_at=1)] if expired else []}
    c = make_client(clock=clock)
    start = clock()
    with pytest.raises(crx.NoQuotes) as ei:
        c.quote("USD/MXN", "buy", 25_000)
    assert ei.value.details == {"rfq_id": RFQ} and trade_polls(session) == [] and clock() == start


def test_waited_answer_without_a_pick_takes_the_best_live_quote(make_client, venue, session, clock):
    # The gateway's wait cap answered before it named a winner.
    venue.waited = True
    venue.waited_view = lambda: {"quote": None, "quotes": [venue.row()]}
    q = make_client(clock=clock).quote("USD/MXN", "buy", 25_000)
    assert q.quote_id == QID and trade_polls(session) == []


def other_row(venue, status):
    """A better-priced row in `status`; None leaves the status out."""
    row = dict(venue.row(), quote_id="0x" + "66" * 32, rate="18.500000", status=status)
    return {k: v for k, v in row.items() if v is not None}


@pytest.mark.parametrize("status", ["dropped", "declined", "live", None])
def test_pick_takes_only_a_quoted_row(make_client, venue, session, clock, status):
    venue.waited = True
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


def test_old_gateway_answer_polls(make_client, venue, session, clock):
    # An open answer without `quotes`: an older gateway answered at once.
    q = make_client(clock=clock).quote("USD/MXN", "buy", 25_000)
    assert q.quote_id == QID and len(trade_polls(session)) == 2


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


@pytest.mark.parametrize("args", [("USD/XX", "buy", 25_000), ("USD/MXN", "long", 25_000),
                                  ("USD/MXN", "buy", -1), ("USD/MXN", "buy", "1.0000001")])
def test_bad_inputs(make_client, session, markets, args):
    session.routes[("GET", "/markets")] = open_market(markets, "USD/MXN")
    with pytest.raises(crx.BadRequest):
        make_client().quote(*args)
    assert session.paths("POST") == []


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
            row["chains"] = [dict(row["chains"][0], chain="ethereum", chain_id=1, paused=False)]
    session.routes[("GET", "/markets")] = m
    with pytest.raises(crx.MarketPaused, match="^USD/MXN is not offered on avax-fuji$"):
        make_client().market("usdmxn")


def test_quote_not_offered_sends_nothing(make_client, session, markets):
    session.routes[("GET", "/markets")] = without(markets, "USD/MXN")
    with pytest.raises(crx.MarketPaused) as ei:
        make_client().quote("USD/MXN", "buy", 25_000)
    assert ei.value.code == "market_paused" and ei.value.details == {"pair": "USD/MXN"}
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


@pytest.mark.parametrize("cid", ["", "x" * 129, "a\nb", "tab\there", "caf\u00e9", "del\x7f", 123, b"id"])
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
