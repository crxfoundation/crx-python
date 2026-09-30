"""rfqs(), send_quote() and confirm() against a scripted gateway, and the maker digests
against vectors from the Rust reference maker (fixtures/maker-vectors.json)."""

import os
from decimal import Decimal
from pathlib import Path

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

import crx
from crx import _eip712 as e7
from crx import _maker
from crx._bind import Binder

from .conftest import BASE, RPC, Clock, fixture

VEC = fixture("maker-vectors.json")
KEY = "0x" + "42" * 32
T0 = 1_790_000_000.0  # s; the vectors' quote_expiry is T0 + 600
RFQ = "0x" + "a1" * 32
QID = "0x" + "44" * 32
TXH = "0x" + "55" * 32
WRAPS_HASH = e7.h0x(keccak(encode(["bytes[]"], [[]])))


def rnd():
    return "0x" + os.urandom(32).hex()


def frame(wire=None, **edit):
    """An rfq.opened frame as a maker seat reads it, from a vector leg."""
    w = wire or VEC["legs"][0]["wire"]
    d = {"rfq_id": RFQ, "chain": "avax-fuji", "kind": "open", "pair": "USDMXN", "pair_id": w["pair_id"],
         "side": w["side"], "notional": w["notional"], "premium_bps": w["premium_bps"], "expiry": w["expiry"],
         "quote_expiry": w["quote_expiry"], "im_bps": w["im_bps"], "leg_id": w["leg_id"],
         "join_ref": w["join_ref"], "quote_window_secs": 120, "opened_at": int(T0 * 1000), "status": "open"}
    d.update(edit)
    return d


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def maker(session, tmp_path, clock):
    c = crx.Client(key=KEY, base_url=BASE, rpc_url=RPC, state_dir=tmp_path / "state", session=session)
    c._clock, c._sleep = clock, clock.sleep
    return c


def rfq_obj(**edit):
    return _maker.rfq_of(frame(**edit), 7)


# ---------- the Rust vectors ----------

def test_domain_matches_the_rust_vector(health):
    c = next(c for c in health["chains"] if c["key"] == "avax-fuji")
    assert c["core"] == VEC["domain"]["core"]
    assert e7.h0x(e7.domain_separator(43113, c["core"])) == VEC["domain"]["separator"] == c["domain"]


@pytest.mark.parametrize("v", VEC["legs"], ids=lambda v: v["client_quote_id"])
def test_leg_digest_nonce_and_sig_match_the_rust_maker(v):
    a = Account.from_key(KEY)
    assert a.address.lower() == VEC["seat"] == v["wire"]["seat"]
    assert str(_maker.nonce_for(VEC["seat"], v["client_quote_id"])) == v["wire"]["nonce"]
    sep = e7.domain_separator(43113, VEC["domain"]["core"])
    digest = e7.leg_digest(sep, v["wire"])
    assert e7.h0x(digest) == v["digest"]
    assert "0x" + bytes(a.unsafe_sign_hash(digest).signature).hex() == v["sig"]


def test_maker_side_matches_the_rust_maker(tmp_path):
    m = VEC["maker_side"]
    raw = m["leg"]
    leg = {"seat": raw["seat"], "leg_id": raw["leg_id"], "pair_id": raw["pair"], "instrument_id": 1,
           "side": raw["side"], "notional": str(Decimal(raw["notional"]).scaleb(-6)),
           "rate": str(Decimal(raw["rate"]).scaleb(-6)), "im_bps": raw["im_bps"], "premium_bps": raw["premium_bps"],
           "expiry": raw["expiry"] * 1000}
    c = e7.half_commitment(e7.arm_words(leg, m["template"]["own_nonce"], m["template"]["quote_expiry"]),
                           m["template"]["own_salt"])
    assert e7.h0x(c) == m["own_c"] == m["template"]["c_maker"]
    d = m["domain"]
    sep = e7.domain_separator(d["chain_id"], d["verifying_contract"])
    b = Binder(None, Account.from_key(KEY), {}, sep, tmp_path, clock=lambda: m["now_ms"] / 1000)
    digest = _maker.check_side(b, dict(m["template"]), leg)
    assert e7.h0x(digest) == m["digest"]
    assert "0x" + bytes(Account.from_key(KEY).unsafe_sign_hash(digest).signature).hex() == m["sig_by_0x42"]
    assert b.last_signed() == m["own_nonce"]


# ---------- the stream ----------

