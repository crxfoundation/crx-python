"""The maker on a chain that takes binding quotes (sign_mode "quote"): send_quote(), confirm(),
drop_quote(), and the Quote digest against the build's golden vector. Side mode is the control."""

from decimal import Decimal

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

import crx
from crx import _eip712 as e7
from crx import _maker

from .conftest import BASE, RPC, Clock
from .test_maker import KEY, RFQ, T0, TXH, VEC, Round, frame, opened, quote_route, sep_of, tape

SEAT = Account.from_key(KEY).address.lower()
TREF = "0x" + "7e" * 32
QE_MAX = int(T0) + 570  # the RFQ opened at T0 and its quote_expiry is T0 + 600
TRADE = "0x" + "9a" * 32
SIDE_PATH = f"/rfqs/{RFQ}/side"


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def maker(session, tmp_path, clock):
    c = crx.Client(key=KEY, base_url=BASE, rpc_url=RPC, state_dir=tmp_path / "state", session=session)
    c._clock, c._sleep = clock, clock.sleep
    return c


def bound(**edit):
    """An rfq.opened frame on a binding chain, as a maker seat reads it."""
    return frame(**{"sign_mode": "quote", "taker_ref": TREF, "quote_expiry_max": QE_MAX, **edit})


def rfq_obj(**edit):
    return _maker.rfq_of(bound(**edit), 7)


def refusal(code, status=409, **details):
    body = {"code": code, "error": code.replace("_", " ")}
    if details:
        body["details"] = details
    return (status, body)


class Gateway:
    """One RFQ on a binding chain: POST and DELETE quotes with the gateway's checks in its order,
    and the maker's view of the RFQ."""

    def __init__(self, session, sep, clock, **edit):
        self.s, self.sep, self.clock = session, sep, clock
        self.d = bound(**edit)
        self.bodies = []        # every quote body posted
        self.digests = []       # the Quote digest of each rested quote
        self.rows = []          # the seat's own quote rows, as the view serves them
        self.live = None        # the seat's live leg on the RFQ
        self.dropped = {}       # leg_id -> at_ms
        self.answer = None      # a refusal the next POST answers, once
        self.accepted = None    # the accepted row
        self.status = "quoted"  # the RFQ's status
        self.ended = False
        self.views = 0
        self.accept_at = None   # accept the newest quote on this view read
        self.script = [("sending", None), ("open", TXH)]
        session.routes[("POST", f"/rfqs/{RFQ}/quotes")] = self.post
        session.routes[("GET", f"/rfqs/{RFQ}")] = self.view

    def post(self, req):
        b = req["body"]
        self.bodies.append(b)
        if self.answer is not None:
            answer, self.answer = self.answer, None
            return answer
        d, qe = self.d, b["quote_expiry"]
        if e7.leg_id_tail(b["leg_id"]) != qe:
            return refusal("bad_request", 400)
        if qe < int(self.clock()) + 60 or qe > d["quote_expiry_max"]:
            return refusal("bad_request", 400)
        if not self.clock() * 1000 < b.get("expires_at", qe * 1000) <= qe * 1000:
            return refusal("bad_request", 400)
        half = {"seat": SEAT, "leg_id": b["leg_id"], "pair_id": d["pair_id"], "instrument_id": 1, "side": d["side"],
                "notional": d["notional"], "rate": b["rate"], "im_bps": d["im_bps"], "premium_bps": d["premium_bps"],
                "expiry": d["expiry"]}
        digest = e7.quote_digest(self.sep, e7.arm_words(half, int(b["nonce"]), qe), b["salt"], d["taker_ref"])
        if Account._recover_hash(digest, signature=b["sig"]).lower() != SEAT:
            return refusal("invalid_signature", 400)
        if b["leg_id"] in self.dropped:
            return refusal("leg_id_taken")
        if self.live not in (None, b["leg_id"]):
            return refusal("leg_live", leg_id=self.live)
        self.live = b["leg_id"]
        for row in self.rows:
            if row["leg_id"] == b["leg_id"] and row["status"] == "quoted":
                row["status"] = "dropped"  # a later quote on the leg replaces the earlier one
        row = {"quote_id": e7.h0x(keccak(b"crx/quote/v1" + digest)), "rfq_id": RFQ,
               "rate": str(Decimal(b["rate"]).normalize()), "leg_hash": e7.h0x(digest), "leg_id": b["leg_id"],
               "join_ref": d["join_ref"], "side": d["side"], "quote_expiry": qe * 1000,
               "expires_at": b.get("expires_at", qe * 1000), "client_quote_id": b["client_quote_id"],
               "status": "quoted"}
        self.rows.append(row)
        self.digests.append(digest)
        self.s.routes[("DELETE", f"/rfqs/{RFQ}/quotes/{b['leg_id']}")] = lambda req, leg=b["leg_id"]: self.delete(leg)
        return dict(row)

    def delete(self, leg):
        if self.accepted is not None and self.accepted["leg_id"] == leg:
            return refusal("already_accepted", trade_id=TRADE)
        if leg in self.dropped:
            return {"dropped": True, "leg_id": leg, "at_ms": self.dropped[leg]}
        if self.ended:
            return refusal("unknown_or_ended", 404)
        self.dropped[leg] = int(self.clock() * 1000)
        self.live = None
        for row in self.rows:
            if row["leg_id"] == leg:
                row["status"] = "dropped"
        return {"dropped": True, "leg_id": leg, "at_ms": self.dropped[leg]}

    def end(self, status):
        """The RFQ leaves the book's live set: accepted, expired or cancelled. The gateway erases
        every salt on it, so each quote still ``quoted`` reads ``dropped`` from here on."""
        self.status, self.live = status, None
        for row in self.rows:
            if row["status"] == "quoted":
                row["status"] = "dropped"

    def accept(self, row=None):
        """The taker accepts this seat's quote ``row`` (default: its newest)."""
        self.accepted = row or self.rows[-1]
        self.accepted["status"] = "accepted"
        self.end("accepted")

    def view(self, req):
        self.views += 1
        if self.accept_at is not None and self.views >= self.accept_at and self.accepted is None:
            self.accept()
        v = {"rfq_id": RFQ, "status": self.status, "quotes": [dict(r) for r in self.rows], "quote": None}
        if self.accepted is not None:
            word, tx = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            v.update(trade_status=word, trade_tx=tx)
        return v


