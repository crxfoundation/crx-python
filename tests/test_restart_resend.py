"""A POST that answers 503 service_restarting goes again for up to 6 s, with the same body and new auth headers."""

import json

from eth_account import Account
from eth_account.messages import encode_defunct

from crx._chain import Rpc
from crx._http import Gateway, rest_message

from .conftest import WEEK, Pauses
from .test_quote_trade import QID, RFQ, TXH, accepts, clock, venue  # noqa: F401 (fixtures)
from .test_transport import http, js, server  # noqa: F401 (fixtures)

LINE = "The service is restarting. Retry in a few seconds."
PATH = f"/rfqs/{RFQ}/accept"
BODY = {"quote_id": QID, "sig": "0x" + "ab" * 65}
TAKEN = {"rfq_id": RFQ, "quote_id": QID, "client_quote_id": None}
TOKEN = "cd" * 32


def restarting_body(secs=1):
    """The gateway's 503 service_restarting body, with ``details.retry_after_secs`` unless ``secs`` is None."""
    body = {"error": LINE, "code": "service_restarting", "outcome": "unavailable", "message": LINE}
    return body if secs is None else {**body, "details": {"retry_after_secs": secs}}


def restarting(secs=1):
    """The 503 service_restarting answer, with ``Retry-After: secs`` unless ``secs`` is None."""
    return 503, "application/json", js(restarting_body(secs)), {} if secs is None else {"Retry-After": str(secs)}


def refused(status, code):
    return status, "application/json", js({"error": code, "code": code})


OK = (200, "application/json", js(TAKEN))

STILL = ("server_error", "service_restarting")

ROWS = [  # name, the POST's answers in order, posts, pauses, the outcome: the 200 body, or (code, gateway code)
    ("restarting, then 200", [restarting(), OK], 2, [1], TAKEN),
    ("restarting for 4 s, then 200", [restarting()] * 4 + [OK], 5, [1] * 4, TAKEN),
    ("restarting past the 6 s cap", [restarting()], 7, [1] * 6, STILL),
    ("Retry-After 30 is past the cap", [restarting(30), OK], 1, [], STILL),
    ("Retry-After 0 waits 0.5 s", [restarting(0), OK], 2, [0.5], TAKEN),
    ("no Retry-After waits 1 s", [restarting(None), OK], 2, [1], TAKEN),
    ("500", [refused(500, "internal"), OK], 1, [], ("server_error", "internal")),
    ("409", [refused(409, "conflict"), OK], 1, [], ("conflict", "conflict")),
    ("503 service_unavailable", [refused(503, "service_unavailable"), OK], 1, [],
     ("service_unavailable", "service_unavailable")),
]


def outcome(call):
    try:
        return call()
    except Exception as e:  # noqa: BLE001 - the row names the error
        return getattr(e, "code", type(e).__name__), getattr(e, "gateway_code", None)


def test_a_post_that_answers_503_service_restarting_goes_once_more_with_the_same_body(
        server, http, account, make_client, venue, session, clock):
    me = account.address.lower()
    raw = json.dumps(BODY, separators=(",", ":")).encode()
    for name, answers, posts, pauses, out in ROWS:
        server.seen.clear()
        server.answers[("POST", PATH)] = list(answers)
        gw = Gateway(server.url, account, http)
        gw.sleep = slept = Pauses()
        assert outcome(lambda: gw.request("POST", PATH, body=BODY)) == out, name
        sent = [(h, b) for m, p, h, b in server.seen if (m, p) == ("POST", PATH)]
        assert (len(sent), slept) == (posts, pauses), name
        assert {b for _, b in sent} == {raw}, name
        stamps = [int(h["x-crx-ts"]) for h, _ in sent]
        assert stamps == sorted(set(stamps)), name  # the resend signs a new stamp
        for h, b in sent:
            msg = rest_message("POST", PATH, me, me, int(h["x-crx-ts"]), b)
            assert Account.recover_message(encode_defunct(text=msg), signature=h["x-crx-sig"]).lower() == me, name

    # A GET keeps its one retry after 0.5 s.
    server.seen.clear()
    server.answers[("GET", "/balance")] = [restarting(), restarting(), OK]
    gw = Gateway(server.url, account, http)
    gw.sleep = slept = Pauses()
    assert outcome(lambda: gw.request("GET", "/balance")) == ("server_error", "service_restarting")
    assert ([m for m, _, _, _ in server.seen], slept) == (["GET", "GET"], [0.5])

    # The session mint goes again, signed anew; the session call sends its token again.
    server.seen.clear()
    mint = (200, "application/json", js({"token": TOKEN, "ttl_ms": 3_600_000, "custody": me, "signer": me}))
    server.answers[("POST", "/session")] = [restarting(), mint]
    server.answers[("POST", PATH)] = [restarting(), OK]
    gw = Gateway(server.url, account, http, login=True)
    gw.sleep = slept = Pauses()
    assert gw.request("POST", PATH, body=BODY) == TAKEN
    assert [(p, h.get("x-crx-session"), b) for _, p, h, b in server.seen] == [
        ("/session", None, b""), ("/session", None, b""), (PATH, TOKEN, raw), (PATH, TOKEN, raw)]
    mints = [int(h["x-crx-ts"]) for _, p, h, _ in server.seen if p == "/session"]
    assert mints[0] < mints[1] and slept == [1, 1]

    # On the /rpc route a read goes once more; a send goes once, and its hash tracks it.
    gw = Gateway(server.url, account, http)
    gw.sleep = Pauses()
    rpc = Rpc(server.url + "/rpc", http, gateway=gw)
    for method, params, posts, out in [("eth_blockNumber", [], 2, 1000),
                                       ("eth_sendRawTransaction", ["0x02"], 1, ("server_error", "service_restarting"))]:
        server.seen.clear()
        server.answers[("POST", "/rpc")] = [restarting(), (200, "application/json",
                                                           js({"jsonrpc": "2.0", "id": 1, "result": "0x3e8"}))]
        assert outcome(lambda: rpc.int(method, *params)) == out, method
        assert [p for _, p, _, _ in server.seen] == ["/rpc"] * posts, method

    # trade(): the signed accept meets the restart once; the resend opens one trade on the same signature.
    first = []

    def accept(req):
        if "sig" in req["body"] and not first:
            first.append(req)
            return 503, restarting_body(), {"Retry-After": "1"}
        return venue.accept(req)

    session.routes[("POST", PATH)] = accept
    c = make_client(clock=clock)
    slept = []
    c._sleep = lambda s: (slept.append(s), clock.sleep(s))
    t = c.trade(c.quote("USD/MXN", "buy", 25_000, settles=WEEK))
    assert (t.status, t.tx, t.quote_id) == ("open", TXH, QID)
    signed = [b for b in accepts(session) if "sig" in b]
    assert len(signed) == 2 and signed[0] == signed[1] and slept[0] == 1
