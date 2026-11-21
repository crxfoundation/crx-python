"""Guardian: read the waiting manual prices from a scripted chain, cancel one through the role contract, post the
signed reason."""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest
from eth_abi import decode, encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes

import crx
from crx import guardian as gd

from .conftest import Resp

NOW = 1_795_200_000
RPC = "https://fuji.test/rpc"
LIVE_RPC = "https://eth.test/rpc"
PORTAL = "https://safe.test"
CORE = "0x" + "c0" * 20
PRINTS = "0x" + "1e" * 20
ROLES = "0x" + "a0" * 20
VETO = "0x" + b"VETO".hex() + "00" * 28
BRL = gd.PAIRS[0]
MXN = gd.PAIRS[1]
TERMS = "0x" + "7e" * 32
E1 = 1_795_100_000  # a leg expiry second, 0x6aff0d60
GUARDIAN_SEED = b"crx-python tests: guardian key"
HEAD = 4_000
MAX_RANGE = 2_000  # blocks the scripted node serves per eth_getLogs call

TEST_CONFIG = {"chainId": 43113, "core": CORE, "lens": CORE, "printLens": PRINTS, "roles": ROLES, "roleKey": VETO,
               "startBlock": 0, "rpc": RPC}


def w(x: int) -> str:
    return f"{x:064x}"


def topic_addr(a: str) -> str:
    return "0x" + "00" * 12 + a[2:].lower()


