"""python -m crx.quickstart_maker: key handling, arguments and the steps. No network."""

from __future__ import annotations

import importlib
import inspect
import re
import runpy
import textwrap
import threading
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import crx
import crx.quickstart_maker as qs

MAKER_KEY = "0x" + "11" * 32
TAKER_KEY = "0x" + "22" * 32
EXAMPLE = Path(__file__).parent.parent / "examples" / "maker.py"
STAMP = re.compile(r"^\d\d:\d\d:\d\d ")
OURS, FOREIGN, SHARED = "0x" + "a1" * 32, "0x" + "b1" * 32, "0x" + "c1" * 32


class FakeClient:
    """Stands in for crx.Client: the maker (no key argument) or the test taker (the key passed in)."""

    maker = taker = None
    cid = None
    asked = None
    lose = None

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.calls = []
        if args:
            self.address = "0x5b38da6a701c568545dcfcb03fcb875f56beddc4"
            FakeClient.taker = self
        else:
            self.address = "0x7638c8075e517393fa62008b5faa6c1ea832fe71"
            FakeClient.maker = self

    def deposit(self, amount):
        self.calls.append(("deposit", amount))
        return SimpleNamespace(status="credited")

    def balance(self):
        self.calls.append(("balance",))
        return SimpleNamespace(free="20000.000000")

    # the taker's calls
    def quote(self, pair, side, notional, **kwargs):
        self.calls.append(("quote", pair, side, notional))
        FakeClient.cid = kwargs["client_rfq_id"]
        FakeClient.asked.set()
        return SimpleNamespace(rate=Decimal("18.09991"), house=False, rfq_id=OURS)

    def trade(self, q):
        self.calls.append(("trade", q.rfq_id))
        return SimpleNamespace(status="open")

    def rfq(self, rfq_id):
        self.calls.append(("rfq", rfq_id))
        if rfq_id == FOREIGN:
            raise crx.AuthError("not entitled", status=401)
        if rfq_id == SHARED:
            return SimpleNamespace(client_rfq_id=None, house_rate=Decimal("18.1"))
        n = sum(1 for c in self.calls if c == ("rfq", OURS))
        return SimpleNamespace(client_rfq_id=FakeClient.cid, house_rate=Decimal("18.09991") if n >= 3 else None)

    # the maker's calls
    def rfqs(self, **kwargs):
        stop = kwargs.pop("stop", None)
        self.calls.append(("rfqs", kwargs, stop))

        def gen():
            yield SimpleNamespace(rfq_id=FOREIGN, pair="USD/MXN", side="sell", notional=Decimal("25000"))
            yield SimpleNamespace(rfq_id=SHARED, pair="USD/MXN", side="sell", notional=Decimal("25000"))
            while not FakeClient.asked.wait(0.01):
                if stop is not None and stop.is_set():
                    return
            yield SimpleNamespace(rfq_id=OURS, pair="USD/MXN", side="sell", notional=Decimal("25000"))
        return gen()

    def send_quote(self, rfq, rate):
        self.calls.append(("send_quote", rfq.rfq_id, rate))
        return SimpleNamespace(rfq_id=rfq.rfq_id, rate=rate)

    def confirm(self, q, **kwargs):
        self.calls.append(("confirm", q.rfq_id, kwargs))
        if FakeClient.lose:
            raise crx.QuoteLost("the taker accepted another quote", reason="another_maker")
        return SimpleNamespace(status="open", rfq_id=q.rfq_id, tx="0x" + "55" * 32)

    def positions(self):
        self.calls.append(("positions",))
        other = SimpleNamespace(rfq_id=FOREIGN, pair="USDMXN", side="buy", notional=5, rate="17", status="open")
        mine = SimpleNamespace(rfq_id=OURS, pair="USDMXN", side="sell", notional=25000, rate="18.09991", status="open")
        return [other, mine]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(crx, "Client", FakeClient)
    monkeypatch.setattr(qs.time, "sleep", lambda _s: None)
    FakeClient.maker = FakeClient.taker = FakeClient.cid = FakeClient.lose = None
    FakeClient.asked = threading.Event()
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
                 "maker.deposit(20_000)", "taker.deposit(20_000)", "for rfq in rfqs:",
                 "q = maker.send_quote(rfq, near_mid(rfq))", "t = maker.confirm(q, timeout=60)"):
        assert line in src


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


def test_quotes_only_its_own_takers_rfq(capsys, fake, keys):
    assert qs.main([]) == 0
    m, t = FakeClient.maker, FakeClient.taker
    assert m.kwargs == {"network": "testnet"} and m.args == ()
    assert t.kwargs == {"network": "testnet"} and t.args == (TAKER_KEY,)
    assert [c for c in m.calls if c[0] == "send_quote"] == [("send_quote", OURS, Decimal("18.09991"))]
    stream = next(c for c in m.calls if c[0] == "rfqs")
    assert stream[1] == {"wait": 60} and isinstance(stream[2], threading.Event)
    assert ("confirm", OURS, {"timeout": 60}) in m.calls
    assert ("quote", "USD/MXN", "buy", 25_000) in t.calls and ("trade", OURS) in t.calls
    assert FakeClient.cid.startswith("maker-qs-")
    lines = capsys.readouterr().out.splitlines()
    assert all(STAMP.match(line) for line in lines)
    assert lines[0].endswith(" 0x7638c8075e517393fa62008b5faa6c1ea832fe71 0x5b38da6a701c568545dcfcb03fcb875f56beddc4")
    assert any(line.endswith(" maker: quoted 18.09991") for line in lines)
    assert any(line.endswith(" maker: open") for line in lines)
    assert any(line.endswith(" taker: open") for line in lines)
    assert lines[-1].endswith(" USDMXN sell 25000 18.09991 open")


