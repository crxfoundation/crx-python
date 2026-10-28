"""python -m crx.quickstart: key handling, arguments and the six steps, with no network access."""

from __future__ import annotations

import inspect
import re
import runpy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import crx
import crx.quickstart as qs

KEY = "0x" + "11" * 32
STAMP = re.compile(r"^\d\d:\d\d:\d\d ")
RFQ = "0x44fd" + "1" * 56 + "42f8"


class FakeClient:
    """Stand in for crx.Client: record each call and answer as the gateway does."""

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.calls = []
        self.address = "0x7638c8075e517393fa62008b5faa6c1ea832fe71"
        FakeClient.last = self

    def deposit(self, amount):
        self.calls.append(("deposit", amount))
        return SimpleNamespace(status="credited")

    def balance(self):
        self.calls.append(("balance",))
        return SimpleNamespace(free="20000.000000", as_of="2026-09-29 21:04:57+00:00", im="3065.908525")

    def quote(self, pair, side, notional, **kwargs):
        self.calls.append(("quote", pair, side, notional, kwargs))
        return SimpleNamespace(pair=pair, rate="5.2562", expiry=kwargs.get("expiry"),
                               expires_at="2026-09-29 21:08:30+00:00", rfq_id=RFQ)

    def accept(self, q):
        self.calls.append(("accept", q.rfq_id))
        return SimpleNamespace(status="open", rfq_id=q.rfq_id)

    def positions(self):
        self.calls.append(("positions",))
        other = SimpleNamespace(rfq_id="r1", pair="USDMXN", side="sell", notional=5, rate="17", entry_price="17",
                                unrealized_pnl="3.1", status="open")
        mine = SimpleNamespace(rfq_id=RFQ, pair="USDBRL", side="buy", notional=25000, rate="5.2562",
                               entry_price="5.2562", unrealized_pnl="-1.204512", status="open")
        return [other, mine]

    def withdraw(self, amount):
        self.calls.append(("withdraw", amount))
        return SimpleNamespace(status="accepted")


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(crx, "Client", FakeClient)
    FakeClient.last = None
    return FakeClient


def no_prompt(*_a, **_k):
    raise AssertionError("asked for the key while one is set")


def test_key_from_env_runs_every_step_without_asking(monkeypatch, capsys, fake):
    monkeypatch.setenv("CRX_WALLET_PK", KEY)
    monkeypatch.setattr(qs.getpass, "getpass", no_prompt)
    assert qs.main([]) == 0
    c = FakeClient.last
    assert c.kwargs == {"network": "testnet"} and c.args == ()
    expiry = c.calls[2][4]["expiry"]
    assert c.calls == [
        ("deposit", 20_000),
        ("balance",),
        ("quote", "USD/BRL", "buy", 25_000, {"expiry": expiry}),
        ("accept", RFQ),
        ("positions",),
        ("balance",),
        ("withdraw", 1_000),
    ]
    lines = capsys.readouterr().out.splitlines()
    # ASCII only, so that a Windows console or a redirected cp1252 stream can print it.
    assert len(lines) == 7 and all(STAMP.match(line) and line.isascii() for line in lines)
    assert lines[0].endswith(" 0x7638c8075e517393fa62008b5faa6c1ea832fe71")
    assert [line[9:] for line in lines[1:]] == [
        "1/6 Deposit 20000 USDC POST /deposit              -> credited, free 20000.000000",
        "2/6 Request a quote    POST /rfqs                 -> USD/BRL 5.2562, rfq 0x44fd...42f8",
        "3/6 Accept             POST /rfqs/{rfq_id}/accept -> open",
        "4/6 Positions          GET  /positions            -> USDBRL buy 25000 at 5.2562, open, P&L -1.204512",
        "5/6 Margin (IM)        GET  /balance              -> IM 3065.908525",
        "6/6 Withdraw 1000 USDC POST /withdraw             -> accepted",
    ]


def test_no_key_exits_2_and_a_crx_error_exits_1(monkeypatch, capsys, tmp_path, fake):
    monkeypatch.chdir(tmp_path)  # no .env file
    monkeypatch.delenv("CRX_WALLET_PK", raising=False)
    monkeypatch.delenv("CRX_WALLET_PK_FILE", raising=False)
    monkeypatch.setattr(qs.getpass, "getpass", lambda prompt: "")
    assert qs.main([]) == 2 and FakeClient.last is None
    assert "No key." in capsys.readouterr().err
    monkeypatch.setenv("CRX_WALLET_PK", KEY)

    def short(self, amount):
        raise crx.InsufficientCollateral("free collateral does not cover the call")
    monkeypatch.setattr(FakeClient, "deposit", short)
    assert qs.main([]) == 1
    assert capsys.readouterr().err == "insufficient_collateral: free collateral does not cover the call\n"


EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "quickstart.py"


def test_the_example_calls_the_client_within_its_signatures(monkeypatch, capsys):
    c = mock.create_autospec(crx.Client, instance=True)
    c.deposit.return_value = SimpleNamespace(status="credited", counted=True)
    c.balance.return_value = SimpleNamespace(free=Decimal("100.000000"))
    c.quote.return_value = SimpleNamespace(pair="USD/MXN", side="buy", notional=Decimal("100"), rate=Decimal("18.4521"),
                                           settles_at=None, closes_at=None, rfq_id=RFQ)
    c.accept.return_value = SimpleNamespace(status="open", rfq_id=RFQ)
    c.positions.return_value = [SimpleNamespace(pair="USD/MXN", side="buy", notional=Decimal("100"),
                                                entry_price=Decimal("18.4521"), settles_at=None,
                                                unrealized_pnl=Decimal("-0.012345"))]
    c.withdraw.return_value = SimpleNamespace(amount=Decimal("50"), status="accepted")
    made, real = [], inspect.signature(crx.Client)

    def client(*args, **kwargs):
        made.append(real.bind(*args, **kwargs).arguments)  # TypeError on an unknown name
        return c
    monkeypatch.setattr(crx, "Client", client)
    runpy.run_path(str(EXAMPLE), run_name="__main__")
    assert made == [{"network": "mainnet", "allow_mainnet": True}]
    assert [name for name, *_ in c.mock_calls] == ["deposit", "balance", "quote", "accept", "positions", "withdraw"]
    assert c.deposit.call_args == mock.call(100) and c.withdraw.call_args == mock.call(50)
    assert c.quote.call_args.args == ("USD/MXN", "buy", 100)
    assert capsys.readouterr().out.splitlines()[0] == "Validated 100.000000"