def tape(rows, seq):
    return {"trades": rows, "seq": seq}


def opened(d, seq):
    return {"type": "rfq.opened", "seq": seq, "ts": 1, "data": d}


def test_rfqs_yields_quotable_rfqs_once(maker, session, monkeypatch):
    monkeypatch.setattr(_maker, "PAGE", 3)
    good = frame()
    other = frame(rfq_id="0x" + "a2" * 32)
    pages = [
        tape([opened(frame(rfq_id="0x" + "b1" * 32, join_ref=None), 1),        # own RFQ
              opened(frame(rfq_id="0x" + "b2" * 32, chain="celo"), 2),          # another chain
              opened(frame(rfq_id="0x" + "b3" * 32, kind="close"), 3)], 3),     # a close round
        tape([opened(frame(rfq_id="0x" + "b4" * 32, opened_at=int(T0 * 1000) - 200_000), 4),  # window closed
              {"type": "trade.opened", "seq": 5, "data": {"rfq_id": RFQ}},
              opened(good, 6)], 6),
        tape([], 6),
        (503, {"code": "upstream", "error": "down"}),
        tape([opened(good, 6), opened(other, 7)], 7),
        tape([], 7),
    ]
    session.routes[("GET", "/trades")] = pages
    stream = maker.rfqs(wait=5)
    assert len(session.calls) == 3  # read to the head at the call
    got = list(stream)
    assert [r.rfq_id for r in got] == [RFQ, "0x" + "a2" * 32]
    r = got[0]
    assert (r.pair, r.side, r.taker_side, r.notional, r.im_bps, r.seq) == ("USD/MXN", "sell", "buy", Decimal("25000"), 100, 6)
    since = [int(c["query"]["since"][0]) for c in session.calls if c["path"] == "/trades"]
    assert since[:5] == [0, 3, 6, 6, 6] and set(since[5:]) == {7}
    assert all(c["query"]["limit"] == ["3"] for c in session.calls if c["path"] == "/trades")


def test_rfqs_reads_the_tape_once_a_second_by_default(maker, session):
    session.routes[("GET", "/trades")] = tape([], 1)
    list(maker.rfqs(wait=10))
    assert len([c for c in session.calls if c["path"] == "/trades"]) == 11  # the catch-up read, then one a second


def test_rfqs_catch_up_error_raises_at_the_call(maker, session):
    session.routes[("GET", "/trades")] = (503, {"code": "upstream", "error": "down"})
    with pytest.raises(crx.ServerError):
        maker.rfqs()


def test_rfqs_ends_once_stop_is_set(maker, session):
    import threading
    stop = threading.Event()
    session.routes[("GET", "/trades")] = tape([opened(frame(), 1)], 1)
    stream = maker.rfqs(stop=stop)
    assert next(stream).rfq_id == RFQ
    stop.set()
    assert list(stream) == []


def test_rfqs_skips_an_rfq_whose_window_closed_while_waiting(maker, session, clock):
    session.routes[("GET", "/trades")] = tape([opened(frame(), 1)], 1)
    stream = maker.rfqs(wait=1)
    clock.t += 200
    assert list(stream) == []


def test_rfqs_resumes_after_since(maker, session):
    session.routes[("GET", "/trades")] = tape([], 41)
    assert list(maker.rfqs(since=41, wait=0)) == []
    assert session.calls[-1]["query"]["since"] == ["41"]


def test_rfqs_break_stops_reading(maker, session):
    session.routes[("GET", "/trades")] = tape([opened(frame(), 1)], 1)
    for r in maker.rfqs():
        break
    n = len(session.calls)
    assert r.rfq_id == RFQ and n == 1


# ---------- send_quote ----------

