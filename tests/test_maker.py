"""The maker's reads and its binding Quote, against a scripted gateway and the maker vectors."""

import dataclasses
import hashlib
import json
import logging
from datetime import datetime, timedelta
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
RFQ = "0x" + "a1" * 32
OTHER = "0x" + "a2" * 32
TXH = "0x" + "55" * 32
TRADE = "0x" + "9a" * 32
PAIRS = {e7.h0x(keccak(text=p)): p for p in ("USD/BRL", "USD/MXN", "USD/PHP")}
QUOTE_TYPE = ("Quote(address seat,bytes32 legId,bytes32 pair,int8 side,uint256 notional,uint64 rate,int16 premiumBps,"
              "uint40 expiry,uint64 nonce,bytes32 salt,bytes32 takerRef)")
QUOTE_TYPE_WITH_END = QUOTE_TYPE.replace("uint64 nonce,", "uint64 nonce,uint64 quoteExpiry,")


def d6(units) -> str:
    """Format integer micro-units as a 6-decimal string: 5410000 -> "5.410000"."""
    n = int(units)
    return f"{n // 10**6}.{n % 10**6:06d}"


def project(fv: dict) -> dict:
    """Project the format vectors' quotes onto the maker half that the SDK signs.

    Regenerate maker-vectors.json:
    python -c "import json, tests.test_maker as t; open('tests/fixtures/maker-vectors.json', 'w')
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


VEC = project(FV)  # a test checks the file against this, byte for byte
DOMAIN = VEC["domain"]
SEP = bytes.fromhex(DOMAIN["separator"][2:])
QV1 = VEC["quotes"][0]
IDS = [q["id"] for q in VEC["quotes"]]


def from_file(qid: str) -> dict:
    """Return quote ``qid`` as maker-vectors.json holds it."""
    return next(q for q in fixture(VEC_FILE)["quotes"] if q["id"] == qid)


def test_maker_vectors_are_the_projection_of_the_format_vectors():
    assert hashlib.sha256((FIX / "format-vectors-v5.json").read_bytes()).hexdigest() == FV_SHA256
    raw = (FIX / VEC_FILE).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == VEC_SHA256
    assert raw.decode() == json.dumps(project(FV), indent=1) + "\n"
    assert IDS == ["QV1", "QV2", "QV3", "QV4", "QV5"]


def b32(word: str) -> bytes:
    return bytes.fromhex(word[2:])


def tail(leg_id: str) -> int:
    """Return the quote end in a leg id: bytes 24 to 31, big endian."""
    return int.from_bytes(b32(leg_id)[24:], "big")


def units6(text) -> int:
    whole, _, frac = str(text).partition(".")
    return int(whole) * 10**6 + int(frac.ljust(6, "0") or "0")


def by_hand(seat, leg_id, pair, side, notional, rate, premium_bps, expiry_ms, nonce, salt, taker_ref,
            quote_expiry=None) -> bytes:
    """Build the Quote digest without the SDK, with ``quote_expiry`` after the nonce when given."""
    kinds = ["bytes32", "address", "bytes32", "bytes32", "int8", "uint256", "uint64", "int16", "uint40", "uint64"]
    words = [keccak(text=QUOTE_TYPE if quote_expiry is None else QUOTE_TYPE_WITH_END), to_checksum_address(seat),
             b32(leg_id), keccak(text=pair), int(side), units6(notional), units6(rate), int(premium_bps),
             int(expiry_ms) // 1000, int(nonce)]
    if quote_expiry is not None:
        kinds.append("uint64")
        words.append(int(quote_expiry))
    struct = keccak(encode(kinds + ["bytes32", "bytes32"], words + [b32(salt), b32(taker_ref)]))
    return keccak(b"\x19\x01" + SEP + struct)


def recover(digest, sig: str) -> str:
    return Account._recover_hash(b32(digest) if isinstance(digest, str) else digest, signature=sig).lower()


def frame(v=QV1, **edit):
    """Return an ``rfq.opened`` frame on vector ``v``'s terms, as a maker reads it, with the maker's own side.

    An edit to None leaves that key out.
    """
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
    """Fix the leg id's 24 random bytes and the salt for the next quote."""
    monkeypatch.setattr(_maker.secrets, "token_bytes", lambda n: {24: head, 32: salt}[n])


