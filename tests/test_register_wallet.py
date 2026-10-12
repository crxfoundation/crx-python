"""register_wallet against a stub Discover server and a stub RPC on 127.0.0.1. Real HTTP, no outside network."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

import crx
from crx import _register

# The public test key: never fund it.
KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ADDR = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
TID = "0123456789ab"
TOKEN = f"crx_ft_{TID}_" + "cd" * 32
NOW = 1790000000.125
TS = 1790000000125
MESSAGE = f"CRX-WALLET-REGISTER\nToken: {TID}\nAddress: {ADDR}\nChain: 1\nTimestamp: {TS}"
# The signature Discover's own proof check passes (register-proof.ts, agentreg-dev), and viem recovers.
SIG = ("0x1fe4fdf6b20422b57aeaa9ce5a054933fac951af4065c85dcd8adf7baad862d5"
       "45fe29ec31e27d15527f94f09e0cb89a39b1cb790caed161df7a361916ae42441c")
PATH = "/api/v1/onboarding/wallet"


class Stub:
    """One server: ``/rpc`` is a JSON-RPC node, ``PATH`` is Discover. Every request is recorded."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.rpc = {"eth_chainId": "0x1", "eth_getCode": "0x"}
        self.answer: tuple = (201, {"address": ADDR, "status": "pending"}, {})
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                headers = {k.lower(): v for k, v in self.headers.items()}
                stub.requests.append({"path": self.path, "headers": headers, "raw": raw})
                if self.path == "/rpc":
                    call = json.loads(raw)
                    out = stub.rpc.get(call["method"])
                    reply = out if isinstance(out, dict) and "error" in out else {"result": out}
                    return self._send(200, {"jsonrpc": "2.0", "id": call["id"], **reply}, {})
                if self.path == PATH:
                    return self._send(*stub.answer)
                return self._send(404, {"error": "not found"}, {})

            def _send(self, status, body, headers):
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("content-type", "application/json" if not isinstance(body, str) else "text/html")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def posts(self) -> list[dict]:
        return [r for r in self.requests if r["path"] == PATH]

    def rpc_calls(self) -> list[dict]:
        return [r for r in self.requests if r["path"] == "/rpc"]


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.server.shutdown()
    s.server.server_close()


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    for k in ("CRX_ALLOW_MAINNET", "CRX_BASE", "CRX_RPC", "CRX_DISCOVER", "CRX_FIRM_TOKEN",
              "CRX_WALLET_PK", "CRX_WALLET_PK_FILE"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def client(stub, tmp_path):
    def make(**kw):
        args = dict(key=KEY, network="mainnet", allow_mainnet=True, base_url=stub.url, rpc_url=stub.url + "/rpc",
                    state_dir=tmp_path / "state")
        args.update(kw)
        c = crx.Client(**args)
        c._clock = lambda: NOW
        return c
    return make


def register(c, stub, **kw):
    return c.register_wallet(kw.pop("token", TOKEN), discover_url=stub.url, **kw)


# ---------- the signed bytes ----------

def test_signature_bytes_vector():
    assert _register.message(TID, ADDR.upper().replace("0X", "0x"), TS) == MESSAGE
    signer = crx.signer.as_signer(Account.from_key(KEY))
    assert _register.sign(signer, MESSAGE, ADDR) == SIG
    assert Account.sign_message(encode_defunct(text=MESSAGE), KEY).signature.hex().removeprefix("0x") == SIG[2:]
    assert Account.recover_message(encode_defunct(text=MESSAGE), signature=SIG).lower() == ADDR


def test_201_posts_once_the_documented_body(client, stub):
    reg = register(client(), stub)
    assert (reg.address, reg.status, reg.repeat) == (ADDR, "pending", False)
    [post] = stub.posts()
    assert post["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert post["headers"]["content-type"] == "application/json"
    assert json.loads(post["raw"]) == {"address": ADDR, "timestamp": TS, "signature": SIG, "chain": "ethereum"}


def test_200_is_the_repeat(client, stub):
    stub.answer = (200, {"address": ADDR, "status": "approved"}, {})
    reg = register(client(), stub)
    assert (reg.status, reg.repeat) == ("approved", True)


@pytest.mark.parametrize("answer", [
    (201, "<html>ok</html>", {}),
    (201, {"address": ADDR}, {}),
    (201, {"status": "pending"}, {}),
    (201, {"address": "0x" + "11" * 20, "status": "pending"}, {}),
])
def test_unreadable_success_raises(client, stub, answer):
    stub.answer = answer
    with pytest.raises(crx.BadAnswer):
        register(client(), stub)
    assert len(stub.posts()) == 1


def test_address_only_proof_signs_nothing(client, stub, monkeypatch):
    monkeypatch.setattr(_register, "PROOF", "address-only")
    s = Recording()
    register(client(key=None, signer=s), stub)
    assert s.signed == []
    assert json.loads(stub.posts()[0]["raw"]) == {"address": ADDR, "chain": "ethereum"}


def test_the_key_never_leaves_and_the_token_goes_to_discover_only(client, stub):
    register(client(), stub)
    key_hex = KEY[2:].lower()
    for r in stub.requests:
        seen = (r["raw"].decode() + json.dumps(r["headers"])).lower()
        assert key_hex not in seen
        if r["path"] != PATH:
            assert TOKEN not in seen and "authorization" not in seen
    assert [r["path"] for r in stub.requests] == ["/rpc", "/rpc", PATH]


def test_token_from_env(client, stub, monkeypatch):
    monkeypatch.setenv("CRX_FIRM_TOKEN", TOKEN)
    client().register_wallet(discover_url=stub.url)
    assert stub.posts()[0]["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_discover_url_from_env(client, stub, monkeypatch):
    monkeypatch.setenv("CRX_DISCOVER", stub.url)
    client().register_wallet(TOKEN)
    assert len(stub.posts()) == 1


# ---------- one test per response code ----------

@pytest.mark.parametrize("status,code,cls,sdk_code", [
    (400, "bad_request", crx.BadRequest, "bad_request"),
    (400, "invalid_signature", crx.InvalidSignature, "invalid_signature"),
    (401, "unauthorized", crx.AuthError, "unauthorized"),
    (403, "seat_cannot_sign", crx.SeatCannotSign, "seat_cannot_sign"),
    (409, "one_wallet", crx.OneWallet, "one_wallet"),
    (409, "mainnet_wallet_held", crx.WalletHeld, "mainnet_wallet_held"),
    (409, "mainnet_wallet_same", crx.WalletSame, "mainnet_wallet_same"),
    (409, "mainnet_wallet_signing", crx.WalletSigning, "mainnet_wallet_signing"),
    (413, "payload_too_large", crx.BadRequest, "bad_request"),
    (429, "rate_limited", crx.RateLimited, "rate_limited"),
    (500, "internal", crx.ServerError, "server_error"),
    (502, "upstream", crx.ServerError, "server_error"),
    (503, "seat_unread", crx.ServerError, "server_error"),
])
def test_refusal_codes(client, stub, status, code, cls, sdk_code):
    stub.answer = (status, {"error": "The served sentence.", "code": code}, {"Retry-After": "7"})
    with pytest.raises(cls) as ei:
        register(client(), stub)
    e = ei.value
    assert (e.code, e.status, e.gateway_code, str(e)) == (sdk_code, status, code, "The served sentence.")
    assert len(stub.posts()) == 1
    assert TOKEN not in str(e) + repr(e) and TOKEN[-64:] not in str(e.details)
    if cls is crx.RateLimited:
        assert e.retry_after == 7


def test_code_in_error_field(client, stub):
    stub.answer = (409, {"error": "one_wallet"}, {})
    with pytest.raises(crx.OneWallet) as ei:
        register(client(), stub)
    assert str(ei.value) and str(ei.value) != "one_wallet"


def test_error_field_is_read_only_with_no_code():
    e = crx.errors.from_register(409, {"code": "conflict", "error": "one_wallet"})
    assert type(e) is crx.CrxError and e.code == "conflict"


def test_invalid_signature_is_not_the_gateway_login_code():
    assert type(crx.errors.from_gateway(400, {"code": "invalid_signature"})) is crx.AuthError
    assert type(crx.errors.from_register(400, {"code": "invalid_signature"})) is crx.InvalidSignature


def test_5xx_html_posts_once(client, stub):
    stub.answer = (502, "<html>bad gateway</html>", {})
    with pytest.raises(crx.ServerError):
        register(client(), stub)
    assert len(stub.posts()) == 1


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_not_followed(client, stub, status):
    stub.answer = (status, {}, {"Location": "/rpc"})
    with pytest.raises(crx.CrxError) as ei:
        register(client(), stub)
    assert ei.value.code == f"http_{status}"
    assert [r["path"] for r in stub.requests] == ["/rpc", "/rpc", PATH]


# ---------- local refusals: nothing sent ----------

@pytest.mark.parametrize("code", ["0x6080604052", "0xEF0100" + "ab" * 20, "0xef0100" + "ab" * 20, "0x00"])
def test_code_on_chain_refused(client, stub, code):
    stub.rpc["eth_getCode"] = code
    s = Recording()
    with pytest.raises(crx.SeatCannotSign) as ei:
        register(client(key=None, signer=s), stub)
    assert ei.value.details["delegation"] is code.lower().startswith("0xef0100")
    assert stub.posts() == [] and s.signed == []


@pytest.mark.parametrize("rpc,err", [
    ({"eth_chainId": "0xaa36a7"}, crx.ConfigError),
    ({"eth_chainId": "0xa869"}, crx.ConfigError),
    ({"eth_getCode": {"error": {"code": -32000, "message": "boom"}}}, crx.BadAnswer),
    ({"eth_getCode": None}, crx.BadAnswer),
    ({"eth_getCode": ""}, crx.BadAnswer),
])
def test_rpc_must_read_no_code_on_chain_1(client, stub, rpc, err):
    stub.rpc.update(rpc)
    with pytest.raises(err):
        register(client(), stub)
    assert stub.posts() == []


def test_rpc_down_refuses(client, stub):
    c = client(rpc_url="http://127.0.0.1:9/rpc")
    with pytest.raises(crx.NetworkError):
        register(c, stub)
    assert stub.requests == []


def test_testnet_refused(client, stub):
    with pytest.raises(crx.ConfigError):
        register(client(network="testnet", allow_mainnet=False), stub)
    assert stub.requests == []


def test_viewer_refused(client, stub):
    with pytest.raises(crx.ConfigError):
        register(client(account="0x5b38da6a701c568545dcfcb03fcb875f56beddc4"), stub)
    assert stub.requests == []


@pytest.mark.parametrize("token", [
    None, "", "has space", KEY, KEY[2:], TOKEN.upper(), TOKEN + "\nAddress: 0x00", TOKEN[:-1],
    f"crx_ft_{TID}0_" + "cd" * 32, "Bearer " + TOKEN, TOKEN.replace("crx_ft_", "crx_fx_"),
])
def test_bad_token_refused(client, stub, token):
    with pytest.raises(crx.ConfigError) as ei:
        client().register_wallet(token, discover_url=stub.url)
    assert KEY[2:] not in str(ei.value).lower() and "cd" * 32 not in str(ei.value)
    assert stub.requests == []


def test_discover_url_must_be_https(client, stub):
    with pytest.raises(crx.ConfigError):
        client().register_wallet(TOKEN, discover_url="http://discover.example.com")
    assert stub.requests == []


class OtherSigner:
    """A signer that names the seat but signs with another key."""

    def __init__(self):
        self.address = ADDR
        self._other = Account.create()

    def sign_typed_data(self, td):
        raise AssertionError("not used")

    def sign_message(self, data):
        return self._other.sign_message(encode_defunct(primitive=data)).signature


class Recording:
    """A custodian signer: records what it is asked to sign."""

    def __init__(self):
        self._acct = Account.from_key(KEY)
        self.address = self._acct.address
        self.signed: list[bytes] = []

    def sign_typed_data(self, td):
        raise AssertionError("not used")

    def sign_message(self, data):
        self.signed.append(bytes(data))
        return self._acct.sign_message(encode_defunct(primitive=data)).signature


def test_signature_must_recover_to_the_address(client, stub):
    with pytest.raises(crx.RefusedToSign):
        register(client(key=None, signer=OtherSigner()), stub)
    assert stub.posts() == []


def test_custodian_signs_the_documented_text_only(client, stub):
    s = Recording()
    register(client(key=None, signer=s), stub)
    assert s.signed == [MESSAGE.encode()]
    assert json.loads(stub.posts()[0]["raw"])["signature"] == SIG