def quote_route(session, sep, answer=None, **edit):
    """POST /rfqs/{id}/quotes: rebuild the Leg the way the gateway does and recover the signer."""
    seen = {}

    def route(req):
        b = req["body"]
        seen["body"] = b
        d = frame(**edit)
        seat = Account.from_key(KEY).address.lower()
        leg = {"seat": seat, "leg_id": d["leg_id"], "join_ref": d["join_ref"], "pair_id": d["pair_id"],
               "instrument_id": 1, "side": d["side"], "notional": d["notional"], "rate": b["rate"],
               "im_bps": d["im_bps"], "premium_bps": d["premium_bps"], "expiry": d["expiry"],
               "nonce": str(_maker.nonce_for(seat, b["client_quote_id"])), "quote_expiry": d["quote_expiry"]}
        digest = e7.leg_digest(sep, leg)
        if Account._recover_hash(digest, signature=b["sig"]).lower() != seat:
            return (400, {"code": "invalid_signature", "error": "does not recover"})
        if answer is not None:
            return answer
        return {"quote_id": QID, "rfq_id": RFQ, "rate": str(Decimal(b["rate"]).normalize()), "leg_hash": e7.h0x(digest),
                "expires_at": b.get("expires_at", d["quote_expiry"]), "client_quote_id": b["client_quote_id"],
                "leg_id": d["leg_id"], "join_ref": d["join_ref"], "side": d["side"], "status": "quoted"}
    session.routes[("POST", f"/rfqs/{RFQ}/quotes")] = route
    return seen


def sep_of(health):
    c = next(c for c in health["chains"] if c["key"] == "avax-fuji")
    return e7.domain_separator(43113, c["core"])


def test_send_quote_signs_the_rust_leg(maker, session, health):
    v = VEC["legs"][0]
    seen = quote_route(session, sep_of(health))
    q = maker.send_quote(rfq_obj(), "18.712300", client_quote_id=v["client_quote_id"])
    assert seen["body"] == {"rate": "18.7123", "client_quote_id": v["client_quote_id"], "sig": v["sig"]}
    assert (q.rfq_id, q.quote_id, q.side, q.rate, q.pair, q.notional) == (
        RFQ, QID, "sell", Decimal("18.712300"), "USD/MXN", Decimal("25000"))
    assert q.leg["nonce"] == v["wire"]["nonce"] and e7.h0x(e7.leg_digest(sep_of(health), q.leg)) == v["digest"]


def test_send_quote_second_vector_and_expires_in(maker, session, health):
    v = VEC["legs"][1]
    w = v["wire"]
    edit = dict(side=w["side"], notional=w["notional"], premium_bps=w["premium_bps"])
    seen = quote_route(session, sep_of(health), **edit)
    q = maker.send_quote(_maker.rfq_of(frame(**edit)), Decimal("156.4321"), client_quote_id=v["client_quote_id"],
                         expires_in=30)
    assert seen["body"]["sig"] == v["sig"] and seen["body"]["expires_at"] == int(T0 * 1000) + 30_000
    assert q.side == "buy" and q.expires_at.timestamp() == T0 + 30


def test_send_quote_fresh_client_quote_id_each_call(maker, session, health):
    seen = quote_route(session, sep_of(health))
    maker.send_quote(rfq_obj(), "18.7")
    a = seen["body"]["client_quote_id"]
    maker.send_quote(rfq_obj(), "18.7")
    assert a != seen["body"]["client_quote_id"] and a.startswith("sdk-q-")


@pytest.mark.parametrize("edit, err", [
    (dict(join_ref=None), crx.BadRequest),                       # own RFQ
    (dict(kind="close"), crx.BadRequest),
    (dict(chain="celo"), crx.BadRequest),
    (dict(pair_id="0x" + "99" * 32), crx.RefusedToSign),        # pair_id is not the pair's
    (dict(side=True), crx.RefusedToSign),
    (dict(side=0), crx.RefusedToSign),
    (dict(im_bps=0), crx.RefusedToSign),
    (dict(notional="-5"), crx.RefusedToSign),
    (dict(expiry=int(T0 * 1000) - 1), crx.RefusedToSign),
    (dict(quote_expiry=int(T0 * 1000) + 90_000_000), crx.RefusedToSign),
    (dict(quote_expiry=int(T0 * 1000) - 1), crx.QuoteLost),
    (dict(leg_id=None), crx.RefusedToSign),
    (dict(instrument_id=2), crx.RefusedToSign),
])
def test_send_quote_refuses_before_signing(maker, session, health, edit, err):
    quote_route(session, sep_of(health))
    with pytest.raises(err):
        maker.send_quote(rfq_obj(**edit), "18.7")
    assert ("POST", f"/rfqs/{RFQ}/quotes") not in [(c["method"], c["path"]) for c in session.calls]


@pytest.mark.parametrize("rate", ["18.1234567", "0", "-1", "abc", "nan", str(2**64)])
def test_send_quote_refuses_a_bad_rate(maker, session, health, rate):
    quote_route(session, sep_of(health))
    with pytest.raises(crx.BadRequest):
        maker.send_quote(rfq_obj(), rate)
    assert not any(c["path"].endswith("/quotes") for c in session.calls)