class Gateway:
    """Script one RFQ: quote POST and DELETE with the gateway's checks in order, and the maker's view.

    The POST rebuilds the Quote by hand, without the SDK. ``derive=False`` takes the body's nonce
    instead of leg id bytes 16 to 23. ``with_end=True`` hashes the body's quote_expiry after the nonce.
    """

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
        """End the RFQ with ``status`` and mark each quote still ``quoted`` as ``dropped``."""
        self.status, self.live = status, None
        for row in self.rows:
            if row["status"] == "quoted":
                row["status"] = "dropped"

    def accept(self, row=None):
        """Accept this seat's quote ``row``, by default its newest."""
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
    """Return a maker client on keys.other, on avax-fuji with the vectors' domain and core."""
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


def test_rfq_reads_the_taker_view_of_that_rfq_only(maker, session):
    view = frame(side=1, client_rfq_id="cid-1", kind="open", status="quoted")
    view["quotes"] = [{"quote_id": "0x" + "44" * 32, "rate": "5.41", "status": "quoted", "client_quote_id": "q1"},
                      {"quote_id": "0x" + "45" * 32, "rate": "5.42", "status": "quoted", "client_quote_id": "q2"}]
    session.routes[("GET", f"/rfqs/{RFQ}")] = view
    r = maker.rfq(RFQ.upper().replace("0X", "0x"))
    assert (r.client_rfq_id, r.own, r.side, r.taker_side, r.status, r.seq) == ("cid-1", True, "buy", "buy", "quoted", None)
    assert [q["rate"] for q in r.quotes] == ["5.41", "5.42"]
    session.routes[("GET", f"/rfqs/{RFQ}")] = frame(rfq_id=OTHER)
    with pytest.raises(crx.BadAnswer, match="answered for another RFQ"):
        maker.rfq(RFQ)


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
              opened(frame(rfq_id=other_id(2), chain="ethereum"), 2),            # another chain
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


def test_rfqs_catch_up_error_raises_at_the_call(maker, session):
    session.routes[("GET", "/trades")] = (503, {"code": "upstream", "error": "down"})
    with pytest.raises(crx.ServerError):
        maker.rfqs()


def test_send_quote_posts_each_vector_quote(maker, session, clock, monkeypatch):
    for qid in IDS:
        v = from_file(qid)
        h = v["half"]
        clock.t = v["quote_expiry"] - 600
        gw = Gateway(session, clock, v)
        fake_random(monkeypatch, b32(h["leg_id"])[:24], b32(v["salt"]))
        q = maker.send_quote(_maker.rfq_of(frame(v), 7), h["rate"], premium_bps=h["premium_bps"])
        (body,) = gw.bodies
        rate = h["rate"].rstrip("0").rstrip(".")
        assert body == {"rate": rate, "leg_id": h["leg_id"], "salt": v["salt"], "sig": v["sig"]}, qid
        assert gw.digests == [b32(v["digest"])], qid
        assert e7.leg_words(q.leg) == e7.leg_words(h) and q.leg["nonce"] == h["nonce"] and "salt" not in q.leg, qid
        assert (q.quote_id, q.rate, q.notional, q.side, q.client_quote_id) == (
            gw.rows[0]["quote_id"], Decimal(h["rate"]), Decimal(h["notional"]), "buy" if h["side"] == 1 else "sell",
            None), qid
        assert q.quote_expiry.timestamp() == v["quote_expiry"] and q.expiry.timestamp() * 1000 == h["expiry"], qid
        assert maker._legs == {RFQ: h["leg_id"]}, qid


def test_with_derive_maker_nonce_off_the_body_carries_the_nonce(maker, gw, monkeypatch):
    monkeypatch.setattr(e7, "DERIVE_MAKER_NONCE", False)
    gw.derive = False
    q = maker.send_quote(rfq_obj(), "5.41")
    (body,) = gw.bodies
    assert set(body) == {"rate", "leg_id", "salt", "sig", "nonce", "client_quote_id"}
    sent = body["client_quote_id"]
    assert sent.startswith("sdk-q-")
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


