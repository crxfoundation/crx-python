"""deposit(), withdraw() and the seat reads against a scripted gateway and chain."""

import copy
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import rlp
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

import crx
import crx._chain
from crx import _eip712 as e7
from crx._chain import revert_name
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


def test_deposit_with_no_receipt_is_send_unknown_and_sends_once(make_client, session, health, account, monkeypatch):
    """No receipt after 300 s: the deposit can still be mined. SendUnknown with its hash, never TxFailed;
    one deposit tx sent."""
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


def test_deposit_mined_revert_is_tx_failed(make_client, session, health, account):
    chain = Chain(session, held=10**12)
    deposit_route(session, health, account, 1000 * 10**6, approve=False)
    session.rpc["eth_getTransactionReceipt"] = lambda p: {"status": "0x0", "blockNumber": "0x1", "logs": []}
    with pytest.raises(crx.TxFailed) as ei:
        make_client(clock=Clock(time.time())).deposit(1000, mint=False)
    assert type(ei.value) is crx.TxFailed and ei.value.details["tx"] == "0x" + f"{1:064x}" and len(chain.sent) == 1
    assert str(ei.value).endswith(" reverted")


def test_deposit_no_mint(make_client, session, health, account):
    chain = Chain(session, held=1000 * 10**6)
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


def deposit_for(account_address, amount_raw):
    return e7.calldata("depositFor(address,uint256)", ["address", "uint256"],
                       [to_checksum_address(account_address), amount_raw])


def sent_data(raw_tx):
    """The data field of a signed type-2 or legacy tx."""
    b = bytes.fromhex(raw_tx[2:])
    fields = rlp.decode(b[1:]) if b[0] == 2 else rlp.decode(b)
    return "0x" + (fields[7] if b[0] == 2 else fields[5]).hex()


@pytest.mark.parametrize("approve", [True, False])
def test_deposit_for_the_seat_itself_is_sent(make_client, session, health, account, approve):
    """A removed account: the gateway serves depositFor(own address). The SDK sends it as served."""
    chain = Chain(session, held=10**12)
    data = deposit_for(account.address, 1000 * 10**6)
    deposit_route(session, health, account, 1000 * 10**6, approve=approve, data_edit=data)
    money_view(session, account, chain, "deposit")
    d = make_client(clock=Clock(time.time())).deposit(1000, mint=False)
    assert d.status == "credited" and len(chain.sent) == len(d.txs) == (2 if approve else 1)
    assert sent_data(chain.sent[-1]) == data and all(Account.recover_transaction(r) == account.address
                                                     for r in chain.sent)


@pytest.mark.parametrize("edit", [
    lambda acct, raw: deposit_for("0x" + "99" * 20, raw),  # another address
    lambda acct, raw: deposit_for(acct, raw + 1),  # its own address, another amount
])
def test_deposit_for_another_address_or_amount_sends_nothing(make_client, session, health, account, edit):
    chain = Chain(session, held=10**12)
    deposit_route(session, health, account, 1000 * 10**6, data_edit=edit(account.address, 1000 * 10**6))
    with pytest.raises(crx.RefusedToSign, match="^the gateway served a tx this SDK does not expect; nothing sent$"):
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


def revert(data):
    return {"code": 3, "message": "execution reverted", "data": data}


ERROR_STRING = "0x08c379a0"
PANIC = "0x4e487b71"
SHORT = "ERC20: transfer amount exceeds balance"


@pytest.mark.parametrize("nest", [False, True])
def test_revert_reads_an_error_string(nest):
    data = ERROR_STRING + encode(["string"], [SHORT]).hex()
    assert revert_name(revert({"data": data} if nest else data)) == SHORT


@pytest.mark.parametrize("code, word", [(0x11, "panic 0x11"), (0x01, "panic 0x01"), (0x32, "panic 0x32")])
def test_revert_reads_a_panic(code, word):
    assert revert_name(revert(PANIC + encode(["uint256"], [code]).hex())) == word


