"""deposit(), withdraw() and the seat reads against a scripted gateway and chain."""

import copy
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

import crx
import crx._chain
from crx import _eip712 as e7

from .conftest import BASE, CHAIN_ID, RPC, Clock, fixture

TOKEN = "0xa52c60e6e14190dad2739f4b401aa3264ae68cb8"


def core_of(health):
    return next(c for c in health["chains"] if c["key"] == "avax-fuji")["core"]


class Chain:
    """Script a chain that records each send and mines every tx with status 1."""

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


def balance_body(account, nonce="3", deposit=None, withdraw=None, free="20000.000000"):
    return {"account": account.address.lower(), "chain": "avax-fuji", "state": "live",
            "collateral": "21111.000000", "free": free, "equity": "21000.000000", "im": "100.000000",
            "mm": "20.000000", "open_legs": 2, "as_of": 1_790_000_000_000, "as_of_block": 52_000_123,
            "withdrawing": "0.000000", "deposit": {"last": deposit},
            "withdraw": {"live": False, "nonce": nonce, "last": withdraw}}


COUNTED = {"final": True}  # a credited deposit that counts in free collateral


def money_view(session, account, chain, key, status="credited", tx=None, **last):
    """Serve GET /balance with ``<key>.last`` naming ``tx`` (by default the newest sent tx) at ``status``."""
    def view(req):
        if not chain.sent:
            return balance_body(account)
        h = tx or "0x" + f"{len(chain.sent):064x}"
        counted = COUNTED if key == "deposit" and status == "credited" else {}
        return balance_body(account, **{key: {"tx": h, "status": status, **counted, **last}})
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


def test_deposit_with_no_receipt_is_send_unknown_and_sends_once(make_client, session, health, account, monkeypatch):
    """With no receipt after 300 s the deposit may still be mined, so the error is SendUnknown with its hash."""
    chain = Chain(session, held=10**12)
    deposit_route(session, health, account, 1000 * 10**6, approve=False)
    session.rpc["eth_getTransactionReceipt"] = None
    clock = Clock(time.time())
    monkeypatch.setattr(crx._chain, "time", SimpleNamespace(monotonic=clock))
    with pytest.raises(crx.SendUnknown) as ei:
        make_client(clock=clock).deposit(1000, mint=False)
    e = ei.value
    h = "0x" + f"{1:064x}"  # the hash the node answered
    assert not isinstance(e, crx.TxFailed) and e.code == "send_unknown" and e.tx == h and len(chain.sent) == 1
    assert str(e) == f"deposit tx {h}. Status unknown; check this transaction before you send again."


def test_deposit_bad_wait_sends_nothing(make_client, session, health, account):
    chain = Chain(session, held=10**12)
    deposit_route(session, health, account, 1000 * 10**6)
    for wait in (-1, float("nan"), float("inf"), "30", True):
        with pytest.raises(crx.BadRequest) as e:
            make_client().deposit(1000, wait=wait)
        assert "wait" in str(e.value), wait
    assert chain.sent == [] and session.paths("POST") == []


def test_deposit_before_health_names_the_base_token_is_not_ready(make_client, session, health, account):
    h = copy.deepcopy(health)
    next(c for c in h["chains"] if c["key"] == "avax-fuji").pop("base_token", None)
    session.routes[("GET", "/health")] = h
    chain = Chain(session, held=1000 * 10**6)
    deposit_route(session, health, account, 1000 * 10**6)
    with pytest.raises(crx.CrxError) as e:
        make_client(clock=Clock(time.time())).deposit(1000)
    assert e.value.code == "not_ready" and chain.sent == []
    assert str(e.value) == "the base token on avax-fuji is not ready yet; it becomes readable once /health names it."


def deposit_for(account_address, amount_raw):
    return e7.calldata("depositFor(address,uint256)", ["address", "uint256"],
                       [to_checksum_address(account_address), amount_raw])


