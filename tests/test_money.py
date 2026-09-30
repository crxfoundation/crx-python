"""deposit(), withdraw() and the seat reads against a scripted gateway and chain."""

import time
from decimal import Decimal

import pytest
from eth_account import Account
from eth_utils import to_checksum_address

import crx
from crx import _eip712 as e7

from .conftest import CHAIN_ID, Clock

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


def balance_body(account, nonce="3", deposit=None, withdraw=None):
    return {"account": account.address.lower(), "chain": "avax-fuji", "state": "live",
            "collateral": "1111.000000", "free": "900.000000", "equity": "1000.000000", "im": "100.000000",
            "mm": "20.000000", "open_legs": 2, "as_of": 1_790_000_000_000, "as_of_block": 52_000_123,
            "withdrawing": "0.000000", "deposit": {"last": deposit},
            "withdraw": {"live": False, "nonce": nonce, "last": withdraw}}


def money_view(session, account, chain, key, status="credited", tx=None, **last):
    """GET /balance: ``<key>.last`` names ``tx`` (default: the newest sent tx) with ``status``."""
    def view(req):
        if not chain.sent:
            return balance_body(account)
        h = tx or "0x" + f"{len(chain.sent):064x}"
        return balance_body(account, **{key: {"tx": h, "status": status, **last}})
    session.routes[("GET", "/balance")] = view


def polls(session, path):
    return sum(1 for c in session.calls if c["path"] == path and c["method"] == "GET")


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
    money_view(session, account, chain, "deposit")
    d = make_client(clock=Clock(time.time())).deposit(1000)
    assert d.amount == Decimal(1000) and len(d.txs) == 3 and len(chain.sent) == 3
    assert d.status == "credited" and d.txs[-1] == "0x" + f"{3:064x}"
    assert all(Account.recover_transaction(r) == account.address for r in chain.sent)
    body = next(c for c in session.calls if c["path"] == "/deposit")["body"]
    assert body == {"chain": "avax-fuji", "amount": "1000"}


def test_deposit_no_mint(make_client, session, health, account):
    chain = Chain(session, held=0)
    deposit_route(session, health, account, 1000 * 10**6, approve=False)
    money_view(session, account, chain, "deposit")
    assert len(make_client(clock=Clock(time.time())).deposit(1000, mint=False).txs) == 1 and len(chain.sent) == 1


@pytest.mark.parametrize("word", ["credited", "failed"])
def test_deposit_polls_until_its_own_tx_is_final(make_client, session, health, account, word):
    chain = Chain(session, held=10**12)
    deposit_route(session, health, account, 1000 * 10**6)
    views = [balance_body(account, deposit={"tx": "0x" + "ee" * 32, "status": "credited", "amount": "5.000000"})]
    views += [balance_body(account, deposit={"tx": "0x" + f"{2:064x}", "status": "pending", "amount": "1000.000000"})] * 2
    views += [balance_body(account, deposit={"tx": "0x" + f"{2:064x}", "status": word, "amount": "1000.000000"})]
    session.routes[("GET", "/balance")] = views
    clock = Clock(time.time())
    t0 = clock()
    d = make_client(clock=clock).deposit(1000)
    assert d.status == word and polls(session, "/balance") == 4 and clock() - t0 == 3


def test_deposit_pending_after_30_s(make_client, session, health, account):
    chain = Chain(session, held=10**12)
    deposit_route(session, health, account, 1000 * 10**6)
    money_view(session, account, chain, "deposit", status="pending")
    clock = Clock(time.time())
    t0 = clock()
    assert make_client(clock=clock).deposit(1000).status == "pending"
    assert 30 <= clock() - t0 <= 31 and polls(session, "/balance") == 31


def test_deposit_other_tx_is_no_match(make_client, session, health, account):
    chain = Chain(session, held=10**12)
    deposit_route(session, health, account, 1000 * 10**6)
    money_view(session, account, chain, "deposit", tx="0x" + "ee" * 32)
    assert make_client(clock=Clock(time.time())).deposit(1000).status == "pending"


