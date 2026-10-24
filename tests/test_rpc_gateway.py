"""The mainnet default RPC: the gateway's /rpc route, each call signed over the exact body bytes it sends."""

import json

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

import crx
from crx import _eip712 as e7
from crx._chain import Rpc, send_tx
from crx._http import Gateway, rest_message
from crx.client import MAINNET_CORE

from .conftest import BASE, RPC, Pauses

CORE = MAINNET_CORE.lower()
TOKEN = "0x" + "a0" * 20
TX = "0x" + "ab" * 32
SIGNED = {"x-crx-address", "x-crx-ts", "x-crx-sig"}
TESTNET_RPC = "https://api.avax-test.network/ext/bc/C/rpc"


def mainnet_health():
    return {"status": "ok", "chains": [{
        "key": "ethereum", "chain_id": 1, "core": CORE, "base_token": TOKEN,
        "domain": e7.h0x(e7.domain_separator(1, CORE)),
    }]}


def recover(h, raw):
    """Return the address that signed the CRX-REST-LOGIN message of ``POST /rpc`` over ``raw``."""
    msg = rest_message("POST", "/rpc", h["x-crx-address"], h.get("x-crx-signer", h["x-crx-address"]),
                       int(h["x-crx-ts"]), raw)
    return Account.recover_message(encode_defunct(text=msg), signature=h["x-crx-sig"]).lower()


class Node:
    """Answer ``POST /rpc`` as the gateway does: recover the signer over the exact body, then answer."""

    def __init__(self, answers):
        self.answers = answers
        self.seen = []  # (signer, headers, raw body)

    def __call__(self, req):
        h, raw = req["headers"], req["raw"]
        self.seen.append((recover(h, raw), h, raw))
        call = json.loads(raw)
        out = self.answers[call["method"]]
        out = out(call["params"]) if callable(out) else out
        return {"jsonrpc": "2.0", "id": call["id"], "result": out}


CHAIN = {"eth_chainId": "0x1", "eth_getCode": lambda p: "0x6080" if p[0].lower() == CORE else "0x"}