UNEXPECTED = "^the gateway served a tx this SDK does not expect; nothing sent$"
DEPOSIT_EDITS = [  # row, amount_raw served, calldata edit, words of the refusal
    ("foreign-tx", 1000 * 10**6, lambda acct, raw: e7.calldata(
        "transfer(address,uint256)", ["address", "uint256"], ["0x" + "99" * 20, 1]), UNEXPECTED),
    ("another-address", 1000 * 10**6, lambda acct, raw: deposit_for("0x" + "99" * 20, raw), UNEXPECTED),
    ("another-amount", 1000 * 10**6, lambda acct, raw: deposit_for(acct, raw + 1), UNEXPECTED),  # its own address
    ("wrong-amount-raw", 2000 * 10**6, None, "amount_raw"),
]


def test_deposit_other_than_asked_sends_nothing(make_client, session, health, account):
    for row, raw, edit, words in DEPOSIT_EDITS:
        chain = Chain(session, held=10**12)
        deposit_route(session, health, account, raw, data_edit=edit and edit(account.address, raw))
        with pytest.raises(crx.RefusedToSign) as e:
            make_client().deposit(1000)
        assert re.search(words, str(e.value)) and chain.sent == [], row


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


def test_deposit_short_of_usdc_sends_nothing(make_client, session, health, account):
    chain = Chain(session, held=12_500_000)
    deposit_route(session, health, account, 1000 * 10**6)
    with pytest.raises(crx.TxFailed, match=r"^not enough USDC: you have 12\.500000, need 1000\.000000$") as e:
        make_client(clock=Clock(time.time())).deposit(1000, mint=False)
    assert e.value.details == {"step": "deposit", "have": "12.500000", "need": "1000.000000"}
    assert chain.sent == []
    approve = e7.h0x(e7.selector("approve(address,uint256)"))
    assert not [p for m, p in session.rpc_calls if m in ("eth_call", "eth_estimateGas")
                and p[0]["data"].startswith(approve)]
    assert "eth_estimateGas" not in session.rpc_methods()


LANDED = "0x" + "7a" * 32
OWN = object()  # stands for the item of the withdraw the gateway queued
SEAT = object()  # stands for the seat's own address
WITHDRAW_TYPE = "WithdrawIntent(address account,uint256 amount,uint64 nonce,uint64 deadline)"
WITHDRAW_KINDS = ["address", "uint256", "uint64", "uint64"]


def intent_words(w):
    return [to_checksum_address(w["account"]), int(w["amount"]), int(w["nonce"]), int(w["deadline"])]


def intent_digest(sep, w):
    """Build the WithdrawIntent digest from its type string, without the SDK."""
    struct = keccak(encode(["bytes32", *WITHDRAW_KINDS], [keccak(text=WITHDRAW_TYPE), *intent_words(w)]))
    return keccak(b"\x19\x01" + sep + struct)


def intent_item(w, recipient=None):
    """Hash the withdraw item, with a ``recipient`` word before the nonce when given."""
    kinds, words = ["uint256", *WITHDRAW_KINDS], [5, *intent_words(w)]
    if recipient is not None:
        kinds.insert(3, "address")
        words.insert(3, to_checksum_address(recipient))
    return keccak(encode(kinds, words))


class Gate:
    """Run POST /withdraw as the gateway does: rebuild the intent, recover its signer, answer its item.

    ``edit`` changes the queued intent. ``recipient`` adds a recipient word to the item (SEAT: the seat).
    """

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
        """Serve GET /balance with ``withdraw.last`` at ``status`` for ``item``, by default the queued one."""
        return lambda req: {**balance_body(self.account, withdraw=item_row(
            status, self.item if item is OWN else item, tx)), **kw}


def gate(session, health, account, **kw):
    return Gate(session, account, CHAIN_ID, core_of(health), **kw)


