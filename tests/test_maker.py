"""The maker's reads and its binding Quote: rfqs(), rfq(), send_quote() against a scripted gateway,
and the Quote against fixtures/maker-vectors.json, the maker half of format-vectors-v5.json QV1-QV5.

The scripted gateway rebuilds each posted Quote by hand from SPEC v5 §3, without the SDK."""

import dataclasses
import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

import crx
from crx import _eip712 as e7
from crx import _maker
from crx.models import ms_to_dt

from .conftest import BASE, FIX, RPC, Clock, fixture

FV = fixture("format-vectors-v5.json")
FV_SHA256 = "4d7c40e3aa2c67c07fc9d15c0fece8cb5fc61f96c312f4176e3001c1b83b4621"
VEC_FILE = "maker-vectors.json"
VEC_SHA256 = "6e2c448608b884dc6137a75ae09ae2a8a5b1d555f324873b07b3959681a77284"
KEY = FV["keys"]["other"]["private_key"]  # anvil test key 1: publicly known, never funded
SEAT = FV["keys"]["other"]["address"]
QE_MAX = int(FV["quotes"][0]["quote_expiry"])
T0 = QE_MAX - 600  # s: QV1's quote end is 600 s out
CLOSES_MS = (T0 + 120) * 1000
RFQ = "0x" + "a1" * 32
OTHER = "0x" + "a2" * 32
TXH = "0x" + "55" * 32
TRADE = "0x" + "9a" * 32
PAIRS = {e7.h0x(keccak(text=p)): p for p in ("USD/BRL", "USD/MXN", "USD/PHP")}
QUOTE_TYPEHASH = "0x00fc8b1401eebfc809c01d3a7351526ef07cfd5902793c3d279de280c0aec2cb"  # SPEC v5 §1
QUOTE_TYPE = ("Quote(address seat,bytes32 legId,bytes32 pair,int8 side,uint256 notional,uint64 rate,int16 premiumBps,"
              "uint40 expiry,uint64 nonce,bytes32 salt,bytes32 takerRef)")
QUOTE_TYPE_WITH_END = QUOTE_TYPE.replace("uint64 nonce,", "uint64 nonce,uint64 quoteExpiry,")


# ---------- the vectors: maker-vectors.json is the maker half of format-vectors-v5.json ----------

def d6(units) -> str:
    """An integer at 6 decimals as a 6-decimal string: 5410000 -> "5.410000"."""
    n = int(units)
    return f"{n // 10**6}.{n % 10**6:06d}"


def project(fv: dict) -> dict:
    """maker-vectors.json, from format-vectors-v5.json: per quote the half the SDK signs (notional
    and rate as 6-decimal strings, expiry in ms), the salt, the taker_ref, the digest and the sig.

    Write the file again:
    .venv-rt/bin/python -c "import json, tests.test_maker as t; open('tests/fixtures/maker-vectors.json', 'w')
    .write(json.dumps(t.project(t.FV), indent=1) + chr(10))"
    """
    dom = fv["domain"]
    return {
        "about": "The maker half of format-vectors-v5.json quotes QV1-QV5, as the SDK signs it. "
                 "Written by project() in tests/test_maker.py; a test checks the file equals it.",
        "key": "format-vectors-v5.json keys.other: anvil test key 1, a publicly known key; never fund it",
        "seat": fv["keys"]["other"]["address"],
        "domain": {"name": dom["name"], "version": dom["version"], "chain_id": dom["chainId"],
                   "verifying_contract": dom["verifyingContract"], "separator": fv["domain_separator"]},
        "quotes": [{
            "id": q["id"],
            "half": {
                "seat": q["maker_leg"]["seat"], "leg_id": q["maker_leg"]["leg_id"], "pair_id": q["maker_leg"]["pair"],
                "side": int(q["maker_leg"]["side"]), "notional": d6(q["maker_leg"]["notional"]),
                "rate": d6(q["maker_leg"]["rate"]), "premium_bps": int(q["maker_leg"]["premium_bps"]),
                "expiry": int(q["maker_leg"]["expiry"]) * 1000, "nonce": q["maker_leg"]["nonce"],
            },
            "quote_expiry": int(q["quote_expiry"]),
            "salt": q["maker_salt"],
            "taker_ref": q["taker_ref"]["value"],
            "digest": q["digest"],
            "sig": q["signature"],
        } for q in fv["quotes"]],
    }


VEC = project(FV)  # what the file holds; the next test checks the file byte for byte
DOMAIN = VEC["domain"]
SEP = bytes.fromhex(DOMAIN["separator"][2:])
QV1 = VEC["quotes"][0]
IDS = [q["id"] for q in VEC["quotes"]]


def from_file(qid: str) -> dict:
    """Quote ``qid`` as maker-vectors.json holds it."""
    return next(q for q in fixture(VEC_FILE)["quotes"] if q["id"] == qid)


def test_maker_vectors_are_the_projection_of_the_format_vectors():
    assert hashlib.sha256((FIX / "format-vectors-v5.json").read_bytes()).hexdigest() == FV_SHA256
    raw = (FIX / VEC_FILE).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == VEC_SHA256
    assert raw.decode() == json.dumps(project(FV), indent=1) + "\n"
    assert IDS == ["QV1", "QV2", "QV3", "QV4", "QV5"]


def test_the_domain_is_the_vectors():
    assert Account.from_key(KEY).address.lower() == SEAT == fixture(VEC_FILE)["seat"]
    assert fixture(VEC_FILE)["domain"] == DOMAIN
    assert e7.h0x(e7.domain_separator(DOMAIN["chain_id"], DOMAIN["verifying_contract"])) == DOMAIN["separator"]


@pytest.mark.parametrize("qid", IDS)
def test_quote_digest_and_sig_reproduce_the_vector(qid):
    v = from_file(qid)
    sep = e7.domain_separator(DOMAIN["chain_id"], DOMAIN["verifying_contract"])
    digest = e7.quote_digest(sep, e7.leg_words(v["half"]), v["salt"], v["taker_ref"])
    assert e7.h0x(digest) == v["digest"]
    assert sign(digest) == v["sig"]
    assert recover(digest, v["sig"]) == SEAT == v["half"]["seat"]