@pytest.fixture
def gw(session, health, clock):
    return Gateway(session, sep_of(health), clock)


def posts(session, path=f"/rfqs/{RFQ}/quotes"):
    return [c for c in session.calls if c["method"] == "POST" and c["path"] == path]


def side_calls(session):
    return [c for c in session.calls if c["path"] == SIDE_PATH]


# ---------- the Quote digest: the build's golden vector, by value ----------

def test_quote_digest_matches_the_golden_vector():
    assert e7.QUOTE_TYPE == (
        "Quote(address seat,bytes32 legId,bytes32 pair,uint8 instrumentId,int8 side,uint256 notional,uint64 rate,"
        "uint16 imBps,int16 premiumBps,uint40 expiry,uint64 nonce,uint64 quoteExpiry,bytes32 salt,bytes32 wrapsHash,"
        "bytes32 takerRef)")
    assert e7.h0x(e7.QUOTE_TYPEHASH) == "0xe0bb0f60332d120c09afbf82ee09ab3bfbab0de2f53237ed66edb7e42ef333f8"
    assert e7.h0x(e7.EMPTY_WRAPS_HASH) == "0x569e75fc77c1a856f6daaf9e69d8a9566ca34aa47f9133711ce065a571af0cfd"
    assert e7.EMPTY_WRAPS_HASH == keccak(encode(["bytes[]"], [[]]))
    leg_id = e7.leg_id_for(b"\xaa" * 24, 1790000000)
    assert leg_id == "0x" + "aa" * 24 + "000000006ab13b80" and e7.leg_id_tail(leg_id) == 1790000000
    taker_ref = e7.h0x(keccak(b"CRX/takerRef/v1" + bytes.fromhex("7638646FcFf3E28E42Dc4a778ea7bbc236701230")
                              + bytes.fromhex("11" * 32)))
    assert taker_ref == "0xf9e4e3eebad3eff3e3a17ed1793fa41363e63a25cd7b4bd91fb818bd0a14aa71"
    half = {"seat": "0x" + "11" * 20, "leg_id": leg_id, "pair_id": "0x" + "22" * 32, "instrument_id": 1, "side": -1,
            "notional": "1000000", "rate": "18.5", "im_bps": 500, "premium_bps": -3, "expiry": 1797000000 * 1000}
    words = e7.arm_words(half, 7, 1790000000)
    salt = "0x" + "33" * 32
    assert e7.h0x(e7.quote_struct_hash(words, salt, taker_ref)) == (
        "0xdb819b4c6cf4434681c18d9efb270326e44edef703538bdd57495c78ec5026c1")
    sep = e7.domain_separator(43113, "0x0f6Fba28791DFD909bd023e63Bc072081610EeeA")
    assert e7.h0x(sep) == "0xa360b83ea212664bd3328ac11dd6558752fee818bba2807d51049806a9f7fd84"
    digest = e7.quote_digest(sep, words, salt, taker_ref)
    assert e7.h0x(digest) == "0xa6765d7b7e7d3d13a4335416dbece63a1dc4b43a4dfe9448cc51df3190aff0cd"
    anvil0 = Account.from_key("0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")  # test key
    assert anvil0.address == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    assert "0x" + bytes(anvil0.unsafe_sign_hash(digest).signature).hex() == (
        "0x0a5fd22c262ea788df873a95c3bfcea07e9941f5ccb8d6aabd779a709fe0818a"
        "2f31747fa102c02db6f648a58cef2d3970e72246bfb14c5eaec618bf66d1022f1c")


