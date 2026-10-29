"""The gateway and the RPC over a socket: requests to a local HTTP server on 127.0.0.1, no other host."""

import json
import socket
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from eth_account import Account
from eth_account.messages import encode_defunct

import crx
from crx._chain import Rpc, RpcError
from crx._http import rest_message

from .conftest import Pauses


class Server:
    """Answer each request from ``answers[(method, path)]`` = (status, content type, body bytes) and record it."""

    def __init__(self):
        self.answers, self.seen = {}, []  # seen: (method, path with query, headers in lower case, body)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def serve(self):
                n = int(self.headers.get("content-length") or 0)
                outer.seen.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()},
                                   self.rfile.read(n)))
                status, kind, body = outer.answers.get((self.command, self.path.split("?")[0]), (404, "", b""))
                self.send_response(status)
                if kind:
                    self.send_header("content-type", kind)
                if status in (301, 302, 307, 308):
                    self.send_header("location", "/elsewhere")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = serve

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.02,), daemon=True).start()  # 0.02 s: a short shutdown

    def json(self, method, path, body, status=200):
        self.answers[(method, path)] = (status, "application/json", json.dumps(body).encode())


@pytest.fixture
def server():
    s = Server()
    yield s
    s.httpd.shutdown()
    s.httpd.server_close()


@pytest.fixture
def http():
    s = requests.Session()
    s.trust_env = False  # no proxy from the environment
    yield s
    s.close()


def test_the_gateway_over_a_socket_signs_reads_and_follows_no_redirect(server, http, account, tmp_path):
    me = account.address.lower()
    server.json("GET", "/balance", {"account": me, "state": "live", "free": "12.500000"})
    server.answers[("GET", "/positions")] = (302, "", b"")
    server.answers[("GET", "/markets")] = (503, "text/html", b"<html><h1>503 Service Unavailable</h1></html>")
    c = crx.Client(key=account.key.hex(), base_url=server.url, rpc_url=server.url + "/rpc",
                   state_dir=tmp_path / "state", session=http)
    c._sleep = pauses = Pauses()
    assert c.balance().free == Decimal("12.500000")
    method, path, h, body = server.seen[0]
    assert (method, path, body, h["x-crx-address"]) == ("GET", "/balance?chain=avax-fuji", b"", me)
    assert h["x-crx-chain"] == "43113"
    msg = rest_message("GET", "/balance", me, me, int(h["x-crx-ts"]), b"", 43113)
    assert msg.split("\n")[2] == "Chain: 43113"
    assert Account.recover_message(encode_defunct(text=msg), signature=h["x-crx-sig"]).lower() == me
    with pytest.raises(crx.CrxError) as ei:
        c.positions()
    assert (ei.value.code, ei.value.status) == ("http_302", 302)
    with pytest.raises(crx.ServerError) as ei:
        c.markets()
    assert (ei.value.status, ei.value.details["body"]) == (503, "503 Service Unavailable")
    assert [p.split("?")[0] for _, p, _, _ in server.seen] == ["/balance", "/positions", "/markets", "/markets"]
    assert pauses == [0.5]  # one GET resend, no redirect followed


RPC_REFUSALS = [  # status, content type, body, error
    (200, "application/json", {"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted"}},
     RpcError),
    (502, "application/json", {"error": "bad gateway"}, crx.BadAnswer),  # a proxy's error is no node revert
    (200, "text/plain", "not json", crx.BadAnswer),
    (200, "application/json", ["not", "an", "object"], crx.BadAnswer),
    (200, "application/json", {"jsonrpc": "2.0", "id": 1}, crx.BadAnswer),
]


def test_the_rpc_over_a_socket_sends_json_rpc_and_types_each_failure(server, http):
    rpc = Rpc(server.url + "/rpc", http)
    server.json("POST", "/rpc", {"jsonrpc": "2.0", "id": 1, "result": "0xa869"})
    assert rpc.int("eth_chainId") == 43113
    call = json.loads(server.seen[0][3])
    assert (call["jsonrpc"], call["method"], call["params"], type(call["id"])) == ("2.0", "eth_chainId", [], int)
    for status, kind, body, err in RPC_REFUSALS:
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        server.answers[("POST", "/rpc")] = (status, kind, raw)
        with pytest.raises(err):
            rpc("eth_call", {"to": "0x" + "00" * 20}, "latest")
    with socket.socket() as s:  # a port that nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(crx.NetworkError) as ei:
        Rpc(f"http://127.0.0.1:{port}/secret-path-key", http)("eth_chainId")
    assert "secret-path-key" not in str(ei.value) and ei.value.__context__ is None