def u256(n):
    return n.to_bytes(32, "big").hex()


@pytest.mark.parametrize("data", [
    ERROR_STRING + u256(32) + u256(2**255),  # a huge length
    ERROR_STRING + u256(32) + u256(2**64),
    ERROR_STRING + u256(2**255) + u256(5) + "00" * 32,  # a huge offset
    ERROR_STRING + u256(2**64) + u256(5) + "00" * 32,
])
def test_revert_a_huge_error_string_stays_hex(data):
    assert revert_name(revert(data)) == ERROR_STRING


@pytest.mark.parametrize("data, word", [
    ("0xdeadbeef" + "00" * 32, "0xdeadbeef"),
    ("0xdeadbeef", "0xdeadbeef"),
    (ERROR_STRING + "00" * 7, ERROR_STRING),  # an Error(string) it cannot read
    (e7.h0x(e7.selector("NotWhitelisted()")), "NotWhitelisted"),
])
def test_revert_unknown_data_stays_hex(data, word):
    assert revert_name(revert(data)) == word


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


def test_deposit_revert_reads_the_reason(make_client, session, health, account):
    chain = Chain(session, held=10**12)
    deposit_route(session, health, account, 1000 * 10**6, approve=False)
    data = ERROR_STRING + encode(["string"], [SHORT]).hex()
    session.rpc["eth_call"] = lambda p: chain.call(p) if p[0]["data"] == e7.calldata("decimals()", [], []) \
        or p[0]["data"].startswith(e7.h0x(e7.selector("balanceOf(address)"))) else {"__error__": revert(data)}
    with pytest.raises(crx.TxFailed, match=f"^deposit would revert: {SHORT}; nothing sent$"):
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
    assert [c["query"]["after"] for c in session.calls if c["path"] == "/trades"] == [["0"], ["1000"]]


def test_trades_limit_at_most_1000_and_loops(make_client, session):
    def page(start, n):
        rows = [{"type": "trade.opened", "seq": start + i + 1, "ts": 1, "data": {}} for i in range(n)]
        return {"trades": rows, "seq": start + n}
    session.routes[("GET", "/trades")] = [page(0, 1000), page(1000, 1000), page(2000, 3)]
    assert [e.seq for e in make_client().trades()] == list(range(1, 2004))
    q = [c["query"] for c in session.calls if c["path"] == "/trades"]
    assert [x["after"] for x in q] == [["0"], ["1000"], ["2000"]]
    assert all(1 <= int(x["limit"][0]) <= 1000 for x in q)


def test_trades_reads_after_the_seq_given_and_never_sends_since(make_client, session):
    """The tape cursor is ``after``: the gateway refuses ``since`` (400).
    Mutation: the query keeps ``since``, or ``trades()`` keeps a ``since=`` alias ⇒ red."""
    session.routes[("GET", "/trades")] = {"trades": [{"type": "trade.opened", "seq": 42, "ts": 1, "data": {}}],
                                          "seq": 42}
    c = make_client()
    assert [e.seq for e in c.trades(after=41)] == [42]
    assert [e.seq for e in c.trades(41)] == [42]
    q = [x["query"] for x in session.calls if x["path"] == "/trades"]
    assert [x["after"] for x in q] == [["41"], ["41"]] and all("since" not in x for x in q)
    with pytest.raises(TypeError):
        c.trades(since=41)


def test_trades_after_past_the_head_raises(make_client, session):
    """An ``after`` past this reader's head is a loud 409, never an empty list.
    Mutation: the SDK reads a 409 as an empty tape ⇒ red."""
    session.routes[("GET", "/trades")] = (409, {"code": "conflict", "error": "after 99 is past your head 5"})
    with pytest.raises(crx.CrxError, match="after 99 is past your head 5") as e:
        make_client().trades(after=99)
    assert (e.value.code, e.value.status) == ("conflict", 409)
    assert session.calls[-1]["query"]["after"] == ["99"]


