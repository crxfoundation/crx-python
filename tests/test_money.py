"""deposit(), withdraw() and the seat reads against a scripted gateway and chain."""

import time
from decimal import Decimal

import pytest
from eth_abi import decode
from eth_account import Account
from eth_utils import to_checksum_address

import crx
from crx import _eip712 as e7

from .conftest import CHAIN_ID

TOKEN = "0xa52c60e6e14190dad2739f4b401aa3264ae68cb8"


def core_of(health):
    return next(c for c in health["chains"] if c["key"] == "avax-fuji")["core"]


class Chain:
    """Counts sends; every receipt is status 1."""

    def __init__(self, session, held=0):
        self.sent = []
        self.held = held
        session.rpc["eth_estimateGas"] = hex(100_000)
        session.rpc["eth_call"] = self.call
        session.rpc["eth_sendRawTransaction"] = self.send
        session.rpc["eth_getTransactionReceipt"] = lambda p: {"status": "0x1", "blockNumber": "0x1", "logs": []}

    def call(self, params):
        data = params[0]["data"]
        if data == e7.calldata("decimals()", [], []):
            return hex(6)
        if data.startswith(e7.h0x(e7.selector("balanceOf(address)"))):
            return hex(self.held)
        return "0x"

    def send(self, params):
        self.sent.append(params[0])
        return "0x" + f"{len(self.sent):064x}"


def deposit_route(session, health, account, amount_raw, approve=True, data_edit=None):
    core = core_of(health)
    txs = [{"to": core, "chain_id": CHAIN_ID, "data": e7.calldata("deposit(uint256)", ["uint256"], [amount_raw])}]
    if approve:
        txs.insert(0, {"to": TOKEN, "chain_id": CHAIN_ID, "data": e7.calldata(
            "approve(address,uint256)", ["address", "uint256"], [to_checksum_address(core), amount_raw])})
    if data_edit:
        txs[-1]["data"] = data_edit
    session.routes[("POST", "/deposit")] = {"transactions": txs, "amount_raw": str(amount_raw)}


def test_deposit_mints_approves_deposits(make_client, session, health, account):
    chain = Chain(session, held=400 * 10**6)
    deposit_route(session, health, account, 1000 * 10**6)
    d = make_client().deposit(1000)
    assert d.amount == Decimal(1000) and len(d.txs) == 3 and len(chain.sent) == 3
    assert all(Account.recover_transaction(r) == account.address for r in chain.sent)
    body = next(c for c in session.calls if c["path"] == "/deposit")["body"]
    assert body == {"chain": "avax-fuji", "amount": "1000"}


def test_deposit_no_mint(make_client, session, health, account):
    chain = Chain(session, held=0)
    deposit_route(session, health, account, 1000 * 10**6, approve=False)
    assert len(make_client().deposit(1000, mint=False).txs) == 1 and len(chain.sent) == 1


def test_deposit_foreign_tx_sends_nothing(make_client, session, health, account):
    chain = Chain(session, held=10**12)
    deposit_route(session, health, account, 1000 * 10**6,
                  data_edit=e7.calldata("transfer(address,uint256)", ["address", "uint256"], ["0x" + "99" * 20, 1]))
    with pytest.raises(crx.RefusedToSign):
        make_client().deposit(1000)
    assert chain.sent == []


def test_deposit_wrong_amount_sends_nothing(make_client, session, health, account):
    chain = Chain(session, held=10**12)
    deposit_route(session, health, account, 2000 * 10**6)
    with pytest.raises(crx.RefusedToSign, match="amount_raw"):
        make_client().deposit(1000)
    assert chain.sent == []


def test_deposit_revert_names_error(make_client, session, health, account):
    chain = Chain(session, held=10**12)
    deposit_route(session, health, account, 1000 * 10**6, approve=False)
    sel = e7.h0x(e7.selector("NotWhitelisted()"))
    session.rpc["eth_call"] = lambda p: chain.call(p) if p[0]["data"] == e7.calldata("decimals()", [], []) \
        or p[0]["data"].startswith(e7.h0x(e7.selector("balanceOf(address)"))) \
        else {"__error__": {"code": 3, "message": "execution reverted", "data": sel}}
    with pytest.raises(crx.TxFailed, match="NotWhitelisted"):
        make_client().deposit(1000)
    assert chain.sent == []