def item_row(status, item, tx=None):
    return {"item": item, "tx": tx, "nonce": "3", "amount": "1000.000000", "paid": None, "status": status,
            "reason": None}


def test_withdraw_one_signed_post_sends_no_tx(make_client, session, health, account):
    chain = Chain(session)
    g = gate(session, health, account)
    session.routes[("GET", "/balance")] = [balance_body(account), g.view("sending"), g.view("accepted", tx=LANDED)]
    clock = Clock(time.time())
    now = int(clock())
    out = make_client(clock=clock).withdraw(1000)
    assert (out.status, out.item, out.tx, out.nonce) == ("accepted", g.item, LANDED, 3)
    # Only the chain check reads the RPC.
    assert chain.sent == [] and set(session.rpc_methods()) <= {"eth_chainId", "eth_getCode"}
    posts = [c for c in session.calls if c["method"] == "POST"]
    assert [c["path"] for c in posts] == ["/withdraw"]
    body = posts[0]["body"]
    # The body names no recipient, because the chain pays the account.
    assert {k: v for k, v in body.items() if k != "sig"} == {
        "chain": "avax-fuji", "amount": "1000", "nonce": "3", "deadline": now + 22 * 3600}
    assert 120 < body["deadline"] - now <= 82_800  # the gateway's deadline window
    assert g.intent == {"account": account.address.lower(), "amount": 1000 * 10**6, "nonce": 3,
                        "deadline": now + 22 * 3600}
    assert g.signer == account.address  # the digest signed is the gateway's 4-member rebuild


REBUILDS = [("1000", "0"), ("0.000001", "3"), ("12345.678901", "18446744073709551615")]  # amount, nonce


def test_withdraw_signs_the_gateway_rebuild(make_client, session, health, account):
    Chain(session)
    for amount, nonce in REBUILDS:
        g = gate(session, health, account)
        session.routes[("GET", "/balance")] = [balance_body(account, nonce=nonce), g.view("accepted")]
        out = make_client(clock=Clock(time.time())).withdraw(amount)
        assert g.signer == account.address, amount
        assert g.intent["amount"] == int(Decimal(amount).scaleb(6)) and g.intent["nonce"] == int(nonce), amount
        assert out.nonce == int(nonce) and out.item == g.item, amount


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
                   session=session, core=core)
    clock = Clock(int(m["deadline"]) - 22 * 3600)
    c._clock, c._sleep = clock, clock.sleep
    out = c.withdraw(Decimal(m["amount"]).scaleb(-6))
    body = next(x for x in session.calls if x["method"] == "POST")["body"]
    assert body["sig"] == w1["signature"] and body["deadline"] == int(m["deadline"]) and body["nonce"] == m["nonce"]
    assert out.item == g.item == e7.h0x(intent_item({**m, "account": taker.address}))


OTHER_ITEM = "/withdraw queued an item other than the signed withdraw"
ITEM_ANSWERS = [  # gate edit, words of the refusal
    (dict(edit={"amount": 2000 * 10**6}), OTHER_ITEM),
    (dict(edit={"account": "0x" + "99" * 20}), OTHER_ITEM),
    (dict(recipient="0x" + "99" * 20), OTHER_ITEM),
    (dict(item=None), "/withdraw named no item"),
]


def test_withdraw_refuses_an_answer_other_than_the_signed_item(make_client, session, health, account):
    Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account)
    for reads, (kw, words) in enumerate(ITEM_ANSWERS, 1):
        g = gate(session, health, account, **kw)
        with pytest.raises(crx.BadAnswer) as e:
            make_client().withdraw(1000)
        assert words in str(e.value) and g.signer == account.address, kw
        assert polls(session, "/balance") == reads and session.paths("POST") == ["/withdraw"] * reads, kw


SERVICE_DOWN = {"code": "service_unavailable", "outcome": "unavailable", "error": "service temporarily unavailable",
                "message": "Service temporarily unavailable: trade not executed."}


