"""send_tx fees, and the replacement of this SDK's own pending tx at the next nonce."""

from types import SimpleNamespace

import pytest
import rlp
from eth_utils import keccak, to_checksum_address

from crx import _chain
from crx import _eip712 as e7
from crx._chain import Rpc, TxLog, send_tx
from crx.errors import TxFailed

from .conftest import CHAIN_ID, RPC

TOKEN = "0xa52c60e6e14190dad2739f4b401aa3264ae68cb8"
CORE = "0x" + "c0" * 20
APPROVE = e7.calldata("approve(address,uint256)", ["address", "uint256"], [CORE, 10**9])
GWEI = 10**9


def num(b):
    return int.from_bytes(b, "big")


def decode(raw):
    """Return (type, fields) of a signed raw tx, with type 2 for EIP-1559 and 0 for legacy."""
    b = bytes.fromhex(raw[2:])
    return (2, rlp.decode(b[1:])) if b[0] == 2 else (0, rlp.decode(b))


def fee_cap(raw):
    kind, f = decode(raw)
    return num(f[3]) if kind == 2 else num(f[1])  # maxFeePerGas, else gasPrice


class Chain:
    """Script a moving base fee: a tx mines only while its fee cap covers it."""

    def __init__(self, session, base, gas_price, tip):
        self.base, self.sent, self.polls = base, [], 0
        session.rpc.update({
            "eth_call": "0x",
            "eth_estimateGas": hex(100_000),
            "eth_gasPrice": hex(gas_price),
            "eth_getBlockByNumber": lambda p: {"number": "0x10",
                                               **({} if self.base is None else {"baseFeePerGas": hex(self.base)})},
            "eth_sendRawTransaction": self.send,
            "eth_getTransactionReceipt": self.receipt,
        })
        if tip is None:
            session.rpc.pop("eth_maxPriorityFeePerGas", None)
        else:
            session.rpc["eth_maxPriorityFeePerGas"] = hex(tip)

    def send(self, params):
        self.sent.append(params[0])
        return "0x" + "ab" * 32

    def receipt(self, params):
        self.polls += 1
        if self.base is not None and fee_cap(self.sent[-1]) < self.base:
            if self.polls >= 3:
                pytest.fail(f"the tx is stuck: fee cap {fee_cap(self.sent[-1])} under base fee {self.base}")
            return None
        return {"status": "0x1", "blockNumber": "0x11", "logs": []}


@pytest.mark.parametrize("page", [(502, "<html>502 Bad Gateway</html>")], ids=["502"])
def test_a_send_with_no_clear_answer_is_watched_and_recorded(session, account, tmp_path, page):
    """A proxy error page after the send is no refusal, because the node may hold the tx."""
    chain = Chain(session, base=30 * GWEI, gas_price=31 * GWEI, tip=GWEI)
    first = chain.send

    def send_then_page(params):
        first(params)
        return {"__http__": page}
    session.rpc["eth_sendRawTransaction"] = send_then_page
    log = TxLog(tmp_path / "txs.json")
    h = send_tx(Rpc(RPC, session), account, CHAIN_ID, "approve", TOKEN, APPROVE, sleep=lambda s: None, log=log)
    want = "0x" + keccak(bytes.fromhex(chain.sent[0][2:])).hex()
    assert h == want and len(chain.sent) == 1 and [r["hash"] for r in log.rows()] == [want]


OLD = 118_760_000  # wei: a legacy approve that holds nonce 12 on mainnet
NOW = 1_790_000_600  # unix s on the mocked wall clock at t = 0: 600 s after the recorded send
H_OLD = "0x" + "cd" * 32
UNDERPRICED = {"__error__": {"code": -32000, "message": "replacement transaction underpriced"}}
PENDING = r"^approve not sent: a transaction from this wallet is pending at nonce {}; it must confirm before this step can be sent\.$"
LEGACY = {"type": 0, "tip": OLD, "cap": OLD, "hash": H_OLD}


def fields(raw):
    """Return (nonce, tip, fee cap) of a signed raw tx, with a legacy gasPrice as both fees."""
    kind, f = decode(raw)
    return (num(f[1]), num(f[2]), num(f[3])) if kind == 2 else (num(f[0]), num(f[1]), num(f[1]))


def row(account, old, nonce=12):
    """Return the tx record row of this SDK's own tx ``old`` at ``nonce``."""
    fee = ({"type": 2, "maxFeePerGas": old["cap"], "maxPriorityFeePerGas": old["tip"]} if old["type"] == 2
           else {"gasPrice": old["cap"]})
    return {"chain_id": CHAIN_ID, "from": account.address, "nonce": nonce, "hash": old["hash"],
            "to": TOKEN, "data": APPROVE, **fee, "time": 1_790_000_000}