def test_send_quote_takes_an_rfq_only(maker):
    with pytest.raises(crx.BadRequest):
        maker.send_quote({"rfq_id": RFQ}, "18.7")


@pytest.mark.parametrize("status, body, err, code", [
    (422, {"code": "insufficient_collateral", "error": "short"}, crx.InsufficientCollateral, "insufficient_collateral"),
    (410, {"code": "rfq_expired", "error": "lapsed"}, crx.QuoteExpired, "quote_expired"),
    (403, {"code": "not_whitelisted", "error": "no"}, crx.NotWhitelisted, "not_whitelisted"),
    (409, {"code": "conflict", "error": "rfq is Accepted"}, crx.CrxError, "conflict"),
])
def test_send_quote_refusals_are_typed(maker, session, health, status, body, err, code):
    quote_route(session, sep_of(health), answer=(status, body))
    with pytest.raises(err) as e:
        maker.send_quote(rfq_obj(), "18.7")
    assert e.value.code == code and e.value.status == status


def test_send_quote_refuses_an_echo_of_another_leg(maker, session, health):
    quote_route(session, sep_of(health), answer={"quote_id": QID, "rfq_id": RFQ, "rate": "18.7",
                                                 "leg_hash": "0x" + "00" * 32})
    with pytest.raises(crx.BadAnswer):
        maker.send_quote(rfq_obj(), "18.7")


def test_trade_refuses_a_maker_quote(maker, session, health):
    quote_route(session, sep_of(health))
    q = maker.send_quote(rfq_obj(), "18.7")
    with pytest.raises(crx.BadRequest):
        maker.trade(q)


# ---------- confirm ----------

class Round:
    """The maker's side of one Side round: GET /side, POST /side, and the RFQ view."""

    def __init__(self, session, sep, clock, q):
        self.s, self.sep, self.clock, self.q = session, sep, clock, q
        self.accept_after = 3  # GET /side polls answered 404 before the accept
        self.polls = 0
        self.view_status = "quoted"
        self.own_status = "quoted"
        self.edit = {}
        self.leg_edit = {}  # a template built over another leg, every hash consistent
        self.drop = None  # a template key the gateway leaves out
        self.sig = None
        self.posts = []
        self.post_answers = [{"taker_signed": True, "maker_signed": True, "ready": True}]
        self.script = [("sending", None), ("open", TXH)]
        session.routes[("GET", f"/rfqs/{RFQ}/side")] = self.get_side
        session.routes[("POST", f"/rfqs/{RFQ}/side")] = self.post_side
        session.routes[("GET", f"/rfqs/{RFQ}")] = self.view

    def template(self):
        leg = dict(self.q.leg, **self.leg_edit)
        own_nonce = int(self.clock() * 1000)
        qe = int(self.clock()) + 300
        salt, c_taker = rnd(), keccak(os.urandom(32))
        c_maker = e7.half_commitment(e7.arm_words(leg, own_nonce, qe), salt)
        t = {"digest_kind": "side", "own_leg_id": leg["leg_id"], "own_nonce": str(own_nonce), "quote_expiry": qe,
             "own_salt": salt, "c_taker": e7.h0x(c_taker), "c_maker": e7.h0x(c_maker),
             "pair_c": e7.h0x(e7.pair_commitment(c_taker, c_maker)), "wraps_hash": WRAPS_HASH,
             "domain_separator": e7.h0x(self.sep), "signed": False}
        t["digest"] = e7.h0x(e7.side_digest(self.sep, t))
        t.update(self.edit)
        t.pop(self.drop, None)
        return t

    def get_side(self, req):
        self.polls += 1
        if self.polls <= self.accept_after:
            return (404, {"code": "unknown_rfq", "error": f"no Side round on rfq {RFQ}"})
        self.t = self.template()
        return self.t

    def post_side(self, req):
        self.posts.append(req["body"]["sig"])
        self.sig = req["body"]["sig"]
        a = self.post_answers.pop(0) if len(self.post_answers) > 1 else self.post_answers[0]
        return a

    def view(self, req):
        mine = {"quote_id": self.q.quote_id, "rate": "18.7", "status": self.own_status, "house": False}
        v = {"rfq_id": RFQ, "status": self.view_status, "quotes": [mine], "quote": None}
        if self.sig is not None:
            word, tx = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            v.update(trade_status=word, trade_tx=tx)
        return v


