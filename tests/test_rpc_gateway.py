"""The mainnet default RPC: the gateway's /rpc route, sent and authenticated like a REST call."""

import json
import logging

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

import crx
from crx import _eip712 as e7
from crx._chain import Rpc, RpcError, TxLog, send_tx
from crx._http import Gateway, rest_message
from crx.client import MAINNET_CORE

from .conftest import BASE, RPC, Pauses

CORE = MAINNET_CORE.lower()
TOKEN = "0x" + "a0" * 20
TX = "0x" + "ab" * 32
SIGNED = {"x-crx-address", "x-crx-ts", "x-crx-sig"}
TESTNET_RPC = "https://api.avax-test.network/ext/bc/C/rpc"
PLACEHOLDER = "https://eth-mainnet.example/v2/<api-key>"


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
        self.seen.append((recover(h, raw) if "x-crx-sig" in h else None, h, raw))
        call = json.loads(raw)
        out = self.answers[call["method"]]
        out = out(call["params"]) if callable(out) else out
        if isinstance(out, tuple):
            return out
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


def rpc_calls(session):
    return [c for c in session.calls if c["path"] == "/rpc"]


def test_mainnet_with_no_rpc_builds_a_client_on_the_gateway_rpc(gw_session, tmp_path, account, monkeypatch):
    c = main(gw_session, tmp_path, account)
    assert c._rpc.on_gateway and c._rpc._gw is c._gw and c._rpc._url == BASE + "/rpc" and c._rpc.poll_s == 4.0
    monkeypatch.setenv("CRX_BASE", BASE + "/")  # the route follows CRX_BASE
    c = main(gw_session, tmp_path, account, base_url=None)
    assert c._rpc._gw is c._gw and c._rpc._url == BASE + "/rpc"
    assert repr(c._rpc) == "Rpc(<url hidden>)" and gw_session.calls == []


def test_the_health_pins_stand_and_no_chain_probe_goes_to_the_gateway(gw_session, tmp_path, account):
    gw_session.routes[("POST", "/rpc")] = Node(CHAIN)
    c = main(gw_session, tmp_path, account)
    assert c._chain_ready()["core"] == CORE and c._chain_ready()["chain_id"] == 1
    assert rpc_calls(gw_session) == [] and gw_session.rpc_calls == []
    gw_session.routes[("GET", "/health")]["chains"][0]["chain_id"] = 43113
    with pytest.raises(crx.ConfigError, match="chain 1"):
        main(gw_session, tmp_path, account)._chain_ready()


def test_gateway_calls_are_signed_over_the_bytes_sent(gw_session, tmp_path, account):
    node = Node(CHAIN)
    gw_session.routes[("POST", "/rpc")] = node
    c = main(gw_session, tmp_path, account)
    assert c._rpc.int("eth_chainId") == 1 and c._rpc("eth_getCode", CORE, "latest") == "0x6080"
    calls = rpc_calls(gw_session)
    assert [json.loads(k["raw"])["method"] for k in calls] == ["eth_chainId", "eth_getCode"]
    assert gw_session.rpc_calls == []  # nothing went to an RPC of the user's own
    me = account.address.lower()
    for (signer, h, raw), call in zip(node.seen, calls):
        assert SIGNED <= set(h) and "x-crx-signer" not in h and "x-crx-session" not in h
        assert signer == me and h["x-crx-address"] == me
        assert call["method"] == "POST" and h["content-type"] == "application/json"
        body = json.loads(raw)
        assert raw == json.dumps(body, separators=(",", ":")).encode()
        assert list(body) == ["jsonrpc", "id", "method", "params"] and body["jsonrpc"] == "2.0"
        assert type(body["id"]) is int and 1 <= body["id"] < 2**31
        # The signature covers the body: one changed byte recovers another address.
        assert recover(h, raw.replace(b'"2.0"', b'"2.1"')) != me
    assert json.loads(node.seen[1][2])["params"] == [CORE, "latest"]
    assert len({json.loads(raw)["id"] for _, _, raw in node.seen}) == 2  # a random id per call
    assert len({h["x-crx-ts"] for _, h, _ in node.seen}) == 2


class Custodian:
    """A custodian signer: EIP-191 messages only."""

    def __init__(self, account):
        self.a, self.address = account, account.address

    def sign_message(self, message):
        return self.a.sign_message(encode_defunct(primitive=message)).signature

    def sign_typed_data(self, td):
        raise AssertionError("no typed data on this path")