def test_deposit_poll_rides_out_gateway_errors(make_client, session, health, account):
    chain = Chain(session, held=10**12)
    deposit_route(session, health, account, 1000 * 10**6)
    done = balance_body(account, deposit={"tx": "0x" + f"{2:064x}", "status": "credited"})
    session.routes[("GET", "/balance")] = [(503, {"code": "upstream", "error": "down"}), "not json", done]
    assert make_client(clock=Clock(time.time())).deposit(1000).status == "credited"


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


LANDED = "0x" + "7a" * 32
OTHER = "0x" + "ee" * 32
OWN = object()  # stands for the item of the withdraw the gateway queued


class Gate:
    """POST /withdraw as the gateway runs it: rebuilds the intent from the body and the seat,
    recovers the signer of that rebuild, answers with its item. ``edit`` changes the queued intent."""

    def __init__(self, session, account, chain_id, core, status=202, item=OWN, edit=None):
        self.session, self.account = session, account
        self.sep = e7.domain_separator(chain_id, core)
        self.status, self.forced, self.edit = status, item, edit or {}
        self.intent = self.signer = self.item = None
        session.routes[("POST", "/withdraw")] = self.answer

    def answer(self, req):
        b, seat = req["body"], self.account.address.lower()
        self.intent = {"account": seat, "amount": e7.scaled6(b["amount"]), "recipient": seat,
                       "nonce": int(b["nonce"]), "deadline": int(b["deadline"])}
        self.signer = Account._recover_hash(e7.withdraw_digest(self.sep, self.intent), signature=b["sig"])
        self.item = e7.h0x(e7.withdraw_item({**self.intent, **self.edit}))
        item = self.item if self.forced is OWN else self.forced
        body = {"item": item, "status": "sending", "chain": b["chain"], "account": seat,
                "amount": f"{Decimal(b['amount']):.6f}", "nonce": b["nonce"], "deadline": b["deadline"]}
        return (self.status, {k: v for k, v in body.items() if v is not None})

    def view(self, status, item=OWN, tx=None, **kw):
        """GET /balance whose ``withdraw.last`` is ``item`` (default: the queued one) at ``status``."""
        return lambda req: {**balance_body(self.account, withdraw=item_row(
            status, self.item if item is OWN else item, tx)), **kw}


def gate(session, health, account, **kw):
    return Gate(session, account, CHAIN_ID, core_of(health), **kw)


def item_row(status, item, tx=None):
    return {"item": item, "tx": tx, "nonce": "3", "amount": "1000.000000", "paid": None, "status": status,
            "reason": None}


@pytest.mark.parametrize("code", [202, 200])
def test_withdraw_one_signed_post_sends_no_tx(make_client, session, health, account, code):
    chain = Chain(session)
    g = gate(session, health, account, status=code)
    session.routes[("GET", "/balance")] = [balance_body(account), g.view("sending"), g.view("accepted", tx=LANDED)]
    clock = Clock(time.time())
    now = int(clock())
    out = make_client(clock=clock).withdraw(1000)
    assert (out.status, out.item, out.tx, out.nonce) == ("accepted", g.item, LANDED, 3)
    # No tx: only the chain check reads the RPC. test_deposit_mints_approves_deposits is the control.
    assert chain.sent == [] and set(session.rpc_methods()) <= {"eth_chainId", "eth_getCode"}
    posts = [c for c in session.calls if c["method"] == "POST"]
    assert [c["path"] for c in posts] == ["/withdraw"]
    body = posts[0]["body"]
    assert {k: v for k, v in body.items() if k != "sig"} == {
        "chain": "avax-fuji", "amount": "1000", "nonce": "3", "deadline": now + 22 * 3600}
    assert 120 < body["deadline"] - now <= 82_800  # the gateway's deadline window
    assert g.intent == {"account": account.address.lower(), "amount": 1000 * 10**6,
                        "recipient": account.address.lower(), "nonce": 3, "deadline": now + 22 * 3600}
    assert g.signer == account.address  # the digest signed is the gateway's rebuild


