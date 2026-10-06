"""REST authentication against the shared auth vector: the message, the header set and the signature,
byte for byte; one stamp per call; a delegate always names itself."""

import hashlib
import json
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

import crx
from crx import _http
from crx._http import Gateway, rest_message

from .conftest import BASE, FIX, Clock, FakeSession

PATH = FIX / "auth-vector-v5.json"
SHA256 = "cb93851ca9d4525d61de47d7d89f9c239dc12af2211af7018e74e464894e5bb6"
SPEC = Path.home() / "crx-scratch" / "readable-taker-terms-2026-10-01" / "spec"
VEC = json.loads(PATH.read_text())
CASES = {c["id"]: c for c in VEC["cases"]}
KEYS = json.loads((FIX / "format-vectors-v5.json").read_text())["keys"]
TAKER, OTHER = KEYS["taker"], KEYS["other"]
SIGNED_HEADERS = {"x-crx-address", "x-crx-ts", "x-crx-sig"}


def eip191(message: str) -> bytes:
    raw = message.encode()
    return keccak(b"\x19Ethereum Signed Message:\n" + str(len(raw)).encode() + raw)


def gateway(key, ts_ms, custody=None, session=None):
    """A gateway signing with ``key``, its clock at ``ts_ms``; ``custody`` set for a delegate."""
    gw = Gateway(BASE, Account.from_key(key["private_key"]), session)
    gw._clock = Clock(ts_ms / 1000)
    gw.custody = custody
    return gw


def at(case):
    """Stamp the next call at the case's ts exactly."""
    ts = int(case["headers"]["x-crx-ts"])
    _http._last_stamp = ts - 1
    return ts


# ---------- the file ----------

def test_fixture_is_the_pinned_file():
    assert hashlib.sha256(PATH.read_bytes()).hexdigest() == SHA256


def test_fixture_is_the_spec_copy():
    if not SPEC.is_dir():
        pytest.skip("no spec folder on this machine")
    assert PATH.read_bytes() == (SPEC / "auth-vector-v5.json").read_bytes()


def test_shape_and_empty_body():
    assert VEC["shape"] == _http.AUTH_SHAPE == "signer_line"
    assert "0x" + keccak(b"").hex() == VEC["empty_body_hash"] == CASES["A2"]["body_hash"]


# ---------- A1, A2, A3: the message, the header set, the signature ----------

@pytest.mark.parametrize("cid,key,custody", [
    ("A1", TAKER, None), ("A2", TAKER, None), ("A3", OTHER, TAKER["address"]),
], ids=["A1-self-custody", "A2-no-body", "A3-delegate"])
def test_case_reproduced(cid, key, custody):
    c = CASES[cid]
    raw = c["body"].encode()
    assert "0x" + keccak(raw).hex() == c["body_hash"]
    ts = at(c)
    gw = gateway(key, ts, custody)
    h = gw.headers(c["method"], c["path"], raw)
    assert h == c["headers"]
    want = SIGNED_HEADERS | ({"x-crx-signer"} if custody else set())
    assert set(h) == want
    msg = rest_message(c["method"], c["path"], h["x-crx-address"], key["address"], ts, raw)
    assert msg == c["message"] and len(msg.split("\n")) == c["message_lines"] == 8 and not msg.endswith("\n")
    assert msg.split("\n")[5] == f"Signer: {c['signer_line']}" and c["signer_line"] == key["address"]
    assert "0x" + eip191(msg).hex() == c["eip191_digest"] and c["replay_key"] == "env:" + c["eip191_digest"]
    assert h["x-crx-sig"] == c["signature"]
    assert Account.recover_message(encode_defunct(text=msg), signature=h["x-crx-sig"]).lower() == c["recovered"]


def test_a1_sent_on_the_wire():
    """The body sent is the exact bytes the Body line hashes; the headers are the vector's."""
    c = CASES["A1"]
    s = FakeSession()
    s.routes[("POST", "/rfqs")] = {"rfq_id": "r"}
    ts = at(c)
    gateway(TAKER, ts, session=s).raw_request("POST", "/rfqs", body=json.loads(c["body"]))
    (call,) = s.calls
    assert call["raw"] == c["body"].encode()
    assert call["headers"] == {"accept": "application/json", "content-type": "application/json", **c["headers"]}


def test_a1_a2_a3_are_one_stamp_sequence():
    """One process, one clock millisecond: each call takes the next stamp, across gateways."""
    a1, a2, a3 = CASES["A1"], CASES["A2"], CASES["A3"]
    ts = int(a1["headers"]["x-crx-ts"])
    own, delegate = gateway(TAKER, ts), gateway(OTHER, ts, TAKER["address"])
    assert own.headers("POST", a1["path"], a1["body"].encode()) == a1["headers"]
    assert own.headers("GET", a2["path"]) == a2["headers"]
    assert delegate.headers("POST", a3["path"], a3["body"].encode()) == a3["headers"]