def test_withdraw_service_unavailable_is_posted_once(make_client, session, account, monkeypatch):
    Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account)
    session.routes[("POST", "/withdraw")] = (503, SERVICE_DOWN)
    signed = []
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(a) or "0x" + "22" * 65)
    clock = Clock(time.time())
    c = make_client(clock=clock)
    t0 = clock()
    with pytest.raises(crx.ServiceUnavailable):
        c.withdraw("10")
    assert len(signed) == 1 and session.paths("POST") == ["/withdraw"] and clock() == t0
    assert c._held_withdraw is None


def withdraw_posts(session):
    return [x["body"] for x in session.calls if x["method"] == "POST" and x["path"] == "/withdraw"]


def timeout_after(take):
    """Return a route that lets the gateway take the post, then times out."""
    def route(req):
        take(req)
        raise requests.ReadTimeout("read timed out")
    return route


class Money:
    """Serve GET /balance: nonce 3 and no withdraw until ``shown``, then the item at ``word`` and nonce 4."""

    def __init__(self, account, g):
        self.account, self.g, self.shown, self.word = account, g, False, "accepted"

    def __call__(self, req):
        if not self.shown:
            return balance_body(self.account)
        return balance_body(self.account, nonce="4", withdraw=item_row(self.word, self.g.item, LANDED))


def returned_pending(make_client, session, health, account):
    """Return a client whose first withdraw(1000) returned ``pending``."""
    Chain(session)
    g = gate(session, health, account)
    money = Money(account, g)
    session.routes[("GET", "/balance")] = money
    clock = Clock(time.time())
    c = make_client(clock=clock)
    first = c.withdraw(1000)
    assert first.status == "pending" and c._held_withdraw["returned"] is True
    return c, money, clock, first


def test_withdraw_after_a_returned_withdraw_signs_the_new_one_only_once_it_is_paid(
        make_client, session, health, account):
    c, money, clock, first = returned_pending(make_client, session, health, account)
    t0 = clock()
    with pytest.raises(crx.WithdrawInProgress) as ei:
        c.withdraw(1000, wait=12)
    e = ei.value
    assert e.code == "withdraw_in_progress" and e.status is None and first.item in str(e)
    assert e.details == {"item": first.item, "nonce": "3", "amount": "1000", "status": "pending"}
    assert clock() - t0 == 10 and len(withdraw_posts(session)) == 1 and c._held_withdraw["returned"] is True
    # The earlier withdraw is paid, so the call neither waits nor raises EarlierOutcome.
    money.shown, money.word = True, "paid"
    t0 = clock()
    second = c.withdraw(1000)
    assert clock() == t0 and second.status == "paid" and second.item != first.item
    a, b = withdraw_posts(session)
    assert (a["nonce"], b["nonce"]) == ("3", "4") and a["sig"] != b["sig"]
    assert c._held_withdraw is None


def test_withdraw_after_a_returned_withdraw_that_ends_refused_says_so_once(make_client, session, health, account):
    c, money, clock, first = returned_pending(make_client, session, health, account)
    money.shown, money.word = True, "refused"
    with pytest.raises(crx.EarlierOutcome) as ei:
        c.withdraw(1000)
    assert ei.value.details == {"item": first.item, "nonce": "3", "amount": "1000", "status": "refused"}
    assert c._held_withdraw is None and len(withdraw_posts(session)) == 1
    money.word = "paid"
    assert c.withdraw(1000).status == "paid"
    assert [b["nonce"] for b in withdraw_posts(session)] == ["3", "4"]


