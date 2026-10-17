"""The worker accepts only its own fixed test requests, through the signed trade path."""

import copy
import importlib

import pytest

import crx
from crx import _eip712 as e7
from .conftest import Clock
from .test_quote_trade import Venue, HEAD, QID, RFQ, signed_posts
from .test_test_request import NOW, request_wire


def worker_type():
    return importlib.import_module("crx.test_request_worker").TestRequestWorker


@pytest.fixture
def setup(make_client, session, health, markets):
    clock = Clock(NOW)
    c = make_client(clock=clock)
    v = Venue(session, health, markets, clock)
    v.rfq_body = {"pair": "USDMXN", "side": "buy", "notional": "100", "premium_bps": 0,
                  "expiry": (NOW + 18_000) * 1000}
    v.qe = NOW + 570
    v.leg = e7.leg_id_for(HEAD, v.qe)
    v.rate = "18.123456"
    wire = request_wire(rfq_id=RFQ, side=1, client_rfq_id="test-request-fixture-1", leg_id=v.leg)
    events = [{"type": "rfq.opened", "seq": 1, "data": copy.deepcopy(wire)}]
    events[0]["data"].pop("status")
    events[0]["data"].pop("kind")
    session.routes[("GET", "/trades")] = lambda req: {"trades": events, "seq": 1}
    session.routes[("GET", f"/rfqs/{RFQ}")] = lambda req: dict(
        wire, **v.view(req), status="accepted" if v.sig else wire["status"])
    session.routes[("GET", f"/rfqs/{RFQ}/accept-check")] = lambda req: {
        "ok": True, "rfq_id": RFQ, "quote_id": QID, "trade_template": v.draft}
    return c, v, wire, events, session, clock


def test_worker_accepts_and_restart_does_not_accept_again(setup, make_client):
    c, venue, wire, events, session, clock = setup
    Worker = worker_type()
    assert Worker(c).run_once()[0].status == "open"
    assert len(signed_posts(session)) == 1
    assert venue.template["typed_data"]["message"]["notionalE6"] == "100000000"
    assert venue.template["typed_data"]["message"]["premiumBps"] == "0"
    check = next(x for x in session.calls if x["path"].endswith("accept-check"))
    assert check["query"] == {"quote_id": [QID]}
    restarted = make_client(clock=clock)
    assert Worker(restarted).run_once() == []
    assert len(signed_posts(session)) == 1
    assert not any(method == "eth_sendRawTransaction" for method, _ in session.rpc_calls)


@pytest.mark.parametrize("edit", [
    {"test_request": False}, {"test_request": "true"}, {"client_rfq_id": False},
    {"side": -1}, {"side": True}, {"notional": "101"}, {"notional": 100},
    {"pair": "USD/BRL"}, {"kind": "close"}, {"premium_bps": 1},
    {"chain": "ethereum"}, {"closes_at": NOW * 1000}, {"expiry": (NOW + 86_400) * 1000},
])
def test_worker_ignores_other_or_malformed_events(setup, edit):
    c, v, wire, events, session, clock = setup
    events[0]["data"].update(edit)
    assert worker_type()(c).run_once() == []
    assert signed_posts(session) == []
    assert not any(x["path"].endswith("accept-check") for x in session.calls)


@pytest.mark.parametrize("edit", [
    {"test_request": False}, {"client_rfq_id": "someone-else"},
    {"client_rfq_id": "test-request-someone-else"}, {"side": -1},
    {"notional": "200"}, {"pair": "USD/BRL"}, {"premium_bps": 1},
    {"status": "accepted"}, {"status": "cancelled"}, {"status": "expired"},
    {"status": "rejected"}, {"trade_status": "pending"}, {"trade_status": "refused"},
])
def test_worker_rechecks_authoritative_view(setup, edit):
    c, v, wire, events, session, clock = setup
    wire.update(edit)
    assert worker_type()(c).run_once() == []
    assert not signed_posts(session)


@pytest.mark.parametrize("check", [
    {"ok": False}, {"ok": 1}, {"ok": True, "rfq_id": RFQ, "quote_id": "0x" + "ff" * 32},
    (409, {"code": "rejected", "error": "closed"}),
    (409, {"code": "rate_out_of_band", "error": "outside band"}),
])
def test_worker_never_signs_a_failed_check(setup, check):
    c, v, wire, events, session, clock = setup
    session.routes[("GET", f"/rfqs/{RFQ}/accept-check")] = check
    assert worker_type()(c).run_once() == []
    assert not signed_posts(session)


def test_worker_returns_to_an_unquoted_request_without_skip_cursor(setup):
    c, v, wire, events, session, clock = setup
    saved = session.routes[("GET", f"/rfqs/{RFQ}")]
    session.routes[("GET", f"/rfqs/{RFQ}")] = dict(wire, quotes=[])
    worker = worker_type()(c)
    assert worker.run_once() == []
    session.routes[("GET", f"/rfqs/{RFQ}")] = saved
    assert worker.run_once()[0].status == "open"
    assert all(x["query"]["after"] == ["0"] for x in session.calls if x["path"] == "/trades")