@pytest.fixture
def sent(maker, session, health):
    quote_route(session, sep_of(health))
    return maker.send_quote(rfq_obj(), "18.7")


@pytest.fixture
def round_(session, health, clock, sent):
    return Round(session, sep_of(health), clock, sent)


def test_confirm_signs_the_maker_side_and_opens(maker, round_, sent, session):
    t = maker.confirm(sent)
    assert (t.status, t.tx, t.rfq_id, t.side, t.rate) == ("open", TXH, RFQ, "sell", Decimal("18.7"))
    assert round_.posts == [round_.sig]
    seat = Account.from_key(KEY).address
    assert Account._recover_hash(e7.side_digest(round_.sep, round_.t), signature=round_.sig) == seat
    floor = (maker._state_dir / f"side-nonce-{seat.lower()}").read_text()
    assert floor == round_.t["own_nonce"]
    assert set(session.rpc_methods()) <= {"eth_chainId", "eth_getCode"}


def test_confirm_reads_the_rfq_view_every_third_poll(maker, round_, sent, session):
    round_.accept_after = 9
    t = maker.confirm(sent)
    before = [c["path"] for c in session.calls if c["method"] == "GET"]
    first_template = [i for i, p in enumerate(before) if p.endswith("/side")][9]
    views = [p for p in before[:first_template] if p == f"/rfqs/{RFQ}"]
    assert t.status == "open" and len(views) == 3


def test_confirm_rides_out_rate_limits(maker, round_, sent, session):
    limited = (429, {"code": "rate_limited", "error": "slow down"})
    side, view = round_.get_side, round_.view
    session.routes[("GET", f"/rfqs/{RFQ}/side")] = [limited, limited, side]
    session.routes[("GET", f"/rfqs/{RFQ}")] = [limited, view]
    round_.accept_after = 0
    round_.post_answers = [limited, {"ready": True}]
    t = maker.confirm(sent)
    assert t.status == "open" and len(round_.posts) == 2 and len(set(round_.posts)) == 1


def test_confirm_resends_the_same_bytes_on_5xx(maker, round_, sent):
    round_.post_answers = [(502, {"code": "upstream", "error": "x"}), (503, {"code": "upstream", "error": "x"}),
                           {"ready": True}]
    t = maker.confirm(sent)
    assert t.status == "open" and len(round_.posts) == 3 and len(set(round_.posts)) == 1


def test_confirm_after_the_side_is_signed_a_dead_gateway_is_trade_unknown(maker, round_, sent):
    round_.post_answers = [(503, {"code": "upstream", "error": "down"})]
    with pytest.raises(crx.TradeUnknown) as e:
        maker.confirm(sent)
    assert e.value.details["rfq_id"] == RFQ and "positions()" in str(e.value)


def test_confirm_a_refused_signature_raises_its_error(maker, round_, sent):
    round_.post_answers = [(400, {"code": "invalid_signature", "error": "recovers to another"})]
    with pytest.raises(crx.AuthError):
        maker.confirm(sent)
    assert len(round_.posts) == 1


def test_confirm_already_signed_skips_to_the_status(maker, round_, sent):
    round_.edit = {"signed": True}
    round_.sig = "0x00"  # the gateway holds this seat's Side from an earlier call
    t = maker.confirm(sent)
    assert t.status == "open" and round_.posts == []


@pytest.mark.parametrize("view_status, own, reason", [
    ("accepted", "quoted", "another_maker"),
    ("opened", "quoted", "another_maker"),
    ("expired", "quoted", "expired"),
    ("quoted", "expired", "expired"),
    ("cancelled", "quoted", "cancelled"),
])
def test_confirm_a_lost_quote_says_why(maker, round_, sent, view_status, own, reason):
    round_.accept_after = 10**6
    round_.view_status, round_.own_status = view_status, own
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert e.value.reason == reason == e.value.details["reason"] and e.value.code == "quote_lost"
    assert round_.posts == []


def test_confirm_no_accept_before_the_wait_is_timeout(maker, round_, sent, clock):
    round_.accept_after = 10**6
    start = clock()
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent, timeout=4)
    assert e.value.reason == "timeout" and 4 <= clock() - start <= 6 and round_.posts == []


def test_confirm_default_wait_ends_with_the_quote_window(maker, round_, sent, clock):
    round_.accept_after = 10**6
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert e.value.reason == "timeout" and clock() >= T0 + 120 + 5