def test_trades_cursor_is_per_client(make_client, session):
    """Each client pages its own tape from its own cursor: one reader's seq never moves another's.
    Mutation: a cursor shared across clients ⇒ red."""
    def page(start, n):
        return {"trades": [{"type": "trade.opened", "seq": start + i + 1, "ts": 1, "data": {}} for i in range(n)],
                "seq": start + n}
    session.routes[("GET", "/trades")] = [page(0, 1000), page(1000, 2), page(0, 3)]
    a, b = make_client(), make_client(account=Account.create().address)
    assert len(a.trades()) == 1002 and [e.seq for e in b.trades()] == [1, 2, 3]
    assert [x["query"]["after"] for x in session.calls if x["path"] == "/trades"] == [["0"], ["1000"], ["0"]]


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


def one_chain(markets, **row_edit):
    """/markets in the one-chain shape: no ``chains``, ``as_of``, ``session.venue`` or ``session.label``;
    each row carries its chain entry's ``max_premium_bps``. ``row_edit`` maps a pair to the keys to set on
    its row (a value ``DROP`` removes the key; a pair mapped to ``DROP`` removes the row)."""
    m = copy.deepcopy(markets)
    m.pop("as_of", None)
    for row in m["markets"]:
        row["max_premium_bps"] = row.pop("chains")[0]["max_premium_bps"]
        row["session"].pop("venue", None)
        row["session"].pop("label", None)
    for pair, edit in row_edit.items():
        row = next(r for r in m["markets"] if r["pair"] == pair.replace("_", "/"))
        if edit is DROP:
            m["markets"].remove(row)
            continue
        row.update(edit)
        for k in [k for k, v in row.items() if v is DROP]:
            del row[k]
    return m


def test_markets_ask_for_this_chain(make_client, session):
    make_client(key=False).markets()
    call = next(c for c in session.calls if c["path"] == "/markets")
    assert call["query"] == {"chain": ["avax-fuji"]}


def test_a_one_chain_row_is_offered_with_the_row_cap(make_client, session, markets):
    session.routes[("GET", "/markets")] = one_chain(markets)
    c = make_client(key=False)
    ms = {m.pair: m for m in c.markets()}
    assert sorted(ms) == list(PAIRS)
    for pair in PAIRS:
        m = ms[pair]
        assert m.paused is False and m.max_premium_bps == 200 and m.open is False
        assert m.min_notional == Decimal(10000) and m.max_notional == Decimal(10_000_000)
        assert (m.min_tenor_s, m.max_tenor_s) == (600, 7_776_000)
    assert ms["USD/MXN"].next_open == datetime.fromtimestamp(1_790_546_400, timezone.utc)
    assert c.market("USDMXN").pair == "USD/MXN" and c.market("USD/BRL").paused is False


@pytest.mark.parametrize("cap,read", [(150, 150), (0, 0), (None, None), (DROP, None), (-1, None), ("200", None),
                                      (True, None)], ids=["150", "0", "null", "missing", "negative", "text", "bool"])
def test_a_one_chain_row_reads_its_premium_cap(make_client, session, markets, cap, read):
    session.routes[("GET", "/markets")] = one_chain(markets, USD_MXN={"max_premium_bps": cap})
    ms = {m.pair: m for m in make_client(key=False).markets()}
    assert ms["USD/MXN"].max_premium_bps == read and ms["USD/MXN"].paused is False
    assert ms["USD/BRL"].max_premium_bps == ms["USD/PHP"].max_premium_bps == 200


def test_a_pair_the_one_chain_reply_omits_is_not_offered(make_client, session, markets):
    session.routes[("GET", "/markets")] = one_chain(markets, USD_PHP=DROP)
    c = make_client(key=False)
    assert {m.pair: m.paused for m in c.markets()} == {"USD/BRL": False, "USD/MXN": False}
    assert c.market("USD/BRL").pair == "USD/BRL"  # the control: a listed pair
    with pytest.raises(crx.MarketPaused, match="USD/PHP is not offered on avax-fuji") as ei:
        c.market("USD/PHP")
    assert ei.value.details == {"pair": "USD/PHP"} and session.paths("POST") == []