@pytest.fixture(autouse=True)
def no_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env file
    for k in ("CRX_ALLOW_MAINNET", "CRX_BASE", "CRX_RPC", "CRX_STATE_DIR", "CRX_WALLET_PK", "CRX_WALLET_PK_FILE"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def gw_session(session):
    session.routes[("GET", "/health")] = mainnet_health()
    return session


def main(session, tmp_path, account, **kw):
    args = dict(key=account.key.hex(), network="mainnet", allow_mainnet=True, base_url=BASE,
                state_dir=tmp_path / "state", session=session)
    args.update(kw)
    return crx.Client(**args)


def test_mainnet_with_no_rpc_builds_a_client_on_the_gateway_rpc(gw_session, tmp_path, account, monkeypatch):
    c = main(gw_session, tmp_path, account)
    assert c._rpc._gw is c._gw and c._rpc._url == BASE + "/rpc" and c._rpc.receipt_poll == 3.0
    monkeypatch.setenv("CRX_BASE", BASE + "/")  # the route follows CRX_BASE
    c = main(gw_session, tmp_path, account, base_url=None)
    assert c._rpc._gw is c._gw and c._rpc._url == BASE + "/rpc"
    assert repr(c._rpc) == "Rpc(<url hidden>)" and gw_session.calls == []


def test_gateway_calls_are_signed_over_the_bytes_sent(gw_session, tmp_path, account):
    node = Node(CHAIN)
    gw_session.routes[("POST", "/rpc")] = node
    c = main(gw_session, tmp_path, account)
    assert c._chain_ready()["chain_id"] == 1
    calls = [k for k in gw_session.calls if k["path"] == "/rpc"]
    assert [json.loads(k["raw"])["method"] for k in calls] == ["eth_chainId", "eth_getCode"]
    assert gw_session.rpc_calls == []  # nothing went to an RPC of the user's own
    me = account.address.lower()
    for (signer, h, raw), call in zip(node.seen, calls):
        assert SIGNED <= set(h) and "x-crx-signer" not in h and "x-crx-session" not in h
        assert signer == me and h["x-crx-address"] == me
        assert call["method"] == "POST" and h["content-type"] == "application/json"
        body = json.loads(raw)
        assert raw == json.dumps(body, separators=(",", ":")).encode()
        assert body["jsonrpc"] == "2.0" and body["id"] == 1
        # The signature covers the body: one changed byte recovers another address.
        assert recover(h, raw.replace(b'"id":1', b'"id":2')) != me
    assert json.loads(node.seen[1][2])["params"] == [CORE, "latest"]
    assert len({h["x-crx-ts"] for _, h, _ in node.seen}) == len(node.seen)  # each call is unique


class OwnNode:
    """Record each RPC post with its keyword arguments."""

    def __init__(self, session):
        self.posts = []
        self._post = session.post
        session.post = self

    def __call__(self, url, **kw):
        self.posts.append(kw)
        return self._post(url, **kw)


@pytest.mark.parametrize("how", ["CRX_RPC", "rpc_url"])
def test_own_rpc_replaces_the_gateway_and_is_not_signed(gw_session, tmp_path, account, monkeypatch, how):
    gw_session.rpc.update(CHAIN)
    own = OwnNode(gw_session)
    if how == "CRX_RPC":
        monkeypatch.setenv("CRX_RPC", RPC)
        c = main(gw_session, tmp_path, account)
    else:
        c = main(gw_session, tmp_path, account, rpc_url=RPC)
    assert c._rpc._gw is None and c._rpc._url == RPC and c._rpc.receipt_poll == 2.0
    c._chain_ready()
    assert gw_session.rpc_methods() == ["eth_chainId", "eth_getCode"]
    assert [set(kw) for kw in own.posts] == [{"json", "timeout"}] * 2  # no headers
    assert "/rpc" not in gw_session.paths()


def test_testnet_row_is_unchanged(session, tmp_path, account):
    assert crx.NETWORKS["testnet"]["rpc_url"] == TESTNET_RPC
    c = crx.Client(key=account.key.hex(), base_url=BASE, state_dir=tmp_path / "state", session=session)
    assert c._rpc._gw is None and c._rpc._url == TESTNET_RPC and c._rpc.receipt_poll == 2.0


def gateway_rpc(session, account, sleep=None):
    gw = Gateway(BASE, account, session)
    gw.sleep = sleep or Pauses()
    return gw, Rpc(BASE + "/rpc", session, gateway=gw)


RPC_403 = (403, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not allowed"}})
RPC_429 = (429, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limit"}}, {"Retry-After": "2"})


@pytest.mark.parametrize("answer,err,words", [
    ((401, {"code": "unauthorized", "error": "bad signature"}), crx.AuthError,
     "gateway.test refused the chain call eth_chainId: bad signature"),
    (RPC_403, crx.AuthError, "gateway.test refused the chain call eth_chainId: method not allowed"),
    ((404, {"code": "not_found"}), crx.ConfigError, "gateway.test serves no chain calls; pass rpc_url= or set CRX_RPC"),
], ids=["401", "403", "404"])
def test_a_gateway_refusal_raises_a_clear_error(session, account, answer, err, words):
    session.routes[("POST", "/rpc")] = answer
    gw, rpc = gateway_rpc(session, account)
    with pytest.raises(err) as ei:
        rpc("eth_chainId")
    assert str(ei.value) == words and ei.value.status == answer[0] and gw.sleep == []
    if answer is RPC_403:
        assert ei.value.details == {"rpc_code": -32601, "method": "eth_chainId"}
    assert len(session.calls) == 1


def test_a_429_waits_retry_after_once_then_resends_signed_again(session, account):
    node = Node(CHAIN)
    session.routes[("POST", "/rpc")] = [RPC_429, node]
    gw, rpc = gateway_rpc(session, account)
    assert rpc.int("eth_chainId") == 1
    assert gw.sleep == [2] and len(session.calls) == 2
    first, second = (c["headers"] for c in session.calls)
    assert session.calls[0]["raw"] == session.calls[1]["raw"]
    assert first["x-crx-ts"] != second["x-crx-ts"] and first["x-crx-sig"] != second["x-crx-sig"]


@pytest.mark.parametrize("after,pauses", [("2", [2]), ("120", [])], ids=["twice", "too-long"])
def test_a_429_that_stays_raises_rate_limited(session, account, after, pauses):
    session.routes[("POST", "/rpc")] = (429, {"code": "rate_limited"}, {"Retry-After": after})
    gw, rpc = gateway_rpc(session, account)
    with pytest.raises(crx.RateLimited) as ei:
        rpc("eth_chainId")
    assert ei.value.retry_after == int(after) and ei.value.details["method"] == "eth_chainId"
    assert gw.sleep == pauses and len(session.calls) == len(pauses) + 1


def chain_for_send(receipts):
    sent = []

    def send(p):
        sent.append(p[0])
        return TX

    node = Node({
        "eth_call": "0x", "eth_estimateGas": hex(100_000), "eth_getTransactionCount": hex(7),
        "eth_getBlockByNumber": {"number": "0x10", "baseFeePerGas": hex(10**9)},
        "eth_maxPriorityFeePerGas": hex(10**8), "eth_sendRawTransaction": send,
        "eth_getTransactionReceipt": lambda p: receipts.pop(0),
    })
    return node, sent


def test_receipts_on_the_gateway_rpc_are_read_every_3_s(session, account):
    node, sent = chain_for_send([None, None, {"status": "0x1"}])
    session.routes[("POST", "/rpc")] = node
    pauses = Pauses()
    _, rpc = gateway_rpc(session, account, pauses)
    assert send_tx(rpc, account, 1, "approve", TOKEN, "0x", sleep=pauses) == TX
    assert pauses == [3.0, 3.0] and len(sent) == 1
    assert all(signer == account.address.lower() for signer, _, _ in node.seen)


def test_a_refused_receipt_read_after_the_send_raises_send_unknown(session, account):
    node, sent = chain_for_send([None])
    refused = [False]

    def answer(req):
        if json.loads(req["raw"])["method"] == "eth_getTransactionReceipt" and refused[0]:
            return RPC_403
        refused[0] = refused[0] or json.loads(req["raw"])["method"] == "eth_getTransactionReceipt"
        return node(req)

    session.routes[("POST", "/rpc")] = answer
    pauses = Pauses()
    _, rpc = gateway_rpc(session, account, pauses)
    with pytest.raises(crx.SendUnknown) as ei:
        send_tx(rpc, account, 1, "approve", TOKEN, "0x", sleep=pauses)
    d = ei.value.details
    assert d["tx"] == TX and d["txs"] == [TX] and d["cause"] == "unauthorized"
    assert len(sent) == 1 and pauses == [3.0]