DAY_MS = 86_400_000
QV1_EXPIRY = QV1["half"]["expiry"]  # 101 days after T0


def no_signature(*_):
    pytest.fail("a Quote digest was built: the refusal came after the signing step")


REFUSALS = [
    ("no_taker_ref", dict(taker_ref=None), crx.RefusedToSign, "names no taker_ref or quote_expiry_max"),
    ("quote_window_under_60_s", dict(quote_expiry_max=T0 + 59), crx.QuoteLost,
     "under 60 s of the RFQ's quote window is left; nothing signed"),
    ("quote_end_over_630_s_out", dict(quote_expiry_max=T0 + 631), crx.RefusedToSign, "more than 630 s ahead"),
    ("wrong_chain", dict(chain="ethereum"), crx.BadRequest, "the RFQ is on ethereum; this client signs for avax-fuji"),
    ("close_rfq", dict(kind="close"), crx.BadRequest, "a close RFQ takes a price-only quote"),
    ("own_rfq", dict(client_rfq_id="cid-1"), crx.BadRequest, "this is your own RFQ"),
    ("side_zero", dict(side=0), crx.RefusedToSign, "the RFQ names no side for this seat"),
    ("notional_negative", dict(notional="-5"), crx.RefusedToSign, "the RFQ's notional cannot be read"),
    ("notional_7_decimals", dict(notional="1.0000001"), crx.RefusedToSign, "the RFQ's notional cannot be read"),
    ("expiry_past", dict(expiry=T0 * 1000 - 1), crx.RefusedToSign, "the RFQ's expiry is not in the future"),
    ("pair", dict(pair="USD"), crx.RefusedToSign, "the RFQ names no AAA/BBB pair"),
    ("premium_out_of_range", dict(premium_bps=32_768), crx.RefusedToSign, "the RFQ's premium_bps cannot be read"),
    # The documented loop: a seat that is maker and taker opens an RFQ where the maker pays 2 % and settles in 92 days.
    ("maker_taker_skim", dict(premium_bps=-200, expiry=(T0 + 92 * 86_400) * 1000), crx.RefusedToSign,
     "premium is -200 bps"),
]


def test_send_quote_refuses_before_signing(maker, gw, session, monkeypatch):
    monkeypatch.setattr(e7, "quote_digest", no_signature)
    for row, edit, err, words in REFUSALS:
        with pytest.raises(err) as e:
            maker.send_quote(rfq_obj(**edit), "5.41")
        assert words in str(e.value), row
    assert posts(session) == [] and gw.bodies == [] and maker._legs == {}


BAD_ARGUMENTS = [  # rate, send_quote keywords, words
    ("18.1234567", {}, "rate"),
    ("0", {}, "rate"),
    ("abc", {}, "rate"),
    (str(2**64), {}, "rate"),
    ("5.41", dict(premium_bps=True), "premium_bps"),
    ("5.41", dict(expiry="2027-01-01"), "expiry"),
    ("5.41", dict(expiry=datetime(2027, 1, 1)), "timezone"),
]


def test_send_quote_and_quote_live_refuse_bad_arguments_before_any_call(maker, gw, session):
    for rate, kw, words in BAD_ARGUMENTS:
        with pytest.raises(crx.BadRequest) as e:
            maker.send_quote(rfq_obj(), rate, **kw)
        assert words in str(e.value), (rate, kw)
    with pytest.raises(crx.BadRequest):
        maker.send_quote("not an rfq", "18.1")
    with pytest.raises(crx.BadRequest):
        maker.quote_live(None, seconds=10, log=None)
    assert session.calls == [] and gw.bodies == [] and maker._legs == {}


EXPIRY_OTHER = [  # row, served expiry, send_quote keywords
    ("datetime", QV1_EXPIRY, dict(expiry=ms_to_dt(QV1_EXPIRY - 1000))),
    ("timedelta", QV1_EXPIRY, dict(expiry=timedelta(days=101))),
    ("rfqs_own", QV1_EXPIRY + 92 * DAY_MS, {}),  # a settlement other than the one the maker priced
]