def test_confirm_round_closed(maker, round_, sent, session):
    session.routes[("GET", f"/rfqs/{RFQ}/side")] = (409, {"code": "round_closed", "error": "closed"})
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert e.value.reason == "round_closed" and round_.posts == []


def test_confirm_round_closed_after_signing(maker, round_, sent):
    round_.post_answers = [(409, {"code": "round_closed", "error": "closed"})]
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert e.value.reason == "round_closed"


def test_confirm_the_accept_wins_over_a_losing_view(maker, round_, sent):
    round_.view_status, round_.own_status = "accepted", "accepted"
    assert maker.confirm(sent).status == "open"


@pytest.mark.parametrize("edit", [
    {"c_maker": "0x" + "00" * 32},
    {"pair_c": "0x" + "00" * 32},
    {"own_leg_id": "0x" + "99" * 32},
    {"digest": "0x" + "00" * 32},
    {"domain_separator": "0x" + "00" * 32},
    {"digest_kind": "allocation_acceptance"},
    {"own_salt": "0x" + "01" * 32},
    {"quote_expiry": int(T0) + 10_000},
    {"own_nonce": "0"},
    {"own_nonce": str(2**64 - 1)},
    {"own_nonce": "nope"},
])
def test_confirm_refuses_a_bad_template(maker, round_, sent, edit):
    round_.edit = edit
    with pytest.raises(crx.RefusedToSign):
        maker.confirm(sent)
    assert round_.posts == []
    assert not (maker._state_dir / f"side-nonce-{maker.address}").exists()


@pytest.mark.parametrize("key", ["digest", "domain_separator"])
def test_confirm_refuses_a_template_without_its_digest_or_domain(maker, round_, sent, key):
    round_.drop = key
    with pytest.raises(crx.RefusedToSign):
        maker.confirm(sent)
    assert round_.posts == []


@pytest.mark.parametrize("leg_edit", [
    {"rate": "18.8"}, {"side": 1}, {"notional": "25001"}, {"im_bps": 200}, {"premium_bps": 1},
    {"expiry": VEC["legs"][0]["wire"]["expiry"] + 1000}, {"seat": "0x" + "12" * 20},
])
def test_confirm_refuses_a_consistent_template_over_another_leg(maker, round_, sent, leg_edit):
    round_.leg_edit = leg_edit
    with pytest.raises(crx.RefusedToSign, match="c_maker"):
        maker.confirm(sent)
    assert round_.posts == []


def test_confirm_refuses_a_nonce_at_or_under_the_floor(maker, round_, sent):
    floor = maker._state_dir / f"side-nonce-{maker.address}"
    floor.parent.mkdir(parents=True)
    floor.write_text(str(int(T0 * 1000) + 10**6))
    with pytest.raises(crx.RefusedToSign):
        maker.confirm(sent)
    assert round_.posts == []


def test_confirm_takes_a_maker_quote_only(maker):
    with pytest.raises(crx.BadRequest):
        maker.confirm("not a quote")


# ---------- rfq(), house_rate ----------

def test_rfq_taker_view_reads_house_rate(maker, session, clock):
    now = int(clock() * 1000)
    view = frame(join_ref=None, side=1, client_rfq_id="cid-1")
    view["quotes"] = [
        {"quote_id": QID, "rate": "18.69", "status": "quoted", "house": False, "expires_at": now + 60_000},
        {"quote_id": "0x" + "45" * 32, "rate": "18.71", "status": "quoted", "house": True, "expires_at": 10**14},
        {"quote_id": "0x" + "46" * 32, "rate": "18.70", "status": "expired", "house": True, "expires_at": 10**14},
    ]
    session.routes[("GET", f"/rfqs/{RFQ}")] = view
    r = maker.rfq(RFQ)
    assert (r.client_rfq_id, r.side, r.taker_side, r.house_rate) == ("cid-1", "buy", "buy", Decimal("18.71"))
    assert len(r.quotes) == 3


def test_rfq_refuses_a_bad_id(maker):
    with pytest.raises(crx.BadRequest):
        maker.rfq("0x12")


def test_no_house_rate_for_a_maker_view():
    assert _maker.rfq_of(frame()).house_rate is None


def test_maker_quote_is_exported():
    assert crx.MakerQuote and crx.Rfq and issubclass(crx.QuoteLost, crx.CrxError)
    assert Path(crx.__file__).parent.joinpath("_maker.py").exists()
