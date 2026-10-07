"""send_tx fees: type 2 where the chain has a base fee, legacy at 1.25 x gasPrice where it has none.
This SDK's own pending tx at the next nonce is replaced at +12.5% or more on both fee fields; another is not."""

import threading
import time
from types import SimpleNamespace

import pytest
import rlp
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from crx import _chain
from crx import _eip712 as e7
from crx._chain import TIP_FLOOR, Rpc, TxLog, send_tx
from crx.errors import BadAnswer, TxFailed

from .conftest import BASE, CHAIN_ID, RPC, Clock

TOKEN = "0xa52c60e6e14190dad2739f4b401aa3264ae68cb8"
CORE = "0x" + "c0" * 20
APPROVE = e7.calldata("approve(address,uint256)", ["address", "uint256"], [CORE, 10**9])
GWEI = 10**9


def num(b):
    return int.from_bytes(b, "big")


def decode(raw):
    """(type, fields) of a signed raw tx: 2 for EIP-1559, 0 for legacy."""
    b = bytes.fromhex(raw[2:])
    return (2, rlp.decode(b[1:])) if b[0] == 2 else (0, rlp.decode(b))


def fee_cap(raw):
    kind, f = decode(raw)
    return num(f[3]) if kind == 2 else num(f[1])  # maxFeePerGas, else gasPrice


class Chain:
    """One base fee that moves; a tx mines only while its fee cap covers it."""

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


def send(session, account):
    return send_tx(Rpc(RPC, session), account, CHAIN_ID, "approve", TOKEN, APPROVE, sleep=lambda s: None)


def test_a_base_fee_rise_after_the_send_still_mines(session, account):
    # Mainnet, 12b: the approve went out at 0.11876 gwei and the base fee rose to 0.129 gwei.
    chain = Chain(session, base=118_760_000, gas_price=118_760_000, tip=1_000_000)
    first = chain.send

    def send_then_rise(params):
        out = first(params)
        chain.base = 129_000_000
        return out

    session.rpc["eth_sendRawTransaction"] = send_then_rise
    assert send(session, account) == "0x" + "ab" * 32
    assert len(chain.sent) == 1 and fee_cap(chain.sent[0]) >= 129_000_000


@pytest.mark.parametrize("page", [(502, "<html>502 Bad Gateway</html>"), (504, "<html>504</html>"),
                                  (502, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "x"}})],
                         ids=["502", "504", "502_json"])
def test_a_send_with_no_clear_answer_is_watched_and_recorded(session, account, tmp_path, page):
    """A proxy page after the send is no refusal: the node may hold the tx. Its own hash is watched, the
    tx record holds it, and it is sent once."""
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


def test_a_5xx_rpc_answer_is_no_node_answer(session):
    session.rpc["eth_chainId"] = {"__http__": (502, {"jsonrpc": "2.0", "id": 1, "error": {"code": 1, "message": "x"}})}
    with pytest.raises(BadAnswer) as ei:
        Rpc(RPC, session)("eth_chainId")
    assert str(ei.value) == "the RPC answered HTTP 502"


@pytest.mark.parametrize("served, tip", [(2 * GWEI, 2 * GWEI), (0, TIP_FLOOR), (None, TIP_FLOOR)])
def test_a_type_2_tx_carries_the_fee_fields(session, account, served, tip):
    base = 30 * GWEI
    chain = Chain(session, base=base, gas_price=31 * GWEI, tip=served)
    send(session, account)
    kind, f = decode(chain.sent[0])
    assert kind == 2 and Account.recover_transaction(chain.sent[0]) == account.address
    assert num(f[0]) == CHAIN_ID and num(f[1]) == 7
    assert num(f[2]) == tip and num(f[3]) == 2 * base + tip
    assert num(f[4]) == 120_000 and "0x" + f[5].hex() == TOKEN and num(f[6]) == 0
    assert "0x" + f[7].hex() == APPROVE and list(f[8]) == []


