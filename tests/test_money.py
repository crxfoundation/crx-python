"""deposit(), withdraw() and the seat reads against a scripted gateway and chain."""

import copy
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

import crx
from crx import _eip712 as e7
from crx.signer import LocalSigner

from .conftest import BASE, CHAIN_ID, RPC, Clock, fixture

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
SEAT = object()  # stands for the seat's own address
WITHDRAW_TYPE = "WithdrawIntent(address account,uint256 amount,uint64 nonce,uint64 deadline)"
WITHDRAW_KINDS = ["address", "uint256", "uint64", "uint64"]


def intent_words(w):
    return [to_checksum_address(w["account"]), int(w["amount"]), int(w["nonce"]), int(w["deadline"])]


def intent_digest(sep, w):
    """The WithdrawIntent digest, built here from the type string: account, amount, nonce, deadline."""
    struct = keccak(encode(["bytes32", *WITHDRAW_KINDS], [keccak(text=WITHDRAW_TYPE), *intent_words(w)]))
    return keccak(b"\x19\x01" + sep + struct)


def intent_item(w, recipient=None):
    """keccak256(uint256(5) ‖ abi.encode(account, amount, nonce, deadline)). With ``recipient``, a
    recipient word sits between the amount and the nonce."""
    kinds, words = ["uint256", *WITHDRAW_KINDS], [5, *intent_words(w)]
    if recipient is not None:
        kinds.insert(3, "address")
        words.insert(3, to_checksum_address(recipient))
    return keccak(encode(kinds, words))


class Gate:
    """POST /withdraw as the gateway runs it: rebuilds the intent from the body and the seat,
    recovers the signer of that rebuild, answers with its item. ``edit`` changes the queued intent;
    ``recipient`` builds the item with a recipient word (SEAT: the seat's own address)."""

    def __init__(self, session, account, chain_id, core, status=202, item=OWN, edit=None, recipient=None):
        self.session, self.account = session, account
        self.sep = e7.domain_separator(chain_id, core)
        self.status, self.forced, self.edit = status, item, edit or {}
        self.recipient = account.address if recipient is SEAT else recipient
        self.intent = self.signer = self.item = None
        session.routes[("POST", "/withdraw")] = self.answer

    def answer(self, req):
        b, seat = req["body"], self.account.address.lower()
        self.intent = {"account": seat, "amount": int(Decimal(b["amount"]).scaleb(6)),
                       "nonce": int(b["nonce"]), "deadline": int(b["deadline"])}
        self.signer = Account._recover_hash(intent_digest(self.sep, self.intent), signature=b["sig"])
        self.item = "0x" + intent_item({**self.intent, **self.edit}, self.recipient).hex()
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
    # The body names no recipient: the chain pays the account.
    assert {k: v for k, v in body.items() if k != "sig"} == {
        "chain": "avax-fuji", "amount": "1000", "nonce": "3", "deadline": now + 22 * 3600}
    assert 120 < body["deadline"] - now <= 82_800  # the gateway's deadline window
    assert g.intent == {"account": account.address.lower(), "amount": 1000 * 10**6, "nonce": 3,
                        "deadline": now + 22 * 3600}
    assert g.signer == account.address  # the digest signed is the gateway's 4-member rebuild


def test_withdraw_signs_the_four_member_intent_on_the_health_domain(
        make_client, session, health, account, monkeypatch):
    Chain(session)
    g = gate(session, health, account)
    session.routes[("GET", "/balance")] = [balance_body(account), g.view("accepted")]
    seen = []
    sign = LocalSigner.sign_typed_data
    monkeypatch.setattr(LocalSigner, "sign_typed_data", lambda self, td: seen.append(copy.deepcopy(td)) or sign(self, td))
    clock = Clock(time.time())
    now = int(clock())
    make_client(clock=clock).withdraw(1000)
    (td,) = seen
    w1 = fixture("format-vectors-v5.json")["withdraw"]["typed_data"]
    assert td["primaryType"] == "WithdrawIntent" and td["types"] == w1["types"]
    assert [m["name"] for m in td["types"]["WithdrawIntent"]] == ["account", "amount", "nonce", "deadline"]
    assert td["domain"] == {"name": "CRX", "version": "rulebook-1.0", "chainId": CHAIN_ID,
                            "verifyingContract": core_of(health)}
    assert td["message"] == {"account": account.address.lower(), "amount": "1000000000", "nonce": "3",
                             "deadline": str(now + 22 * 3600)}


