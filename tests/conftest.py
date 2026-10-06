"""Fakes: a recorded gateway and a scripted chain. No network."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from eth_account import Account
from requests.structures import CaseInsensitiveDict

import crx

FIX = Path(__file__).parent / "fixtures"
BASE = "https://gateway.test"
RPC = "https://rpc.test/secret-path-key"
CHAIN_ID = 43113


def fixture(name: str):
    return json.loads((FIX / name).read_text())


class Resp:
    def __init__(self, status: int, body, headers=None, url: str = BASE):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.headers = CaseInsensitiveDict(headers or {})
        self.url = url

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return copy.deepcopy(self._body)


class FakeSession:
    """Routes gateway calls to ``routes[(METHOD, path)]`` and RPC calls to ``rpc[method]``.

    A route is a response body (200), a (status, body[, headers]) tuple, a list of either
    (served in order, the last one repeats), or a callable(req) -> one of those.
    """

    def __init__(self):
        self.routes: dict = {}
        self.rpc: dict = {}
        self.calls: list = []
        self.rpc_calls: list = []

    def _serve(self, spec, req):
        if isinstance(spec, list):
            item = spec.pop(0) if len(spec) > 1 else spec[0]
            return self._serve(item, req)
        if callable(spec):
            return self._serve(spec(req), req)
        if isinstance(spec, tuple):
            return Resp(*spec)
        return Resp(200, spec)

    def request(self, method, url, headers=None, data=None, timeout=None, allow_redirects=True):
        assert allow_redirects is False, "gateway calls must not follow redirects"
        u = urlsplit(url)
        assert url.startswith(BASE), "gateway call went to an unexpected host"
        req = {
            "method": method, "path": u.path, "query": parse_qs(u.query), "headers": headers or {},
            "raw": data or b"", "body": json.loads(data) if data else None, "timeout": timeout,
        }
        self.calls.append(req)
        key = (method, u.path)
        if key not in self.routes:
            return Resp(404, {"code": "not_found", "error": f"no route {method} {u.path}"}, url=url)
        r = self._serve(self.routes[key], req)
        r.url = url
        return r

    def post(self, url, json=None, timeout=None):
        assert url == RPC
        method, params = json["method"], json["params"]
        self.rpc_calls.append((method, params))
        if method not in self.rpc:
            return Resp(200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": f"no {method}"}})
        out = self.rpc[method]
        out = out(params) if callable(out) else out
        if isinstance(out, dict) and "__error__" in out:
            return Resp(200, {"jsonrpc": "2.0", "id": 1, "error": out["__error__"]})
        return Resp(200, {"jsonrpc": "2.0", "id": 1, "result": out})

    def paths(self, method=None):
        return [c["path"] for c in self.calls if method is None or c["method"] == method]

    def rpc_methods(self):
        return [m for m, _ in self.rpc_calls]


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@pytest.fixture(autouse=True)
def no_keepalive(request, monkeypatch):
    """No maker keepalive thread, unless the test is marked ``keepalive``: a thread's reads would
    land in the scripted call lists."""
    if request.node.get_closest_marker("keepalive") is None:
        monkeypatch.setattr(crx.Client, "_keep_live", lambda self: None)


@pytest.fixture(autouse=True)
def fresh_stamp():
    """Each test starts with no REST stamp: the next call is stamped at its clock's now."""
    crx._http._last_stamp = 0
    yield
    crx._http._last_stamp = 0


@pytest.fixture
def health():
    return fixture("health.json")


@pytest.fixture
def markets():
    return fixture("markets.json")


@pytest.fixture
def account():
    return Account.create()


@pytest.fixture
def session(health, markets):
    s = FakeSession()
    s.routes[("GET", "/health")] = health
    s.routes[("GET", "/markets")] = markets
    c = next(c for c in health["chains"] if c["key"] == "avax-fuji")
    s.rpc.update({
        "eth_chainId": hex(CHAIN_ID),
        "eth_getCode": lambda p: "0x6080" if p[0].lower() == c["core"] else "0x",
        "eth_blockNumber": hex(1000),
        "eth_gasPrice": hex(25 * 10**9),
        "eth_getBlockByNumber": {"number": hex(1000), "baseFeePerGas": hex(25 * 10**9)},
        "eth_maxPriorityFeePerGas": hex(10**9),
        "eth_getTransactionCount": hex(7),
    })
    return s


@pytest.fixture
def make_client(session, tmp_path, account):
    def make(key=True, clock=None, **kw):
        c = crx.Client(
            key=account.key.hex() if key else None, base_url=BASE, rpc_url=RPC,
            state_dir=tmp_path / "state", session=session, **kw,
        )
        if clock is not None:
            c._clock, c._sleep = clock, clock.sleep
        return c
    return make


def open_market(markets: dict, pair: str) -> dict:
    m = copy.deepcopy(markets)
    for row in m["markets"]:
        if row["pair"] == pair:
            row["session"]["open"] = True
    return m
