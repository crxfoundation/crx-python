"""The single GET retry and the maker keepalive."""

import threading
import time

import pytest
import requests

import crx
from crx._http import GET_RETRY_PAUSE
from crx._maker import rfq_of

from .conftest import Pauses

RID = "0x" + "ab" * 32


def down(req):
    raise requests.ConnectionError("connection reset")


def balance_of(c):
    return {"account": c.address, "collateral": "100", "free": "100"}


def gets(session, path):
    return [x for x in session.calls if x["method"] == "GET" and x["path"] == path]


def test_get_is_sent_once_more_and_answers(make_client, session):
    c = make_client()
    c._sleep = pauses = Pauses()
    session.routes[("GET", "/balance")] = [down, balance_of(c)]
    assert c.balance().free == 100
    sent = gets(session, "/balance")
    assert len(sent) == 2 and pauses == [GET_RETRY_PAUSE]
    assert sent[0]["headers"]["x-crx-ts"] != sent[1]["headers"]["x-crx-ts"]  # the retry carries a new stamp


def test_get_raises_after_the_second_failure(make_client, session):
    c = make_client()
    c._sleep = pauses = Pauses()
    session.routes[("GET", "/balance")] = (503, {"code": "upstream", "error": "down"})
    with pytest.raises(crx.ServerError):
        c.balance()
    assert len(gets(session, "/balance")) == 2 and pauses == [GET_RETRY_PAUSE]


@pytest.mark.parametrize("method, path", [("POST", "/withdraw")])
def test_post_put_delete_are_sent_once(make_client, session, method, path):
    c = make_client()
    c._sleep = pauses = Pauses()
    session.routes[(method, path)] = down
    with pytest.raises(crx.NetworkError):
        c._gw.request(method, path, body={} if method == "POST" else None)
    assert [(x["method"], x["path"]) for x in session.calls] == [(method, path)] and pauses == []


class Tape:
    """Serve an empty GET /trades page and record each read's time, query and headers."""

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
    """Make a maker call: drop a leg."""
    c.drop_quote(rfq_of({"rfq_id": RID}), leg_id=RID)


@pytest.mark.keepalive
def test_maker_call_starts_keepalive(make_client, session, account):
    c = make_client(keepalive=1.0)
    tape = Tape(session)
    try:
        start(c)
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
def test_a_read_that_fails_does_not_end_the_keepalive(make_client, session):
    """A GET /trades with no answer is logged, and the keepalive reads again."""
    c = make_client(keepalive=1.0)
    tape = Tape(session)

    def no_answer(req):
        tape(req)
        down(req)
    session.routes[("GET", "/trades")] = no_answer
    try:
        start(c)
        assert tape.wait_beats(2) and c._live.thread.is_alive()
    finally:
        c.close()
    c._live.thread.join(3)
    assert not c._live.thread.is_alive()