def balance_body(account, nonce="3"):
    return {"account": account.address.lower(), "chain": "avax-fuji", "state": "live",
            "collateral": "1111.000000", "free": "900.000000", "equity": "1000.000000", "im": "100.000000",
            "mm": "20.000000", "pending_deposit": "0.000000", "open_legs": 2, "as_of": 1_790_000_000_000,
            "as_of_fold": 42, "withdraw": {"live": False, "nonce": nonce, "last": None}}


def withdraw_route(session, health, account, amount=1000, edit=None, digest=None):
    core = core_of(health)
    w = {"account": account.address.lower(), "amount": str(amount * 10**6), "recipient": account.address.lower(),
         "nonce": "3", "deadline": int(time.time()) + 3600}
    w.update(edit or {})
    sep = e7.domain_separator(CHAIN_ID, core)
    session.routes[("POST", "/withdraw")] = {"intent": w, "digest": digest or e7.h0x(e7.withdraw_digest(sep, w)),
                                             "core": core, "chain_id": CHAIN_ID, "kind": 5}
    return w, sep


def test_withdraw_signs_and_arms(make_client, session, health, account):
    chain = Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account)
    w, sep = withdraw_route(session, health, account)
    out = make_client().withdraw(1000)
    assert out.nonce == 3 and len(chain.sent) == 1
    # The armed envelope carries this seat's signature over the served intent.
    call = next(p for m, p in session.rpc_calls if m == "eth_estimateGas")[0]
    assert call["to"].lower() == core_of(health)
    kind, env = decode(["uint8", "bytes"], e7.hx(call["data"])[4:])
    item, sig = decode(["bytes", "bytes"], env)
    (sig,) = decode(["bytes"], sig)
    assert kind == 5
    assert Account._recover_hash(e7.withdraw_digest(sep, w), signature=sig) == account.address


@pytest.mark.parametrize("edit", [{"recipient": "0x" + "99" * 20}, {"amount": str(2000 * 10**6)}, {"nonce": "9"},
                                  {"deadline": int(time.time()) + 10 * 86_400}])
def test_withdraw_refuses_foreign_intent(make_client, session, health, account, edit):
    chain = Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account)
    withdraw_route(session, health, account, edit=edit)
    with pytest.raises(crx.RefusedToSign):
        make_client().withdraw(1000)
    assert chain.sent == []


def test_withdraw_refuses_served_digest_mismatch(make_client, session, health, account):
    chain = Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account)
    withdraw_route(session, health, account, digest="0x" + "ab" * 32)
    with pytest.raises(crx.RefusedToSign, match="digest"):
        make_client().withdraw(1000)
    assert chain.sent == []


def test_balance(make_client, session, account):
    session.routes[("GET", "/balance")] = balance_body(account)
    b = make_client().balance()
    assert (b.free, b.collateral, b.open_legs, b.withdraw_nonce) == (Decimal("900"), Decimal("1111"), 2, 3)
    call = next(c for c in session.calls if c["path"] == "/balance")
    assert call["query"] == {"chain": ["avax-fuji"]}


def test_balance_for_other_account_refused(make_client, session, account):
    session.routes[("GET", "/balance")] = balance_body(Account.create())
    with pytest.raises(crx.BadAnswer):
        make_client().balance()


def test_positions(make_client, session):
    session.routes[("GET", "/positions")] = {"positions": [
        {"trade_id": "0x01", "rfq_id": "0x02", "pair": "USDMXN", "side": -1, "notional": "25000.000000",
         "rate": "18.700000", "status": "opened"}]}
    (p,) = make_client().positions()
    assert (p.side, p.notional, p.rate) == ("sell", Decimal("25000"), Decimal("18.7"))


def test_trades_pages(make_client, session):
    page1 = {"trades": [{"type": "trade.opened", "ts": 1, "data": {"trade_id": str(i)}} for i in range(1000)], "seq": 1000}
    page2 = {"trades": [{"type": "trade.settled", "ts": 2, "data": {"trade_id": "0"}}], "seq": 1001}
    session.routes[("GET", "/trades")] = [page1, page2]
    ev = make_client().trades()
    assert len(ev) == 1001 and ev[-1].type == "trade.settled" and ev[-1].seq is None
    assert [c["query"]["since"] for c in session.calls if c["path"] == "/trades"] == [["0"], ["1000"]]


def test_markets_parse(make_client):
    ms = {m.pair: m for m in make_client(key=False).markets()}
    for live in ("USD/MXN", "USD/BRL", "USD/PHP"):
        assert not ms[live].paused and ms[live].min_notional == Decimal(10000)
    assert ms["USD/JPY"].paused
    assert ms["USD/MXN"].next_open is not None
