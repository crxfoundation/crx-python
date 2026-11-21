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
    """A scripted test network node: the venue's logs and entries, the CRX print lens and the guardian role
    contract."""

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
        self.throttle: dict[str, list[tuple]] = {}  # method -> HTTP answers served before the node answers
        self.receipt_reads: dict[str, int] = {}
        self.blind_reads: set[int] = set()  # receipt reads, by number per transaction, that answer null

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
                raise Revert(keccak(text="NotAuthorized(address)")[:4] + bytes(12) + bytes.fromhex(call["from"][2:]))
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
        self.receipt_reads[h] = n = self.receipt_reads.get(h, 0) + 1
        return None if n in self.blind_reads else self.receipts.get(h)


class Revert(Exception):
    def __init__(self, data: bytes, message: str = "reverted"):
        super().__init__(message)
        self.data = data


class Session:
    """Route RPC posts to the node, and portal or Hermes calls to ``routes[(METHOD, path)]``. A route is a
    ``(status, body)`` or a list of them, served in order; the last one repeats."""

    def __init__(self, node):
        self.node = node
        self.routes: dict = {}
        self.posts: list[dict] = []
        self.gets: list[str] = []

    def post(self, url, json=None, timeout=None, allow_redirects=True):
        if url in (RPC, LIVE_RPC):
            if self.node.throttle.get(json["method"]):
                return Resp(*self.node.throttle[json["method"]].pop(0))
            out = self.node(json["method"], json["params"])
            return Resp(200, {"jsonrpc": "2.0", "id": json["id"], **out})
        assert allow_redirects is False
        path = url[len(PORTAL):]
        self.posts.append({"path": path, "body": json})
        spec = self.routes.get(("POST", path), (404, {"code": "not_found"}))
        if isinstance(spec, list):
            spec = spec.pop(0) if len(spec) > 1 else spec[0]
        return Resp(*spec)

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        self.gets.append(url)
        path = url[len(PORTAL):]
        spec = self.routes.get(("GET", path, (params or {}).get("pair")), self.routes.get(("GET", path)))
        return Resp(*(spec or (404, {"code": "not_found"})))


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

    limited = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32090, "message": "Too many requests"}}
    node.throttle = {"eth_getLogs": [(429, "Too Many Requests"), (200, limited)], "eth_call": [(502, "Bad Gateway")]}
    g = make(with_key=False)
    pauses = []
    g._sleep = pauses.append

    got = g.proposals()

    assert pauses == [1.0, 3.0, 1.0]  # each rate refusal or answer with no JSON is read again after a pause
    assert [(p.tx, p.kind) for p in got] == [(a, "fixing"), (t, "trade_price")]
    p = got[0]
    assert (p.pair, p.feed_id, p.expiry, p.price, p.seconds_left) == ("USD/BRL", BRL["feedId"], E1,
                                                                     Decimal("6.160000"), 3600)
    assert (p.reference, p.reference_source, p.diff_bp) == (Decimal("5.980000"), "crx", Decimal("301.0"))
    assert p.ready_at.timestamp() == NOW + 3600 and p.proposed_at.timestamp() == NOW + 3600 - 7200
    assert (got[1].pair, got[1].reference, got[1].diff_bp) == (None, None, None)
    assert all(hi - lo < MAX_RANGE for lo, hi in node.ranges) and node.ranges[0] == (0, 1_999)
    node.throttle = {"eth_chainId": [(429, "Too Many Requests")] * 3}
    with pytest.raises(crx.BadAnswer, match="not JSON"):
        make(with_key=False).proposals()


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
    session.routes[("POST", "/api/v1/reasons")] = (200, {"recorded": True, "tx": "0x" + w(9), "firm": "Test firm"})
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


