"""REST authentication against the shared auth vector, byte for byte."""

import json

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

import crx
from crx import _http
from crx._http import rest_message

from .conftest import BASE, FIX, Clock, FakeSession

PATH = FIX / "auth-vector-v5.json"
VEC = json.loads(PATH.read_text())
CASES = {c["id"]: c for c in VEC["cases"]}
KEYS = json.loads((FIX / "format-vectors-v5.json").read_text())["keys"]
TAKER, OTHER = KEYS["taker"], KEYS["other"]


def at(case):
    """Make the next call carry the case's timestamp exactly."""
    ts = int(case["headers"]["x-crx-ts"])
    _http._last_stamp = ts - 1
    return ts


def client_gateway(network, key, ts_ms, custody, tmp_path):
    """Return the gateway of a ``network`` client that signs with ``key`` at ``ts_ms``."""
    extra = {"allow_mainnet": True} if network == "mainnet" else {}
    c = crx.Client(key=key["private_key"], network=network, base_url=BASE, state_dir=tmp_path / "state",
                   session=FakeSession(), **extra)
    gw = c._gw
    gw._clock = Clock(ts_ms / 1000)
    gw.custody = custody
    return gw


@pytest.mark.parametrize("cid,key,custody", [
    ("A1", TAKER, None), ("A2", TAKER, None), ("A3", OTHER, TAKER["address"]),
], ids=["A1-self-custody", "A2-no-body", "A3-delegate"])
def test_a_chain_1_client_signs_the_vector_bytes_with_no_chain_line(cid, key, custody, tmp_path):
    c = CASES[cid]
    raw = c["body"].encode()
    gw = client_gateway("mainnet", key, at(c), custody, tmp_path)
    assert gw.chain_id is None
    h = gw.headers(c["method"], c["path"], raw)
    assert h == c["headers"] and "x-crx-chain" not in h
    assert rest_message(c["method"], c["path"], h["x-crx-address"], key["address"], int(h["x-crx-ts"]), raw,
                        gw.chain_id) == c["message"]


@pytest.mark.parametrize("cid,key,custody", [
    ("A1", TAKER, None), ("A2", TAKER, None), ("A3", OTHER, TAKER["address"]),
], ids=["A1-self-custody", "A2-no-body", "A3-delegate"])
def test_a_testnet_client_signs_chain_43113_after_audience_and_sends_x_crx_chain(cid, key, custody, tmp_path):
    c = CASES[cid]
    raw = c["body"].encode()
    gw = client_gateway("testnet", key, at(c), custody, tmp_path)
    assert gw.chain_id == 43113
    h = gw.headers(c["method"], c["path"], raw)
    assert h["x-crx-chain"] == "43113"
    assert {k: v for k, v in h.items() if k not in ("x-crx-chain", "x-crx-sig")} == {
        k: v for k, v in c["headers"].items() if k != "x-crx-sig"}
    lines = c["message"].split("\n")
    want = "\n".join(lines[:2] + ["Chain: 43113"] + lines[2:])
    msg = rest_message(c["method"], c["path"], h["x-crx-address"], key["address"], int(h["x-crx-ts"]), raw, 43113)
    assert msg == want and len(msg.split("\n")) == 9
    assert Account.recover_message(encode_defunct(text=msg), signature=h["x-crx-sig"]).lower() == c["recovered"]
    assert h["x-crx-sig"] != c["signature"]