@pytest.mark.parametrize("chains", [[], None], ids=["empty", "null"])
def test_a_row_with_an_empty_chains_key_stays_paused(make_client, session, markets, chains):
    m = copy.deepcopy(markets)
    next(r for r in m["markets"] if r["pair"] == "USD/PHP")["chains"] = chains
    session.routes[("GET", "/markets")] = m
    c = make_client(key=False)
    assert {x.pair: x.paused for x in c.markets()} == {"USD/BRL": False, "USD/MXN": False, "USD/PHP": True}
    with pytest.raises(crx.MarketPaused):
        c.market("USD/PHP")


# ---------- the next long close ----------

NY = ZoneInfo("America/New_York")
TAIL = timedelta(minutes=15)  # the reopen tail after the Sunday open
LONG_CLOSE = (datetime(2026, 10, 9, 17, 0, tzinfo=NY), datetime(2026, 10, 11, 18, 0, tzinfo=NY) + TAIL)
TWO_H = 2 * 3600


def ms(t):
    return int(t.timestamp()) * 1000


def mxn_at(make_client, session, markets, now, long_close=LONG_CLOSE):
    """USD/MXN read at ``now``; ``long_close`` None leaves the key out, as an older gateway does."""
    m = copy.deepcopy(markets)
    row = next(r for r in m["markets"] if r["pair"] == "USD/MXN")
    if long_close is not None:
        row["next_long_close"] = {"starts_at": ms(long_close[0]), "ends_at": ms(long_close[1])}
    session.routes[("GET", "/markets")] = m
    return make_client(key=False, clock=Clock(now.timestamp())).market("USD/MXN")


def test_an_end_in_the_long_close_moves_to_its_end(make_client, session, markets):
    m = mxn_at(make_client, session, markets, datetime(2026, 10, 9, 15, 30, tzinfo=NY))  # Friday
    assert m.next_long_close == tuple(t.astimezone(timezone.utc) for t in LONG_CLOSE)
    end = m.next_valid_end(TWO_H)
    assert end == datetime(2026, 10, 11, 18, 0, tzinfo=NY) + TAIL and end.tzinfo == timezone.utc


def test_an_end_before_the_long_close_stays(make_client, session, markets):
    now = datetime(2026, 10, 6, 15, 30, tzinfo=NY)  # Tuesday
    assert mxn_at(make_client, session, markets, now).next_valid_end(TWO_H) == now + timedelta(hours=2)


def test_no_long_close_key_is_now_plus_min_secs(make_client, session, markets):
    now = datetime(2026, 10, 9, 15, 30, tzinfo=NY)
    m = mxn_at(make_client, session, markets, now, long_close=None)
    assert m.next_long_close is None and m.next_valid_end(TWO_H) == now + timedelta(hours=2)


@pytest.mark.parametrize("edge, moves", [(LONG_CLOSE[1], False), (LONG_CLOSE[0], True)], ids=["end", "start"])
def test_the_long_close_holds_its_start_not_its_end(make_client, session, markets, edge, moves):
    m = mxn_at(make_client, session, markets, edge - timedelta(hours=2))
    assert m.next_valid_end(TWO_H) == (LONG_CLOSE[1] if moves else edge)


@pytest.mark.parametrize("lc", [{"starts_at": 5}, {"starts_at": 2_000, "ends_at": 1_000}, {"starts_at": "1", "ends_at": 2},
                                None, [], {"starts_at": 1, "ends_at": 10**20},
                                {"starts_at": 1, "ends_at": 253_402_300_800_000}, {"starts_at": 2**70, "ends_at": 2**71}],
                         ids=["no-end", "reversed", "text", "null", "list", "huge", "year-10000", "2^70"])