def test_a_chain_with_no_base_fee_gets_legacy_at_1_25_x(session, account):
    chain = Chain(session, base=None, gas_price=25 * GWEI, tip=GWEI)
    send(session, account)
    kind, f = decode(chain.sent[0])
    assert kind == 0 and Account.recover_transaction(chain.sent[0]) == account.address
    assert num(f[1]) == 31_250_000_000 and num(f[2]) == 120_000
    assert "eth_maxPriorityFeePerGas" not in session.rpc_methods()


def test_the_receipt_wait_is_300_s(session, account, monkeypatch):
    chain = Chain(session, base=GWEI, gas_price=GWEI, tip=GWEI)
    session.rpc["eth_getTransactionReceipt"] = None
    t = [0.0]
    monkeypatch.setattr(_chain, "time", SimpleNamespace(monotonic=lambda: t[0]))
    with pytest.raises(TxFailed, match=r"^approve tx 0x(ab){32} has no receipt after 300 s$"):
        send_tx(Rpc(RPC, session), account, CHAIN_ID, "approve", TOKEN, APPROVE,
                sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert 300 < t[0] <= 302 and len(chain.sent) == 1


@pytest.mark.parametrize("base", ["-0x1", "-0x3b9aca00"])
def test_a_base_fee_below_0_is_refused_before_any_send(session, account, base):
    chain = Chain(session, base=GWEI, gas_price=GWEI, tip=GWEI)
    session.rpc["eth_getBlockByNumber"] = {"number": "0x10", "baseFeePerGas": base}
    with pytest.raises(BadAnswer, match="baseFeePerGas"):
        send(session, account)
    assert chain.sent == [] and "eth_sendRawTransaction" not in session.rpc_methods()



# ---------- a pending tx at the next nonce ----------

OLD = 118_760_000  # wei: a legacy approve that holds nonce 12 on mainnet
NOW = 1_790_000_600  # unix s on the mocked wall clock at t = 0: 600 s after the recorded send
H_OLD = "0x" + "cd" * 32
UNDERPRICED = {"__error__": {"code": -32000, "message": "replacement transaction underpriced"}}
PENDING = r"^approve not sent: a transaction from this wallet is pending at nonce {}; it must confirm before this step can be sent\.$"
REFUSED = r"^approve not sent: the replacement at nonce 12 was refused by the node\.$"
LEGACY = {"type": 0, "tip": OLD, "cap": OLD, "hash": H_OLD}
TYPE2 = {"type": 2, "tip": 2 * GWEI, "cap": 25 * GWEI, "hash": H_OLD}


def up(x):
    """1.125 x, rounded up."""
    return -(-x * 9 // 8)


def fields(raw):
    """(nonce, tip, fee cap) of a signed raw tx. A legacy gasPrice is both."""
    kind, f = decode(raw)
    return (num(f[1]), num(f[2]), num(f[3])) if kind == 2 else (num(f[0]), num(f[1]), num(f[1]))


def row(account, old, nonce=12):
    """The tx record row of this SDK's own tx ``old`` at ``nonce``."""
    fee = ({"type": 2, "maxFeePerGas": old["cap"], "maxPriorityFeePerGas": old["tip"]} if old["type"] == 2
           else {"gasPrice": old["cap"]})
    return {"chain_id": CHAIN_ID, "from": account.address, "nonce": nonce, "hash": old["hash"],
            "to": TOKEN, "data": APPROVE, **fee, "time": 1_790_000_000}


class Pool(Chain):
    """A node whose pool holds one pending tx of the sender at nonce ``latest``, and a mocked clock.

    It takes a tx at that nonce only at +10% on both fee fields. ``shown``: the pending count
    shows the held tx; else both counts are ``latest``. ``refuse``: it refuses every tx at that nonce.
    ``mine_after``: the held tx mines at that sleep.
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


@pytest.mark.parametrize("old, gas, tip, cap", [
    (LEGACY, (129_000_000, 130_000_000, 1_000_000), 133_605_000, 391_605_000),
    (TYPE2, (30 * GWEI, 31 * GWEI, 2 * GWEI), 2_250_000_000, 62_250_000_000),
])
def test_an_own_recorded_tx_at_the_nonce_is_replaced_over_12_5_pct(session, account, tmp_path, monkeypatch,
                                                                   old, gas, tip, cap):
    log = TxLog(tmp_path / "txs.json")
    log.add(row(account, old))
    base, gas_price, served = gas
    pool = Pool(session, account, dict(old), monkeypatch, base=base, gas_price=gas_price, tip=served)
    send_on(pool, session, account, log)
    assert [fields(r) for r in pool.sent] == [(12, tip, cap)]
    assert tip >= up(old["tip"]) and cap >= up(old["cap"]) and pool.held is None and pool.sleeps == []
    assert [r["nonce"] for r in log.rows()] == [12, 12] and log.rows()[1]["maxFeePerGas"] == cap


@pytest.mark.parametrize("record", [None, "another hash"])
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


@pytest.mark.parametrize("mines", [True, False])
def test_an_own_tx_sent_under_60_s_ago_gets_60_s_to_mine(session, account, tmp_path, monkeypatch, mines):
    # Another send of this SDK, in flight: it mines, or it is replaced once 60 s old (sent 5 s before t = 0).
    log = TxLog(tmp_path / "txs.json")
    log.add({**row(account, LEGACY), "time": NOW - 5})
    pool = Pool(session, account, dict(LEGACY), monkeypatch, mine_after=3 if mines else None)
    send_on(pool, session, account, log)
    if mines:
        assert [fields(r) for r in pool.sent] == [(13, TIP_FLOOR, 2 * 129_000_000 + TIP_FLOOR)]
        assert pool.sleeps == [0, 0, 0]
    else:
        assert [fields(r) for r in pool.sent] == [(12, 133_605_000, 391_605_000)]
        assert pool.sleeps == [0] * 28 and pool.t == 56


def test_an_unknown_pending_tx_that_mines_in_the_wait_goes_on_as_0_7_1(session, account, tmp_path, monkeypatch):
    pool = Pool(session, account, dict(LEGACY), monkeypatch, mine_after=3)
    send_on(pool, session, account, TxLog(tmp_path / "txs.json"))
    assert [fields(r) for r in pool.sent] == [(13, TIP_FLOOR, 2 * 129_000_000 + TIP_FLOOR)]
    assert pool.sleeps == [0, 0, 0] and session.rpc_methods().count("eth_call") == 2


def test_an_own_tx_the_count_hides_is_replaced_after_the_refusal(session, account, tmp_path, monkeypatch):
    # The node that counts does not hold the SDK's stuck tx; the node that takes the send does.
    log = TxLog(tmp_path / "txs.json")
    log.add(row(account, LEGACY))
    pool = Pool(session, account, dict(LEGACY), monkeypatch, shown=False)
    send_on(pool, session, account, log)
    assert [fields(r) for r in pool.sent] == [(12, TIP_FLOOR, 2 * 129_000_000 + TIP_FLOOR),
                                              (12, 133_605_000, 391_605_000)]
    assert pool.held is None and pool.sleeps == []


def test_an_unknown_tx_the_count_hides_is_not_replaced(session, account, tmp_path, monkeypatch):
    pool = Pool(session, account, dict(LEGACY), monkeypatch, shown=False)
    with pytest.raises(TxFailed, match=PENDING.format(12)):
        send_on(pool, session, account, TxLog(tmp_path / "txs.json"))
    assert len(pool.sent) == 1 and pool.t == 60


@pytest.mark.parametrize("shown, sends", [(True, 1), (False, 2)])
def test_an_own_replacement_the_node_still_refuses(session, account, tmp_path, monkeypatch, shown, sends):
    log = TxLog(tmp_path / "txs.json")
    log.add(row(account, LEGACY))
    pool = Pool(session, account, dict(LEGACY), monkeypatch, shown=shown, refuse=True)
    with pytest.raises(TxFailed, match=REFUSED) as e:
        send_on(pool, session, account, log)
    assert e.value.details == {"step": "approve", "nonce": 12}
    assert [fields(r)[0] for r in pool.sent] == [12] * sends and "eth_getTransactionReceipt" not in session.rpc_methods()
    assert len(log.rows()) == 1


@pytest.mark.parametrize("base, served, tip", [(129_000_000, 1_000_000, TIP_FLOOR), (30 * GWEI, 2 * GWEI, 2 * GWEI)])
@pytest.mark.parametrize("with_log", [False, True])
def test_no_pending_tx_keeps_the_0_7_1_fees(session, account, tmp_path, monkeypatch, base, served, tip, with_log):
    pool = Pool(session, account, None, monkeypatch, base=base, gas_price=base + served, tip=served)
    send_on(pool, session, account, TxLog(tmp_path / "txs.json") if with_log else None)
    assert [fields(r) for r in pool.sent] == [(12, tip, 2 * base + tip)]
    assert pool.sleeps == [] and "eth_getTransactionByHash" not in session.rpc_methods()


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


def test_a_tx_with_no_receipt_is_replaced_at_once_on_the_next_send(session, account, tmp_path, monkeypatch):
    log = TxLog(tmp_path / "txs.json")
    pool = Pool(session, account, None, monkeypatch)
    session.rpc["eth_getTransactionReceipt"] = None
    with pytest.raises(TxFailed, match="has no receipt after 300 s"):
        send_on(pool, session, account, log)
    (first,) = log.rows()
    _, tip, cap = fields(pool.sent[0])
    pool.held = {"type": 2, "tip": tip, "cap": cap, "hash": first["hash"]}  # still pending at 12
    session.rpc["eth_getTransactionReceipt"] = pool.receipt
    session.rpc_calls.clear()
    pool.sleeps.clear()
    send_on(pool, session, account, log)
    n, tip2, cap2 = fields(pool.sent[1])
    assert n == 12 and tip2 >= up(tip) and cap2 >= up(cap) and pool.held is None and pool.sleeps == []


def test_deposit_replaces_its_own_stuck_approve_then_deposits_at_the_next_nonce(
        make_client, session, health, account, tmp_path, monkeypatch):
    from .test_money import core_of, deposit_route, money_view

    raw = 1000 * 10**6
    approve = e7.calldata("approve(address,uint256)", ["address", "uint256"], [to_checksum_address(core_of(health)), raw])
    TxLog(tmp_path / "state" / "txs.json").add({**row(account, LEGACY), "data": approve})
    pool = Pool(session, account, dict(LEGACY), monkeypatch)

    def call(params):
        data = params[0]["data"]
        if data == e7.calldata("decimals()", [], []):
            return hex(6)
        return hex(raw) if data.startswith(e7.h0x(e7.selector("balanceOf(address)"))) else "0x"

    session.rpc["eth_call"] = call
    deposit_route(session, health, account, raw)
    money_view(session, account, pool, "deposit")
    clock = Clock(time.time())
    c = make_client(clock=clock)
    c._sleep = lambda s: (pool.sleep(s), clock.sleep(s))
    d = c.deposit(1000, mint=False)
    assert d.status == "credited" and len(d.txs) == 2
    assert [fields(r) for r in pool.sent] == [(12, 133_605_000, 391_605_000),
                                              (13, TIP_FLOOR, 2 * 129_000_000 + TIP_FLOOR)]
    assert [r["nonce"] for r in TxLog(tmp_path / "state" / "txs.json").rows()] == [13]


@pytest.mark.parametrize("what, to, data", [
    ("deposit", CORE, e7.calldata("deposit(uint256)", ["uint256"], [10**9])),
    ("approve", TOKEN, e7.calldata("approve(address,uint256)", ["address", "uint256"], [CORE, 2 * 10**9])),
], ids=["deposit", "other-approve"])
def test_a_tx_with_another_intent_is_not_sent_over_a_pending_approve(session, account, tmp_path, monkeypatch,
                                                                      what, to, data):
    log = TxLog(tmp_path / "txs.json")
    log.add(row(account, LEGACY))  # the SDK's own approve of 10**9, pending at 12
    pool = Pool(session, account, dict(LEGACY), monkeypatch)
    with pytest.raises(TxFailed, match=PENDING.format(12).replace("approve", what, 1)):
        send_tx(Rpc(RPC, session), account, CHAIN_ID, what, to, data, sleep=pool.sleep, log=log)
    assert pool.sent == [] and pool.t == 60 and pool.held is not None


@pytest.mark.parametrize("polls", [1, 4])
def test_the_replaced_tx_mines_first_and_the_step_returns_its_hash(session, account, tmp_path, monkeypatch, polls):
    log = TxLog(tmp_path / "txs.json")
    log.add(row(account, LEGACY))
    pool = Pool(session, account, dict(LEGACY), monkeypatch)
    seen = []

    def receipt(params):
        seen.append(params[0])
        if params[0] == H_OLD and seen.count(H_OLD) >= polls:
            return {"status": "0x1", "blockNumber": "0x11", "logs": []}
        return None  # the replacement never mines

    session.rpc["eth_getTransactionReceipt"] = receipt
    assert send_on(pool, session, account, log) == H_OLD
    assert [fields(r)[0] for r in pool.sent] == [12] and seen[:2] == ["0x" + f"{1:064x}", H_OLD]
    assert seen.count(H_OLD) == polls and pool.t == 2 * (polls - 1)


def test_concurrent_writers_keep_every_row(tmp_path):
    path = tmp_path / "state" / "txs.json"
    start = threading.Barrier(8)

    def writer(i):
        log = TxLog(path)
        start.wait()
        for k in range(12):
            log.add({"chain_id": 1000 * i + k, "from": "0x" + "22" * 20, "nonce": 0, "hash": "0x" + f"{i:032x}{k:032x}"})

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r["chain_id"] for r in TxLog(path).rows()) == sorted(1000 * i + k for i in range(8) for k in range(12))
    lock = path.with_name("txs.json.lock")
    assert lock.stat().st_mode & 0o777 == 0o600 and sorted(p.name for p in path.parent.iterdir()) == ["txs.json", "txs.json.lock"]


@pytest.mark.parametrize("own_dir, mode", [(True, 0o700), (False, 0o755)])
def test_only_the_sdk_own_state_dir_is_forced_to_700(tmp_path, account, own_dir, mode):
    folder = tmp_path / "state"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    TxLog(folder / "txs.json", own_dir=own_dir).add(row(account, LEGACY))
    assert folder.stat().st_mode & 0o777 == mode and (folder / "txs.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("named, mode", [("env", 0o755), ("arg", 0o755), ("default", 0o700)])
def test_a_state_dir_the_user_named_keeps_its_mode(session, account, tmp_path, monkeypatch, named, mode):
    import crx
    from crx import client as client_mod

    folder = tmp_path / "named"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    monkeypatch.delenv("CRX_STATE_DIR", raising=False)
    if named == "env":
        monkeypatch.setenv("CRX_STATE_DIR", str(folder))
    elif named == "default":
        monkeypatch.setattr(client_mod, "DEFAULT_STATE_DIR", str(folder))
    c = crx.Client(key=account.key.hex(), base_url=BASE, rpc_url=RPC, session=session,
                   state_dir=folder if named == "arg" else None)
    pool = Pool(session, account, None, monkeypatch)
    c._sleep = pool.sleep
    c._send("approve", TOKEN, APPROVE)
    assert [fields(r)[0] for r in pool.sent] == [12]
    assert folder.stat().st_mode & 0o777 == mode and (folder / "txs.json").stat().st_mode & 0o777 == 0o600
