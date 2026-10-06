"""A GET is sent once more after a network error, a 5xx or mark_unavailable; other methods once.
The maker keepalive."""

import gc
import threading
import time

import pytest
import requests

import crx
from crx._http import GET_RETRY_PAUSE
from crx._maker import rfq_of

from .test_quote_trade import Custodian, custodian_client

RID = "0x" + "ab" * 32
SEAT = "0x" + "5b" * 20


class Pauses(list):
    def __call__(self, s):
        self.append(s)


def down(req):
    raise requests.ConnectionError("connection reset")


def balance_of(c):
    return {"account": c.address, "collateral": "100", "free": "100"}


NO_MARK = (503, {"code": "mark_unavailable", "error": "USD/MXN is temporarily unavailable",
                 "details": {"pair": "USD/MXN"}})


def gets(session, path):
    return [x for x in session.calls if x["method"] == "GET" and x["path"] == path]


# ---------- the GET retry ----------

def test_retry_pause_is_short():
    assert 0 < GET_RETRY_PAUSE <= 1


@pytest.mark.parametrize("first", [down, (503, {"code": "upstream", "error": "down"}),
                                   (502, "<html>bad gateway</html>"), NO_MARK],
                         ids=["network", "503", "502", "mark_unavailable"])
def test_get_is_sent_once_more_and_answers(make_client, session, first):
    c = make_client()
    c._sleep = pauses = Pauses()
    session.routes[("GET", "/balance")] = [first, balance_of(c)]
    assert c.balance().free == 100
    sent = gets(session, "/balance")
    assert len(sent) == 2 and pauses == [GET_RETRY_PAUSE]
    assert sent[0]["headers"]["x-crx-ts"] != sent[1]["headers"]["x-crx-ts"]  # a new login on the retry


@pytest.mark.parametrize("fail, err", [(down, crx.NetworkError),
                                       ((503, {"code": "upstream", "error": "down"}), crx.ServerError),
                                       (NO_MARK, crx.MarkUnavailable)])
def test_get_raises_after_the_second_failure(make_client, session, fail, err):
    c = make_client()
    c._sleep = pauses = Pauses()
    session.routes[("GET", "/balance")] = fail
    with pytest.raises(err):
        c.balance()
    assert len(gets(session, "/balance")) == 2 and pauses == [GET_RETRY_PAUSE]


@pytest.mark.parametrize("status, code", [(400, "bad_request"), (401, "unauthorized"), (404, "not_found"),
                                          (409, "conflict"), (429, "rate_limited"), (422, "rate_out_of_band"),
                                          (409, "position_matured")])
def test_get_refusal_is_not_sent_again(make_client, session, status, code):
    c = make_client()
    c._sleep = pauses = Pauses()
    session.routes[("GET", "/balance")] = (status, {"code": code, "error": "no"})
    with pytest.raises(crx.CrxError):
        c.balance()
    assert len(gets(session, "/balance")) == 1 and pauses == []


@pytest.mark.parametrize("method, path", [("POST", "/rfqs"), ("POST", f"/rfqs/{RID}/quotes"), ("POST", "/withdraw"),
                                          ("POST", "/deposit"), ("PUT", f"/viewers/{SEAT}"),
                                          ("DELETE", f"/rfqs/{RID}/quotes/{RID}")])
@pytest.mark.parametrize("fail, err", [(down, crx.NetworkError),
                                       ((503, {"code": "upstream", "error": "down"}), crx.ServerError),
                                       (NO_MARK, crx.MarkUnavailable)])
def test_post_put_delete_are_sent_once(make_client, session, method, path, fail, err):
    c = make_client()
    c._sleep = pauses = Pauses()
    session.routes[(method, path)] = fail
    with pytest.raises(err):
        c._gw.request(method, path, body={} if method == "POST" else None)
    assert [(x["method"], x["path"]) for x in session.calls] == [(method, path)] and pauses == []


def test_mark_unavailable_get_retry_keeps_the_rfq_read_alive(make_client, session):
    # A maker loop reads one RFQ: one 503 mark_unavailable no longer ends it.
    c = make_client()
    c._sleep = pauses = Pauses()
    session.routes[("GET", f"/rfqs/{RID}")] = [NO_MARK, {"rfq_id": RID, "kind": "open"}]
    assert c._gw.request("GET", f"/rfqs/{RID}")["rfq_id"] == RID
    assert len(gets(session, f"/rfqs/{RID}")) == 2 and pauses == [GET_RETRY_PAUSE]


def test_withdraw_post_is_sent_once_on_503(make_client, session):
    c = make_client()
    c._sleep = pauses = Pauses()
    session.routes[("GET", "/balance")] = dict(balance_of(c), withdraw={"nonce": "3"})
    session.routes[("POST", "/withdraw")] = (503, {"code": "upstream", "error": "down"})
    with pytest.raises(crx.ServerError):
        c.withdraw(10)
    assert len([x for x in session.calls if x["path"] == "/withdraw"]) == 1 and pauses == []


@pytest.mark.parametrize("fail, err", [(down, crx.NetworkError),
                                       ((503, {"code": "upstream", "error": "down"}), crx.ServerError),
                                       (NO_MARK, crx.MarkUnavailable)])