class Pool(Chain):
    """A node whose pool holds the sender's pending tx at nonce ``latest``, with a mocked clock.

    The node takes a tx at that nonce only at +10% on both fee fields. With ``shown``, the pending
    count includes the held tx. ``refuse`` refuses every tx at that nonce. ``mine_after`` mines the
    held tx at that sleep.
    """

    def __init__(self, session, account, old, monkeypatch, *, base=129_000_000, gas_price=130_000_000,
                 tip=1_000_000, shown=True, refuse=False, mine_after=None):
        super().__init__(session, base, gas_price, tip)
        self.addr, self.held, self.shown, self.refuse = account.address, old, shown, refuse
        self.latest, self.sleeps, self.mine_after, self.t = 12, [], mine_after, 0.0
        session.rpc["eth_getTransactionCount"] = self.count
        session.rpc["eth_getTransactionByHash"] = self.by_hash
        monkeypatch.setattr(_chain, "time", SimpleNamespace(monotonic=lambda: self.t))
        monkeypatch.setattr(_chain, "wall", lambda: NOW + self.t)

    def count(self, params):
        return hex(self.latest + (params[1] == "pending" and self.shown and self.held is not None))

    def by_hash(self, params):
        o = self.held
        if not o or params[0] != o["hash"]:
            return None
        t = {"hash": params[0], "from": self.addr.lower(), "nonce": hex(self.latest), "blockNumber": None}
        if o["type"] == 2:
            return {**t, "type": "0x2", "maxPriorityFeePerGas": hex(o["tip"]), "maxFeePerGas": hex(o["cap"])}
        return {**t, "type": "0x0", "gasPrice": hex(o["cap"])}

    def send(self, params):
        n, tip, cap = fields(params[0])
        self.sent.append(params[0])
        if n == self.latest and self.held is not None:
            if self.refuse or tip * 10 < self.held["tip"] * 11 or cap * 10 < self.held["cap"] * 11:
                return UNDERPRICED
            self.held = None
        return "0x" + f"{len(self.sent):064x}"

    def receipt(self, params):
        r = super().receipt(params)
        if r:
            self.latest = fields(self.sent[-1])[0] + 1
        return r

    def sleep(self, s):
        self.sleeps.append(len(self.sent))
        self.t += s
        if self.mine_after is not None and len(self.sleeps) >= self.mine_after and self.held is not None:
            self.held, self.latest = None, self.latest + 1


def send_on(pool, session, account, log=None):
    return send_tx(Rpc(RPC, session), account, CHAIN_ID, "approve", TOKEN, APPROVE, sleep=pool.sleep, log=log)


@pytest.mark.parametrize("record", ["another hash"])
def test_an_unknown_pending_tx_is_not_replaced_and_raises_after_60_s(session, account, tmp_path, monkeypatch, record):
    log = TxLog(tmp_path / "txs.json")
    if record:
        log.add({**row(account, LEGACY), "hash": "0x" + "ee" * 32})  # the SDK's tx at 12 is not the node's
    pool = Pool(session, account, dict(LEGACY), monkeypatch)
    with pytest.raises(TxFailed, match=PENDING.format(12)) as e:
        send_on(pool, session, account, log)
    assert e.value.details == {"step": "approve", "nonce": 12}
    assert pool.sent == [] and pool.t == 60 and len(pool.sleeps) == 30
    assert "eth_sendRawTransaction" not in session.rpc_methods()


def test_the_tx_record_is_mode_600_and_holds_no_key(session, account, tmp_path, monkeypatch):
    path = tmp_path / "state" / "txs.json"
    log = TxLog(path)
    other = "0x" + "11" * 20
    for n in (10, 11):
        log.add(row(account, {**LEGACY, "hash": "0x" + f"{n:064x}"}, nonce=n))
    log.add({**row(account, LEGACY, nonce=3), "from": other})
    pool = Pool(session, account, None, monkeypatch)
    tx = send_on(pool, session, account, log)
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    text = path.read_text()
    assert account.key.hex().removeprefix("0x") not in text.lower()
    rows = log.rows()
    assert [(r["from"], r["nonce"]) for r in rows] == [(other, 3), (account.address, 12)]  # 10 and 11 are mined
    _, f = decode(pool.sent[0])
    assert rows[1] == {
        "chain_id": CHAIN_ID, "from": account.address, "nonce": 12,
        "hash": "0x" + keccak(bytes.fromhex(pool.sent[0][2:])).hex(),
        "to": to_checksum_address(TOKEN), "data": APPROVE,
        "type": 2, "maxFeePerGas": num(f[3]), "maxPriorityFeePerGas": num(f[2]), "time": rows[1]["time"],
    }
    assert tx == "0x" + f"{1:064x}" and isinstance(rows[1]["time"], int)