def test_a_lost_quote_exits_1_with_its_code(capsys, fake, keys):
    FakeClient.lose = True
    assert qs.main([]) == 1
    assert capsys.readouterr().err.strip() == "quote_lost: the taker accepted another quote"


def test_a_taker_refusal_ends_the_run_at_once_with_its_own_error(capsys, fake, keys, monkeypatch):
    def refused(self, *a, **k):
        raise crx.BelowMin("a pool wallet trades at least 25000 USD notional; got 10000")

    monkeypatch.setattr(FakeClient, "quote", refused)
    FakeClient.asked.wait = lambda timeout=None: False  # the taker's RFQ never opens
    start = qs.time.monotonic()
    assert qs.main([]) == 1
    assert qs.time.monotonic() - start < 5
    out, err = capsys.readouterr()
    assert err.strip() == "below_min: a pool wallet trades at least 25000 USD notional; got 10000"
    assert "maker role" not in err
    assert any(line.endswith(" taker: below_min a pool wallet trades at least 25000 USD notional; got 10000")
               for line in out.splitlines())
    assert not any(c[0] == "send_quote" for c in FakeClient.maker.calls)


def test_a_taker_rfq_with_no_quote_gets_the_maker_role_hint(capsys, fake, keys, monkeypatch):
    def unquoted(self, *a, **k):
        raise crx.NoQuotes("no quote before the wait ended")

    monkeypatch.setattr(FakeClient, "quote", unquoted)
    FakeClient.asked.wait = lambda timeout=None: False
    assert qs.main([]) == 1
    assert capsys.readouterr().err.startswith("no_quotes: your test taker's RFQ did not reach the maker seat")


def test_rate_limits_in_the_match_loop_are_waited_out(capsys, fake, keys, monkeypatch):
    real = FakeClient.rfq
    hits = []

    def limited(self, rfq_id):
        hits.append(rfq_id)
        if rfq_id == OURS and len([h for h in hits if h == OURS]) in (1, 2, 5):
            raise crx.RateLimited("slow down", status=429)
        return real(self, rfq_id)

    monkeypatch.setattr(FakeClient, "rfq", limited)
    assert qs.main([]) == 0
    assert [c for c in FakeClient.maker.calls if c[0] == "send_quote"] == [("send_quote", OURS, Decimal("18.09991"))]


def test_a_rate_limit_that_holds_exits_1(capsys, fake, keys, monkeypatch):
    real = FakeClient.rfq

    def limited(self, rfq_id):
        if rfq_id == OURS:
            raise crx.RateLimited("slow down", status=429)
        return real(self, rfq_id)

    monkeypatch.setattr(FakeClient, "rfq", limited)
    assert qs.main([]) == 1
    assert capsys.readouterr().err.strip() == "rate_limited: slow down"
    assert not any(c[0] == "send_quote" for c in FakeClient.maker.calls)


@pytest.mark.parametrize("status", ["refused", "pending", "sending"])
def test_a_trade_not_open_ends_cleanly(capsys, fake, keys, monkeypatch, status):
    def confirm(self, q, **kwargs):
        return SimpleNamespace(status=status, rfq_id=q.rfq_id, tx=None)

    monkeypatch.setattr(FakeClient, "confirm", confirm)
    assert qs.main([]) == 1
    out, err = capsys.readouterr()
    assert err.strip() == f"not_open: the trade is {status}, not open"
    assert "Traceback" not in out + err
    assert ("positions",) not in FakeClient.maker.calls


def test_an_open_trade_not_yet_listed_ends_without_error(capsys, fake, keys, monkeypatch):
    monkeypatch.setattr(FakeClient, "positions", lambda self: [])
    assert qs.main([]) == 0


def test_no_rfq_for_the_taker_exits_1(capsys, fake, keys, monkeypatch):
    monkeypatch.setattr(FakeClient, "rfqs", lambda self, **k: iter([]))
    assert qs.main([]) == 1
    assert capsys.readouterr().err.startswith("no_quotes: your test taker's RFQ did not reach the maker seat")


def test_no_house_quote_exits_1(capsys, fake, keys, monkeypatch):
    real = FakeClient.rfq

    def never(self, rfq_id):
        v = real(self, rfq_id)
        return SimpleNamespace(client_rfq_id=v.client_rfq_id, house_rate=None)

    monkeypatch.setattr(FakeClient, "rfq", never)
    assert qs.main([]) == 1
    assert capsys.readouterr().err.strip() == "no_quotes: no house quote to price from"
    assert not any(c[0] == "send_quote" for c in FakeClient.maker.calls)


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
