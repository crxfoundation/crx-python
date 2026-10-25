"""Single-maker quickstart: bounded credit and trade waits, with no network."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
import crx
import crx.quickstart_maker as qs
from .conftest import Clock


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
        self.deposit_status = "credited"
        self.unfinished = "pending"  # the status of a deposit still not counted when its wait ends
        self.clock = None
        self.finals = [False, True]
        self.rfq = SimpleNamespace(rfq_id="0x44fd" + "0" * 56 + "42f8", pair="USD/MXN", side="sell", notional=Decimal(100),
                                   reference_rate=Decimal("18.123456"))

    def balance(self):
        self.calls.append("balance")
        if self.pending is not None and self.credit:
            self.free += self.pending
            self.pending = None
        return SimpleNamespace(free=self.free, raw={"deposit": {"last": {
            "status": "pending" if self.pending is not None else "credited"}}})

    def deposit(self, amount, **kwargs):
        """Credit at once when ``credit`` is set, else stay pending for the whole ``wait``."""
        self.calls.append("deposit")
        self.deposits.append((amount, kwargs))
        if self.deposit_status == "failed":
            raise crx.DepositFailed("the deposit failed")
        self.pending = amount
        if self.credit:
            return SimpleNamespace(status="credited", counted=True)
        self.clock.sleep(kwargs["wait"])
        return SimpleNamespace(status=self.unfinished, counted=False)

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
                                rate=Decimal("18.123456"), entry_price=Decimal("18.123456"),
                                unrealized_pnl=Decimal("0.012345"), status="open", final=final)]

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
    maker.clock = clock
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
    assert maker.deposits == [(Decimal("100"), {"mint": False, "wait": 5})]
    assert maker.calls.count("confirm") == 3
    assert maker.calls.index("balance") < maker.calls.index("deposit") < maker.calls.index("test_request")
    lines = [line[9:] for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        "0x" + "aa" * 20,
        "1/4 Deposit        POST /deposit              -> 100 USDC, free 107.5",
        "2/4 Test request   POST /test-requests        -> USD/MXN sell 100, rfq 0x44fd...42f8",
        "3/4 Quote          POST /rfqs/{rfq_id}/quotes -> quoted 18.123456",
        "4/4 Final position GET  /positions            -> USD/MXN sell 100 at 18.123456, open, final, P&L 0.012345",
    ]
    out = "\n".join(lines)
    assert out.isascii()
    assert maker.calls.count("positions") == 2
    assert "maker: accepted" not in out


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
