"""REST authentication against the shared auth vector, byte for byte."""

import hashlib
import json

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

from crx import _http
from crx._http import Gateway, rest_message

from .conftest import BASE, FIX, Clock, FakeSession

PATH = FIX / "auth-vector-v5.json"
SHA256 = "cb93851ca9d4525d61de47d7d89f9c239dc12af2211af7018e74e464894e5bb6"
VEC = json.loads(PATH.read_text())
CASES = {c["id"]: c for c in VEC["cases"]}
KEYS = json.loads((FIX / "format-vectors-v5.json").read_text())["keys"]
TAKER, OTHER = KEYS["taker"], KEYS["other"]
SIGNED_HEADERS = {"x-crx-address", "x-crx-ts", "x-crx-sig"}


def eip191(message: str) -> bytes:
    raw = message.encode()
    return keccak(b"\x19Ethereum Signed Message:\n" + str(len(raw)).encode() + raw)


def gateway(key, ts_ms, custody=None, session=None):
    """Return a gateway that signs with ``key`` at ``ts_ms``, as a delegate when ``custody`` is set."""
    gw = Gateway(BASE, Account.from_key(key["private_key"]), session)
    gw._clock = Clock(ts_ms / 1000)
    gw.custody = custody
    return gw


def at(case):
    """Make the next call carry the case's timestamp exactly."""
    ts = int(case["headers"]["x-crx-ts"])
    _http._last_stamp = ts - 1
    return ts


def test_fixture_is_the_pinned_file():
    assert hashlib.sha256(PATH.read_bytes()).hexdigest() == SHA256
    assert VEC["shape"] == _http.AUTH_SHAPE == "signer_line"
    assert "0x" + keccak(b"").hex() == VEC["empty_body_hash"] == CASES["A2"]["body_hash"]


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
    """The sent body is the exact bytes that the Body line hashes, with the vector's headers."""
    c = CASES["A1"]
    s = FakeSession()
    s.routes[("POST", "/rfqs")] = {"rfq_id": "r"}
    ts = at(c)
    gateway(TAKER, ts, session=s).raw_request("POST", "/rfqs", body=json.loads(c["body"]))
    (call,) = s.calls
    assert call["raw"] == c["body"].encode()
    assert call["headers"] == {"accept": "application/json", "content-type": "application/json", **c["headers"]}


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