def test_worker_retries_transient_reads_in_bounded_run(setup):
    c, v, wire, events, session, clock = setup
    good = session.routes[("GET", f"/rfqs/{RFQ}")]
    session.routes[("GET", f"/rfqs/{RFQ}")] = [
        (503, {"code": "unavailable", "error": "unavailable"}),
        (503, {"code": "unavailable", "error": "unavailable"}), good,
    ]
    worker_type()(c, poll=1).run(duration=5)
    assert len(signed_posts(session)) == 1
    assert clock() <= NOW + 7


def test_worker_does_not_wait_for_collection_window(setup):
    c, v, wire, events, session, clock = setup
    v.winner = False
    v.make_template()
    worker_type()(c).run_once()
    assert len(signed_posts(session)) == 1
    assert clock() < NOW + 10


def test_worker_refuses_event_without_taker_only_field(setup):
    c, v, wire, events, session, clock = setup
    events[0]["data"].pop("client_rfq_id")
    assert worker_type()(c).run_once() == []
    assert not signed_posts(session)


def test_worker_accepts_compact_pair_wire(setup):
    c, v, wire, events, session, clock = setup
    wire["pair"] = events[0]["data"]["pair"] = "USDMXN"
    assert worker_type()(c).run_once()[0].pair == "USD/MXN"
    assert len(signed_posts(session)) == 1


@pytest.mark.parametrize("rate", ["NaN", "Infinity", "-1", "0", True, None, {}])
def test_worker_never_signs_invalid_rate(setup, rate):
    c, v, wire, events, session, clock = setup
    v.row_edit["rate"] = rate
    assert worker_type()(c).run_once() == []
    assert not signed_posts(session)


def test_worker_checks_template_before_signing(setup):
    c, v, wire, events, session, clock = setup
    v.msg_edit["notionalE6"] = "200000000"
    assert worker_type()(c).run_once() == []
    assert not signed_posts(session)


def test_worker_checks_whole_history_and_bounds_bad_pagination(setup, monkeypatch):
    from crx import _maker
    c, v, wire, events, session, clock = setup
    monkeypatch.setattr(_maker, "PAGE", 2)
    def page(req):
        after = int(req["query"]["after"][0])
        if after == 0:
            return {"trades": [{"type": "trade.opened"}] * 2, "seq": 2}
        return {"trades": events, "seq": 3}
    session.routes[("GET", "/trades")] = page
    assert worker_type()(c).run_once()[0].status == "open"
    assert [x["query"]["after"] for x in session.calls if x["path"] == "/trades"] == [["0"], ["2"]]
    session.routes[("GET", "/trades")] = lambda req: {
        "trades": [{"type": "trade.opened"}] * 2,
        "seq": int(req["query"]["after"][0]) + 2}
    with pytest.raises(crx.BadAnswer, match="max_pages"):
        worker_type()(c, max_pages=2).run_once()


@pytest.mark.parametrize("cid", [None, "", "ordinary-rfq", "test-request-", "test-request- ", 123])
def test_worker_rejects_missing_or_wrong_test_request_ownership(setup, cid):
    c, v, wire, events, session, clock = setup
    wire["client_rfq_id"] = events[0]["data"]["client_rfq_id"] = cid
    assert worker_type()(c).run_once() == []
    assert not signed_posts(session)


def test_worker_does_not_renew_expired_rfq(setup):
    c, v, wire, events, session, clock = setup
    clock.t = NOW + 121
    assert worker_type()(c).run_once() == []
    assert not signed_posts(session)
    assert not any(x["method"] == "POST" for x in session.calls)


def test_worker_requires_explicit_enable_before_constructing_client(monkeypatch, capsys):
    module = importlib.import_module("crx.test_request_worker")
    def forbidden(**kwargs):
        raise AssertionError("disabled CLI constructed a client")
    monkeypatch.setattr(module, "Client", forbidden)
    with pytest.raises(SystemExit) as error:
        module.main(["--key-file", "fixture-path", "--state-dir", "fixture-state"])
    assert error.value.code == 2
    assert "--enable" in capsys.readouterr().err


def test_enabled_worker_uses_client_rpc_environment_without_rpc_argument(monkeypatch, setup):
    module = importlib.import_module("crx.test_request_worker")
    c, v, wire, events, session, clock = setup
    calls = []
    def client(**kwargs):
        calls.append(kwargs)
        return c
    monkeypatch.setattr(module, "Client", client)
    monkeypatch.setenv("CRX_RPC", "https://rpc.test/fixture-token")
    assert module.main(["--enable", "--key-file", "fixture-path", "--state-dir", "fixture-state",
                        "--duration", "1"]) == 0
    assert len(calls) == 1 and "rpc_url" not in calls[0]
    assert calls[0]["network"] == "mainnet" and calls[0]["allow_mainnet"] is True
    assert len(signed_posts(session)) == 1


def test_worker_accepts_renewed_rfq_under_original_request_deadline(setup):
    c, v, wire, events, session, clock = setup
    clock.t = NOW + 7200
    v.qe = NOW + 7770
    v.leg = e7.leg_id_for(HEAD, v.qe)
    renewal = {"closes_at": (NOW + 7320) * 1000, "quote_expiry_max": v.qe, "leg_id": v.leg,
               "taker_ref": "0x" + "c4" * 32}
    wire.update(renewal)
    events[0]["data"].update(renewal)
    assert worker_type()(c).run_once()[0].status == "open"
    assert len(signed_posts(session)) == 1
    assert wire["request_expires_at"] == (NOW + 10800) * 1000
    assert wire["expiry"] == (NOW + 18000) * 1000