def test_leg_id_takes_24_random_bytes():
    with pytest.raises(ValueError):
        e7.leg_id_for(b"\x00" * 32, 1)


# ---------- send_quote ----------

def test_send_quote_signs_a_binding_quote(maker, gw, session):
    r = rfq_obj()
    assert r.sign_mode == "quote"
    q = maker.send_quote(r, "18.712300", client_quote_id="cq-1")
    (body,) = gw.bodies
    assert set(body) == {"rate", "leg_id", "salt", "nonce", "quote_expiry", "client_quote_id", "sig"}
    assert body["rate"] == "18.7123" and body["quote_expiry"] == QE_MAX and body["client_quote_id"] == "cq-1"
    assert e7.leg_id_tail(body["leg_id"]) == QE_MAX and len(body["leg_id"]) == len(body["salt"]) == 66
    assert body["leg_id"] != bound()["leg_id"]  # the seat's own leg id, not the frame's
    assert body["nonce"] == str(int(body["nonce"])) and 0 <= int(body["nonce"]) < 2**64
    # The signature recovers to the seat over the Quote of its own half, the salt and the frame's taker_ref.
    assert Account._recover_hash(gw.digests[0], signature=body["sig"]).lower() == SEAT
    assert q.quote_id == e7.h0x(keccak(b"crx/quote/v1" + gw.digests[0]))
    assert (q.sign_mode, q.leg_id, q.side, q.rate, q.notional) == (
        "quote", body["leg_id"], "sell", Decimal("18.712300"), Decimal("25000"))
    assert q.quote_expiry.timestamp() == QE_MAX and q.expires_at.timestamp() == QE_MAX
    assert "salt" not in q.leg and body["salt"] not in str(q.raw)  # the salt is not kept
    assert side_calls(session) == []


@pytest.mark.parametrize("mode", [None, "side", "other"])
def test_send_quote_signs_the_leg_where_the_rfq_is_not_binding(maker, session, health, mode):
    # The control: no sign_mode, or any other word, is the Leg flow byte for byte (the Rust vector).
    v = VEC["legs"][0]
    seen = quote_route(session, sep_of(health))
    edit = {} if mode is None else {"sign_mode": mode, "taker_ref": TREF, "quote_expiry_max": QE_MAX}
    r = _maker.rfq_of(frame(**edit), 7)
    assert r.sign_mode == "side"
    q = maker.send_quote(r, "18.712300", client_quote_id=v["client_quote_id"])
    assert seen["body"] == {"rate": "18.7123", "client_quote_id": v["client_quote_id"], "sig": v["sig"]}
    assert q.sign_mode == "side" and q.leg_id == v["wire"]["leg_id"] and maker._legs == {}