class Node:
    """A scripted Fuji node: the venue's logs and entries, the CRX print lens and the guardian role contract."""

    def __init__(self):
        self.logs: list[dict] = []
        self.pending: dict[tuple[str, int], tuple[str, int, int, int]] = {}  # (feed, expiry) -> proposer, ready, ...
        self.prints = {BRL["feedId"]: (5_980_000, -6, NOW - 600), MXN["feedId"]: (18_000_000, -6, NOW - 600)}
        self.members: set[str] = set()
        self.sent: list[dict] = []
        self.receipts: dict[str, dict] = {}
        self.ranges: list[tuple[int, int]] = []
        self.calls: list[str] = []
        self.chain_id = 43113

    # Script

    def log(self, topic0, topics, data=b"", block=1, tx=None):
        raw = {"address": CORE, "topics": [topic0, *topics], "data": "0x" + data.hex(), "blockNumber": hex(block),
               "logIndex": hex(len(self.logs)), "transactionHash": tx or "0x" + w(len(self.logs) + 1),
               "removed": False}
        self.logs.append(raw)
        return raw

    def propose(self, feed, expiry, price, ready, block, proposer="0x" + "5e" * 20, pending=True, expo=-6):
        data = encode(["int64", "int32", "int64[6]", "uint64", "uint8"], [price, expo, [0] * 6, ready, 2])
        raw = self.log(gd.PROPOSED, [topic_addr(proposer), feed, "0x" + w(expiry)], data, block)
        if pending:
            self.pending[(feed, expiry)] = (proposer, ready, price, expo)
        return raw["transactionHash"]

    def cancelled(self, feed, expiry, block, by="0x" + "5a" * 20):
        self.pending.pop((feed, expiry), None)
        return self.log(gd.CANCELLED, [topic_addr(by), feed, "0x" + w(expiry)], b"", block)

    def written(self, feed, expiry, block, by="0x" + "9b" * 20):
        self.pending.pop((feed, expiry), None)
        data = encode(["int64", "int32", "int64[6]", "uint8"], [1, -6, [0] * 6, 2])
        return self.log(gd.WRITTEN, [topic_addr(by), feed, "0x" + w(expiry)], data, block)

    # JSON-RPC

    def __call__(self, method, params):
        self.calls.append(method)
        fn = getattr(self, "rpc_" + method, None)
        if fn is None:
            return {"error": {"code": -32601, "message": f"no {method}"}}
        try:
            return {"result": fn(*params)}
        except Revert as e:
            return {"error": {"code": 3, "message": "execution reverted", "data": "0x" + e.data.hex()}}

    def rpc_eth_chainId(self):
        return hex(self.chain_id)

    def rpc_eth_blockNumber(self):
        return hex(HEAD)

    def rpc_eth_getLogs(self, f):
        lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        if hi - lo + 1 > MAX_RANGE:
            raise Revert(b"", "range")
        self.ranges.append((lo, hi))
        return [x for x in self.logs if lo <= int(x["blockNumber"], 16) <= hi and x["topics"][0] in f["topics"][0]]

    def rpc_eth_getBlockByNumber(self, tag, _full):
        if tag == "latest":
            return {"number": hex(HEAD), "baseFeePerGas": hex(25 * 10**9)}
        return {"number": tag, "timestamp": hex(NOW - (HEAD - int(tag, 16)) * 2)}

    def rpc_eth_maxPriorityFeePerGas(self):
        return hex(10**9)

    def rpc_eth_getTransactionCount(self, _who, _tag):
        return hex(len(self.sent))

    def rpc_eth_estimateGas(self, call):
        self.rpc_eth_call(call, "latest")
        return hex(100_000)

    def rpc_eth_call(self, call, _tag):
        to, data = call["to"].lower(), bytes.fromhex(call["data"][2:])
        sel, body = data[:4], data[4:]
        if to == CORE and sel == gd.PENDING:
            feed, expiry = decode(["bytes32", "uint64"], body)
            p = self.pending.get(("0x" + feed.hex(), expiry))
            who, ready, price, expo = p if p else ("0x" + "00" * 20, 0, 0, 0)
            return "0x" + encode(["address", "uint64", "uint8", "int64", "int32", "int64[6]"],
                                 [who, ready, 2, price, expo, [0] * 6]).hex()
        if to == PRINTS and sel == gd.LATEST:
            price, expo, at = self.prints.get("0x" + body.hex(), (0, 0, 0))
            return "0x" + encode(["(int64,int32,bool)", "uint64"], [(price, expo, price > 0), at]).hex()
        if to == ROLES and sel == gd.EXEC:
            if call.get("from", "").lower() not in self.members:
                raise Revert(keccak(text="NoMembership()")[:4])
            target, _value, inner, _op, _key, _rev = decode(
                ["address", "uint256", "bytes", "uint8", "bytes32", "bool"], body)
            feed, expiry = decode(["bytes32", "uint64"], inner[4:])
            if ("0x" + feed.hex(), expiry) not in self.pending:
                raise Revert(keccak(text="ModuleTransactionFailed()")[:4])
            return "0x" + w(1)
        if to == CORE and sel == gd.PROPOSE_TEST:
            return "0x"
        raise Revert(b"\x00\x00\x00\x00")

    def rpc_eth_sendRawTransaction(self, raw):
        tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        sender = Account.recover_transaction(raw).lower()
        to, data = "0x" + bytes(tx["to"]).hex(), "0x" + bytes(tx["data"]).hex()
        self.sent.append({"from": sender, "to": to, "data": data})
        h = "0x" + keccak(HexBytes(raw)).hex()
        body = bytes.fromhex(data[10:])
        logs = []
        if to == ROLES:
            _t, _v, inner, _o, _k, _r = decode(["address", "uint256", "bytes", "uint8", "bytes32", "bool"], body)
            feed, expiry = decode(["bytes32", "uint64"], inner[4:])
            logs.append(self.cancelled("0x" + feed.hex(), expiry, HEAD, by="0x" + "5a" * 20))
        elif to == CORE:
            feed, expiry, price, expo = decode(["bytes32", "uint64", "int64", "int32"], body)
            self.propose("0x" + feed.hex(), expiry, price, NOW + gd.DELAY_S, HEAD, proposer=sender, expo=expo)
            logs.append(self.logs[-1])
        self.receipts[h] = {"status": "0x1", "blockNumber": hex(HEAD), "logs": logs}
        return h

    def rpc_eth_getTransactionReceipt(self, h):
        return self.receipts.get(h)


class Revert(Exception):
    def __init__(self, data: bytes, message: str = "reverted"):
        super().__init__(message)
        self.data = data