def test_withdraw_after_a_raise_never_waits(make_client, session, health, account):
    Chain(session)
    g = gate(session, health, account)
    session.routes[("POST", "/withdraw")] = timeout_after(g.answer)
    money = Money(account, g)
    session.routes[("GET", "/balance")] = money
    clock = Clock(time.time())
    c = make_client(clock=clock)
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    t0 = clock()
    with pytest.raises(crx.WithdrawUnknown, match=r"call withdraw\(1000\) to read it"):
        c.withdraw(500)
    assert clock() == t0 and c._held_withdraw["returned"] is False
    money.shown, money.word = True, "paid"
    with pytest.raises(crx.EarlierOutcome) as ei:
        c.withdraw(500)
    assert ei.value.details["status"] == "paid" and clock() == t0 and len(withdraw_posts(session)) == 1


def test_withdraw_nonce_passed_row_of_another_amount_is_not_the_intent(make_client, session, health, account):
    Chain(session)
    g = gate(session, health, account)
    session.routes[("POST", "/withdraw")] = timeout_after(lambda req: None)
    session.routes[("GET", "/balance")] = Money(account, g)
    c = make_client(clock=Clock(time.time()))
    with pytest.raises(crx.WithdrawUnknown):
        c.withdraw(1000)
    row = dict(item_row("paid", None, LANDED), amount="250.000000")
    session.routes[("GET", "/balance")] = balance_body(account, nonce="4", withdraw=row)
    with pytest.raises(crx.EarlierOutcome, match="read balance"):
        c.withdraw(1000)


UNPROVEN = [  # row, POST /withdraw answer, GET /balance answer, posts; None keeps the route
    ("refused-before-the-held-check", (503, {"code": "mark_unavailable", "error": "starting",
                                            "details": {"venue_status": "unknown"}}), None, 2),
    ("unread-balance", None, (503, {"code": "mark_unavailable", "error": "starting"}), 1),
]


def test_withdraw_repeat_that_cannot_prove_the_intent_missing_keeps_the_hold(make_client, session, health, account):
    # A refusal before the gateway's held-item check, or an unread /balance, leaves the first post possibly queued.
    Chain(session)
    for row, post, balance, posts in UNPROVEN:
        before = len(withdraw_posts(session))
        g = gate(session, health, account)
        session.routes[("POST", "/withdraw")] = timeout_after(g.answer)
        session.routes[("GET", "/balance")] = Money(account, g)
        clock = Clock(time.time())
        c = make_client(clock=clock)
        with pytest.raises(crx.WithdrawUnknown):
            c.withdraw(1000)
        if post is not None:
            session.routes[("POST", "/withdraw")] = post
        if balance is not None:
            session.routes[("GET", "/balance")] = balance
        clock.t += 300
        with pytest.raises(crx.WithdrawUnknown):
            c.withdraw(1000)
        assert c._held_withdraw is not None, row
        with pytest.raises(crx.WithdrawUnknown):
            c.withdraw(500)
        assert len(withdraw_posts(session)) - before == posts, row


def test_withdraw_in_progress_with_no_wait_sends_nothing_more(make_client, session, account):
    chain = Chain(session)
    session.routes[("GET", "/balance")] = balance_body(account)
    session.routes[("POST", "/withdraw")] = (409, {
        "code": "withdraw_in_progress", "error": "one withdraw at a time; the next opens when this one is paid"})
    with pytest.raises(crx.WithdrawInProgress) as ei:
        make_client().withdraw(1000, wait=0)
    assert ei.value.code == "withdraw_in_progress" and ei.value.status == 409
    assert len([c for c in session.calls if c["path"] == "/withdraw"]) == 1
    assert chain.sent == [] and polls(session, "/balance") == 1


def test_balance(make_client, session, account):
    session.routes[("GET", "/balance")] = balance_body(account)
    b = make_client().balance()
    assert (b.free, b.collateral, b.open_legs, b.withdraw_nonce) == (Decimal("20000"), Decimal("21111"), 2, 3)
    assert b.as_of_block == 52_000_123
    assert not hasattr(b, "pending_deposit") and not hasattr(b, "as_of_fold")
    call = next(c for c in session.calls if c["path"] == "/balance")
    assert call["query"] == {"chain": ["avax-fuji"]}