@pytest.mark.parametrize("amount,nonce", [("1000", "0"), ("0.000001", "3"), ("12345.678901", "18446744073709551615")])
def test_withdraw_signs_the_gateway_rebuild(make_client, session, health, account, amount, nonce):
    Chain(session)
    g = gate(session, health, account)
    session.routes[("GET", "/balance")] = [balance_body(account, nonce=nonce), g.view("accepted")]
    out = make_client(clock=Clock(time.time())).withdraw(amount)
    assert g.signer == account.address and g.intent["amount"] == e7.scaled6(amount) and g.intent["nonce"] == int(nonce)
    assert out.nonce == int(nonce) and out.item == g.item


def test_withdraw_item_vector():
    seat = "0x7638646FcFf3E28E42Dc4a778ea7bbc236701230"
    w = {"account": seat, "amount": 1_000_000_000, "recipient": seat, "nonce": 3, "deadline": 1_790_082_800}
    assert e7.h0x(e7.withdraw_item(w)) == "0x46d62f771582cf423fca01063c182410c03b62dda1125c000036f8d515584b76"


@pytest.mark.parametrize("item,word", [(OWN, "accepted"), (OTHER, "pending"), (None, "pending")])
def test_withdraw_matches_by_item(make_client, session, health, account, item, word):
    # Same nonce and tx on every row: only the item names this withdraw.
    Chain(session)
    g = gate(session, health, account)
    session.routes[("GET", "/balance")] = [balance_body(account), g.view("accepted", item=item, tx=LANDED)]
    out = make_client(clock=Clock(time.time())).withdraw(1000)
    assert (out.status, out.tx) == (word, LANDED if word == "accepted" else None)


def test_withdraw_answer_without_item(make_client, session, health, account):
    Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account)
    gate(session, health, account, item=None)
    with pytest.raises(crx.BadAnswer, match="no item"):
        make_client().withdraw(1000)


@pytest.mark.parametrize("edit", [{"recipient": "0x" + "99" * 20}, {"amount": 2000 * 10**6}, {"nonce": 4},
                                  {"deadline": 1}, {"account": "0x" + "99" * 20}])
def test_withdraw_refuses_an_item_other_than_signed(make_client, session, health, account, edit):
    # The gateway queues a withdraw other than the one signed: its item differs.
    Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account)
    g = gate(session, health, account, edit=edit)
    with pytest.raises(crx.BadAnswer, match="other than the signed"):
        make_client().withdraw(1000)
    assert g.signer == account.address and polls(session, "/balance") == 1


@pytest.mark.parametrize("nonce", [None, "", "-1", "0x3"])
def test_withdraw_without_nonce_signs_nothing(make_client, session, health, account, nonce):
    Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account, nonce=nonce)
    gate(session, health, account)
    with pytest.raises(crx.RefusedToSign, match="nonce"):
        make_client().withdraw(1000)
    assert session.paths("POST") == []


@pytest.mark.parametrize("word", ["accepted", "refused", "paid"])
def test_withdraw_polls_until_its_own_item_is_final(make_client, session, health, account, word):
    Chain(session)
    g = gate(session, health, account)
    old = g.view("paid", item=OTHER, tx=OTHER)
    session.routes[("GET", "/balance")] = [
        old,  # the nonce read
        old, g.view("sending"), g.view("pending", tx=LANDED), g.view(word, tx=LANDED),
    ]
    clock = Clock(time.time())
    t0 = clock()
    assert make_client(clock=clock).withdraw(1000).status == word
    assert polls(session, "/balance") == 5 and clock() - t0 == 3


@pytest.mark.parametrize("word", ["sending", "pending"])
def test_withdraw_waits_30_s_on_testnet(make_client, session, health, account, word):
    chain = Chain(session)
    g = gate(session, health, account)
    session.routes[("GET", "/balance")] = [balance_body(account), g.view(word)]
    clock = Clock(time.time())
    t0 = clock()
    out = make_client(clock=clock).withdraw(1000)
    assert (out.status, out.tx) == (word, None)
    assert 30 <= clock() - t0 <= 31 and polls(session, "/balance") == 1 + 31 and chain.sent == []


def test_withdraw_in_progress_sends_nothing(make_client, session, account):
    chain = Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account)
    session.routes[("POST", "/withdraw")] = (409, {
        "code": "withdraw_in_progress", "error": "one withdraw at a time; the next opens when this one is paid"})
    with pytest.raises(crx.CrxError) as ei:
        make_client().withdraw(1000)
    assert ei.value.code == "withdraw_in_progress" and ei.value.status == 409
    assert chain.sent == [] and polls(session, "/balance") == 1