def test_a_later_quote_keeps_the_leg_and_takes_a_new_salt(maker, gw):
    a = maker.send_quote(rfq_obj(), "18.70")
    b = maker.send_quote(rfq_obj(), "18.69")
    first, second = gw.bodies
    assert first["leg_id"] == second["leg_id"] == a.leg_id == b.leg_id
    assert first["salt"] != second["salt"] and first["client_quote_id"] != second["client_quote_id"]
    assert first["sig"] != second["sig"] and a.quote_id != b.quote_id
    assert [r["status"] for r in gw.rows] == ["dropped", "quoted"]


def test_each_rfq_gets_its_own_leg(maker, gw, session, health, clock):
    other = "0x" + "a2" * 32
    session.routes[("POST", f"/rfqs/{other}/quotes")] = lambda req: dict(
        gw.post(req), rfq_id=other)
    a = maker.send_quote(rfq_obj(), "18.7")
    gw.live = None
    b = maker.send_quote(rfq_obj(rfq_id=other), "18.7")
    assert a.leg_id != b.leg_id and maker._legs == {RFQ: a.leg_id, other: b.leg_id}
    clock.t += 580  # past both legs' quote_expiry: the next quote forgets them
    with pytest.raises(crx.QuoteLost):
        maker.send_quote(rfq_obj(), "18.7")
    assert maker._legs == {}


@pytest.mark.parametrize("expires_in, want", [(30, int(T0 * 1000) + 30_000), (10_000, QE_MAX * 1000)])
def test_expires_in_ends_the_book_life_at_the_quote_expiry_at_most(maker, gw, expires_in, want):
    q = maker.send_quote(rfq_obj(), "18.7", expires_in=expires_in)
    assert gw.bodies[0]["expires_at"] == want and q.expires_at.timestamp() * 1000 == want
    assert gw.bodies[0]["quote_expiry"] == QE_MAX


@pytest.mark.parametrize("edit, err", [
    (dict(taker_ref=None), crx.RefusedToSign),
    (dict(taker_ref="0x12"), crx.RefusedToSign),
    (dict(quote_expiry_max=None), crx.RefusedToSign),
    (dict(quote_expiry_max=int(T0) + 59), crx.QuoteLost),                 # under 60 s left
    (dict(quote_expiry_max=int(T0) + 631, quote_expiry=int(T0 * 1000) + 3_600_000), crx.RefusedToSign),
    (dict(quote_expiry_max=int(T0) + 601), crx.RefusedToSign),            # past the RFQ's own quote_expiry
    (dict(join_ref=None), crx.BadRequest),                                # own RFQ
    (dict(pair_id="0x" + "99" * 32), crx.RefusedToSign),
    (dict(im_bps=0), crx.RefusedToSign),
])
def test_binding_quote_refuses_before_signing(maker, gw, session, edit, err):
    with pytest.raises(err):
        maker.send_quote(rfq_obj(**edit), "18.7")
    assert posts(session) == [] and maker._legs == {}


@pytest.mark.parametrize("expires_in", [0, -1, 86_401])
def test_binding_quote_refuses_a_bad_expires_in(maker, gw, session, expires_in):
    with pytest.raises(crx.BadRequest):
        maker.send_quote(rfq_obj(), "18.7", expires_in=expires_in)
    assert posts(session) == []


def test_binding_quote_needs_no_leg_id_on_the_frame(maker, gw):
    assert maker.send_quote(rfq_obj(leg_id=None), "18.7").sign_mode == "quote"


def test_sixty_seconds_left_still_quotes(maker, gw, clock):
    clock.t = QE_MAX - 60
    assert maker.send_quote(rfq_obj(), "18.7").leg_id.endswith(f"{QE_MAX:016x}")