def test_balance_for_other_account_refused(make_client, session, account):
    session.routes[("GET", "/balance")] = balance_body(Account.create())
    with pytest.raises(crx.BadAnswer):
        make_client().balance()


def test_positions_read_the_entry_price_mark_and_pnl(make_client, session):
    row = {"trade_id": "0x01", "rfq_id": "0x02", "pair": "USD/MXN", "side": 1, "notional": "25000.000000",
           "rate": "18.700000", "status": "open", "entry_price": "18.700000", "mark_price": "18.750000",
           "mark_time": 1_790_000_000_123, "unrealized_pnl": "-12.345678"}
    unmarked = dict(row, trade_id="0x03", entry_price=None, mark_price=None, mark_time=None, unrealized_pnl=None,
                    rate="18.6", status="settled")
    unreadable = {"trade_id": "0x04", "rfq_id": "0x02", "pair": "USD/MXN", "side": 1, "notional": "1", "rate": "18.7",
                  "status": "open", "mark_price": "x", "mark_time": 1_790_000_000_123, "unrealized_pnl": "1.5"}
    session.routes[("GET", "/positions")] = {"positions": [row, unmarked, unreadable]}
    p, q, r = make_client().positions()
    assert (p.entry_price, p.mark_price, p.unrealized_pnl) == (
        Decimal("18.700000"), Decimal("18.750000"), Decimal("-12.345678"))
    assert p.mark_time == datetime(2026, 9, 21, 14, 13, 20, 123000, tzinfo=timezone.utc)
    assert p.raw["unrealized_pnl"] == "-12.345678" and p.side == "buy"
    # A null entry price reads the rate, and null mark keys read None.
    assert (q.entry_price, q.mark_price, q.mark_time, q.unrealized_pnl) == (Decimal("18.6"), None, None, None)
    assert r.mark_price is None


def test_trades_pages(make_client, session):
    page1 = {"trades": [{"type": "trade.opened", "ts": 1, "data": {"trade_id": str(i)}} for i in range(1000)], "seq": 1000}
    page2 = {"trades": [{"type": "trade.settled", "ts": 2, "data": {"trade_id": "0"}}], "seq": 1001}
    session.routes[("GET", "/trades")] = [page1, page2]
    ev = make_client().trades()
    assert len(ev) == 1001 and ev[-1].type == "trade.settled" and ev[-1].seq is None
    assert [c["query"]["after"] for c in session.calls if c["path"] == "/trades"] == [["0"], ["1000"]]


def test_reads_refuse_an_answer_they_cannot_read(make_client, session):
    page = {"trades": [{"type": "trade.opened", "ts": 1, "data": {"trade_id": str(i)}} for i in range(1000)], "seq": 7}
    rows = [
        ("markets", ("GET", "/markets"), {"markets": None}, "/markets lists no markets"),
        ("positions", ("GET", "/positions"), {"positions": None}, "/positions lists no positions"),
        ("trades", ("GET", "/trades"), {"trades": None, "seq": 0}, "/trades lists no trades"),
        ("trades", ("GET", "/trades"), page, "/trades did not advance its seq"),
    ]
    c = make_client()
    for call, route, body, msg in rows:
        session.routes[route] = body
        with pytest.raises(crx.BadAnswer) as ei:
            getattr(c, call)()
        assert str(ei.value) == msg, msg


def test_next_check_is_the_next_05_past_the_hour_on_testnet(make_client, session):
    rows = [
        ("2026-10-01T08:00:00", "2026-10-01T08:05:00"),
        ("2026-10-01T08:04:59", "2026-10-01T08:05:00"),
        ("2026-10-01T08:05:00", "2026-10-01T09:05:00"),
        ("2026-10-01T23:58:30", "2026-10-02T00:05:00"),
    ]
    for now, at in rows:
        t = datetime.fromisoformat(now).replace(tzinfo=timezone.utc).timestamp()
        got = make_client(key=False, clock=Clock(t)).next_check()
        assert got == datetime.fromisoformat(at).replace(tzinfo=timezone.utc) and got.tzinfo is not None, now
    assert session.calls == []