@pytest.mark.parametrize("v", VEC["quotes"], ids=lambda v: v["id"])
def test_the_hand_built_quote_reproduces_the_vector(v):
    # The scripted gateway's Quote, checked here once against the vectors.
    assert e7.h0x(keccak(text=QUOTE_TYPE)) == QUOTE_TYPEHASH == e7.h0x(e7.typehash("Quote"))
    h = v["half"]
    assert e7.h0x(by_hand(h["seat"], h["leg_id"], PAIRS[h["pair_id"]], h["side"], h["notional"], h["rate"],
                          h["premium_bps"], h["expiry"], int(h["nonce"]), v["salt"], v["taker_ref"])) == v["digest"]


@pytest.mark.parametrize("v", VEC["quotes"], ids=lambda v: v["id"])
def test_the_vector_leg_id_ends_in_the_quote_end_and_gives_the_nonce(v):
    leg = v["half"]["leg_id"]
    assert e7.leg_id_tail(leg) == tail(leg) == v["quote_expiry"]
    assert e7.maker_nonce(leg) == int(v["half"]["nonce"]) == int.from_bytes(bytes.fromhex(leg[2:])[16:24], "big")


@pytest.mark.parametrize("case", FV["maker_nonce"]["cases"], ids=lambda c: c["id"])
def test_maker_nonce_is_the_u64_of_leg_id_bytes_16_to_24(case):
    assert e7.maker_nonce(case["leg_id"]) == int(case["nonce"])
    assert int(case["nonce"]) == int.from_bytes(bytes.fromhex(case["leg_id"][2:])[16:24], "big")


def test_nd1_a_quote_over_the_next_nonce_does_not_recover_the_seat():
    n = FV["maker_nonce"]["negative"]
    h = QV1["half"]
    assert n["leg_id"] == h["leg_id"] and n["derived_nonce"] == h["nonce"]
    assert int(n["signed_nonce"]) == int(n["derived_nonce"]) + 1
    sep = e7.domain_separator(DOMAIN["chain_id"], DOMAIN["verifying_contract"])
    off = e7.quote_digest(sep, e7.leg_words(dict(h, nonce=n["signed_nonce"])), QV1["salt"], QV1["taker_ref"])
    derived = e7.quote_digest(sep, e7.leg_words(h), QV1["salt"], QV1["taker_ref"])
    assert e7.h0x(off) == n["signed_digest"] and sign(off) == n["signature"]
    assert e7.h0x(derived) == n["gateway_digest"] == QV1["digest"]
    assert recover(derived, n["signature"]) == n["recovered_from_gateway_digest"] != SEAT == n["maker_seat"]


# ---------- the hand-built Quote and the scripted gateway ----------

def b32(word: str) -> bytes:
    return bytes.fromhex(word[2:])


def tail(leg_id: str) -> int:
    """The quote end a leg id carries: bytes 24 to 31, big endian."""
    return int.from_bytes(b32(leg_id)[24:], "big")


def units6(text) -> int:
    whole, _, frac = str(text).partition(".")
    return int(whole) * 10**6 + int(frac.ljust(6, "0") or "0")