@pytest.mark.parametrize("amount,nonce", [("1000", "0"), ("0.000001", "3"), ("12345.678901", "18446744073709551615")])
def test_withdraw_signs_the_gateway_rebuild(make_client, session, health, account, amount, nonce):
    Chain(session)
    g = gate(session, health, account)
    session.routes[("GET", "/balance")] = [balance_body(account, nonce=nonce), g.view("accepted")]
    out = make_client(clock=Clock(time.time())).withdraw(amount)
    assert g.signer == account.address
    assert g.intent["amount"] == int(Decimal(amount).scaleb(6)) and g.intent["nonce"] == int(nonce)
    assert out.nonce == int(nonce) and out.item == g.item


def test_withdraw_digest_and_item_match_vector_w1():
    v = fixture("format-vectors-v5.json")
    w1, taker = v["withdraw"], v["keys"]["taker"]
    td, m = w1["typed_data"], w1["typed_data"]["message"]
    assert m["account"] == taker["address"] and w1["signer"] == "taker"
    w = {"account": m["account"], "amount": int(m["amount"]), "nonce": int(m["nonce"]), "deadline": int(m["deadline"])}
    chain_id, core = td["domain"]["chainId"], td["domain"]["verifyingContract"]
    sep = e7.domain_separator(chain_id, core)
    assert e7.h0x(sep) == v["domain_separator"]
    assert e7.h0x(e7.withdraw_digest(sep, w)) == w1["digest"] == e7.h0x(intent_digest(sep, w))
    assert e7.typed_data("WithdrawIntent", chain_id, core, e7.withdraw_message(w)) == td
    assert e7.h0x(Account.from_key(taker["private_key"]).sign_typed_data(full_message=td).signature) == w1["signature"]
    assert e7.withdraw_item(w) == intent_item(w)
    assert e7.withdraw_item(w) != intent_item(w, recipient=m["account"])


def test_withdraw_posts_vector_w1(session, tmp_path):
    # The client on W1's domain, key, nonce and deadline sends W1's signature and checks its item.
    v = fixture("format-vectors-v5.json")
    w1, taker = v["withdraw"], Account.from_key(v["keys"]["taker"]["private_key"])
    m, core = w1["typed_data"]["message"], w1["typed_data"]["domain"]["verifyingContract"]
    session.routes[("GET", "/health")] = {"status": "ok", "chains": [{
        "key": "avax-fuji", "chain_id": CHAIN_ID, "core": core, "domain": v["domain_separator"]}]}
    session.rpc["eth_getCode"] = lambda p: "0x6080" if p[0].lower() == core else "0x"
    Chain(session)
    g = Gate(session, taker, CHAIN_ID, core)
    session.routes[("GET", "/balance")] = [balance_body(taker, nonce=m["nonce"]), g.view("accepted")]
    c = crx.Client(key=v["keys"]["taker"]["private_key"], base_url=BASE, rpc_url=RPC, state_dir=tmp_path / "state",
                   session=session)
    clock = Clock(int(m["deadline"]) - 22 * 3600)
    c._clock, c._sleep = clock, clock.sleep
    out = c.withdraw(Decimal(m["amount"]).scaleb(-6))
    body = next(x for x in session.calls if x["method"] == "POST")["body"]
    assert body["sig"] == w1["signature"] and body["deadline"] == int(m["deadline"]) and body["nonce"] == m["nonce"]
    assert out.item == g.item == e7.h0x(intent_item({**m, "account": taker.address}))


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


@pytest.mark.parametrize("edit", [{"amount": 2000 * 10**6}, {"nonce": 4}, {"deadline": 1},
                                  {"account": "0x" + "99" * 20}])
def test_withdraw_refuses_an_item_other_than_signed(make_client, session, health, account, edit):
    # The gateway queues a withdraw other than the one signed: its item differs.
    Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account)
    g = gate(session, health, account, edit=edit)
    with pytest.raises(crx.BadAnswer, match="other than the signed"):
        make_client().withdraw(1000)
    assert g.signer == account.address and polls(session, "/balance") == 1


@pytest.mark.parametrize("recipient", [SEAT, "0x" + "99" * 20], ids=["seat", "other"])
def test_withdraw_refuses_an_item_with_a_recipient_word(make_client, session, health, account, recipient):
    # The item hashes account, amount, nonce, deadline. An item with a recipient word, even the
    # seat's own, is another withdraw. test_withdraw_one_signed_post_sends_no_tx is the control.
    Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account)
    g = gate(session, health, account, recipient=recipient)
    with pytest.raises(crx.BadAnswer, match="/withdraw queued an item other than the signed withdraw"):
        make_client().withdraw(1000)
    assert g.signer == account.address and polls(session, "/balance") == 1
    assert session.paths("POST") == ["/withdraw"]


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


