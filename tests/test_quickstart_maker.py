"""Single-maker quickstart: bounded credit and trade waits, with no network."""

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import importlib
import runpy

import pytest
import crx
import crx.quickstart_maker as qs
from .conftest import Clock

EXAMPLE = Path(__file__).parent.parent / "examples" / "maker.py"


class Maker:
    def __init__(self):
        self.address = "0x" + "aa" * 20
        self.free = Decimal("7.5")
        self.pending = None
        self.deposits = []
        self.statuses = ["pending", "pending", "open"]
        self.calls = []
        self.closed = False
        self.credit = True
        self.deposit_status = "pending"
        self.finals = [False, True]
        self.rfq = SimpleNamespace(rfq_id="rfq", pair="USD/MXN", side="sell", notional=Decimal(100),
                                   reference_rate=Decimal("18.123456"))

    def balance(self):
        self.calls.append("balance")
        if self.pending is not None and self.credit:
            self.free += self.pending
            self.pending = None
        return SimpleNamespace(free=self.free, raw={"deposit": {"last": {
            "status": "pending" if self.pending is not None else "credited"}}})

    def deposit(self, amount, **kwargs):
        self.calls.append("deposit")
        self.deposits.append((amount, kwargs))
        self.pending = amount
        return SimpleNamespace(status=self.deposit_status)

    def test_request(self):
        self.calls.append("test_request")
        return self.rfq

    def send_quote(self, rfq, rate):
        assert rfq is self.rfq and rate == Decimal("18.123456")
        self.calls.append("send_quote")
        return SimpleNamespace(rfq_id="rfq", quote_id="quote", rate=rate)

    def confirm(self, q, **kwargs):
        self.calls.append("confirm")
        word = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return SimpleNamespace(status=word, rfq_id=q.rfq_id, tx="0x" + "ff" * 32)

    def positions(self):
        self.calls.append("positions")
        final = self.finals.pop(0) if len(self.finals) > 1 else self.finals[0]
        return [SimpleNamespace(rfq_id="other", final=True),
                SimpleNamespace(rfq_id="rfq", pair="USD/MXN", side="sell", notional=Decimal(100),
                                rate=Decimal("18.123456"), status="open", final=final)]

    def close(self):
        self.closed = True


@pytest.fixture
def setup(monkeypatch):
    maker = Maker()
    clients = []

    def client(*args, **kwargs):
        assert args == ()
        clients.append(kwargs)
        return maker

    clock = Clock(0)
    monkeypatch.setattr(crx, "Client", client)
    monkeypatch.setattr(qs, "time", SimpleNamespace(monotonic=clock, sleep=clock.sleep), raising=False)
    monkeypatch.setenv("CRX_WALLET_PK", "test-fixture-only")
    monkeypatch.delenv("CRX_WALLET_PK_FILE", raising=False)
    monkeypatch.delenv("CRX_TAKER_PK", raising=False)
    return maker, clients, clock


def test_one_client_deposits_the_minimum_for_a_small_shortfall_and_waits_through_pending(setup, capsys):
    maker, clients, clock = setup
    t = qs.steps(credit_timeout=5, trade_timeout=5, poll=1)
    assert t.status == "open" and maker.closed
    assert clients == [{"network": "mainnet", "base_url": "https://api.crxfx.com", "allow_mainnet": True}]
    assert maker.deposits == [(Decimal("100"), {"mint": False})]
    assert maker.calls.count("confirm") == 3
    assert maker.calls.index("balance") < maker.calls.index("deposit") < maker.calls.index("test_request")
    out = capsys.readouterr().out
    assert out.splitlines()[-2].endswith("maker: open")
    assert out.splitlines()[-1].endswith("USD/MXN sell 100 18.123456 open final")
    assert maker.calls.count("positions") == 2
    assert "maker: accepted" not in out


def test_shortfall_above_the_minimum_is_deposited_whole(setup, monkeypatch):
    maker, clients, clock = setup
    monkeypatch.setattr(qs, "COLLATERAL", Decimal("120"))
    maker.free = Decimal("0")
    assert qs.steps(credit_timeout=5, trade_timeout=5, poll=1).status == "open"
    assert maker.deposits == [(Decimal("120.000000"), {"mint": False})]


def test_credit_wait_counts_from_the_deposit_call(setup):
    maker, clients, clock = setup
    real = maker.deposit

    def slow_deposit(amount, **kwargs):
        clock.sleep(90)
        return real(amount, **kwargs)

    maker.deposit = slow_deposit
    maker.credit = False
    calls = []
    real_balance = maker.balance

    def balance():
        calls.append(1)
        if len(calls) == 4:
            maker.credit = True
        return real_balance()

    maker.balance = balance
    assert qs.steps(credit_timeout=60, trade_timeout=5, poll=1).status == "open"
    assert clock() >= 90 and len(maker.deposits) == 1


def test_funded_maker_deposits_nothing(setup):
    maker, clients, clock = setup
    maker.free = Decimal(20)
    assert qs.steps().status == "open"
    assert maker.deposits == []


