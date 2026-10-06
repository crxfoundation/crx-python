"""python -m crx.quickstart: key handling, arguments and the seven steps. No network."""

from __future__ import annotations

import importlib
import inspect
import re
import runpy
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account

import crx
import crx.quickstart as qs

from .conftest import FakeSession

KEY = "0x" + "11" * 32
EXAMPLE = Path(__file__).parent.parent / "examples" / "quickstart.py"
STAMP = re.compile(r"^\d\d:\d\d:\d\d ")


class FakeClient:
    """Stands in for crx.Client: records each call, answers like the gateway."""

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
                               expires_at="2026-09-29 21:08:30+00:00", rfq_id="r2")

    def trade(self, q):
        self.calls.append(("trade", q.rfq_id))
        return SimpleNamespace(status="open", rfq_id=q.rfq_id)

    def positions(self):
        self.calls.append(("positions",))
        other = SimpleNamespace(rfq_id="r1", pair="USDMXN", side="sell", notional=5, rate="17", status="open")
        mine = SimpleNamespace(rfq_id="r2", pair="USDBRL", side="buy", notional=25000, rate="5.2562", status="open")
        return [other, mine]

    def withdraw(self, amount):
        self.calls.append(("withdraw", amount))
        return SimpleNamespace(status="accepted")


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(crx, "Client", FakeClient)
    FakeClient.last = None
    return FakeClient


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("CRX_WALLET_PK", raising=False)
    monkeypatch.delenv("CRX_WALLET_PK_FILE", raising=False)


def no_prompt(*_a, **_k):
    raise AssertionError("asked for the key while one is set")


def test_import_runs_nothing(monkeypatch, no_key):
    def boom(*_a, **_k):
        raise AssertionError("import built a client")

    monkeypatch.setattr(crx, "Client", boom)
    monkeypatch.setattr(qs.getpass, "getpass", no_prompt)
    importlib.reload(qs)
    assert callable(qs.main) and callable(qs.steps)


def _from_step_2_on(text: str) -> str:
    """The steps from "# 2. Fund." on, minus that step's comment line: the example connects to the
    production gateway, the packaged module to the test network; every other line is the same."""
    lines = text[text.index("# 2. Fund."):].splitlines()
    return "\n".join(lines[1:]).rstrip("\n")


def test_steps_are_the_example_line_for_line():
    example = EXAMPLE.read_text()
    body = textwrap.dedent(inspect.getsource(qs.steps)).splitlines()
    first = next(i for i, line in enumerate(body) if line.startswith("    # 1. Connect"))
    steps = textwrap.dedent("\n".join(body[first:]))
    assert _from_step_2_on(example) == _from_step_2_on(steps)
    assert example[example.index("# 1. Connect"):].count("crx.Client(") == 1
    assert inspect.getsource(qs.log) in example
    assert example.startswith("from datetime import datetime, timedelta, timezone\n\nimport crx\n")