NY = ZoneInfo("America/New_York")
TAIL = timedelta(minutes=15)  # the reopen tail after the Sunday open
LONG_CLOSE = (datetime(2026, 10, 9, 17, 0, tzinfo=NY), datetime(2026, 10, 11, 18, 0, tzinfo=NY) + TAIL)
TWO_H = 2 * 3600


def ms(t):
    return int(t.timestamp()) * 1000


def mxn_at(make_client, session, markets, now, long_close=LONG_CLOSE):
    """Read USD/MXN at ``now``, leaving ``next_long_close`` out when ``long_close`` is None."""
    m = copy.deepcopy(markets)
    row = next(r for r in m["markets"] if r["pair"] == "USD/MXN")
    if long_close is not None:
        row["next_long_close"] = {"starts_at": ms(long_close[0]), "ends_at": ms(long_close[1])}
    session.routes[("GET", "/markets")] = m
    return make_client(key=False, clock=Clock(now.timestamp())).market("USD/MXN")


def test_next_valid_end_moves_out_of_the_long_close(make_client, session, markets):
    friday = datetime(2026, 10, 9, 15, 30, tzinfo=NY)
    m = mxn_at(make_client, session, markets, friday)
    assert m.next_long_close == tuple(t.astimezone(timezone.utc) for t in LONG_CLOSE)
    end = m.next_valid_end(TWO_H)
    assert end == LONG_CLOSE[1] and end.tzinfo == timezone.utc
    m = mxn_at(make_client, session, markets, friday, long_close=None)
    assert m.next_long_close is None and m.next_valid_end(TWO_H) == friday + timedelta(hours=2)
    for edge, moves in [(LONG_CLOSE[1], False), (LONG_CLOSE[0], True)]:
        m = mxn_at(make_client, session, markets, edge - timedelta(hours=2))
        assert m.next_valid_end(TWO_H) == (LONG_CLOSE[1] if moves else edge), edge


PAIRS = ("USD/BRL", "USD/MXN", "USD/PHP")


def test_health(make_client, health):
    assert make_client().health() == health


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


def test_withdraw_with_no_amount_takes_all_free_collateral_rounded_down(make_client, session, health, account):
    Chain(session)
    g = gate(session, health, account)
    session.routes[("GET", "/balance")] = [balance_body(account, free="1234.5678919"), g.view("accepted")]
    w = make_client(clock=Clock(time.time())).withdraw()
    body = next(c for c in session.calls if c["path"] == "/withdraw")["body"]
    assert body["amount"] == "1234.567891" and w.amount == Decimal("1234.567891")
    assert g.intent["amount"] == 1_234_567_891 and g.signer == account.address


FREE_REFUSALS = [  # amount, free, error, details
    ("1000.000001", "1000.000000", crx.InsufficientCollateral, {"free": "1000.000000", "requested": "1000.000001"}),
    (None, None, crx.MarkUnavailable, {}),
]


def test_withdraw_checks_free_collateral_before_a_signature(make_client, session, health, account, monkeypatch):
    Chain(session)
    gate(session, health, account)
    signed = []
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(1))
    for amount, free, err, details in FREE_REFUSALS:
        session.routes[("GET", "/balance")] = balance_body(account, free=free)
        c = make_client()
        with pytest.raises(err) as ei:
            c.withdraw(wait=0) if amount is None else c.withdraw(amount, wait=0)
        assert ei.value.details == details and ei.value.status is None, err
        assert signed == [] and session.paths("POST") == [] and c._held_withdraw is None, err