def test_a_custodian_signer_sends_its_session_token(gw_session, tmp_path, account):
    me = account.address.lower()
    gw_session.routes[("POST", "/session")] = {"token": "ab" * 32, "custody": me, "signer": me, "ttl_ms": 3_600_000}
    node = Node(CHAIN)
    gw_session.routes[("POST", "/rpc")] = node
    c = main(gw_session, tmp_path, account, key=None, signer=Custodian(account))
    assert c._rpc.int("eth_chainId") == 1 and c._rpc.int("eth_chainId") == 1
    assert gw_session.paths("POST") == ["/session", "/rpc", "/rpc"]
    assert all(h["x-crx-session"] == "ab" * 32 and not SIGNED & set(h) for _, h, _ in node.seen)


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
    assert not c._rpc.on_gateway and c._rpc._url == RPC and c._rpc.poll_s == 2.0
    c._chain_ready()  # an own RPC is still probed
    assert gw_session.rpc_methods() == ["eth_chainId", "eth_getCode"]
    assert [set(kw) for kw in own.posts] == [{"json", "timeout"}] * 2  # no headers
    assert rpc_calls(gw_session) == []


@pytest.mark.parametrize("network,want", [("mainnet", BASE + "/rpc"), ("testnet", TESTNET_RPC)])
def test_a_placeholder_crx_rpc_is_ignored_with_one_line(gw_session, tmp_path, account, monkeypatch, caplog,
                                                        network, want):
    monkeypatch.setenv("CRX_RPC", PLACEHOLDER)
    with caplog.at_level(logging.WARNING, logger="crx"):
        c = main(gw_session, tmp_path, account, network=network)
    assert c._rpc._url == want and c._rpc.on_gateway is (network == "mainnet")
    assert [r.getMessage() for r in caplog.records] == ["CRX_RPC holds a placeholder (< or >) and is ignored"]
    assert "example" not in caplog.text and "api-key" not in caplog.text


def test_testnet_row_is_unchanged(session, tmp_path, account):
    assert crx.NETWORKS["testnet"]["rpc_url"] == TESTNET_RPC
    c = crx.Client(key=account.key.hex(), base_url=BASE, state_dir=tmp_path / "state", session=session)
    assert not c._rpc.on_gateway and c._rpc._url == TESTNET_RPC and c._rpc.poll_s == 2.0


def gateway_rpc(session, account, sleep=None):
    gw = Gateway(BASE, account, session)
    gw.sleep = sleep if sleep is not None else Pauses()
    return gw, Rpc(BASE + "/rpc", session, gateway=gw)


RPC_403 = (403, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "The method is not allowed."}})
RPC_429 = (429, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "Too many requests."}},
           {"Retry-After": "2"})


@pytest.mark.parametrize("answer,err,words", [
    ((401, {"code": "unauthorized", "error": "bad signature"}), crx.AuthError, "bad signature"),
    (RPC_403, crx.AuthError, "The method is not allowed."),
    ((403, {"code": "not_whitelisted", "error": "The seat is not whitelisted."}), crx.NotWhitelisted,
     "The seat is not whitelisted."),
    ((404, {"code": "not_found", "error": "no route"}), crx.CrxError, "no route"),
    ((302, "", {"Location": "https://elsewhere.test/rpc"}), crx.CrxError, "HTTP 302 from gateway.test"),
    ((503, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "Busy."}}, {"Retry-After": "5"}),
     crx.ServerError, "Busy."),
], ids=["401", "403-rpc", "403-whitelist", "404", "302", "503"])
def test_any_answer_but_200_raises_the_typed_gateway_error(session, account, answer, err, words):
    session.routes[("POST", "/rpc")] = answer
    gw, rpc = gateway_rpc(session, account)
    with pytest.raises(err) as ei:
        rpc("eth_chainId")
    assert type(ei.value) is err and str(ei.value) == words and ei.value.status == answer[0]
    assert ei.value.details["method"] == "eth_chainId" and gw.sleep == [] and len(session.calls) == 1
    if answer is RPC_403:
        assert ei.value.details["rpc_code"] == -32601


def test_a_429_waits_retry_after_once_then_resends_signed_again(session, account):
    session.routes[("POST", "/rpc")] = [RPC_429, Node(CHAIN)]
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