def test_leg_id_taken_is_typed_and_the_next_quote_takes_a_new_leg(maker, gw):
    gw.answer = refusal("leg_id_taken")
    with pytest.raises(crx.LegIdTaken) as e:
        maker.send_quote(rfq_obj(), "18.7")
    assert (e.value.code, e.value.status) == ("leg_id_taken", 409) and maker._legs == {}
    q = maker.send_quote(rfq_obj(), "18.7")
    assert q.leg_id != gw.bodies[0]["leg_id"]


def test_leg_live_names_the_leg_and_the_next_quote_goes_on_it(maker, gw):
    live = e7.leg_id_for(b"\x01" * 24, QE_MAX)
    gw.live = live
    with pytest.raises(crx.LegLive) as e:
        maker.send_quote(rfq_obj(), "18.7")
    assert (e.value.code, e.value.status, e.value.leg_id) == ("leg_live", 409, live)
    q = maker.send_quote(rfq_obj(), "18.69")
    assert q.leg_id == live == gw.bodies[1]["leg_id"] and gw.bodies[0]["leg_id"] != live


def test_leg_live_on_a_leg_near_its_end_is_dropped_by_its_id(maker, gw, session, clock):
    # Under 60 s of life left: no quote goes on it. The error's leg_id drops it; the next quote takes a new leg.
    stale = e7.leg_id_for(b"\x02" * 24, int(clock()) + 59)
    gw.live = stale
    session.routes[("DELETE", f"/rfqs/{RFQ}/quotes/{stale}")] = lambda req: gw.delete(stale)
    with pytest.raises(crx.LegLive) as e:
        maker.send_quote(rfq_obj(), "18.7")
    assert e.value.leg_id == stale and maker._legs == {}
    assert maker.drop_quote(rfq_obj(), leg_id=e.value.leg_id).leg_id == stale
    assert maker.send_quote(rfq_obj(), "18.7").leg_id not in (stale, gw.bodies[0]["leg_id"])


def test_quote_fills_full_is_typed(maker, gw):
    gw.answer = refusal("quote_fills_full")
    with pytest.raises(crx.QuoteFillsFull) as e:
        maker.send_quote(rfq_obj(), "18.7")
    assert (e.value.code, e.value.status) == ("quote_fills_full", 409)


@pytest.mark.parametrize("edit", [{"leg_hash": "0x" + "00" * 32}, {"leg_id": "0x" + "00" * 32}, {"rate": "18.8"},
                                  {"rfq_id": "0x" + "a2" * 32}, {"quote_id": None}])
def test_binding_quote_refuses_an_echo_of_another_quote(maker, gw, session, edit):
    session.routes[("POST", f"/rfqs/{RFQ}/quotes")] = lambda req: dict(gw.post(req), **edit)
    with pytest.raises(crx.BadAnswer):
        maker.send_quote(rfq_obj(), "18.7")


# ---------- confirm ----------

@pytest.fixture
def sent(maker, gw):
    return maker.send_quote(rfq_obj(), "18.7")


def test_confirm_signs_nothing_on_a_binding_quote(maker, gw, sent, session):
    gw.accept_at = 2
    t = maker.confirm(sent)
    assert (t.status, t.tx, t.rfq_id, t.quote_id, t.side, t.rate) == (
        "open", TXH, RFQ, sent.quote_id, "sell", Decimal("18.7"))
    assert side_calls(session) == []  # GET /side is never polled, and no Side is posted
    assert len(posts(session)) == 1
    assert not (maker._state_dir / f"side-nonce-{SEAT}").exists()
    assert set(session.rpc_methods()) <= {"eth_chainId", "eth_getCode"}


def test_confirm_reads_the_view_every_third_poll(maker, gw, sent, clock):
    gw.accept_at = 4
    start = clock()
    assert maker.confirm(sent, timeout=60).status == "open"
    assert 9 <= clock() - start <= 12  # three reads 3 s apart before the accept shows


def test_confirm_rides_out_a_gateway_that_does_not_answer(maker, gw, sent, session):
    gw.accept()
    session.routes[("GET", f"/rfqs/{RFQ}")] = [
        (429, {"code": "rate_limited", "error": "slow down"}), (503, {"code": "upstream", "error": "down"}), gw.view]
    assert maker.confirm(sent).status == "open"