def test_pending_deposit_timeout_never_requests_rfq_or_deposits_twice(setup):
    maker, clients, clock = setup
    maker.credit = False
    with pytest.raises(crx.CrxError) as e:
        qs.steps(credit_timeout=3, poll=1)
    assert e.value.code == "deposit_timeout" and clock() == 3
    assert len(maker.deposits) == 1 and "test_request" not in maker.calls
    assert maker.closed
    with pytest.raises(crx.CrxError):
        qs.steps(credit_timeout=3, poll=1)
    assert len(maker.deposits) == 1


def test_existing_pending_credit_is_waited_before_any_new_deposit(setup):
    maker, clients, clock = setup
    maker.pending = Decimal("12.5")
    assert qs.steps().status == "open"
    assert maker.deposits == []


def test_failed_deposit_stops_flow(setup):
    maker, clients, clock = setup
    maker.deposit_status = "failed"
    with pytest.raises(crx.CrxError) as e:
        qs.steps()
    assert e.value.code == "deposit_failed"
    assert "test_request" not in maker.calls


@pytest.mark.parametrize("free", [None, Decimal("NaN"), Decimal("Infinity"), Decimal("-1")])
def test_unreadable_balance_does_not_deposit(setup, free):
    maker, clients, clock = setup
    maker.free = free
    with pytest.raises(crx.BadAnswer):
        qs.steps()
    assert maker.deposits == []


@pytest.mark.parametrize("status", ["pending", "sending"])
def test_pending_trade_timeout_is_failure_even_with_transaction_hash(setup, status, capsys):
    maker, clients, clock = setup
    maker.statuses = [status]
    with pytest.raises(crx.CrxError) as e:
        qs.steps(trade_timeout=3, poll=1)
    assert e.value.code == "trade_timeout" and clock() == 3
    assert "maker: open" not in capsys.readouterr().out
    assert maker.closed


def test_refused_trade_is_failure(setup):
    maker, clients, clock = setup
    maker.statuses = ["refused"]
    with pytest.raises(crx.CrxError) as e:
        qs.steps()
    assert e.value.code == "not_open"


def test_executable_and_example_use_same_single_maker_flow(setup):
    maker, clients, clock = setup
    assert qs.main([]) == 0
    assert len(clients) == 1
    runpy.run_path(str(EXAMPLE), run_name="__main__")
    assert len(clients) == 2
    assert len(maker.deposits) == 1


def test_executable_returns_failure_for_pending_timeout(setup, capsys):
    maker, clients, clock = setup
    maker.statuses = ["pending"]
    assert qs.main([]) == 1
    assert "trade_timeout" in capsys.readouterr().err


def test_trade_not_final_by_the_deadline_fails(setup, capsys):
    maker, clients, clock = setup
    maker.finals = [False]
    with pytest.raises(crx.CrxError) as e:
        qs.steps(credit_timeout=5, trade_timeout=5, final_timeout=30, poll=1)
    assert e.value.code == "final_timeout"
    assert "final" not in capsys.readouterr().out.splitlines()[-1]


def test_import_runs_nothing(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("import constructed a client")
    monkeypatch.setattr(crx, "Client", forbidden)
    importlib.reload(qs)


def test_help_requires_only_maker_key(capsys):
    with pytest.raises(SystemExit) as e:
        qs.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "CRX_WALLET_PK" in out and "CRX_TAKER_PK" not in out


def test_missing_key_stops_before_client(setup, monkeypatch):
    maker, clients, clock = setup
    monkeypatch.delenv("CRX_WALLET_PK")
    monkeypatch.setattr(qs.getpass, "getpass", lambda _: "")
    assert qs.main([]) == 2
    assert clients == []


@pytest.mark.parametrize("finishes", [True, False])
def test_real_client_quote_and_confirmation_with_recorded_gateway(
        session, health, tmp_path, monkeypatch, finishes):
    from .test_maker import Gateway, new_maker, TXH
    from .test_test_request import NOW, request_wire

    clock = Clock(NOW)
    maker = new_maker(session, health, tmp_path, clock)
    maker._settle_wait = 1
    wire = request_wire()
    venue = Gateway(session, clock, **wire)
    venue.accept_at = 1
    venue.script = [("pending", TXH)] * 4 + ([("open", TXH)] if finishes else [])
    session.routes[("POST", "/test-requests")] = wire
    session.routes[("GET", "/positions")] = {"positions": [{
        "trade_id": "t1", "rfq_id": wire["rfq_id"], "pair": "USD/MXN", "side": -1, "notional": "100",
        "rate": "18.123456", "status": "open", "final": True}]}
    session.routes[("GET", "/balance")] = {
        "account": maker.address, "free": "20", "deposit": {"last": None}}
    monkeypatch.setattr(crx, "Client", lambda **kwargs: maker)
    monkeypatch.setattr(qs, "time", SimpleNamespace(monotonic=clock, sleep=clock.sleep))
    if finishes:
        trade = qs.steps(trade_timeout=5, poll=1)
        assert trade.status == "open" and trade.notional == Decimal(100)
    else:
        with pytest.raises(crx.CrxError) as error:
            qs.steps(trade_timeout=5, poll=1)
        assert error.value.code == "trade_timeout"
    assert len(venue.bodies) == 1 and venue.bodies[0]["rate"] == "18.123456"
    assert venue.views >= 5
    assert set(session.rpc_methods()) <= {"eth_chainId", "eth_getCode"}
