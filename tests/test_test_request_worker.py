"""The worker accepts only its own fixed test requests, through the signed trade path."""

import copy
import importlib

import pytest

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
                  "expiry": (NOW + 4_500) * 1000}
    v.qe = NOW + 570
    v.leg = e7.leg_id_for(HEAD, v.qe)
    v.rate = "18.123456"
    wire = request_wire(rfq_id=RFQ, side=1, client_rfq_id="test-request-fixture-1", leg_id=v.leg)
    events = [{"type": "rfq.opened", "seq": 1, "data": copy.deepcopy(wire)}]
    events[0]["data"].pop("status")
    events[0]["data"].pop("kind")
    session.routes[("GET", "/trades")] = lambda req: {"trades": events, "seq": 1}
    def view(req):
        current = dict(wire, **v.view(req), status="accepted" if v.sig else wire["status"])
        if v.sig:
            current["quote"] = current["quote"] or v.row()
            current["quote"]["status"] = "accepted"
            current["quotes"][0]["status"] = "accepted"
        return current
    session.routes[("GET", f"/rfqs/{RFQ}")] = view
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