def by_hand(seat, leg_id, pair, side, notional, rate, premium_bps, expiry_ms, nonce, salt, taker_ref,
            quote_expiry=None) -> bytes:
    """The Quote digest of SPEC v5 §3, built without the SDK. ``quote_expiry`` adds that member after the nonce."""
    kinds = ["bytes32", "address", "bytes32", "bytes32", "int8", "uint256", "uint64", "int16", "uint40", "uint64"]
    words = [keccak(text=QUOTE_TYPE if quote_expiry is None else QUOTE_TYPE_WITH_END), to_checksum_address(seat),
             b32(leg_id), keccak(text=pair), int(side), units6(notional), units6(rate), int(premium_bps),
             int(expiry_ms) // 1000, int(nonce)]
    if quote_expiry is not None:
        kinds.append("uint64")
        words.append(int(quote_expiry))
    struct = keccak(encode(kinds + ["bytes32", "bytes32"], words + [b32(salt), b32(taker_ref)]))
    return keccak(b"\x19\x01" + SEP + struct)


def sign(digest: bytes) -> str:
    return "0x" + bytes(Account.from_key(KEY).unsafe_sign_hash(digest).signature).hex()


def recover(digest, sig: str) -> str:
    return Account._recover_hash(b32(digest) if isinstance(digest, str) else digest, signature=sig).lower()


def frame(v=QV1, **edit):
    """An ``rfq.opened`` frame as a maker seat reads it (SPEC v5 §5.4), on the terms of vector ``v``.
    ``side`` is the maker's own. An edit to None leaves that key out."""
    h = v["half"]
    d = {"rfq_id": RFQ, "chain": "avax-fuji", "pair": PAIRS[h["pair_id"]], "side": h["side"],
         "notional": h["notional"], "expiry": h["expiry"], "premium_bps": h["premium_bps"],
         "quote_expiry_max": v["quote_expiry"], "closes_at": (v["quote_expiry"] - 480) * 1000,
         "taker_ref": v["taker_ref"]}
    d.update(edit)
    return {k: x for k, x in d.items() if x is not None}


def rfq_obj(**edit):
    return _maker.rfq_of(frame(**edit), 7)


def refusal(code, status=409, error=None, **details):
    body = {"code": code, "error": error or code.replace("_", " ")}
    if details:
        body["details"] = details
    return (status, body)


def posts(session, path=f"/rfqs/{RFQ}/quotes"):
    return [c for c in session.calls if c["method"] == "POST" and c["path"] == path]


def fake_random(monkeypatch, head: bytes, salt: bytes) -> None:
    """The leg id's 24 random bytes and the salt the next quote takes."""
    monkeypatch.setattr(_maker.secrets, "token_bytes", lambda n: {24: head, 32: salt}[n])


class Gateway:
    """One RFQ: POST and DELETE quotes with the gateway's checks in its order, and the maker's view of it.

    The POST rebuilds the Quote by hand: the seat from ``x-crx-address``, the RFQ's terms, the body's
    rate, leg id and salt, the nonce from leg id bytes 16 to 23, the RFQ's taker_ref. ``derive=False``
    takes the body's nonce; ``with_end=True`` hashes the body's quote_expiry after the nonce."""

    def __init__(self, session, clock, v=QV1, **edit):
        self.s, self.clock = session, clock
        self.d = frame(v, **edit)
        self.derive, self.with_end = True, False
        self.bodies = []        # every quote body posted
        self.digests = []       # the Quote digest of each rested quote
        self.rows = []          # the seat's own quote rows
        self.live = None        # the seat's live leg on the RFQ
        self.dropped = {}       # leg_id -> at_ms
        self.answer = None      # a refusal the next POST answers, once
        self.accepted = None    # the accepted row
        self.status = "quoted"  # the RFQ's status
        self.ended = False
        self.views = 0
        self.accept_at = None   # accept the newest quote on this view read
        self.script = [("sending", None), ("open", TXH)]
        session.routes[("POST", f"/rfqs/{self.d['rfq_id']}/quotes")] = self.post
        session.routes[("GET", f"/rfqs/{self.d['rfq_id']}")] = self.view

    def digest(self, seat, b):
        d = self.d
        nonce = int.from_bytes(b32(b["leg_id"])[16:24], "big") if self.derive else int(b["nonce"])
        return by_hand(seat, b["leg_id"], d["pair"], d["side"], d["notional"], b["rate"], d["premium_bps"],
                       d["expiry"], nonce, b["salt"], d["taker_ref"], b["quote_expiry"] if self.with_end else None)

    def post(self, req):
        b = req["body"]
        self.bodies.append(b)
        if self.answer is not None:
            answer, self.answer = self.answer, None
            return answer
        end = tail(b["leg_id"])
        if end < int(self.clock()) + 60 or end > self.d["quote_expiry_max"]:
            return refusal("bad_request", 400)
        seat = req["headers"]["x-crx-address"].lower()
        digest = self.digest(seat, b)
        if recover(digest, b["sig"]) != seat:
            return refusal("invalid_signature", 400)
        if b["leg_id"] in self.dropped:
            return refusal("leg_id_taken")
        if self.live not in (None, b["leg_id"]):
            return refusal("leg_live", leg_id=self.live)
        self.live = b["leg_id"]
        for row in self.rows:
            if row["leg_id"] == b["leg_id"] and row["status"] == "quoted":
                row["status"] = "dropped"  # a later quote on the leg replaces the earlier one
        qid = e7.h0x(keccak(b"crx/quote/v1" + digest))
        row = {"quote_id": qid, "rfq_id": self.d["rfq_id"], "rate": str(Decimal(b["rate"]).normalize()),
               "leg_hash": e7.h0x(digest), "leg_id": b["leg_id"], "expires_at": end * 1000,
               "client_quote_id": b.get("client_quote_id", qid), "status": "quoted"}
        self.rows.append(row)
        self.digests.append(digest)
        self.s.routes[("DELETE", f"/rfqs/{self.d['rfq_id']}/quotes/{b['leg_id']}")] = (
            lambda req, leg=b["leg_id"]: self.delete(leg))
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
        """The RFQ leaves the live set: accepted, expired or cancelled. Each quote still ``quoted`` reads ``dropped``."""
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
        rows = [{k: r[k] for k in ("quote_id", "rate", "status", "client_quote_id")} for r in self.rows]
        v = dict(self.d, kind="open", status=self.status, quotes=rows)
        if self.accepted is not None:
            word, tx = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            v.update(trade_status=word, trade_tx=tx)
        return v


@pytest.fixture
def clock():
    return Clock(T0)


def new_maker(session, health, tmp_path, clock):
    """A maker seat on keys.other, on avax-fuji with the vectors' domain (chain 43113, their core: core=)."""
    fuji = next(c for c in health["chains"] if c["key"] == "avax-fuji")
    fuji.update(core=DOMAIN["verifying_contract"], domain=DOMAIN["separator"])
    session.routes[("GET", "/health")] = health
    session.rpc["eth_getCode"] = lambda p: "0x6080" if p[0].lower() == DOMAIN["verifying_contract"] else "0x"
    c = crx.Client(key=KEY, base_url=BASE, rpc_url=RPC, state_dir=tmp_path / "state", session=session,
                   core=DOMAIN["verifying_contract"])
    c._clock, c._sleep = clock, clock.sleep
    return c


@pytest.fixture
def maker(session, health, tmp_path, clock):
    return new_maker(session, health, tmp_path, clock)


@pytest.fixture
def gw(session, clock):
    return Gateway(session, clock)


# ---------- the RFQ view ----------

def test_rfq_of_reads_the_maker_view():
    r = rfq_obj()
    h = QV1["half"]
    assert (r.rfq_id, r.pair, r.side, r.taker_side, r.notional, r.premium_bps, r.seq) == (
        RFQ, "USD/BRL", "sell", "buy", Decimal("5000000.000000"), 0, 7)
    assert r.expiry.timestamp() * 1000 == h["expiry"]
    assert r.quote_expiry_max.timestamp() == QE_MAX  # unix seconds on the wire
    assert r.closes_at.timestamp() * 1000 == CLOSES_MS  # unix ms on the wire
    assert (r.kind, r.status, r.client_rfq_id, r.own) == ("open", None, None, False)  # rfq.opened has no kind, status
    for gone in ("join_ref", "leg_id", "pair_id", "instrument_id", "im_bps", "sign_mode", "house_rate", "opened_at"):
        assert not hasattr(r, gone)


@pytest.mark.parametrize("pair", ["USDBRL", "usd/brl", "USD/BRL"])
def test_rfq_of_reads_the_pair_in_slash_form(pair):
    assert rfq_obj(pair=pair).pair == "USD/BRL"


def test_rfq_of_reads_a_buy_side():
    r = rfq_obj(side=1)
    assert (r.side, r.taker_side) == ("buy", "sell")


def test_rfq_of_names_no_rfq_without_an_id():
    assert _maker.rfq_of(frame(rfq_id=None)) is None
    assert _maker.rfq_of(frame(rfq_id="0x12")) is None


@pytest.mark.parametrize("cid, own", [("cid-1", True), ("", False), (None, False)])
def test_an_rfq_is_own_when_it_names_a_client_rfq_id(cid, own):
    r = rfq_obj(client_rfq_id=cid)
    assert r.own is own and r.client_rfq_id == (cid or None)


def test_rfq_reads_the_taker_view(maker, session):
    view = frame(side=1, client_rfq_id="cid-1", kind="open", status="quoted")
    view["quotes"] = [{"quote_id": "0x" + "44" * 32, "rate": "5.41", "status": "quoted", "client_quote_id": "q1"},
                      {"quote_id": "0x" + "45" * 32, "rate": "5.42", "status": "quoted", "client_quote_id": "q2"}]
    session.routes[("GET", f"/rfqs/{RFQ}")] = view
    r = maker.rfq(RFQ.upper().replace("0X", "0x"))
    assert (r.client_rfq_id, r.own, r.side, r.taker_side, r.status, r.seq) == ("cid-1", True, "buy", "buy", "quoted", None)
    assert [q["rate"] for q in r.quotes] == ["5.41", "5.42"]


def test_rfq_refuses_a_bad_id_before_any_call(maker, session):
    with pytest.raises(crx.BadRequest, match="rfq_id is a 0x 32-byte hex word"):
        maker.rfq("0x12")
    assert session.calls == []


def test_rfq_refuses_a_view_of_another_rfq(maker, session):
    session.routes[("GET", f"/rfqs/{RFQ}")] = frame(rfq_id=OTHER)
    with pytest.raises(crx.BadAnswer, match="answered for another RFQ"):
        maker.rfq(RFQ)


# ---------- the stream ----------

def tape(rows, seq):
    return {"trades": rows, "seq": seq}


def opened(d, seq):
    return {"type": "rfq.opened", "seq": seq, "ts": 1, "data": d}


def other_id(n):
    return "0x" + f"b{n}" * 32


def test_rfqs_yields_quotable_rfqs_once(maker, session, monkeypatch):
    monkeypatch.setattr(_maker, "PAGE", 3)
    good = frame(pair="USDBRL")
    other = frame(rfq_id=OTHER)
    pages = [
        tape([opened(frame(rfq_id=other_id(1), client_rfq_id="cid-1"), 1),      # own RFQ
              opened(frame(rfq_id=other_id(2), chain="celo"), 2),                # another chain
              opened(frame(rfq_id=other_id(3), kind="close"), 3)], 3),           # a close RFQ
        tape([opened(frame(rfq_id=other_id(4), closes_at=T0 * 1000), 4),         # quote window closed
              opened(frame(rfq_id=other_id(5), closes_at=None, quote_expiry_max=T0), 5),  # quote end passed
              opened(frame(rfq_id=other_id(6), status="cancelled"), 6)], 6),     # ended
        tape([{"type": "trade.opened", "seq": 7, "data": {"rfq_id": RFQ}}, opened(good, 8)], 8),
        tape([], 8),
        (503, {"code": "upstream", "error": "down"}),
        tape([opened(good, 8), opened(other, 9)], 9),
        tape([], 9),
    ]
    session.routes[("GET", "/trades")] = pages
    stream = maker.rfqs(wait=5)
    assert len(session.calls) == 3  # read to the head at the call
    got = list(stream)
    assert [r.rfq_id for r in got] == [RFQ, OTHER]
    r = got[0]
    assert (r.pair, r.side, r.taker_side, r.notional, r.premium_bps, r.seq) == (
        "USD/BRL", "sell", "buy", Decimal("5000000.000000"), 0, 8)
    assert (r.closes_at.timestamp(), r.quote_expiry_max.timestamp()) == (T0 + 120, QE_MAX)
    after = [int(c["query"]["after"][0]) for c in session.calls if c["path"] == "/trades"]
    assert after[:6] == [0, 3, 6, 8, 8, 8] and set(after[6:]) == {9}
    assert all(c["query"]["limit"] == ["3"] for c in session.calls if c["path"] == "/trades")


def test_rfqs_yields_an_rfq_with_an_empty_client_rfq_id(maker, session):
    session.routes[("GET", "/trades")] = tape([opened(frame(client_rfq_id=""), 1)], 1)
    (r,) = list(maker.rfqs(wait=0))
    assert r.rfq_id == RFQ and r.own is False


def test_rfqs_never_yields_an_own_rfq(maker, session):
    session.routes[("GET", "/trades")] = [tape([opened(frame(client_rfq_id="cid-1"), 1)], 1),
                                          tape([opened(frame(rfq_id=OTHER, client_rfq_id="cid-2"), 2)], 2)]
    assert list(maker.rfqs(wait=3)) == []


def test_rfqs_reads_the_tape_once_a_second_by_default(maker, session):
    session.routes[("GET", "/trades")] = tape([], 1)
    list(maker.rfqs(wait=10))
    assert len([c for c in session.calls if c["path"] == "/trades"]) == 11  # the catch-up read, then one a second


def test_rfqs_catch_up_error_raises_at_the_call(maker, session):
    session.routes[("GET", "/trades")] = (503, {"code": "upstream", "error": "down"})
    with pytest.raises(crx.ServerError):
        maker.rfqs()


@pytest.mark.parametrize("page", [{"trades": None, "seq": 1}, {"trades": [], "seq": "1"}, {"seq": 1}])
def test_rfqs_refuses_a_page_it_cannot_read(maker, session, page):
    session.routes[("GET", "/trades")] = page
    with pytest.raises(crx.BadAnswer, match="/trades sent a page this SDK cannot read"):
        maker.rfqs()


def test_rfqs_ends_once_stop_is_set(maker, session):
    stop = threading.Event()
    session.routes[("GET", "/trades")] = tape([opened(frame(), 1)], 1)
    stream = maker.rfqs(stop=stop)
    assert next(stream).rfq_id == RFQ
    stop.set()
    assert list(stream) == []


def test_rfqs_skips_an_rfq_whose_window_closed_while_waiting(maker, session, clock):
    session.routes[("GET", "/trades")] = tape([opened(frame(), 1)], 1)
    stream = maker.rfqs(wait=1)
    clock.t += 120  # closes_at
    assert list(stream) == []


def test_rfqs_resumes_after_the_seq_given(maker, session):
    """The tape cursor is ``after``: the gateway refuses ``since`` (400).
    Mutation: the query keeps ``since``, or ``rfqs()`` keeps a ``since=`` alias ⇒ red."""
    session.routes[("GET", "/trades")] = tape([], 41)
    assert list(maker.rfqs(after=41, wait=0)) == []
    assert session.calls[-1]["query"]["after"] == ["41"] and "since" not in session.calls[-1]["query"]
    with pytest.raises(TypeError):
        maker.rfqs(since=41)


def test_rfqs_after_past_the_head_raises_and_is_not_read_again(maker, session):
    """A 409 past this reader's head ends the stream loudly; it is not a transient error.
    Mutation: the 409 joins the retried errors, or the poll keeps ``since`` ⇒ red."""
    past = (409, {"code": "conflict", "error": "after 5 is past your head 0"})
    session.routes[("GET", "/trades")] = [tape([], 5), past]
    with pytest.raises(crx.CrxError, match="past your head") as e:
        list(maker.rfqs(wait=30))
    assert (e.value.code, e.value.status) == ("conflict", 409)
    assert [c["query"]["after"] for c in session.calls if c["path"] == "/trades"] == [["0"], ["5"]]


def test_rfqs_break_stops_reading(maker, session):
    session.routes[("GET", "/trades")] = tape([opened(frame(), 1)], 1)
    for r in maker.rfqs():
        break
    assert r.rfq_id == RFQ and len(session.calls) == 1


ASK = crx.Ask(rfq_id=RFQ, pair="USD/BRL", side="buy", notional=Decimal("5000000"), expiry=None, raw={}, rfq={},
              _client=None)


@pytest.mark.parametrize("only", [ASK, RFQ, RFQ.upper()], ids=["ask", "rfq_id", "upper_case"])
def test_rfqs_only_yields_that_rfq_then_ends(maker, session, only):
    session.routes[("GET", "/trades")] = [
        tape([opened(frame(rfq_id=OTHER), 1)], 1),
        tape([opened(frame(rfq_id="0x" + "a3" * 32), 2), opened(frame(), 3), opened(frame(rfq_id=OTHER), 4)], 4),
        tape([opened(frame(), 5)], 5),
    ]
    assert [r.rfq_id for r in maker.rfqs(only=only, wait=30)] == [RFQ]
    assert len(session.calls) == 2  # the catch-up read, then the read that carried it: none after


def test_rfqs_only_takes_the_rfq_from_the_catch_up_read(maker, session):
    session.routes[("GET", "/trades")] = tape([opened(frame(rfq_id=OTHER), 1), opened(frame(), 2)], 2)
    assert [r.rfq_id for r in maker.rfqs(only=ASK, wait=30)] == [RFQ] and len(session.calls) == 1


def test_rfqs_only_raises_no_quotes_when_the_rfq_never_arrives(maker, session, clock):
    session.routes[("GET", "/trades")] = tape([opened(frame(rfq_id=OTHER), 1)], 1)
    start = clock()
    with pytest.raises(crx.NoQuotes) as ei:
        list(maker.rfqs(only=ASK, wait=3))
    assert ei.value.details == {"rfq_id": RFQ}
    assert str(ei.value) == "the RFQ did not show in this account's open RFQs before the wait ended"
    assert clock() - start == 3


def test_next_on_rfqs_only_raises_no_quotes_when_the_rfq_never_arrives(maker, session):
    session.routes[("GET", "/trades")] = tape([opened(frame(rfq_id=OTHER), 1)], 1)
    with pytest.raises(crx.NoQuotes) as ei:
        next(maker.rfqs(only=ASK, wait=3))
    assert ei.value.details == {"rfq_id": RFQ}


def test_next_on_rfqs_only_returns_that_rfq(maker, session):
    session.routes[("GET", "/trades")] = tape([opened(frame(rfq_id=OTHER), 1), opened(frame(), 2)], 2)
    assert next(maker.rfqs(only=ASK, wait=10)).rfq_id == RFQ


def test_rfqs_only_ends_without_error_once_stop_is_set(maker, session):
    stop = threading.Event()
    stop.set()
    session.routes[("GET", "/trades")] = tape([opened(frame(rfq_id=OTHER), 1)], 1)
    assert list(maker.rfqs(only=ASK, stop=stop)) == []


@pytest.mark.parametrize("only", ["", "0x12", RFQ + "00", 7, object()])
def test_rfqs_only_refuses_what_names_no_rfq_before_any_call(maker, session, only):
    with pytest.raises(crx.BadRequest, match="only is an Ask, or an RFQ id"):
        maker.rfqs(only=only)
    assert session.calls == []


# ---------- send_quote: the body and the signature ----------

@pytest.mark.parametrize("qid", IDS)
def test_send_quote_posts_the_vector_quote(maker, session, clock, monkeypatch, qid):
    v = from_file(qid)
    h = v["half"]
    clock.t = v["quote_expiry"] - 600
    gw = Gateway(session, clock, v)
    fake_random(monkeypatch, b32(h["leg_id"])[:24], b32(v["salt"]))
    q = maker.send_quote(_maker.rfq_of(frame(v), 7), h["rate"], premium_bps=h["premium_bps"])
    (body,) = gw.bodies
    rate = h["rate"].rstrip("0").rstrip(".")
    assert body == {"rate": rate, "leg_id": h["leg_id"], "salt": v["salt"], "sig": v["sig"]}
    assert gw.digests == [b32(v["digest"])]
    assert e7.leg_words(q.leg) == e7.leg_words(h) and q.leg["nonce"] == h["nonce"] and "salt" not in q.leg
    assert (q.quote_id, q.rate, q.notional, q.side, q.client_quote_id) == (
        gw.rows[0]["quote_id"], Decimal(h["rate"]), Decimal(h["notional"]), "buy" if h["side"] == 1 else "sell", None)
    assert q.quote_expiry.timestamp() == v["quote_expiry"] and q.expiry.timestamp() * 1000 == h["expiry"]
    assert maker._legs == {RFQ: h["leg_id"]}


def test_send_quote_body_is_rate_leg_id_salt_sig(maker, gw, session):
    q = maker.send_quote(rfq_obj(), "5.410000")
    (call,) = posts(session)
    body = call["body"]
    assert set(body) == {"rate", "leg_id", "salt", "sig"} and body["rate"] == "5.41"
    assert tail(body["leg_id"]) == QE_MAX and len(body["leg_id"]) == len(body["salt"]) == 66
    assert body["leg_id"] != QV1["half"]["leg_id"] and body["salt"] != QV1["salt"]  # fresh random bytes
    nonce = int.from_bytes(b32(body["leg_id"])[16:24], "big")
    h = QV1["half"]
    digest = by_hand(SEAT, body["leg_id"], "USD/BRL", h["side"], h["notional"], "5.41", h["premium_bps"],
                     h["expiry"], nonce, body["salt"], QV1["taker_ref"])
    assert recover(digest, body["sig"]) == SEAT
    assert q.leg["nonce"] == str(nonce) and q.client_quote_id is None and q.quote_expiry.timestamp() == QE_MAX


def test_send_quote_sends_client_quote_id_only_when_given(maker, gw):
    q = maker.send_quote(rfq_obj(), "5.41", client_quote_id="cq-1")
    (body,) = gw.bodies
    assert set(body) == {"rate", "leg_id", "salt", "sig", "client_quote_id"} and body["client_quote_id"] == "cq-1"
    assert q.client_quote_id == "cq-1"


@pytest.mark.parametrize("case", FV["maker_nonce"]["cases"], ids=lambda c: c["id"])
def test_send_quote_signs_under_the_nonce_of_its_leg_id(maker, session, clock, monkeypatch, case):
    leg = b32(case["leg_id"])
    end = tail(case["leg_id"])
    clock.t = end - 600
    gw = Gateway(session, clock, quote_expiry_max=end, closes_at=(end - 480) * 1000)
    fake_random(monkeypatch, leg[:24], b"\x61" * 32)
    q = maker.send_quote(rfq_obj(quote_expiry_max=end), "5.41")
    (body,) = gw.bodies
    assert body["leg_id"] == case["leg_id"] and q.leg["nonce"] == case["nonce"]
    h = QV1["half"]

    def under(nonce):
        return by_hand(SEAT, case["leg_id"], "USD/BRL", h["side"], h["notional"], "5.41", h["premium_bps"],
                       h["expiry"], nonce, "0x" + "61" * 32, QV1["taker_ref"])
    assert recover(under(int(case["nonce"])), body["sig"]) == SEAT
    assert recover(under(int(case["nonce"]) + 1), body["sig"]) != SEAT


def test_a_quote_over_the_next_nonce_is_refused_by_the_gateway(maker, gw, monkeypatch):
    # ND1 on the wire: the gateway rebuilds the Quote over the derived nonce and recovers another address.
    monkeypatch.setattr(e7, "maker_nonce", lambda leg_id: int.from_bytes(b32(leg_id)[16:24], "big") + 1)
    with pytest.raises(crx.AuthError) as e:
        maker.send_quote(rfq_obj(), "5.41")
    assert (e.value.gateway_code, e.value.status) == ("invalid_signature", 400) and len(gw.bodies) == 1


@pytest.mark.parametrize("cqid", ["cq-1", None])
def test_with_derive_maker_nonce_off_the_body_carries_the_nonce(maker, gw, monkeypatch, cqid):
    monkeypatch.setattr(e7, "DERIVE_MAKER_NONCE", False)
    gw.derive = False
    q = maker.send_quote(rfq_obj(), "5.41", client_quote_id=cqid)
    (body,) = gw.bodies
    assert set(body) == {"rate", "leg_id", "salt", "sig", "nonce", "client_quote_id"}
    sent = body["client_quote_id"]
    assert sent == cqid if cqid else sent.startswith("sdk-q-")
    nonce = int.from_bytes(keccak(b32(SEAT) + sent.encode())[-8:], "big")  # low 64 bits of keccak(seat ‖ id)
    derived = int.from_bytes(b32(body["leg_id"])[16:24], "big")
    assert body["nonce"] == str(nonce) != str(derived) and q.leg["nonce"] == str(nonce)
    h = QV1["half"]

    def under(n):
        return by_hand(SEAT, body["leg_id"], "USD/BRL", h["side"], h["notional"], "5.41", h["premium_bps"],
                       h["expiry"], n, body["salt"], QV1["taker_ref"])
    assert recover(under(nonce), body["sig"]) == SEAT and recover(under(derived), body["sig"]) != SEAT


def test_with_drop_quote_expiry_off_the_body_and_the_quote_carry_the_quote_end(maker, gw, monkeypatch):
    monkeypatch.setattr(e7, "DROP_QUOTE_EXPIRY", False)
    gw.with_end = True
    maker.send_quote(rfq_obj(), "5.41")
    (body,) = gw.bodies
    assert set(body) == {"rate", "leg_id", "salt", "sig", "quote_expiry"}
    assert body["quote_expiry"] == QE_MAX == tail(body["leg_id"])
    assert e7.type_string("Quote") == QUOTE_TYPE_WITH_END
    nonce = int.from_bytes(b32(body["leg_id"])[16:24], "big")
    h = QV1["half"]

    def under(end):
        return by_hand(SEAT, body["leg_id"], "USD/BRL", h["side"], h["notional"], "5.41", h["premium_bps"],
                       h["expiry"], nonce, body["salt"], QV1["taker_ref"], end)
    assert recover(under(QE_MAX), body["sig"]) == SEAT and recover(under(None), body["sig"]) != SEAT


def test_the_switches_are_on_by_default():
    assert (e7.DERIVE_MAKER_NONCE, e7.DROP_QUOTE_EXPIRY) == (True, True)
    assert e7.type_string("Quote") == QUOTE_TYPE


# ---------- send_quote: refusals before signing ----------

REFUSALS = [
    ("no_taker_ref", dict(taker_ref=None), crx.RefusedToSign, "names no taker_ref or quote_expiry_max"),
    ("bad_taker_ref", dict(taker_ref="0x12"), crx.RefusedToSign, "names no taker_ref or quote_expiry_max"),
    ("no_quote_expiry_max", dict(quote_expiry_max=None), crx.RefusedToSign, "names no taker_ref or quote_expiry_max"),
    ("quote_window_under_60_s", dict(quote_expiry_max=T0 + 59), crx.QuoteLost,
     "under 60 s of the RFQ's quote window is left; nothing signed"),
    ("quote_end_over_630_s_out", dict(quote_expiry_max=T0 + 631), crx.RefusedToSign, "more than 630 s ahead"),
    ("wrong_chain", dict(chain="celo"), crx.BadRequest, "the RFQ is on celo; this client signs for avax-fuji"),
    ("close_rfq", dict(kind="close"), crx.BadRequest, "a close RFQ takes a price-only quote"),
    ("own_rfq", dict(client_rfq_id="cid-1"), crx.BadRequest, "this is your own RFQ"),
    ("side_zero", dict(side=0), crx.RefusedToSign, "the RFQ names no side for this seat"),
    ("side_bool", dict(side=True), crx.RefusedToSign, "the RFQ names no side for this seat"),
    ("side_missing", dict(side=None), crx.RefusedToSign, "the RFQ names no side for this seat"),
    ("notional_negative", dict(notional="-5"), crx.RefusedToSign, "the RFQ's notional cannot be read"),
    ("notional_zero", dict(notional="0"), crx.RefusedToSign, "the RFQ's notional cannot be read"),
    ("notional_number", dict(notional=5_000_000), crx.RefusedToSign, "the RFQ's notional cannot be read"),
    ("notional_7_decimals", dict(notional="1.0000001"), crx.RefusedToSign, "the RFQ's notional cannot be read"),
    ("expiry_past", dict(expiry=T0 * 1000 - 1), crx.RefusedToSign, "the RFQ's expiry is not in the future"),
    ("expiry_at_this_ms", dict(expiry=T0 * 1000), crx.RefusedToSign, "the RFQ's expiry is not in the future"),
    ("expiry_missing", dict(expiry=None), crx.RefusedToSign, "the RFQ's expiry is not in the future"),
    ("pair", dict(pair="USD"), crx.RefusedToSign, "the RFQ names no AAA/BBB pair"),
    ("premium_out_of_range", dict(premium_bps=32_768), crx.RefusedToSign, "the RFQ's premium_bps cannot be read"),
    ("premium_text", dict(premium_bps="x"), crx.RefusedToSign, "the RFQ's premium_bps cannot be read"),
]


@pytest.mark.parametrize("edit, err, words", [r[1:] for r in REFUSALS], ids=[r[0] for r in REFUSALS])
def test_send_quote_refuses_before_signing(maker, gw, session, edit, err, words):
    with pytest.raises(err) as e:
        maker.send_quote(rfq_obj(**edit), "5.41")
    assert words in str(e.value)
    assert posts(session) == [] and gw.bodies == [] and maker._legs == {}


def test_a_quote_window_under_60_s_is_lost_as_expired(maker, gw, session):
    with pytest.raises(crx.QuoteLost) as e:
        maker.send_quote(rfq_obj(quote_expiry_max=T0 + 59), "5.41")
    assert (e.value.reason, e.value.code) == ("expired", "quote_lost") and posts(session) == []


def test_sixty_seconds_left_still_quotes(maker, gw, clock):
    clock.t = QE_MAX - 60
    assert tail(maker.send_quote(rfq_obj(), "5.41").leg_id) == QE_MAX


def test_a_quote_end_630_s_out_still_quotes(maker, session, clock):
    gw = Gateway(session, clock, quote_expiry_max=T0 + 630)
    maker.send_quote(rfq_obj(quote_expiry_max=T0 + 630), "5.41")
    assert tail(gw.bodies[0]["leg_id"]) == T0 + 630


@pytest.mark.parametrize("rate", ["18.1234567", "0", "-1", "abc", "nan", str(2**64)])
def test_send_quote_refuses_a_bad_rate(maker, gw, session, rate):
    with pytest.raises(crx.BadRequest, match="rate"):
        maker.send_quote(rfq_obj(), rate)
    assert posts(session) == []


@pytest.mark.parametrize("cqid", ["q" * 129, "", "q-é", "q\n1", 7])
def test_send_quote_refuses_a_client_quote_id_the_gateway_refuses(maker, gw, session, cqid):
    with pytest.raises(crx.BadRequest, match="client_quote_id is 1 to 128 printable ASCII characters"):
        maker.send_quote(rfq_obj(), "5.41", client_quote_id=cqid)
    assert posts(session) == []


def test_send_quote_takes_an_rfq_only(maker, session):
    with pytest.raises(crx.BadRequest, match="send_quote\\(\\) takes an Rfq"):
        maker.send_quote(frame(), "5.41")
    assert posts(session) == []



# ---------- send_quote: the maker's own premium and expiry ----------

DAY_MS = 86_400_000
QV1_EXPIRY = QV1["half"]["expiry"]  # 101 days after T0


class NoOffset(tzinfo):
    """A tzinfo whose offset is None: a naive datetime in all but name."""

    def utcoffset(self, dt):
        return None


def no_signature(*_):
    pytest.fail("a Quote digest was built: the refusal came after the signing step")


def test_the_documented_loop_refuses_the_maker_taker_skim(maker, gw, session, monkeypatch):
    # A seat that is maker and taker opens an RFQ where the maker pays 2 % and settles in 92 days.
    monkeypatch.setattr(e7, "quote_digest", no_signature)
    rfq = rfq_obj(premium_bps=-200, expiry=(T0 + 92 * 86_400) * 1000)
    with pytest.raises(crx.RefusedToSign) as e:
        maker.send_quote(rfq, "5.41")
    assert "premium" in str(e.value) and "-200" in str(e.value)
    assert posts(session) == [] and gw.bodies == [] and maker._legs == {}


@pytest.mark.parametrize("served, own", [(-200, 0), (200, 0), (1, 0), (-200, -100), (0, -200), (1, -1)])
def test_send_quote_refuses_a_premium_other_than_its_own(maker, gw, session, monkeypatch, served, own):
    monkeypatch.setattr(e7, "quote_digest", no_signature)
    kw = {} if own == 0 else {"premium_bps": own}
    with pytest.raises(crx.RefusedToSign, match="premium"):
        maker.send_quote(rfq_obj(premium_bps=served), "5.41", **kw)
    assert posts(session) == [] and gw.bodies == [] and maker._legs == {}


def test_send_quote_signs_the_premium_it_names(maker, session, clock):
    gw = Gateway(session, clock, premium_bps=-200)
    q = maker.send_quote(rfq_obj(premium_bps=-200), "5.41", premium_bps=-200)
    assert q.leg["premium_bps"] == -200 and len(gw.bodies) == 1
    assert recover(gw.digests[0], gw.bodies[0]["sig"]) == SEAT  # the gateway rebuilt the Quote over -200


def test_send_quote_refuses_a_served_expiry_other_than_the_rfqs_own(maker, gw, session, monkeypatch):
    # The Rfq the maker priced shows one settlement; the terms it would sign name another.
    monkeypatch.setattr(e7, "quote_digest", no_signature)
    rfq = rfq_obj()
    rfq.raw["expiry"] = QV1_EXPIRY + 92 * DAY_MS
    with pytest.raises(crx.RefusedToSign, match="expiry"):
        maker.send_quote(rfq, "5.41")
    assert posts(session) == [] and gw.bodies == [] and maker._legs == {}


def test_send_quote_refuses_an_rfq_shown_with_another_expiry(maker, gw, session, monkeypatch):
    monkeypatch.setattr(e7, "quote_digest", no_signature)
    rfq = dataclasses.replace(rfq_obj(), expiry=ms_to_dt(QV1_EXPIRY - DAY_MS))
    with pytest.raises(crx.RefusedToSign, match="expiry"):
        maker.send_quote(rfq, "5.41")
    assert posts(session) == [] and gw.bodies == []


def test_send_quote_by_default_signs_the_expiry_the_rfq_shows(maker, gw):
    rfq = rfq_obj()
    q = maker.send_quote(rfq, "5.41")
    assert q.leg["expiry"] == QV1_EXPIRY and q.expiry == rfq.expiry


@pytest.mark.parametrize("expiry", [ms_to_dt(QV1_EXPIRY), QV1_EXPIRY, timedelta(days=102),
                                    timedelta(milliseconds=QV1_EXPIRY - T0 * 1000)],
                         ids=["datetime", "unix_ms", "timedelta", "timedelta_to_the_ms"])
def test_send_quote_quotes_the_expiry_it_names(maker, gw, expiry):
    q = maker.send_quote(rfq_obj(), "5.41", expiry=expiry)
    assert q.leg["expiry"] == QV1_EXPIRY and len(gw.bodies) == 1


@pytest.mark.parametrize("expiry", [ms_to_dt(QV1_EXPIRY - 1000), QV1_EXPIRY + 1, QV1_EXPIRY - DAY_MS,
                                    timedelta(days=101), timedelta(milliseconds=QV1_EXPIRY - T0 * 1000 - 1)],
                         ids=["datetime", "unix_ms_later", "unix_ms_earlier", "timedelta", "timedelta_1_ms_short"])
def test_send_quote_refuses_an_expiry_other_than_the_one_it_names(maker, gw, session, monkeypatch, expiry):
    monkeypatch.setattr(e7, "quote_digest", no_signature)
    with pytest.raises(crx.RefusedToSign, match="expiry"):
        maker.send_quote(rfq_obj(), "5.41", expiry=expiry)
    assert posts(session) == [] and gw.bodies == [] and maker._legs == {}


def test_a_named_expiry_takes_the_place_of_the_rfqs_own(maker, session, clock):
    later = QV1_EXPIRY + 30 * DAY_MS
    gw = Gateway(session, clock, expiry=later)
    rfq = rfq_obj(expiry=later)
    rfq = dataclasses.replace(rfq, expiry=ms_to_dt(QV1_EXPIRY))
    q = maker.send_quote(rfq, "5.41", expiry=ms_to_dt(later))
    assert q.leg["expiry"] == later and len(gw.bodies) == 1


def test_an_rfq_that_shows_no_expiry_quotes_on_the_premium_and_logs_the_served_one(maker, gw, caplog):
    rfq = dataclasses.replace(rfq_obj(), expiry=None)
    with caplog.at_level(logging.WARNING, logger="crx"):
        q = maker.send_quote(rfq, "5.41")
    assert q.leg["expiry"] == QV1_EXPIRY
    day = ms_to_dt(QV1_EXPIRY).strftime("%Y-%m-%d")
    assert any("expiry" in r.getMessage() and day in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("shown", [datetime(2027, 1, 1), "2027-01-01", QV1_EXPIRY], ids=["naive", "text", "int"])
def test_an_rfq_that_shows_an_unreadable_expiry_is_refused(maker, gw, session, shown):
    rfq = dataclasses.replace(rfq_obj(), expiry=shown)
    with pytest.raises(crx.RefusedToSign, match="expiry"):
        maker.send_quote(rfq, "5.41")
    assert posts(session) == []


def test_an_rfq_that_shows_no_expiry_still_refuses_another_premium(maker, gw, session):
    rfq = dataclasses.replace(rfq_obj(premium_bps=-200), expiry=None)
    with pytest.raises(crx.RefusedToSign, match="premium"):
        maker.send_quote(rfq, "5.41")
    assert posts(session) == []


@pytest.mark.parametrize("kw, words", [
    (dict(premium_bps=True), "premium_bps"),
    (dict(premium_bps="-200"), "premium_bps"),
    (dict(premium_bps=-200.0), "premium_bps"),
    (dict(premium_bps=32_768), "premium_bps"),
    (dict(premium_bps=-32_769), "premium_bps"),
    (dict(premium_bps=None), "premium_bps"),
    (dict(expiry="2027-01-01"), "expiry"),
    (dict(expiry=True), "expiry"),
    (dict(expiry=1.5), "expiry"),
    (dict(expiry=datetime(2027, 1, 1)), "timezone"),
    (dict(expiry=datetime(2027, 1, 1, tzinfo=NoOffset())), "timezone"),
    (dict(expiry=timedelta(0)), "expiry"),
    (dict(expiry=-1), "expiry"),
])
def test_send_quote_refuses_bad_own_terms_before_any_call(maker, gw, session, kw, words):
    with pytest.raises(crx.BadRequest, match=words):
        maker.send_quote(rfq_obj(), "5.41", **kw)
    assert posts(session) == [] and gw.bodies == [] and maker._legs == {}


# ---------- send_quote: the gateway's refusals ----------

def test_quote_format_outdated_is_typed(maker, gw):
    gw.answer = refusal("quote_format_outdated", 400, "the quote is signed in an older format")
    with pytest.raises(crx.QuoteFormatOutdated) as e:
        maker.send_quote(rfq_obj(), "5.41")
    assert isinstance(e.value, crx.BadRequest)
    assert (e.value.code, e.value.gateway_code, e.value.status) == ("quote_format_outdated", "quote_format_outdated", 400)
    assert str(e.value) == "the quote is signed in an older format"


@pytest.mark.parametrize("status, body, err, code", [
    (422, {"code": "insufficient_collateral", "error": "short"}, crx.InsufficientCollateral, "insufficient_collateral"),
    (410, {"code": "rfq_expired", "error": "lapsed"}, crx.QuoteExpired, "quote_expired"),
    (403, {"code": "not_whitelisted", "error": "no"}, crx.NotWhitelisted, "not_whitelisted"),
    (409, {"code": "conflict", "error": "rfq is Accepted"}, crx.CrxError, "conflict"),
    (400, {"code": "bad_request", "error": "bad leg"}, crx.BadRequest, "bad_request"),
])
def test_send_quote_refusals_are_typed(maker, gw, status, body, err, code):
    gw.answer = (status, body)
    with pytest.raises(err) as e:
        maker.send_quote(rfq_obj(), "5.41")
    assert (e.value.code, e.value.status, str(e.value)) == (code, status, body["error"])
    assert not isinstance(e.value, crx.QuoteFormatOutdated)


def test_trade_refuses_a_maker_quote(maker, gw):
    q = maker.send_quote(rfq_obj(), "5.41")
    with pytest.raises(crx.BadRequest, match="trade\\(\\) takes the Quote that quote\\(\\) returned"):
        maker.trade(q)


def test_the_maker_types_are_exported():
    assert crx.MakerQuote and crx.Rfq and crx.Drop and issubclass(crx.QuoteLost, crx.CrxError)
    assert issubclass(crx.QuoteFormatOutdated, crx.BadRequest) and crx.QuoteFormatOutdated.code == "quote_format_outdated"