# ---------- R1, R2: never a byte-identical resend ----------

def test_r1_same_clock_ms_gets_the_next_stamp_r2():
    r1, r2 = CASES["R1"], CASES["R2"]
    assert r1["headers"] == CASES["A1"]["headers"] and r1["expect"].startswith("refused")
    ts = int(r1["headers"]["x-crx-ts"])
    gw = gateway(TAKER, ts)
    raw = r1["body"].encode()
    first, second = gw.headers("POST", "/rfqs", raw), gw.headers("POST", "/rfqs", raw)
    assert (int(first["x-crx-ts"]), int(second["x-crx-ts"])) == (ts, ts + 1)
    assert first == r1["headers"] and second == r2["headers"] and first["x-crx-sig"] != second["x-crx-sig"]
    assert rest_message("POST", "/rfqs", TAKER["address"], TAKER["address"], ts + 1, raw) == r2["message"]


def test_stamp_never_goes_back_when_the_clock_does():
    gw = gateway(TAKER, 1_790_000_000_000)
    first = int(gw.headers("GET", "/balance")["x-crx-ts"])
    gw._clock.t -= 5
    assert int(gw.headers("GET", "/balance")["x-crx-ts"]) == first + 1


# ---------- N1: a delegate always names itself ----------

def test_n1_delegate_call_always_sends_x_crx_signer():
    n1 = CASES["N1"]
    assert n1["recovered"] == OTHER["address"] != n1["signer_line"] == TAKER["address"]
    assert Account.recover_message(encode_defunct(text=n1["message"]), signature=n1["signature"]).lower() == OTHER["address"]
    ts = at(n1)
    h = gateway(OTHER, ts, TAKER["address"]).headers(n1["method"], n1["path"], n1["body"].encode())
    assert h["x-crx-signer"] == OTHER["address"] and h["x-crx-address"] == TAKER["address"]
    assert h["x-crx-ts"] == n1["headers"]["x-crx-ts"] and h["x-crx-sig"] != n1["signature"]
    msg = rest_message(n1["method"], n1["path"], TAKER["address"], OTHER["address"], ts, n1["body"].encode())
    assert f"Signer: {OTHER['address']}" in msg.split("\n")
    assert Account.recover_message(encode_defunct(text=msg), signature=h["x-crx-sig"]).lower() == OTHER["address"]


def test_self_custody_never_sends_x_crx_signer():
    gw = gateway(TAKER, 1_790_000_000_000, custody=None)
    assert "x-crx-signer" not in gw.headers("GET", "/balance")


# ---------- L1: the 9-line message is never sent ----------

def test_l1_no_nonce_header_and_no_nonce_line():
    l1 = VEC["legacy"]
    assert l1["message_lines"] == 9 and "Nonce: n-1" in l1["message"].split("\n")
    ts = int(l1["headers"]["x-crx-ts"])
    raw = l1["body"].encode()
    for gw in (gateway(TAKER, ts), gateway(OTHER, ts, TAKER["address"])):
        h = gw.headers("POST", l1["path"], raw)
        assert "x-crx-nonce" not in h and h["x-crx-sig"] != l1["signature"]
    msg = rest_message("POST", l1["path"], TAKER["address"], TAKER["address"], ts, raw)
    assert not any(line.startswith("Nonce:") for line in msg.split("\n"))
    assert msg.split("\n") == [line for line in l1["message"].split("\n") if not line.startswith("Nonce:")]


# ---------- the 7-line shape ----------

def test_no_signer_line_shape(monkeypatch):
    monkeypatch.setattr(_http, "AUTH_SHAPE", "no_signer_line")
    c = CASES["A3"]
    ts = at(c)
    raw = c["body"].encode()
    h = gateway(OTHER, ts, TAKER["address"]).headers(c["method"], c["path"], raw)
    msg = rest_message(c["method"], c["path"], TAKER["address"], OTHER["address"], ts, raw)
    lines = msg.split("\n")
    assert len(lines) == 7 and not any(line.startswith("Signer:") for line in lines)
    assert lines == [line for line in c["message"].split("\n") if not line.startswith("Signer:")]
    assert Account.recover_message(encode_defunct(text=msg), signature=h["x-crx-sig"]).lower() == OTHER["address"]
    assert h["x-crx-sig"] != c["signature"]
    monkeypatch.undo()
    assert _http.AUTH_SHAPE == "signer_line"
    assert rest_message(c["method"], c["path"], TAKER["address"], OTHER["address"], ts, raw) == c["message"]


def test_no_key_signs_nothing():
    gw = Gateway(BASE, None, FakeSession())
    with pytest.raises(crx.ConfigError, match="needs the seat key"):
        gw.headers("GET", "/balance")