def test_failed_session_mint_is_not_sent_again(session, tmp_path, account, fail, err):
    s = Custodian(account)
    c = custodian_client(session, tmp_path, s)
    c._sleep = pauses = Pauses()
    session.routes[("POST", "/session")] = fail
    session.routes[("GET", "/balance")] = balance_of(c)
    with pytest.raises(err):
        c.balance()
    assert [(x["method"], x["path"]) for x in session.calls] == [("POST", "/session")]
    assert len(s.messages) == 1 and pauses == []


# ---------- the maker keepalive ----------

class Tape:
    """GET /trades: an empty page. Keeps each read's query and time."""

    def __init__(self, session, answer=None):
        self.reads, self.lock, self.answer = [], threading.Lock(), answer
        session.routes[("GET", "/trades")] = self
        session.routes[("GET", f"/rfqs/{RID}")] = {"rfq_id": RID, "kind": "open"}
        session.routes[("DELETE", f"/rfqs/{RID}/quotes/{RID}")] = {"dropped": True, "leg_id": RID}

    def __call__(self, req):
        with self.lock:
            self.reads.append((time.monotonic(), req["query"], req["headers"]))
        return self.answer or {"trades": [], "seq": 0}

    def beats(self):
        with self.lock:
            return [r for r in self.reads if r[1].get("limit") == ["1"]]

    def wait_beats(self, n, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if len(self.beats()) >= n:
                return True
            time.sleep(0.05)
        return False


def start(c):
    """A maker call: drop a leg."""
    c.drop_quote(rfq_of({"rfq_id": RID}), leg_id=RID)


def test_keepalive_default_and_range(make_client):
    assert make_client()._keepalive_s == 5.0
    for ok in (1, 5.0, 10):
        assert make_client(keepalive=ok)._keepalive_s == float(ok)
    assert make_client(keepalive=None)._keepalive_s is None
    for bad in (0, 0.5, 10.5, True, "5", float("nan")):
        with pytest.raises(crx.ConfigError):
            make_client(keepalive=bad)


@pytest.mark.keepalive
def test_taker_calls_start_no_keepalive(make_client, session):
    c = make_client(keepalive=1.0)
    Tape(session)
    session.routes[("GET", "/balance")] = balance_of(c)
    c.balance()
    c.trades()
    c.rfq(RID)
    assert c._live is None


@pytest.mark.keepalive
@pytest.mark.parametrize("call", ["rfqs", "send_quote", "confirm", "drop_quote"])
def test_maker_call_starts_keepalive(make_client, session, account, call):
    c = make_client(keepalive=1.0)
    tape = Tape(session)
    try:
        if call == "rfqs":
            c.rfqs(wait=0)
        elif call == "drop_quote":
            start(c)
        else:
            with pytest.raises(crx.BadRequest):  # refused after the keepalive starts
                getattr(c, call)(*(("not an rfq", "18.1") if call == "send_quote" else ("not a quote",)))
        assert c._live is not None and c._live.thread.daemon
        assert tape.wait_beats(1)
        _, query, headers = tape.beats()[0]
        assert query == {"after": ["0"], "limit": ["1"]}
        assert headers["x-crx-address"] == account.address.lower() and headers["x-crx-sig"]
        live = c._live
        start(c)
        assert c._live is live  # one thread per client
    finally:
        c.close()
    c._live.thread.join(3)
    assert not c._live.thread.is_alive()


@pytest.mark.keepalive
def test_keepalive_reads_only_when_the_client_does_not(make_client, session):
    c = make_client(keepalive=2.0)
    tape = Tape(session)
    try:
        c.trades()  # a tape read: the seat is live for now
        start(c)
        end = time.monotonic() + 4.5
        while time.monotonic() < end:
            c.trades()
            time.sleep(0.2)
        assert tape.beats() == []
        assert tape.wait_beats(1, timeout=5.0)  # the client stopped reading: the keepalive reads
    finally:
        c.close()


@pytest.mark.keepalive
def test_failed_keepalive_read_waits_its_interval(make_client, session):
    c = make_client(keepalive=1.0)
    tape = Tape(session, answer=(503, {"code": "upstream", "error": "down"}))
    try:
        start(c)
        time.sleep(2.6)
        n = len(tape.beats())
        assert 2 <= n <= 4  # at 0, 1 and 2 s: no tight loop, no GET retry
        assert c._live.thread.is_alive()
    finally:
        c.close()


@pytest.mark.keepalive
def test_keepalive_off_and_after_close(make_client, session):
    Tape(session)
    off = make_client(keepalive=None)
    start(off)
    assert off._live is None
    closed = make_client(keepalive=1.0)
    closed.close()
    start(closed)
    assert closed._live is None


@pytest.mark.keepalive
def test_keepalive_ends_once_the_client_is_freed(make_client, session):
    Tape(session)
    c = make_client(keepalive=1.0)
    start(c)
    thread = c._live.thread
    del c
    gc.collect()
    thread.join(3)
    assert not thread.is_alive()
