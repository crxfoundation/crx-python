"""A restored tape can reject an old cursor; recovery re-reads the tape once."""

import crx
import pytest

from .conftest import Clock


def conflict(head=1200):
    return 409, {
        "code": "conflict", "error": "cursor is past the restored head",
        "details": {"field": "after", "head": head},
    }


def event(seq):
    return {"type": "trade.opened", "seq": seq, "ts": 1_700_000_000_000, "data": {"trade_id": str(seq)}}


def rfq_event(seq, rid):
    now = 1_700_000_000
    return {"type": "rfq.opened", "seq": seq, "ts": now * 1000, "data": {
        "rfq_id": "0x" + f"{rid:064x}", "pair": "USD/MXN", "side": -1, "notional": "100",
        "chain": "avax-fuji", "kind": "open", "status": "open",
        "expiry": (now + 600) * 1000, "closes_at": (now + 300) * 1000,
        "quote_expiry_max": now + 300,
    }}


def test_trades_restore_recovery_is_opt_in_and_bounded(make_client, session):
    c = make_client()
    session.routes[("GET", "/trades")] = conflict()
    with pytest.raises(crx.CrxError) as ei:
        c.trades(after=500)
    assert (ei.value.code, ei.value.status, ei.value.details["field"]) == ("conflict", 409, "after")
    assert session.calls[-1]["query"]["after"] == ["500"]

    calls = []
    def restore_once(req):
        after = int(req["query"]["after"][0])
        calls.append(after)
        if after == 500:
            return conflict()
        if after == 0:
            return {"trades": [event(n) for n in range(1, 1001)], "seq": 1000}
        return {"trades": [event(1001)], "seq": 1001}

    session.calls.clear()
    session.routes[("GET", "/trades")] = restore_once
    rows = c.trades(after=500, recover_restore=True)
    assert calls == [500, 0, 1000]
    assert [row.seq for row in rows] == list(range(1, 1002))
    assert len({row.seq for row in rows}) == len(rows)

def test_trades_surfaces_a_second_restore_conflict(make_client, session):
    c = make_client()
    calls = []
    def restore_twice(req):
        after = int(req["query"]["after"][0])
        calls.append(after)
        if len(calls) == 1:
            return {"trades": [event(n) for n in range(1, 1001)], "seq": 1000}
        if len(calls) == 2:
            return conflict(1000)
        if len(calls) == 3:
            return {"trades": [event(n) for n in range(1, 1001)], "seq": 1000}
        if len(calls) == 4:
            return conflict(1000)
        raise AssertionError("recovery looped after its second conflict")

    session.routes[("GET", "/trades")] = restore_twice
    with pytest.raises(crx.CrxError) as ei:
        c.trades(after=500, recover_restore=True)
    assert (ei.value.code, ei.value.status) == ("conflict", 409)
    assert calls == [500, 1000, 0, 1000]


def test_rfqs_restore_recovery_is_opt_in_and_bounded(make_client, session):
    clock = Clock(1_700_000_000)
    c = make_client(clock=clock)
    session.routes[("GET", "/trades")] = conflict()
    with pytest.raises(crx.CrxError) as ei:
        list(c.rfqs(after=500, wait=0))
    assert (ei.value.code, ei.value.status, ei.value.details["field"]) == ("conflict", 409, "after")
    assert session.calls[-1]["query"]["after"] == ["500"]

    calls = []
    def restore_once(req):
        after = int(req["query"]["after"][0])
        calls.append(after)
        if after == 500:
            return {"trades": [rfq_event(501, 1)], "seq": 501}
        if after == 501:
            return conflict()
        if after == 0:
            return {"trades": [rfq_event(1, 1), rfq_event(2, 2)], "seq": 2}
        return {"trades": [], "seq": 2}

    session.calls.clear()
    session.routes[("GET", "/trades")] = restore_once
    got = list(c.rfqs(after=500, wait=2, poll=1, recover_restore=True))
    assert calls == [500, 501, 0, 2]
    assert [r.rfq_id for r in got] == ["0x" + f"{n:064x}" for n in (1, 2)]
    assert len({r.rfq_id for r in got}) == len(got)

def test_rfqs_surfaces_a_second_restore_conflict(make_client, session):
    calls = []
    def restore_twice(req):
        after = int(req["query"]["after"][0])
        calls.append(after)
        if len(calls) == 1:
            return {"trades": [rfq_event(501, 1)], "seq": 501}
        if len(calls) == 2:
            return conflict(501)
        if len(calls) == 3:
            return {"trades": [rfq_event(1, 1)], "seq": 1}
        if len(calls) == 4:
            return conflict(1)
        raise AssertionError("recovery looped after its second conflict")

    c = make_client(clock=Clock(1_700_000_000))
    session.routes[("GET", "/trades")] = restore_twice
    stream = c.rfqs(after=500, wait=10, poll=1, recover_restore=True)
    assert next(stream).rfq_id == "0x" + f"{1:064x}"
    with pytest.raises(crx.CrxError) as ei:
        next(stream)
    assert (ei.value.code, ei.value.status) == ("conflict", 409)
    assert calls == [500, 501, 0, 1]