def test_help_names_the_key_variable(capsys):
    with pytest.raises(SystemExit) as e:
        qs.main(["-h"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "python -m crx.quickstart" in out and "CRX_WALLET_PK" in out


def test_unknown_argument_exits_2(capsys, fake):
    with pytest.raises(SystemExit) as e:
        qs.main(["--mainnet"])
    assert e.value.code == 2
    assert FakeClient.last is None


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
        ("trade", "r2"),
        ("positions",),
        ("balance",),
        ("withdraw", 1_000),
    ]
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 10 and all(STAMP.match(line) for line in lines)
    assert lines[0].endswith(" 0x7638c8075e517393fa62008b5faa6c1ea832fe71")
    assert lines[3][9:] == "USD/BRL 5.2562"
    assert lines[4].endswith(f" {expiry}")
    assert lines[7].endswith(" USDBRL buy 25000 5.2562 open")
    assert lines[9].endswith(" accepted")


# A week from Monday at four times of day, past the old fixed expiry date.
STARTS = [datetime(2026, 12, 14, h, m, tzinfo=timezone.utc) + timedelta(days=i)
          for i in range(7) for h, m in ((0, 0), (17, 59), (18, 1), (23, 59))]


@pytest.mark.parametrize("start", STARTS, ids=lambda t: t.strftime("%a-%H%M"))
def test_the_expiry_is_30_days_out_on_a_weekday_at_18_utc(monkeypatch, fake, markets, start):
    """The quote's expiry follows the run date: the SDK's default day, at 15:00 in São Paulo."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return start.astimezone(tz) if tz else start.replace(tzinfo=None)

    monkeypatch.setattr(qs, "datetime", Clock)
    monkeypatch.setenv("CRX_WALLET_PK", KEY)
    assert qs.main([]) == 0
    expiry = next(c[4]["expiry"] for c in FakeClient.last.calls if c[0] == "quote")
    tenor = next(m["tenor"] for m in markets["markets"] if m["pair"] == "USD/BRL")
    assert expiry > start and expiry.weekday() < 5
    assert expiry.utcoffset() == timedelta(0) and (expiry.hour, expiry.minute, expiry.second) == (18, 0, 0)
    assert expiry.date() == crx.client._default_expiry(start).date()
    assert tenor["min_secs"] <= (expiry - start).total_seconds() <= tenor["max_secs"]


def test_the_deposit_covers_the_ask(monkeypatch, fake):
    """A seat with nothing but the script's deposit must get its quote.

    Before a quote, the gateway checks that the seat's free collateral
    covers the trade: about 21 USD per 100 USD asked on USD/BRL. The deposit
    is at least twice that. The ask is 25,000, the least a Testnet account
    may ask.
    """
    monkeypatch.setenv("CRX_WALLET_PK", KEY)
    assert qs.main([]) == 0
    calls = FakeClient.last.calls
    deposit = next(c[1] for c in calls if c[0] == "deposit")
    ask = next(c[3] for c in calls if c[0] == "quote")
    assert ask == 25_000
    assert deposit >= 2 * 0.21 * ask


def test_key_file_env_counts_as_set(monkeypatch, fake, no_key):
    monkeypatch.setenv("CRX_WALLET_PK_FILE", "/nowhere/seat.key")
    monkeypatch.setattr(qs.getpass, "getpass", no_prompt)
    assert qs.main([]) == 0


def test_unset_key_is_asked_hidden_and_never_printed(monkeypatch, capsys, fake, no_key):
    asked = []

    def prompt(text):
        asked.append(text)
        return f"  {KEY}\n"

    monkeypatch.setattr(qs.getpass, "getpass", prompt)
    assert qs.main([]) == 0
    assert asked == ["CRX_WALLET_PK (hidden): "]
    assert qs.os.environ["CRX_WALLET_PK"] == KEY
    out, err = capsys.readouterr()
    assert KEY[2:] not in out + err and KEY[2:] not in asked[0]


def test_asked_key_reaches_the_real_client(monkeypatch, no_key):
    monkeypatch.setattr(qs.getpass, "getpass", lambda _t: KEY)
    assert qs.ask_key() is True
    c = crx.Client(network="testnet", session=FakeSession())
    assert c.address == Account.from_key(KEY).address.lower()
    assert KEY[2:] not in repr(c)


@pytest.mark.parametrize("answer", ["", "   ", EOFError, KeyboardInterrupt, "no-terminal"])
def test_no_key_given_exits_2_before_any_call(monkeypatch, capsys, fake, no_key, answer):
    def prompt(_t):
        if answer == "no-terminal":
            # getpass's fallback: it warns, then would read with echo on.
            qs.warnings.warn("Can not control echo on the terminal.", qs.getpass.GetPassWarning)
            return KEY
        if isinstance(answer, type):
            raise answer()
        return answer

    monkeypatch.setattr(qs.getpass, "getpass", prompt)
    assert qs.main([]) == 2
    assert FakeClient.last is None
    assert "Set CRX_WALLET_PK" in capsys.readouterr().err


def test_bad_key_reports_config_without_the_key(monkeypatch, capsys, no_key):
    bad = "0x" + "zz" * 10
    monkeypatch.setattr(qs.getpass, "getpass", lambda _t: bad)
    assert qs.main([]) == 1
    out, err = capsys.readouterr()
    assert err.startswith("config: ") and "zz" not in out + err


def test_crx_error_mid_run_exits_1_with_its_code(monkeypatch, capsys, fake):
    monkeypatch.setenv("CRX_WALLET_PK", KEY)

    def closed(self, *a, **k):
        raise crx.MarketClosed("session closed", code="market_closed")

    monkeypatch.setattr(FakeClient, "quote", closed)
    assert qs.main([]) == 1
    out, err = capsys.readouterr()
    assert len(out.splitlines()) == 3
    assert err.strip() == "market_closed: session closed"


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_runs_as_a_module(monkeypatch, fake):
    monkeypatch.setenv("CRX_WALLET_PK", KEY)
    monkeypatch.setattr("sys.argv", ["crx.quickstart"])
    with pytest.raises(SystemExit) as e:
        runpy.run_module("crx.quickstart", run_name="__main__", alter_sys=False)
    assert e.value.code == 0