def send_chain(receipts, counts=None, **edit):
    """Return (node, sent raw txs) for one send. ``counts`` scripts (latest, pending) nonce reads."""
    sent = []
    counts = counts if counts is not None else [(7, 7)]

    def send(p):
        sent.append(p[0])
        return TX

    def count(p):
        latest, pending = counts[0] if len(counts) == 1 else counts.pop(0) if p[1] == "pending" else counts[0]
        return hex(latest if p[1] == "latest" else pending)

    answers = {
        "eth_call": "0x", "eth_estimateGas": hex(100_000), "eth_getTransactionCount": count,
        "eth_getBlockByNumber": {"number": "0x10", "baseFeePerGas": hex(10**9)},
        "eth_maxPriorityFeePerGas": hex(10**8), "eth_sendRawTransaction": send,
        "eth_getTransactionReceipt": lambda p: receipts.pop(0),
        "eth_getTransactionByHash": None,
    }
    answers.update(edit)
    return Node(answers), sent


def test_nonce_and_receipt_reads_on_the_gateway_rpc_pause_4_s(session, account):
    node, sent = send_chain([None, None, {"status": "0x1"}], counts=[(7, 8), (8, 8)])
    session.routes[("POST", "/rpc")] = node
    pauses = Pauses()
    _, rpc = gateway_rpc(session, account, pauses)
    assert send_tx(rpc, account, 1, "approve", TOKEN, "0x", sleep=pauses) == TX
    assert pauses == [4.0, 4.0, 4.0] and len(sent) == 1  # one nonce pause, two receipt pauses
    assert all(signer == account.address.lower() for signer, _, _ in node.seen)


@pytest.mark.parametrize("where", ["eth_call", "eth_estimateGas", "eth_maxPriorityFeePerGas"])
def test_a_429_is_never_a_revert_or_a_zero_tip(session, account, where):
    node, sent = send_chain([{"status": "0x1"}], **{where: (429, {"code": "rate_limited"}, {"Retry-After": "1"})})
    session.routes[("POST", "/rpc")] = node
    pauses = Pauses()
    _, rpc = gateway_rpc(session, account, pauses)
    with pytest.raises(crx.RateLimited):
        send_tx(rpc, account, 1, "approve", TOKEN, "0x", sleep=pauses)
    assert sent == [] and pauses == [1]


def test_a_node_revert_in_a_200_still_reads_as_would_revert(session, account):
    revert = (200, {"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted",
                                                          "data": "0x" + e7.calldata("NotWhitelisted()", [], [])[2:]}})
    node, sent = send_chain([], eth_call=revert)
    session.routes[("POST", "/rpc")] = node
    _, rpc = gateway_rpc(session, account)
    with pytest.raises(RpcError):
        rpc("eth_call", {"to": TOKEN, "data": "0x"}, "latest")
    with pytest.raises(crx.TxFailed, match="approve would revert: NotWhitelisted; nothing sent"):
        send_tx(rpc, account, 1, "approve", TOKEN, "0x", sleep=Pauses())
    assert sent == []


def test_a_5xx_to_a_send_is_tracked_by_its_hash(session, account, tmp_path):
    node, sent = send_chain([None, {"status": "0x1"}],
                            eth_sendRawTransaction=(503, {"code": "service_unavailable", "error": "Busy."}))
    session.routes[("POST", "/rpc")] = node
    pauses = Pauses()
    _, rpc = gateway_rpc(session, account, pauses)
    log = TxLog(tmp_path / "txs.json")
    h = send_tx(rpc, account, 1, "approve", TOKEN, "0x", sleep=pauses, log=log)
    assert h.startswith("0x") and len(h) == 66 and h != TX and [r["hash"] for r in log.rows()] == [h]
    receipts = [json.loads(raw)["params"] for _, _, raw in node.seen if b"eth_getTransactionReceipt" in raw]
    assert receipts == [[h], [h]] and pauses == [4.0]


def test_a_refused_receipt_read_after_the_send_raises_send_unknown(session, account):
    node, sent = send_chain([None])
    reads = []

    def answer(req):
        if json.loads(req["raw"])["method"] == "eth_getTransactionReceipt":
            reads.append(1)
            if len(reads) > 1:
                return RPC_403
        return node(req)

    session.routes[("POST", "/rpc")] = answer
    pauses = Pauses()
    _, rpc = gateway_rpc(session, account, pauses)
    with pytest.raises(crx.SendUnknown) as ei:
        send_tx(rpc, account, 1, "approve", TOKEN, "0x", sleep=pauses)
    d = ei.value.details
    assert d["tx"] == TX and d["txs"] == [TX] and d["cause"] == "unauthorized"
    assert len(sent) == 1 and pauses == [4.0]
