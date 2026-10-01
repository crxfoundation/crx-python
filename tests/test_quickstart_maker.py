"""python -m crx.quickstart_maker: key handling, arguments and the steps. No network."""

from __future__ import annotations

import importlib
import inspect
import re
import runpy
import textwrap
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import crx
import crx.quickstart_maker as qs
from crx import _maker

MAKER_KEY = "0x" + "11" * 32
TAKER_KEY = "0x" + "22" * 32
EXAMPLE = Path(__file__).parent.parent / "examples" / "maker.py"
STAMP = re.compile(r"^\d\d:\d\d:\d\d ")
OURS = "0x" + "a1" * 32
TXH = "0x" + "55" * 32
CHECK = datetime(2026, 10, 1, 9, 5, tzinfo=timezone.utc)


class FakeClient:
    """Stands in for crx.Client: the maker (no key argument) or the test taker (the key passed in)."""

    maker = taker = None
    order = []      # every call, both clients, in the order made
    house = False   # the winning quote is the house desk's
    lose = None     # the reason confirm() loses the quote for
    status = "open"  # the trade status confirm() returns

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.calls = []
        if args:
            self.address = "0x5b38da6a701c568545dcfcb03fcb875f56beddc4"
            FakeClient.taker = self
        else:
            self.address = "0x7638c8075e517393fa62008b5faa6c1ea832fe71"
            FakeClient.maker = self

    def did(self, *call):
        self.calls.append(call)
        FakeClient.order.append(call[0])

    def deposit(self, amount):
        self.did("deposit", amount)
        return SimpleNamespace(status="credited")

    def balance(self):
        self.did("balance")
        return SimpleNamespace(free="20000.000000")

    # the taker's calls
    def ask(self, pair, side, notional, **kwargs):
        self.did("ask", pair, side, notional, kwargs)
        self.asked = SimpleNamespace(rfq_id=OURS, quote=self.winner)
        return self.asked

    def winner(self):
        self.did("ask.quote")
        return SimpleNamespace(rate=Decimal("18.12"), house=FakeClient.house, rfq_id=OURS)

    def trade(self, q):
        self.did("trade", q.rfq_id)
        return SimpleNamespace(status="pending")

    # the maker's calls
    def rfqs(self, **kwargs):
        self.did("rfqs", kwargs)
        return iter([SimpleNamespace(rfq_id=kwargs["only"].rfq_id, pair="USD/MXN", side="sell",
                                     notional=Decimal("25000"))])

    def send_quote(self, rfq, rate):
        self.did("send_quote", rfq.rfq_id, rate)
        return SimpleNamespace(rfq_id=rfq.rfq_id, rate=Decimal(rate))

    def confirm(self, q, **kwargs):
        self.did("confirm", q.rfq_id, kwargs)
        if FakeClient.lose == "timeout":
            raise crx.QuoteLost("no accept before the wait ended", reason="timeout")
        if FakeClient.lose:
            raise crx.QuoteLost("the taker accepted another quote", reason=FakeClient.lose)
        return SimpleNamespace(status=FakeClient.status, rfq_id=q.rfq_id, tx=TXH)

    def drop_quote(self, q):
        self.did("drop_quote", q.rfq_id)

    def next_check(self):
        self.did("next_check")
        return CHECK


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(crx, "Client", FakeClient)
    FakeClient.maker = FakeClient.taker = FakeClient.lose = None
    FakeClient.order, FakeClient.house, FakeClient.status = [], False, "open"
    return FakeClient