# The example of the portal's reason text v2, word for word.
V2_EXAMPLE = """CRX-GUARDIAN-REASON
Mode: test
Pair: USD/BRL
Fixes: 2026-11-21 14:00 ET
Tx: 0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1
Feed: 0xd2db4dbf1aea74e0f666b0e8f73b9580d407f5e5cf931940b06dc633d7a95906
Expiry: 1795287600
Reason: 300 bp above the reference
Domain: safe.dev.crxfx.com
Issued at: 2026-11-21T06:00:00.000Z"""


def test_the_posted_reason_is_the_v2_spec_text_signed_by_the_guardian_key(cancelled, session, key):
    [post] = session.posts
    body = post["body"]
    assert post["path"] == "/api/v1/reasons"
    text = (f"CRX-GUARDIAN-REASON\nMode: test\nPair: USD/BRL\nFixes: 2026-11-19 09:53 ET\nTx: {cancelled.tx}\n"
            f"Feed: {BRL['feedId']}\nExpiry: {E1}\nReason: 3 % off Pyth spot\nDomain: safe.test\n"
            "Issued at: 2026-11-20T18:40:00.000Z")
    assert Account.recover_message(encode_defunct(text=text), signature=body["sig"]) == key.address
    assert body == {"mode": "test", "tx": cancelled.tx, "feedId": BRL["feedId"], "expiry": str(E1),
                    "reason": "3 % off Pyth spot", "signer": key.address.lower(), "sig": body["sig"],
                    "issuedAt": "2026-11-20T18:40:00.000Z", "domain": "safe.test"}
    assert gd.reason_message_v2("test", "0x" + "a1" * 32, BRL["feedId"].upper().replace("0X", "0x"), 1795287600,
                                "300 bp above the reference", "safe.dev.crxfx.com",
                                "2026-11-21T06:00:00.000Z") == V2_EXAMPLE
    trade = gd.reason_message_v2("live", "0x" + "a1" * 32, TERMS, gd.TRADE_PRICE_TAG | 1795287600, "the trade is off",
                                 "safe.crxfx.com", "2026-11-21T06:00:00Z").split("\n")
    assert trade[2:4] == ["Trade: 0x7e7e...7e7e", "Ends: 2026-11-21 14:00 ET"]
    final = gd.reason_message_v2("test", "0x" + "a1" * 32, MXN["pairId"], gd.FINAL_FIX_MARK, "the final is off",
                                 "safe.test", "2026-11-21T06:00:00Z").split("\n")
    assert final[2:4] == ["Pair: USD/MXN", "Tx: 0x" + "a1" * 32]


def test_a_lagging_rpc_or_portal_leaves_the_cancel_in_force_and_the_reason_can_be_sent_again(make, node, session,
                                                                                            key):
    node.members.add(key.address.lower())
    node.propose(BRL["feedId"], E1, 6_160_000, NOW + 3600, block=100)
    node.propose(MXN["feedId"], E1, 18_100_000, NOW + 3700, block=200)
    route = ("POST", "/api/v1/reasons")
    session.routes[route] = [(503, {"error": {"code": "not_seen_yet", "message": "The chain read is behind."}}),
                             (409, {"error": {"code": "not_a_cancel", "message": "Not a guardian cancel."}}),
                             (200, "<html>OK</html>")]
    node.blind_reads = {2}  # the read after the mined cancel reaches a node that has not seen it yet
    g = make()
    pauses = []
    g._sleep = pauses.append

    c = g.cancel(proposal(), reason="3 % off Pyth spot")

    assert (c.reason_recorded, c.reason_error, len(session.posts)) == (False, "bad_answer", 3)
    assert pauses == [2.0, 5.0, 15.0] and (BRL["feedId"], E1) not in node.pending
    assert str(c).endswith("The reason is not recorded (bad_answer); the cancel stands.")
    first = session.posts[0]["body"]
    for answer, want in [((409, {"error": {"code": "already_recorded", "message": "On record."}}),
                          (True, "already_recorded")),
                         ((200, {"recorded": True, "tx": c.tx, "firm": "Test firm"}), (True, None))]:
        session.routes[route], session.posts[:] = [answer], []
        assert g.record_reason(c.tx, BRL["feedId"], E1, "3 % off Pyth spot") == want
        assert [p["body"] for p in session.posts] == [first]
    session.posts.clear()
    node.blind_reads = set(range(2, 99))
    c2 = g.cancel(proposal(feed=MXN["feedId"]), reason="3 % off Pyth spot")
    assert (c2.reason_recorded, c2.reason_error, session.posts) == (False, "no_receipt", [])
    assert (MXN["feedId"], E1) not in node.pending


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