def test_an_unreadable_long_close_reads_as_none(make_client, session, markets, lc):
    m = copy.deepcopy(markets)
    next(r for r in m["markets"] if r["pair"] == "USD/MXN")["next_long_close"] = lc
    session.routes[("GET", "/markets")] = m
    assert make_client(key=False).market("USD/MXN").next_long_close is None


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


# ---------- withdraw: the 409 after a fold ----------

# The wire text of crx-api: its Conflict error displays as "conflict: {0}".
UNREAD = {"error": "conflict: the latest settlement is not read yet; retry shortly", "code": "conflict",
          "outcome": "unavailable", "message": "Service unavailable."}


def unread_gate(session, health, account, clock, refusals):
    """POST /withdraw answers ``refusals`` 409s first (an int, or every post with None), then as ``Gate``."""
    g = gate(session, health, account)
    posts = []

    def answer(req):
        posts.append((clock(), req["body"]))
        if refusals is None or len(posts) <= refusals:
            return (409, UNREAD)
        return g.answer(req)

    session.routes[("POST", "/withdraw")] = answer
    session.routes[("GET", "/balance")] = [balance_body(account), g.view("accepted", tx=LANDED)]
    return g, posts


@pytest.mark.parametrize("text", [UNREAD["error"], UNREAD["error"].removeprefix("conflict: ")], ids=["wire", "bare"])
def test_withdraw_posts_the_same_intent_again_until_the_settlement_is_read(
        make_client, session, health, account, monkeypatch, text):
    monkeypatch.setitem(UNREAD, "error", text)
    Chain(session)
    clock = Clock(time.time())
    g, posts = unread_gate(session, health, account, clock, refusals=3)
    t0 = clock()
    out = make_client(clock=clock).withdraw(1000)
    assert (out.status, out.item) == ("accepted", g.item)
    assert [round(t - t0) for t, _ in posts] == [0, 15, 45, 105]  # 15 s, 30 s, then 60 s apart
    assert all(b == posts[0][1] for _, b in posts)  # one signature: the same item each time


def test_withdraw_stops_after_20_minutes(make_client, session, health, account):
    Chain(session)
    clock = Clock(time.time())
    _, posts = unread_gate(session, health, account, clock, refusals=None)
    t0 = clock()
    with pytest.raises(crx.CrxError) as e:
        make_client(clock=clock).withdraw(1000)
    assert str(e.value) == ("withdraw not sent: the latest settlement is not yet confirmed; "
                            "the request was retried for 20 minutes.")
    assert (e.value.code, e.value.status) == ("conflict", 409)
    times = [round(t - t0) for t, _ in posts]
    assert times[:5] == [0, 15, 45, 105, 165] and times[-1] == 1200 and len(times) == 23
    assert all(b == posts[0][1] for _, b in posts)


@pytest.mark.parametrize("status, body", [
    (409, {"error": "CRX is paused; withdraw after it resumes", "code": "conflict"}),
    (409, {"error": "a withdraw is in progress", "code": "withdraw_in_progress"}),
    (409, {"error": "conflict: the latest settlement is not read yet; retry shortly", "code": "other"}),
    (409, {"error": "conflict: CRX is paused; the latest settlement is not read yet", "code": "conflict"}),
], ids=["paused", "in-progress", "other-code", "text-not-at-start"])
def test_another_409_raises_at_once(make_client, session, health, account, status, body):
    Chain(session)
    clock = Clock(time.time())
    gate(session, health, account)
    session.routes[("GET", "/balance")] = balance_body(account)
    posts = []
    session.routes[("POST", "/withdraw")] = lambda req: posts.append(clock()) or (status, body)
    t0 = clock()
    with pytest.raises(crx.CrxError) as e:
        make_client(clock=clock).withdraw(1000)
    assert str(e.value) == body["error"] and len(posts) == 1 and clock() == t0