@pytest.fixture
def no_keys(monkeypatch):
    for k in ("CRX_WALLET_PK", "CRX_WALLET_PK_FILE", "CRX_TAKER_PK"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("CRX_WALLET_PK", MAKER_KEY)
    monkeypatch.setenv("CRX_TAKER_PK", TAKER_KEY)
    monkeypatch.setattr(qs.getpass, "getpass", no_prompt)


def no_prompt(*_a, **_k):
    raise AssertionError("asked for a key while one is set")


def test_import_runs_nothing(monkeypatch, no_keys):
    def boom(*_a, **_k):
        raise AssertionError("import built a client")

    monkeypatch.setattr(crx, "Client", boom)
    monkeypatch.setattr(qs.getpass, "getpass", no_prompt)
    importlib.reload(qs)
    assert callable(qs.main) and callable(qs.steps)


def test_steps_are_the_example_line_for_line():
    example = EXAMPLE.read_text()
    body = textwrap.dedent(inspect.getsource(qs.steps)).splitlines()
    first = next(i for i, line in enumerate(body) if line.startswith("    # 1. Connect"))
    steps = textwrap.dedent("\n".join(body[first:]))
    assert example[example.index("# 1. Connect"):].rstrip("\n") == steps.rstrip("\n")
    assert inspect.getsource(qs.log) in example


def test_the_contract_names_hold():
    src = inspect.getsource(qs.steps)
    for line in ('maker = crx.Client(network="testnet")',
                 'taker = crx.Client(os.environ["CRX_TAKER_PK"], network="testnet")',
                 "maker.deposit(20_000)", "taker.deposit(20_000)", 'RATE = "18.12"',
                 'ask = taker.ask("USD/MXN", "buy", 25_000)', "for rfq in maker.rfqs(only=ask, wait=60):",
                 "q = maker.send_quote(rfq, RATE)", "won = ask.quote()", "t = taker.trade(won)",
                 "t = maker.confirm(q, timeout=60)", "maker.drop_quote(q)"):
        assert line in src


@pytest.mark.parametrize("src", [EXAMPLE.read_text(), inspect.getsource(qs.steps)], ids=["example", "steps"])
def test_the_reader_sees_no_thread_no_tag_and_no_rfq_read(src):
    for gone in ("threading", "uuid", "random", "time.sleep", "Event", "taker_done", "taker_failed", "def ask",
                 "client_rfq_id", ".rfq(", "else:", "finally:", "continue", "break"):
        assert gone not in src
    assert src.count("try:") == src.count("except ") == 1 and "except crx.QuoteLost as e:" in src
    assert not any(hasattr(qs, name) for name in ("threading", "time", "uuid", "random"))


def test_help_names_both_key_variables(capsys):
    with pytest.raises(SystemExit) as e:
        qs.main(["-h"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "python -m crx.quickstart_maker" in out and "CRX_WALLET_PK" in out and "CRX_TAKER_PK" in out


def test_unknown_argument_exits_2(fake):
    with pytest.raises(SystemExit) as e:
        qs.main(["--mainnet"])
    assert e.value.code == 2 and FakeClient.maker is None


def test_the_taker_asks_the_maker_quotes_the_taker_accepts(capsys, fake, keys):
    assert qs.main([]) == 0
    m, t = FakeClient.maker, FakeClient.taker
    assert m.kwargs == {"network": "testnet"} and m.args == ()
    assert t.kwargs == {"network": "testnet"} and t.args == (TAKER_KEY,)
    assert FakeClient.order == ["deposit", "deposit", "balance", "balance", "ask", "rfqs", "send_quote",
                                "ask.quote", "trade", "confirm"]
    assert t.calls[2:] == [("ask", "USD/MXN", "buy", 25_000, {}), ("ask.quote",), ("trade", OURS)]
    assert m.calls[2:] == [("rfqs", {"only": t.asked, "wait": 60}), ("send_quote", OURS, "18.12"),
                           ("confirm", OURS, {"timeout": 60})]
    lines = capsys.readouterr().out.splitlines()
    assert all(STAMP.match(line) for line in lines)
    assert [line[9:] for line in lines] == [
        "0x7638c8075e517393fa62008b5faa6c1ea832fe71 0x5b38da6a701c568545dcfcb03fcb875f56beddc4",
        "20000.000000 20000.000000", "USD/MXN sell 25000", "maker: quoted 18.12",
        "taker: winning quote 18.12 maker", "taker: pending", "maker: open"]


def test_a_lost_quote_exits_1_with_its_code_and_drops_nothing(capsys, fake, keys):
    FakeClient.lose = "another_maker"
    assert qs.main([]) == 1
    assert capsys.readouterr().err.strip() == "quote_lost: the taker accepted another quote"
    assert "drop_quote" not in FakeClient.order


def test_no_accept_in_the_wait_drops_the_quote_and_exits_1(capsys, fake, keys):
    FakeClient.lose = "timeout"
    assert qs.main([]) == 1
    assert capsys.readouterr().err.strip() == "quote_lost: no accept before the wait ended"
    assert FakeClient.order[-2:] == ["confirm", "drop_quote"] and FakeClient.maker.calls[-1] == ("drop_quote", OURS)


def test_a_winning_house_quote_is_not_accepted(capsys, fake, keys):
    FakeClient.house, FakeClient.lose = True, "timeout"
    assert qs.main([]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[-1].endswith(" taker: winning quote 18.12 house")
    assert "trade" not in FakeClient.order and FakeClient.order[-1] == "drop_quote"


def test_a_taker_refusal_ends_the_run_with_its_own_error(capsys, fake, keys, monkeypatch):
    def refused(self, *a, **k):
        raise crx.BelowMin("a pool wallet trades at least 25000 USD notional; got 10000")

    monkeypatch.setattr(FakeClient, "ask", refused)
    assert qs.main([]) == 1
    out, err = capsys.readouterr()
    assert err.strip() == "below_min: a pool wallet trades at least 25000 USD notional; got 10000"
    assert "Traceback" not in out + err and FakeClient.order[-1] == "balance"


def test_an_rfq_with_no_winner_exits_1(capsys, fake, keys, monkeypatch):
    def unquoted(self):
        raise crx.NoQuotes("no quote before the wait ended: no maker online, or the market just closed")

    monkeypatch.setattr(FakeClient, "winner", unquoted)
    assert qs.main([]) == 1
    assert capsys.readouterr().err.startswith("no_quotes: no quote before the wait ended")
    assert FakeClient.order[-1] == "send_quote"


def test_an_rfq_that_never_reaches_the_maker_exits_1(capsys, fake, keys, monkeypatch):
    monkeypatch.setattr(FakeClient, "rfqs", lambda self, **k: _maker._only(iter([]), k["only"].rfq_id, None))
    assert qs.main([]) == 1
    out, err = capsys.readouterr()
    assert err.strip() == ("no_quotes: the RFQ did not reach this account before the wait ended: "
                           "check its maker role and collateral")
    assert "Traceback" not in out + err and FakeClient.order[-1] == "ask"


def test_an_accepted_trade_that_landed_names_the_next_check_and_exits_0(capsys, fake, keys):
    FakeClient.status = "pending"
    assert qs.main([]) == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert err == "" and all(STAMP.match(line) for line in lines)
    assert lines[-2].endswith(" maker: pending")
    assert lines[-1].endswith(f" maker: accepted; it opens at the next hourly check, {CHECK.astimezone():%H:%M}")


@pytest.mark.parametrize("status, tx", [("refused", None), ("refused", TXH), ("pending", None), ("sending", None)])
def test_a_trade_not_open_ends_cleanly(capsys, fake, keys, monkeypatch, status, tx):
    def confirm(self, q, **kwargs):
        return SimpleNamespace(status=status, rfq_id=q.rfq_id, tx=tx)

    monkeypatch.setattr(FakeClient, "confirm", confirm)
    assert qs.main([]) == 1
    out, err = capsys.readouterr()
    assert err.strip() == f"not_open: the trade is {status}, not open"
    assert "Traceback" not in out + err and "next_check" not in FakeClient.order


def test_unset_keys_are_asked_hidden_and_never_printed(monkeypatch, capsys, fake, no_keys):
    asked = []
    answers = {"CRX_WALLET_PK (hidden): ": f"  {MAKER_KEY}\n", "CRX_TAKER_PK (hidden): ": TAKER_KEY}

    def prompt(text):
        asked.append(text)
        return answers[text]

    monkeypatch.setattr(qs.getpass, "getpass", prompt)
    assert qs.main([]) == 0
    assert asked == ["CRX_WALLET_PK (hidden): ", "CRX_TAKER_PK (hidden): "]
    assert qs.os.environ["CRX_WALLET_PK"] == MAKER_KEY and qs.os.environ["CRX_TAKER_PK"] == TAKER_KEY
    out, err = capsys.readouterr()
    for k in (MAKER_KEY, TAKER_KEY):
        assert k[2:] not in out + err


def test_only_the_missing_key_is_asked(monkeypatch, fake, no_keys):
    monkeypatch.setenv("CRX_WALLET_PK_FILE", "/nowhere/seat.key")
    asked = []
    monkeypatch.setattr(qs.getpass, "getpass", lambda t: asked.append(t) or TAKER_KEY)
    assert qs.main([]) == 0
    assert asked == ["CRX_TAKER_PK (hidden): "]


@pytest.mark.parametrize("answer", ["", "   ", EOFError, KeyboardInterrupt, "no-terminal"])
def test_no_taker_key_exits_2_before_any_call(monkeypatch, capsys, fake, no_keys, answer):
    monkeypatch.setenv("CRX_WALLET_PK", MAKER_KEY)

    def prompt(_t):
        if answer == "no-terminal":
            # getpass's fallback: it warns, then would read with echo on.
            qs.warnings.warn("Can not control echo on the terminal.", qs.getpass.GetPassWarning)
            return TAKER_KEY
        if isinstance(answer, type):
            raise answer()
        return answer

    monkeypatch.setattr(qs.getpass, "getpass", prompt)
    assert qs.main([]) == 2
    assert FakeClient.maker is None
    assert "Set CRX_TAKER_PK" in capsys.readouterr().err


def test_bad_taker_key_reports_config_without_the_key(monkeypatch, capsys, no_keys):
    bad = "0x" + "zz" * 10
    monkeypatch.setenv("CRX_WALLET_PK", MAKER_KEY)
    monkeypatch.setenv("CRX_TAKER_PK", bad)
    monkeypatch.setattr(crx.Client, "__init__", _no_net_init(crx.Client.__init__))
    assert qs.main([]) == 1
    out, err = capsys.readouterr()
    assert err.startswith("config: ") and "zz" not in out + err


def _no_net_init(init):
    from .conftest import FakeSession

    def wrapped(self, *a, **k):
        k.setdefault("session", FakeSession())
        return init(self, *a, **k)
    return wrapped


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_runs_as_a_module(monkeypatch, fake, keys):
    monkeypatch.setattr("sys.argv", ["crx.quickstart_maker"])
    with pytest.raises(SystemExit) as e:
        runpy.run_module("crx.quickstart_maker", run_name="__main__", alter_sys=False)
    assert e.value.code == 0