@pytest.mark.parametrize("status, reason", [
    ("accepted", "another_maker"),   # the taker accepted another maker's quote
    ("opened", "another_maker"),
    ("expired", "expired"),
    ("cancelled", "cancelled"),
])
def test_confirm_a_binding_quote_on_an_ended_rfq_says_why(maker, gw, sent, session, status, reason):
    # The states the gateway serves: the RFQ ended, so this seat's live quote reads dropped.
    gw.end(status)
    assert [r["status"] for r in gw.rows] == ["dropped"]
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert e.value.reason == reason and e.value.details["rfq_id"] == RFQ and side_calls(session) == []


@pytest.mark.parametrize("status, own, reason", [
    ("quoted", "dropped", "dropped"),    # a live RFQ: the seat's drop, or a gateway restart
    ("quoted", "expired", "expired"),    # the quote's own book life ended
    ("expired", "expired", "expired"),
])
def test_confirm_a_binding_quote_that_ended_on_its_own_says_why(maker, gw, sent, session, status, own, reason):
    gw.status, gw.rows[0]["status"] = status, own
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert e.value.reason == reason and side_calls(session) == []


def test_lost_reads_the_rfq_status_before_the_quotes_own():
    def view(status, *rows):
        return {"status": status, "quotes": [{"quote_id": i, "status": s} for i, s in rows]}
    assert _maker.lost(view("accepted", ("a", "dropped")), "a") == "another_maker"
    assert _maker.lost(view("accepted", ("a", "dropped"), ("b", "accepted")), "a") == "dropped"
    assert _maker.lost(view("accepted", ("a", "dropped"), ("b", "accepted")), "b") is None
    assert _maker.lost(view("accepted"), "a") == "another_maker"
    assert _maker.lost(view("cancelled", ("a", "dropped")), "a") == "cancelled"
    assert _maker.lost(view("expired", ("a", "dropped")), "a") == "expired"
    assert _maker.lost(view("quoted", ("a", "dropped")), "a") == "dropped"
    assert _maker.lost(view("quoted", ("a", "dropped"), ("b", "quoted")), "a") == "dropped"
    assert _maker.lost(view("quoted", ("a", "quoted")), "a") is None


def test_confirm_no_accept_before_the_wait_is_timeout(maker, gw, sent, session, clock):
    start = clock()
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent, timeout=4)
    assert e.value.reason == "timeout" and 4 <= clock() - start <= 6 and side_calls(session) == []


@pytest.mark.parametrize("accepted", [False, True])
def test_confirm_the_earlier_quote_on_a_leg_reads_dropped(maker, gw, sent, accepted):
    # Replaced by this seat's later quote: dropped, before and after the taker accepts the later one.
    later = maker.send_quote(rfq_obj(), "18.69")
    if accepted:
        gw.accept()
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert e.value.reason == "dropped"
    gw.accept_at = 1
    assert maker.confirm(later).rate == Decimal("18.69")


def test_a_quote_that_may_rest_after_a_network_error_is_dropped_by_its_rfq(maker, gw, session):
    # The post reached the gateway and its answer was lost: the client still holds the leg.
    import requests

    def lost_answer(req):
        gw.post(req)
        raise requests.ConnectionError("reset")
    session.routes[("POST", f"/rfqs/{RFQ}/quotes")] = lost_answer
    with pytest.raises(crx.NetworkError):
        maker.send_quote(rfq_obj(), "18.7")
    leg = gw.bodies[0]["leg_id"]
    assert gw.rows[0]["status"] == "quoted" and maker._legs == {RFQ: leg}
    assert maker.drop_quote(rfq_obj()).leg_id == leg and gw.rows[0]["status"] == "dropped"


# ---------- drop_quote ----------