def test_trades_limit_at_most_1000_and_loops(make_client, session):
    def page(start, n):
        rows = [{"type": "trade.opened", "seq": start + i + 1, "ts": 1, "data": {}} for i in range(n)]
        return {"trades": rows, "seq": start + n}
    session.routes[("GET", "/trades")] = [page(0, 1000), page(1000, 1000), page(2000, 3)]
    assert [e.seq for e in make_client().trades()] == list(range(1, 2004))
    q = [c["query"] for c in session.calls if c["path"] == "/trades"]
    assert [x["since"] for x in q] == [["0"], ["1000"], ["2000"]]
    assert all(1 <= int(x["limit"][0]) <= 1000 for x in q)


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


PAIRS = ("USD/BRL", "USD/MXN", "USD/PHP")
DROP = object()  # removes the key


def markets_with(markets, pair, **chain_row):
    """/markets with ``pair``'s avax-fuji row updated by ``chain_row``; a value ``DROP`` removes the key."""
    m = copy.deepcopy(markets)
    row = next(r for r in m["markets"] if r["pair"] == pair)["chains"][0]
    assert row["chain"] == "avax-fuji"
    row.update(chain_row)
    for k in [k for k, v in row.items() if v is DROP]:
        del row[k]
    return m


def test_markets_parse(make_client):
    ms = {m.pair: m for m in make_client(key=False).markets()}
    assert sorted(ms) == list(PAIRS)
    for pair in PAIRS:
        m = ms[pair]
        assert not m.paused and m.min_notional == Decimal(10000) and m.max_notional == Decimal(10_000_000)
        assert m.pair_id == "0x" + keccak(text=pair).hex() and (m.base, m.quote) == (pair[:3], pair[4:])
        assert m.max_premium_bps == 200 and m.open is False and (m.min_tenor_s, m.max_tenor_s) == (600, 7_776_000)
    assert ms["USD/MXN"].next_open == datetime.fromtimestamp(1_790_546_400, timezone.utc)


def test_a_pair_markets_does_not_list_is_not_offered(make_client, session):
    c = make_client(key=False)
    assert c.market("USDMXN").pair == "USD/MXN"  # the control: a listed pair
    with pytest.raises(crx.MarketPaused) as ei:
        c.market("USD/JPY")
    assert ei.value.code == "market_paused" and str(ei.value) == "USD/JPY is not offered on avax-fuji"
    assert ei.value.details == {"pair": "USD/JPY"} and session.paths("POST") == []


def test_a_pair_with_no_row_for_this_chain_is_paused(make_client, session, markets):
    session.routes[("GET", "/markets")] = markets_with(markets, "USD/PHP", chain="base")
    c = make_client(key=False)
    assert {m.pair: m.paused for m in c.markets()} == {"USD/BRL": False, "USD/MXN": False, "USD/PHP": True}
    with pytest.raises(crx.MarketPaused, match="USD/PHP is not offered on avax-fuji"):
        c.market("USD/PHP")
    assert session.paths("POST") == []


@pytest.mark.parametrize("paused,read", [(True, True), (False, False), (DROP, False)], ids=["true", "false", "missing"])
def test_markets_read_the_paused_key(make_client, session, markets, paused, read):
    session.routes[("GET", "/markets")] = markets_with(markets, "USD/BRL", paused=paused)
    c = make_client(key=False)
    assert {m.pair: m.paused for m in c.markets()} == {"USD/BRL": read, "USD/MXN": False, "USD/PHP": False}
    assert c.market("USD/BRL").paused is read


@pytest.mark.parametrize("cap,read", [(200, 200), (0, 0), (None, None), (DROP, None)], ids=["200", "0", "null", "missing"])
def test_markets_read_the_premium_cap(make_client, session, markets, cap, read):
    session.routes[("GET", "/markets")] = markets_with(markets, "USD/MXN", max_premium_bps=cap)
    ms = {m.pair: m for m in make_client(key=False).markets()}
    assert ms["USD/MXN"].max_premium_bps == read
    assert ms["USD/BRL"].max_premium_bps == ms["USD/PHP"].max_premium_bps == 200