def test_withdraw_reads_an_unpriced_balance_again_every_5_s(make_client, session, health, account):
    # Free collateral reads None briefly after a settlement update.
    Chain(session)
    g = gate(session, health, account)
    clock = Clock(time.time())
    reads = []

    def balance(req):
        reads.append(clock())
        if len(reads) <= 2:
            return balance_body(account, free=None)
        return balance_body(account, free="100.000000") if len(reads) == 3 else g.view("accepted")(req)
    session.routes[("GET", "/balance")] = balance
    t0 = clock()
    w = make_client(clock=clock).withdraw()
    assert [t - t0 for t in reads[:3]] == [0, 5, 10] and w.status == "accepted"
    assert w.amount == Decimal("100") and session.paths("POST") == ["/withdraw"]


def test_withdraw_with_no_nonce_until_the_bound_raises_and_signs_nothing(
        make_client, session, health, account, monkeypatch):
    Chain(session)
    gate(session, health, account)
    b = balance_body(account, nonce=None)
    b["withdraw"]["nonce_reason"] = "settlement_unread"
    session.routes[("GET", "/balance")] = b
    signed = []
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(1))
    clock = Clock(time.time())
    t0 = clock()
    c = make_client(clock=clock)
    with pytest.raises(crx.RefusedToSign) as ei:
        c.withdraw(50)
    assert str(ei.value) == "/balance names no withdraw nonce for this seat (settlement_unread); nothing signed"
    assert ei.value.details == {"nonce_reason": "settlement_unread"}
    assert clock() - t0 == 90 and polls(session, "/balance") == 31
    assert signed == [] and session.paths("POST") == [] and c._held_withdraw is None


def test_withdraw_short_of_free_collateral_for_120_s_raises(make_client, session, health, account):
    Chain(session)
    gate(session, health, account)
    session.routes[("GET", "/balance")] = balance_body(account, free="0.500000")
    clock = Clock(time.time())
    t0 = clock()
    with pytest.raises(crx.BelowMin) as ei:
        make_client(clock=clock).withdraw()
    assert ei.value.details["free"] == "0.500000" and clock() - t0 == 120
    assert polls(session, "/balance") == 25 and session.paths("POST") == []


IN_FLIGHT = {"code": "trade_in_flight", "error": "a trade of this account is still opening; withdraw after it"}


def test_withdraw_posts_the_same_signed_intent_every_5_s_until_taken(
        make_client, session, health, account, monkeypatch):
    """The same signed intent goes again every 5 s until the refusal clears."""
    Chain(session)
    clock = Clock(time.time())
    g = gate(session, health, account)
    session.routes[("GET", "/balance")] = [balance_body(account), g.view("accepted")]
    posts = []

    def wd(req):
        posts.append((clock(), req["body"]))
        return (409, IN_FLIGHT) if len(posts) <= 3 else g.answer(req)
    session.routes[("POST", "/withdraw")] = wd
    signed = []
    real = crx.client.sign_typed
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(1) or real(*a, **k))
    c = make_client(clock=clock)
    t0 = clock()
    w = c.withdraw(1000)
    assert (w.status, w.item) == ("accepted", g.item) and g.signer == account.address
    assert [t - t0 for t, _ in posts] == [0, 5, 10, 15]
    assert all(b == posts[0][1] for _, b in posts) and len(signed) == 1
    assert c._held_withdraw["returned"] is True  # "accepted" is in progress, so the hold stays


AMOUNTS = [  # value, the amount or the words of the refusal
    ("1000", Decimal("1000")), (100, Decimal("100")), ("0.000001", Decimal("0.000001")),
    ("abc", "is not a number"), ("0", "must be above zero"), ("-1", "must be above zero"),
    ("NaN", "must be above zero"), ("Infinity", "must be above zero"), ("1.0000001", "more than 6 decimals"),
]


def test_amount_parsing():
    for value, out in AMOUNTS:
        if isinstance(out, Decimal):
            assert crx.client._amount(value) == out, value
        else:
            with pytest.raises(crx.BadRequest) as e:
                crx.client._amount(value)
            assert out in str(e.value), value