def test_drop_quote_ends_the_leg_and_the_next_quote_takes_a_new_one(maker, gw, sent, session, clock):
    d = maker.drop_quote(sent)
    assert (d.rfq_id, d.leg_id, d.at.timestamp()) == (RFQ, sent.leg_id, T0)
    (call,) = [c for c in session.calls if c["method"] == "DELETE"]
    assert call["path"] == f"/rfqs/{RFQ}/quotes/{sent.leg_id}" and call["raw"] == b"" and "x-crx-sig" in call["headers"]
    clock.t += 5
    assert maker.drop_quote(sent).at == d.at  # a repeat answers the first drop's time
    with pytest.raises(crx.QuoteLost) as e:
        maker.confirm(sent)
    assert e.value.reason == "dropped"
    again = maker.send_quote(rfq_obj(), "18.7")
    assert again.leg_id != sent.leg_id and gw.rows[-1]["status"] == "quoted"


def test_drop_after_the_accept_is_already_accepted(maker, gw, sent):
    gw.accept()
    with pytest.raises(crx.AlreadyAccepted) as e:
        maker.drop_quote(sent)
    assert (e.value.code, e.value.status, e.value.trade_id) == ("already_accepted", 409, TRADE)
    assert maker._legs == {RFQ: sent.leg_id}
    assert maker.confirm(sent).status == "open"  # the trade stands


def test_drop_on_an_ended_rfq_is_unknown_or_ended(maker, gw, sent):
    gw.ended = True
    with pytest.raises(crx.UnknownOrEnded) as e:
        maker.drop_quote(sent)
    assert (e.value.code, e.value.status) == ("unknown_or_ended", 404) and maker._legs == {}


def test_drop_by_rfq_and_leg_id(maker, gw, sent, session):
    assert maker.drop_quote(rfq_obj(), leg_id=sent.leg_id.upper().replace("0X", "0x")).leg_id == sent.leg_id
    other = Gateway(session, gw.sep, gw.clock)
    q = maker.send_quote(rfq_obj(), "18.7")
    assert maker.drop_quote(rfq_obj()).leg_id == q.leg_id  # the leg this client quoted on
    assert other.dropped and maker._legs == {}


@pytest.mark.parametrize("answer", [{"dropped": False}, {"dropped": True, "leg_id": "0x" + "00" * 32}, {}])
def test_drop_refuses_an_answer_for_another_leg(maker, gw, sent, session, answer):
    session.routes[("DELETE", f"/rfqs/{RFQ}/quotes/{sent.leg_id}")] = answer
    with pytest.raises(crx.BadAnswer):
        maker.drop_quote(sent)
    assert maker._legs == {RFQ: sent.leg_id}


def test_drop_refuses_before_any_call(maker, session, health):
    quote_route(session, sep_of(health))
    leg_quote = maker.send_quote(_maker.rfq_of(frame(), 7), "18.7")
    n = len(session.calls)
    for bad, kw in ((leg_quote, {}), ("not a quote", {}), (rfq_obj(), {}), (rfq_obj(), {"leg_id": "0x12"})):
        with pytest.raises(crx.BadRequest):
            maker.drop_quote(bad, **kw)
    assert len(session.calls) == n


def test_a_viewer_cannot_drop(session, tmp_path):
    c = crx.Client(key=KEY, base_url=BASE, rpc_url=RPC, state_dir=tmp_path, session=session,
                   account="0x5b38da6a701c568545dcfcb03fcb875f56beddc4")
    with pytest.raises(crx.ConfigError):
        c.drop_quote(rfq_obj(), leg_id="0x" + "01" * 32)


# ---------- the quickstart's maker calls, off the tape, in both modes ----------

@pytest.mark.parametrize("mode", ["quote", "side"])
def test_the_quickstart_maker_calls_run_in_both_modes(maker, session, health, clock, mode):
    sep = sep_of(health)
    if mode == "quote":
        gw = Gateway(session, sep, clock)
        gw.accept_at = 1
        row = opened(bound(), 1)
    else:
        quote_route(session, sep)
        row = opened(frame(), 1)
    session.routes[("GET", "/trades")] = tape([row], 1)
    for rfq in maker.rfqs(wait=60):
        assert rfq.sign_mode == mode
        q = maker.send_quote(rfq, "18.7")
        if mode == "side":
            Round(session, sep, clock, q).accept_after = 1
        t = maker.confirm(q, timeout=60)
        break
    assert (t.status, t.tx, q.sign_mode) == ("open", TXH, mode)
    assert (len(side_calls(session)) > 0) is (mode == "side")