def test_only_a_bool_picks_the_mode_and_the_pins_name_each_chain():
    live, test = gd.CONFIG["live"], gd.CONFIG["test"]
    assert to_checksum_address(live["core"]) == crx.client.MAINNET_CORE
    assert (live["chainId"], live["roles"], live["roleKey"]) == (1, None, VETO)
    assert (test["chainId"], test["rpc"]) == (43113, "https://avalanche-fuji-c-chain-rpc.publicnode.com")
    for bad in (1, "true", None):
        with pytest.raises(crx.ConfigError, match=r"^test must be True or False\.$"):
            crx.Guardian(test=bad, rpc_url=LIVE_RPC)


def test_a_portal_that_refuses_the_v2_text_gets_the_v1_text_once(make, session, key):
    route = ("POST", "/api/v1/reasons")
    tx = "0x" + "a1" * 32
    session.routes[route] = [(401, {"error": {"code": "bad_signature", "message": "No match."}}),
                             (200, {"recorded": True, "tx": tx, "firm": "Test firm"})]
    g = make()
    pauses = []
    g._sleep = pauses.append

    assert g.record_reason(tx, BRL["feedId"], E1, "3 % off Pyth spot") == (True, None)

    v2, v1 = [p["body"] for p in session.posts]
    assert (v2["issuedAt"], v2["domain"], pauses) == ("2026-11-20T18:40:00.000Z", "safe.test", [])
    assert v1 == {"mode": "test", "tx": tx, "feedId": BRL["feedId"], "expiry": str(E1), "reason": "3 % off Pyth spot",
                  "signer": key.address.lower(), "sig": v1["sig"]}
    text = gd.reason_message("test", tx, BRL["feedId"], E1, "3 % off Pyth spot")
    assert Account.recover_message(encode_defunct(text=text), signature=v1["sig"]) == key.address
    session.routes[route], session.posts[:] = [(401, {"error": {"code": "bad_proof", "message": "Too old."}}),
                                               (401, {"error": {"code": "bad_signature", "message": "No match."}})], []
    assert g.record_reason(tx, BRL["feedId"], E1, "3 % off Pyth spot") == (False, "bad_signature")
    assert ["issuedAt" in p["body"] for p in session.posts] == [True, False]


class Custody:
    """A custody or hardware signer: an address and the two sign calls of an eth_account LocalAccount."""

    def __init__(self, inner, tx_key=None, nonce_shift=0, text_fails=False):
        self.address = inner.address
        self._inner, self._tx_key, self._shift, self._text_fails = inner, tx_key or inner, nonce_shift, text_fails
        self.asked: list[dict] = []

    def sign_transaction(self, tx):
        self.asked.append(tx)
        return self._tx_key.sign_transaction({**tx, "nonce": tx["nonce"] + self._shift})

    def sign_message(self, message):
        if self._text_fails:
            raise TimeoutError("the device did not answer")
        return self._inner.sign_message(message)