class Session:
    """Route RPC posts to the node, and portal or Hermes calls to ``routes[(METHOD, path)]``."""

    def __init__(self, node):
        self.node = node
        self.routes: dict = {}
        self.posts: list[dict] = []
        self.gets: list[str] = []

    def post(self, url, json=None, timeout=None, allow_redirects=True):
        if url in (RPC, LIVE_RPC):
            out = self.node(json["method"], json["params"])
            return Resp(200, {"jsonrpc": "2.0", "id": json["id"], **out})
        assert allow_redirects is False
        path = url[len(PORTAL):]
        self.posts.append({"path": path, "body": json})
        spec = self.routes.get(("POST", path), (404, {"code": "not_found"}))
        return Resp(*spec)

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        self.gets.append(url)
        return Resp(*self.routes.get(("GET", url[len(PORTAL):]), (404, {"code": "not_found"})))


@pytest.fixture
def key():
    return Account.from_key(hashlib.sha256(GUARDIAN_SEED).digest())


@pytest.fixture
def node(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env file
    for k in ("CRX_RPC", "CRX_SAFE_URL", "CRX_HERMES_URL", "CRX_STATE_DIR", "CRX_WALLET_PK", "CRX_WALLET_PK_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setitem(gd.CONFIG, "test", TEST_CONFIG)
    return Node()


@pytest.fixture
def session(node):
    return Session(node)


@pytest.fixture
def make(session, tmp_path, key):
    def build(test=True, with_key=True, **kw):
        g = crx.Guardian(key=key.key.hex() if with_key else None, test=test, session=session, portal_url=PORTAL,
                         state_dir=tmp_path / "state", rpc_url=None if test else LIVE_RPC, **kw)
        g._clock, g._sleep = (lambda: NOW), (lambda s: None)
        return g
    return build


def proposal(feed=BRL["feedId"], expiry=E1, mode="test"):
    return gd.Proposal(pair="USD/BRL", kind="fixing", feed_id=feed, expiry=expiry, price=Decimal("6.16"), expo=-6,
                       reference=None, reference_source=None, reference_at=None, diff_bp=None, proposed_at=None,
                       ready_at=None, seconds_left=3600, proposer="0x" + "5e" * 20, tx="0x" + w(1), mode=mode)


def test_proposals_keep_each_entry_the_chain_still_holds_at_its_logged_ready_time(make, node):
    a = node.propose(BRL["feedId"], E1, 6_160_000, NOW + 3600, block=100)
    node.propose(MXN["feedId"], E1, 18_100_000, NOW + 3700, block=2_500)
    node.cancelled(MXN["feedId"], E1, block=2_600)  # cancelled
    node.propose(BRL["pairId"], gd.FINAL_FIX_MARK, 6_000_000, NOW + 3800, block=3_000, pending=False)  # gone
    node.propose(MXN["feedId"], gd.MARK_SLOT_A, 18_000_000, NOW + 3900, block=3_100)
    node.pending[(MXN["feedId"], gd.MARK_SLOT_A)] = ("0x" + "5e" * 20, NOW + 9999, 18_000_000, -6)  # other ready
    node.propose(BRL["feedId"], gd.MARK_SLOT_B, 6_000_000, NOW - 25 * 3600, block=3_150)  # lapsed mark
    trade = gd.TRADE_PRICE_TAG | E1
    t = node.propose(TERMS, trade, 6_100_000, NOW + 4000, block=3_200)

    got = make(with_key=False).proposals()

    assert [(p.tx, p.kind) for p in got] == [(a, "fixing"), (t, "trade_price")]
    p = got[0]
    assert (p.pair, p.feed_id, p.expiry, p.price, p.seconds_left) == ("USD/BRL", BRL["feedId"], E1,
                                                                     Decimal("6.160000"), 3600)
    assert (p.reference, p.reference_source, p.diff_bp) == (Decimal("5.980000"), "crx", Decimal("301.0"))
    assert p.ready_at.timestamp() == NOW + 3600 and p.proposed_at.timestamp() == NOW + 3600 - 7200
    assert (got[1].pair, got[1].reference, got[1].diff_bp) == (None, None, None)
    assert all(hi - lo < MAX_RANGE for lo, hi in node.ranges) and node.ranges[0] == (0, 1_999)


@pytest.mark.parametrize("expiry", [gd.TRADE_HOLD_TAG | E1, gd.TRADE_HOLD_TAG, gd.TRADE_PRICE_TAG - 1,
                                    gd.LISTED_FIX_MARK, 1 << 41])
def test_cancel_refuses_a_hold_release_or_a_key_outside_the_role_before_any_chain_call(make, node, session, expiry):
    with pytest.raises(crx.BadRequest) as e:
        make().cancel(proposal(expiry=expiry), reason="3 % off Pyth spot")
    assert e.value.code == "not_cancellable" and "nothing was sent" in str(e.value)
    assert node.calls == [] and session.posts == []


@pytest.fixture
def cancelled(make, node, session, key):
    node.members.add(key.address.lower())
    node.propose(BRL["feedId"], E1, 6_160_000, NOW + 3600, block=100)
    session.routes[("POST", "/api/v1/reasons")] = (200, {"ok": True})
    return make().cancel(proposal(), reason="  3 % off Pyth spot ")


# execTransactionWithRole(core, 0, cancelManualPrice(USD/BRL feed, E1), 0, "VETO", true), word by word.
PINNED = "0x" + "c6fe8747" + "".join([
    "000000000000000000000000c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0",  # to: the core
    "0000000000000000000000000000000000000000000000000000000000000000",  # value
    "00000000000000000000000000000000000000000000000000000000000000c0",  # offset of data
    "0000000000000000000000000000000000000000000000000000000000000000",  # operation: call
    "5645544f00000000000000000000000000000000000000000000000000000000",  # roleKey "VETO"
    "0000000000000000000000000000000000000000000000000000000000000001",  # shouldRevert
    "0000000000000000000000000000000000000000000000000000000000000044",  # data length: 68
    "a76b1adb"  # cancelManualPrice(bytes32,uint64)
    "d2db4dbf1aea74e0f666b0e8f73b9580d407f5e5cf931940b06dc633d7a95906"  # feedId
    "000000000000000000000000000000000000000000000000000000006aff0d60"  # expiry
    + "00" * 28,  # padding to 96 bytes
])


def test_cancel_sends_exactly_the_role_call_from_the_guardian_wallet(cancelled, node, key):
    assert node.sent == [{"from": key.address.lower(), "to": ROLES, "data": PINNED}]
    assert gd.cancel_call(CORE, VETO, BRL["feedId"], E1) == PINNED
    assert (BRL["feedId"], E1) not in node.pending
    assert cancelled.reason_recorded is True and cancelled.reason_error is None
    assert str(cancelled) == f"Cancelled. Transaction {cancelled.tx}."


def test_the_posted_reason_is_the_spec_text_signed_by_the_guardian_key(cancelled, session, key):
    [post] = session.posts
    body = post["body"]
    assert post["path"] == "/api/v1/reasons"
    text = (f"CRX-GUARDIAN-REASON\nMode: test\nTx: {cancelled.tx}\nFeed: {BRL['feedId']}\nExpiry: {E1}\n"
            "Reason: 3 % off Pyth spot")
    assert Account.recover_message(encode_defunct(text=text), signature=body["sig"]) == key.address
    assert body == {"mode": "test", "tx": cancelled.tx, "feedId": BRL["feedId"], "expiry": str(E1),
                    "reason": "3 % off Pyth spot", "signer": key.address.lower(), "sig": body["sig"]}


def test_a_refused_reason_leaves_the_cancel_in_force(make, node, session, key):
    node.members.add(key.address.lower())
    node.propose(BRL["feedId"], E1, 6_160_000, NOW + 3600, block=100)
    session.routes[("POST", "/api/v1/reasons")] = (409, {"code": "not_a_cancel"})
    c = make().cancel(proposal(), reason="3 % off Pyth spot")
    assert (c.reason_recorded, c.reason_error) == (False, "not_a_cancel")
    assert (BRL["feedId"], E1) not in node.pending and len(session.posts) == 1
    assert str(c).endswith("The reason is not recorded (not_a_cancel); the cancel stands.")


def test_a_wallet_with_no_guardian_role_is_refused_with_nothing_sent(make, node, session):
    with pytest.raises(crx.NotGuardian, match=r"^Live cancel is not open for this wallet\.$"):
        make(test=False).cancel(proposal(mode="live"), reason="3 % off Pyth spot")
    assert node.calls == []
    node.propose(BRL["feedId"], E1, 6_160_000, NOW + 3600, block=100)
    with pytest.raises(crx.NotGuardian, match=r"^This wallet is not a guardian\. Nothing was sent\.$"):
        make().cancel(proposal(), reason="3 % off Pyth spot")
    assert node.sent == [] and session.posts == []


def test_history_gives_each_recent_proposal_its_outcome(make, node):
    a = node.propose(BRL["feedId"], E1, 6_160_000, NOW + 3600, block=100)
    b = node.propose(MXN["feedId"], E1, 18_100_000, NOW - 3600, block=200)
    x = node.cancelled(MXN["feedId"], E1, block=300)
    c = node.propose(BRL["feedId"], E1 + 1, 6_000_000, NOW - 7200, block=400)
    node.written(BRL["feedId"], E1 + 1, block=500)
    old = node.propose(BRL["feedId"], E1 + 2, 6_000_000, NOW - 40 * 86_400, block=600)
    r1 = node.propose(MXN["feedId"], gd.MARK_SLOT_A, 18_000_000, NOW, block=700)
    r2 = node.propose(MXN["feedId"], gd.MARK_SLOT_A, 18_000_000, NOW + 60, block=800)

    got = make(with_key=False).history(days=30)

    assert {o.proposal.tx: o.outcome for o in got} == {a: "pending", b: "cancelled", c: "written",
                                                      r1: "replaced", r2: "pending"}
    assert old not in {o.proposal.tx for o in got}
    hit = next(o for o in got if o.proposal.tx == b)
    assert (hit.by, hit.outcome_tx) == ("0x" + "5a" * 20, x["transactionHash"])
    assert hit.outcome_at.timestamp() == NOW - (HEAD - 300) * 2
    assert {o.proposal.tx for o in make(with_key=False).history(pair="USDC/USDT")} == set()


def test_post_test_price_proposes_the_reference_moved_by_off_bp_and_refuses_live(make, node, key):
    p = make().post_test_price("USD/BRL", off_bp=300)
    data = gd.PROPOSE_TEST + encode(["bytes32", "uint64", "int64", "int32"],
                                    [bytes.fromhex(BRL["feedId"][2:]), NOW, 6_159_400, -6])
    assert node.sent == [{"from": key.address.lower(), "to": CORE, "data": "0x" + data.hex()}]
    assert (p.pair, p.kind, p.expiry, p.price, p.reference, p.diff_bp, p.seconds_left) == (
        "USD/BRL", "fixing", NOW, Decimal("6.159400"), Decimal("5.980000"), Decimal("300.0"), 7200)
    node.calls.clear()
    with pytest.raises(crx.BadRequest) as e:
        make(test=False).post_test_price("USD/BRL", off_bp=300)
    assert e.value.code == "test_only" and node.calls == []


def test_watch_yields_each_new_proposal_once(make, node):
    a = node.propose(BRL["feedId"], E1, 6_160_000, NOW + 3600, block=100)
    pauses = []
    g = make(with_key=False)
    g._sleep = pauses.append
    seen = g.watch(every=5)
    assert next(seen).tx == a
    z = node.propose(MXN["feedId"], E1, 18_100_000, NOW + 3700, block=HEAD)
    assert next(seen).tx == z
    assert pauses == [5.0]


def test_the_live_pins_name_the_chain_1_core_and_lens():
    live = gd.CONFIG["live"]
    assert to_checksum_address(live["core"]) == crx.client.MAINNET_CORE
    assert (live["chainId"], live["roles"], live["roleKey"]) == (1, None, VETO)