def test_balance(make_client, session, account):
    session.routes[("GET", "/balance")] = balance_body(account)
    b = make_client().balance()
    assert (b.free, b.collateral, b.open_legs, b.withdraw_nonce) == (Decimal("900"), Decimal("1111"), 2, 3)
    assert b.as_of_block == 52_000_123
    assert not hasattr(b, "pending_deposit") and not hasattr(b, "as_of_fold")
    call = next(c for c in session.calls if c["path"] == "/balance")
    assert call["query"] == {"chain": ["avax-fuji"]}


def test_balance_for_other_account_refused(make_client, session, account):
    session.routes[("GET", "/balance")] = balance_body(Account.create())
    with pytest.raises(crx.BadAnswer):
        make_client().balance()


def test_positions(make_client, session):
    session.routes[("GET", "/positions")] = {"positions": [
        {"trade_id": "0x01", "rfq_id": "0x02", "pair": "USDMXN", "side": -1, "notional": "25000.000000",
         "rate": "18.700000", "status": "pending"}]}
    (p,) = make_client().positions()
    assert (p.side, p.notional, p.rate, p.status) == ("sell", Decimal("25000"), Decimal("18.7"), "pending")


def test_trades_pages(make_client, session):
    page1 = {"trades": [{"type": "trade.opened", "ts": 1, "data": {"trade_id": str(i)}} for i in range(1000)], "seq": 1000}
    page2 = {"trades": [{"type": "trade.settled", "ts": 2, "data": {"trade_id": "0"}}], "seq": 1001}
    session.routes[("GET", "/trades")] = [page1, page2]
    ev = make_client().trades()
    assert len(ev) == 1001 and ev[-1].type == "trade.settled" and ev[-1].seq is None
    assert [c["query"]["since"] for c in session.calls if c["path"] == "/trades"] == [["0"], ["1000"]]


def test_trades_own_by_default(make_client, session):
    # A seat that takes and makes: the gateway serves client_rfq_id to the RFQ's taker only.
    tape = {"seq": 9, "trades": [
        {"type": "rfq.opened", "seq": 1, "ts": 1, "data": {"rfq_id": "0xa", "pair": "USDMXN", "client_rfq_id": "sdk-1"}},
        {"type": "rfq.opened", "seq": 2, "ts": 1, "data": {"rfq_id": "0xb", "pair": "USDBRL"}},
        {"type": "rfq.quoted", "seq": 3, "ts": 2, "data": {"rfq_id": "0xa", "rate": "18.7"}},
        {"type": "margin.notice", "seq": 4, "ts": 3, "data": {}},
        {"type": "trade.opened", "seq": 5, "ts": 4, "data": {"rfq_id": "0xa", "trade_id": "0xt"}},
        {"type": "rfq.opened", "seq": 6, "ts": 5, "data": {"rfq_id": "0xc", "pair": "USDPHP"}},
        {"type": "rfq.quoted", "seq": 7, "ts": 6, "data": {"rfq_id": "0xc", "rate": "58.1"}},
        {"type": "trade.opened", "seq": 8, "ts": 7, "data": {"rfq_id": "0xc", "trade_id": "0xu"}},
        {"type": "rfq.opened", "seq": 9, "ts": 8, "data": {"rfq_id": "0xd", "pair": "USDMXN", "client_rfq_id": ""}},
    ]}
    session.routes[("GET", "/trades")] = tape
    c = make_client()
    # Another seat's RFQ this seat quoted and filled (0xc) keeps its quote and fill, not its rfq.opened.
    assert [e.seq for e in c.trades()] == [1, 3, 4, 5, 7, 8]
    assert [e.seq for e in c.trades(market=True)] == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_markets_parse(make_client):
    ms = {m.pair: m for m in make_client(key=False).markets()}
    for live in ("USD/MXN", "USD/BRL", "USD/PHP"):
        assert not ms[live].paused and ms[live].min_notional == Decimal(10000)
    assert ms["USD/JPY"].paused
    assert ms["USD/MXN"].next_open is not None
