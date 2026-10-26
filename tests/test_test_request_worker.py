"""The worker accepts only its own fixed test requests, through the signed trade path."""

import copy
import importlib

import pytest

from crx import _eip712 as e7
from .conftest import Clock
from .test_quote_trade import Venue, HEAD, QID, RFQ, TXH, signed_posts
from .test_test_request import NOW, request_wire

RFQ2 = "0x" + "12" * 32


def worker_type():
    return importlib.import_module("crx.test_request_worker").TestRequestWorker


def add_request(session, venue, events, n):
    """Serve one open test request on ``venue.rfq`` and its ``rfq.opened`` event."""
    v, rid = venue, venue.rfq
    v.rfq_body = {"pair": "USDMXN", "side": "buy", "notional": "100", "premium_bps": 0,
                  "expiry": (NOW + 4_500) * 1000}
    v.qe = NOW + 570
    v.leg = e7.leg_id_for(HEAD, v.qe)
    v.rate = "18.123456"
    wire = request_wire(rfq_id=rid, side=1, client_rfq_id=f"test-request-fixture-{n}", leg_id=v.leg)
    event = {"type": "rfq.opened", "seq": n, "data": copy.deepcopy(wire)}
    event["data"].pop("status")
    event["data"].pop("kind")
    events.append(event)

    def view(req):
        current = dict(wire, **v.view(req), status="accepted" if v.sig else wire["status"])
        if v.sig:
            current["quote"] = current["quote"] or v.row()
            current["quote"]["status"] = "accepted"
            current["quotes"][0]["status"] = "accepted"
        return current
    session.routes[("GET", f"/rfqs/{rid}")] = view
    session.routes[("GET", f"/rfqs/{rid}/accept-check")] = lambda req: {
        "ok": True, "rfq_id": rid, "quote_id": QID, "trade_template": v.draft}
    return wire


@pytest.fixture
def setup(make_client, session, health, markets):
    clock = Clock(NOW)
    c = make_client(clock=clock)
    v = Venue(session, health, markets, clock)
    events = []
    wire = add_request(session, v, events, 1)
    session.routes[("GET", "/trades")] = lambda req: {"trades": events, "seq": len(events)}
    return c, v, wire, events, session, clock


def test_worker_accepts_and_restart_does_not_accept_again(setup, make_client):
    c, venue, wire, events, session, clock = setup
    Worker = worker_type()
    assert len(Worker(c).run_once()) == 1
    assert len(signed_posts(session)) == 1
    assert venue.template["typed_data"]["message"]["notionalE6"] == "100000000"
    assert venue.template["typed_data"]["message"]["premiumBps"] == "0"
    check = next(x for x in session.calls if x["path"].endswith("accept-check"))
    assert check["query"] == {"quote_id": [QID]}
    restarted = make_client(clock=clock)
    assert Worker(restarted).run_once() == []
    assert len(signed_posts(session)) == 1
    assert not any(method == "eth_sendRawTransaction" for method, _ in session.rpc_calls)


def test_worker_accepts_a_second_request_while_the_first_trade_is_pending(setup, health, markets):
    c, first, wire, events, session, clock = setup
    first.script = [("pending", TXH)]
    second = Venue(session, health, markets, clock, rfq=RFQ2)
    second.counter = (NOW + 60) * 1000  # the gateway's nonces rise per seat
    add_request(session, second, events, 2)
    trades = worker_type()(c).run_once()
    assert [t.rfq_id for t in trades] == [RFQ, RFQ2]
    assert first.sig is not None and second.sig is not None
    assert clock() < NOW + c._settle_wait  # no scan waits on a trade status