def test_an_account_signer_cancels_and_signs_the_reason_as_a_key_does(make, node, session, key, tmp_path):
    node.members.add(key.address.lower())
    node.propose(BRL["feedId"], E1, 6_160_000, NOW + 3600, block=100)
    session.routes[("POST", "/api/v1/reasons")] = (200, {"recorded": True, "tx": "0x" + w(9), "firm": "Test firm"})
    signer = Custody(key)
    g = make(with_key=False, account=signer)

    c = g.cancel(proposal(), reason="3 % off Pyth spot")

    assert g.address == key.address.lower() and c.reason_recorded is True
    assert node.sent == [{"from": key.address.lower(), "to": ROLES, "data": PINNED}] and len(signer.asked) == 1
    [post] = session.posts
    assert post["body"]["signer"] == key.address.lower()
    for extra in ({"key": key.key.hex()}, {"key_file": tmp_path / "k"}):
        with pytest.raises(crx.ConfigError, match=r"^Set one of key, key_file and account\.$"):
            crx.Guardian(test=True, account=signer, **extra)
    with pytest.raises(crx.ConfigError, match="account needs an address"):
        crx.Guardian(test=True, account=object())


def test_an_account_signature_of_another_transaction_or_wallet_is_refused_and_a_failed_text_leaves_the_cancel(
        make, node, session, key):
    node.members.add(key.address.lower())
    node.propose(BRL["feedId"], E1, 6_160_000, NOW + 3600, block=100)
    other = Account.from_key(hashlib.sha256(b"crx-python tests: another key").digest())
    for signer in (Custody(key, nonce_shift=1), Custody(key, tx_key=other)):
        with pytest.raises(crx.RefusedToSign, match="nothing was sent"):
            make(with_key=False, account=signer).cancel(proposal(), reason="3 % off Pyth spot")
    assert node.sent == [] and session.posts == []

    c = make(with_key=False, account=Custody(key, text_fails=True)).cancel(proposal(), reason="3 % off Pyth spot")

    assert (c.reason_recorded, c.reason_error, session.posts) == (False, "not_signed", [])
    assert (BRL["feedId"], E1) not in node.pending
    assert str(c).endswith("The reason is not recorded (not_signed); the cancel stands.")


def test_post_test_price_and_proposals_compare_with_one_reference(make, node, session):
    ref = ("GET", "/api/v1/reference", "USD/BRL")
    session.routes[ref] = (200, {"pair": "USD/BRL", "pyth": 5.980589999999999, "pythAt": NOW - 2, "crx": 5.98064,
                                 "crxAt": NOW - 600, "base": "pyth"})
    g = make()
    clock = [NOW]
    g._clock = lambda: clock[0]

    p = g.post_test_price("USD/BRL", off_bp=300)
    session.routes[ref] = (200, {"pair": "USD/BRL", "pyth": 5.99, "pythAt": NOW + 5, "crx": 5.98064,
                                 "crxAt": NOW - 600, "base": "pyth"})
    clock[0] = NOW + 10
    [q] = g.proposals()

    assert (p.reference, p.reference_source, p.price, p.diff_bp) == (Decimal("5.98059"), "pyth", Decimal("6.160008"),
                                                                     Decimal("300.0"))
    assert (q.tx, q.reference, q.reference_source, q.reference_at, q.diff_bp) == (
        p.tx, p.reference, p.reference_source, p.reference_at, p.diff_bp)
    clock[0] = NOW + 20  # the kept reference lapses; the portal's base names the CRX print
    session.routes[ref] = (200, {"pair": "USD/BRL", "pyth": 5.99, "pythAt": NOW - 900, "crx": 5.98064,
                                 "crxAt": NOW - 600, "base": "crx"})
    [q] = g.proposals()
    assert (q.reference, q.reference_source) == (Decimal("5.98064"), "crx")
    clock[0] = NOW + 40  # a portal without /reference: its proposal rows carry the reference
    del session.routes[ref]
    session.routes[("GET", "/api/v1/proposals")] = (200, [{"pair": "USD/BRL", "reference": {
        "pyth": 5.985, "pythAt": NOW + 30, "crx": None, "crxAt": None}}])
    [q] = g.proposals()
    assert (q.reference, q.reference_source) == (Decimal("5.985"), "pyth")