def test_send_quote_refuses_an_expiry_other_than_the_one_it_names(maker, gw, session, monkeypatch):
    monkeypatch.setattr(e7, "quote_digest", no_signature)
    for row, served, kw in EXPIRY_OTHER:
        rfq = rfq_obj()
        rfq.raw["expiry"] = served
        with pytest.raises(crx.RefusedToSign) as e:
            maker.send_quote(rfq, "5.41", **kw)
        assert "expiry" in str(e.value), row
    with pytest.raises(crx.RefusedToSign, match="expiry"):
        maker.send_quote(dataclasses.replace(rfq_obj(), expiry=datetime(2027, 1, 1)), "5.41")
    assert posts(session) == [] and gw.bodies == [] and maker._legs == {}


def test_send_quote_quotes_the_expiry_it_names_or_the_served_one(maker, gw, caplog):
    for n, expiry in enumerate([QV1_EXPIRY, timedelta(days=102)], 1):
        q = maker.send_quote(rfq_obj(), "5.41", expiry=expiry)
        assert q.leg["expiry"] == QV1_EXPIRY and len(gw.bodies) == n, expiry
    rfq = dataclasses.replace(rfq_obj(), expiry=None)
    with caplog.at_level(logging.WARNING, logger="crx"):
        q = maker.send_quote(rfq, "5.41")
    assert q.leg["expiry"] == QV1_EXPIRY
    day = ms_to_dt(QV1_EXPIRY).strftime("%Y-%m-%d")
    assert any("expiry" in r.getMessage() and day in r.getMessage() for r in caplog.records)


SETTLED = [{"type": "trade.settled", "seq": n, "ts": 1, "data": {}} for n in range(1, 5)]


def live_tape(session, seen_at_call=(), later=(opened(frame(), 6),), down=()):
    """Serve GET /trades: the first read holds SETTLED and ``seen_at_call``, and later reads add ``later``.

    Each read answers the rows after its ``after``. Reads numbered in ``down`` answer 503.
    """
    reads = []

    def read(req):
        reads.append(req)
        if len(reads) in down:
            return (503, {"code": "upstream", "error": "down"})
        rows = [*SETTLED, *seen_at_call] + ([] if len(reads) == 1 else list(later))
        after = int(req["query"]["after"][0])
        return tape([r for r in rows if r["seq"] > after], max(r["seq"] for r in rows))
    session.routes[("GET", "/trades")] = read
    return reads


def deletes(session):
    return [c for c in session.calls if c["method"] == "DELETE"]


def body(lines):
    """Return the log lines without their time stamps."""
    assert all(len(line) > 9 and line[2] == line[5] == ":" and line[8] == " " for line in lines)
    return [line[9:] for line in lines]


def test_quote_live_quotes_new_rfqs_and_returns_the_trade(maker, gw, session, clock):
    # Only the RFQ that opens after the call is quoted.
    live_tape(session, seen_at_call=[opened(frame(rfq_id=OTHER), 4)])
    gw.accept_at = 1
    priced, lines = [], []

    def price(r):
        priced.append(r.rfq_id)
        return Decimal("5.41")
    trades = maker.quote_live(price, seconds=10, log=lines.append)
    assert priced == [RFQ]
    (t,) = trades
    assert (t.status, t.tx, t.rfq_id, t.rate, t.side) == ("open", TXH, RFQ, Decimal("5.41"), "sell")
    assert body(lines) == [
        SEAT,
        "rfq 0xa1a1a1a1 USD/BRL sell 5000000.000000 5.41",
        "quoted 0xa1a1a1a1 5.41",
        "trade 0xa1a1a1a1 USD/BRL sell 5000000.000000 5.41 open",
    ]
    assert len(gw.bodies) == 1 and deletes(session) == []


def test_quote_live_drops_every_live_quote_on_ctrl_c(maker, gw, session, clock):
    live_tape(session)

    def log(line):
        if " quoted " in line:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        maker.quote_live(lambda r: "5.41", seconds=60, log=log)
    assert len(deletes(session)) == 1 and list(gw.dropped) == [gw.bodies[0]["leg_id"]]
