"""Gateway transport: signed REST calls (CRX-REST-LOGIN), or a session token in custodian mode."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests
from eth_utils import keccak

from .errors import BadAnswer, ConfigError, CrxError, NetworkError, clean, from_gateway
from .signer import as_signer, sign_login

log = logging.getLogger("crx")
SESSION_HEADER = "x-crx-session"
SESSION_MARGIN = 60.0  # s: a token is minted again this long before its life ends
_TOKEN = re.compile(r"[0-9a-f]{64}")


def host_of(url: str) -> str:
    """The host alone: a secret in the path, query or userinfo never prints."""
    return clean(urlsplit(url).hostname or "?", 100)


def rest_message(method: str, path: str, custody: str, signer: str, ts: int, nonce: str, raw: bytes) -> str:
    return "\n".join(
        [
            "CRX-REST-LOGIN", "Audience: crx-gateway", f"Method: {method}", f"Path: {path}",
            f"Custody: {custody}", f"Signer: {signer}", f"Timestamp: {ts}", f"Nonce: {nonce}",
            f"Body: 0x{keccak(raw).hex()}",
        ]
    )


def takes_session(method: str, path: str) -> bool:
    """True for the calls a session token stands in for: all but the mint and the viewer grants."""
    return path != "/session" and not (method in ("PUT", "DELETE") and path.startswith("/viewers/"))


class Gateway:
    """``login``: custodian mode. One ``POST /session`` (one EIP-191 sign) yields a token for the
    calls that take one, until the token's life ends or the gateway refuses it (restart, revoke).
    A gateway with session tokens off gets the five signed headers on every call."""

    def __init__(
        self, base_url: str, account: Any, session: requests.Session, timeout: float = 10, login: bool = False,
    ) -> None:
        u = urlsplit(base_url)
        local = u.hostname in ("localhost", "127.0.0.1", "::1")
        if u.scheme != "https" and not (u.scheme == "http" and local):
            raise ConfigError("the gateway URL must be https (http only for localhost)")
        if u.username or u.password:
            raise ConfigError("the gateway URL must not carry credentials")
        self.base_url = base_url.rstrip("/")
        self._signer = as_signer(account)
        self.custody: str | None = None  # the account read as a viewer; None = the signer's own seat
        self._session = session
        self._timeout = timeout
        self.login = login
        self._clock = time.time
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_until = 0.0
        self._sessions_off = False

    def __repr__(self) -> str:
        return f"Gateway({host_of(getattr(self, 'base_url', ''))})"

    def __getstate__(self) -> Any:
        raise TypeError("a Gateway holds a key and cannot be pickled or copied")

    @property
    def host(self) -> str:
        return host_of(self.base_url)

    def headers(self, method: str, path: str, raw: bytes = b"") -> dict[str, str]:
        """Signed headers. The last line of the message is keccak256 of the exact body bytes sent.

        Custody is ``custody`` when set, else the signer.
        """
        if self._signer is None:
            raise ConfigError("this call needs the seat key: set CRX_WALLET_PK or pass key=")
        signer = self._signer.address.lower()
        custody = self.custody or signer
        ts, nonce = int(time.time() * 1000), uuid.uuid4().hex
        msg = rest_message(method, path, custody, signer, ts, nonce, raw)
        return {
            "x-crx-address": custody, "x-crx-signer": signer, "x-crx-ts": str(ts),
            "x-crx-nonce": nonce, "x-crx-sig": sign_login(self._signer, msg),
        }

    def _uses_session(self, method: str, path: str) -> bool:
        return self.login and self.custody is None and not self._sessions_off and takes_session(method, path)

    def token(self) -> str | None:
        """The live session token, minted when there is none or its life is over. None when the gateway
        has session tokens off."""
        with self._lock:
            if self._token is not None and self._clock() < self._token_until:
                return self._token
            self._token = None
            if self._sessions_off:
                return None
            r = self._send("POST", "/session", b"", None, None, self.headers("POST", "/session"))
            if r.status_code == 404:
                self._sessions_off = True
                log.warning("%s has session tokens off: every call signs its own login", self.host)
                return None
            body = self.parse(r)
            me = self._signer.address.lower()
            tok, ttl = body.get("token"), body.get("ttl_ms")
            if (not isinstance(tok, str) or not _TOKEN.fullmatch(tok) or str(body.get("custody")).lower() != me
                    or str(body.get("signer")).lower() != me or isinstance(ttl, bool) or not isinstance(ttl, int)
                    or ttl <= 0):
                raise BadAnswer("POST /session sent a token this SDK cannot use")
            life = ttl / 1000
            self._token, self._token_until = tok, self._clock() + max(life - SESSION_MARGIN, life / 2)
            return tok

    def drop_token(self, token: str) -> None:
        """Forget ``token``: the next call mints a new one."""
        with self._lock:
            if self._token == token:
                self._token = None

    def _send(
        self, method: str, path: str, raw: bytes, query: dict | None, timeout: float | None, auth: dict,
    ) -> requests.Response:
        headers = {"accept": "application/json", **auth}
        if raw:
            headers["content-type"] = "application/json"
        url = self.base_url + path + ("?" + urlencode(query) if query else "")
        failed = None
        try:
            # No redirects: signed headers and tokens go to this host only.
            return self._session.request(
                method, url, headers=headers, data=raw or None, timeout=timeout or self._timeout,
                allow_redirects=False)
        except requests.RequestException as e:
            failed = type(e).__name__
        # Raised outside the except block: no context carries the URL.
        raise NetworkError(f"{self.host} did not answer ({failed})")

    def raw_request(
        self, method: str, path: str, *, body: Any = None, query: dict | None = None, auth: bool = True,
        timeout: float | None = None,
    ) -> requests.Response:
        """``timeout`` (s) for this call only; None: the gateway's own.

        In custodian mode a call that takes a session token sends it. A 401 on a token mints a new
        one and sends the call once more: the gateway refused it before it read the call.
        """
        raw = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        if auth and self._uses_session(method, path):
            for _ in range(2):
                tok = self.token()
                if tok is None:
                    break
                r = self._send(method, path, raw, query, timeout, {SESSION_HEADER: tok})
                if r.status_code != 401:
                    return r
                self.drop_token(tok)
            else:
                return r
        # The signed path carries no query string.
        return self._send(method, path, raw, query, timeout, self.headers(method, path, raw) if auth else {})

    def request(
        self, method: str, path: str, *, body: Any = None, query: dict | None = None, auth: bool = True,
        ok: tuple[int, ...] = (200,), timeout: float | None = None,
    ) -> dict:
        return self.parse(self.raw_request(method, path, body=body, query=query, auth=auth, timeout=timeout), ok)

    @staticmethod
    def body_of(r: requests.Response) -> Any:
        try:
            return r.json()
        except ValueError:
            return None

    @staticmethod
    def error_of(r: requests.Response, host: str = "") -> CrxError:
        """The typed error for a refused answer. ``host`` defaults to the host of the answer's URL."""
        body = Gateway.body_of(r)
        text = "" if isinstance(body, dict) else (r.text or "")[:8192]
        headers = getattr(r, "headers", None) or {}
        return from_gateway(
            r.status_code, body, text, host=host or host_of(getattr(r, "url", "") or ""),
            retry_after=headers.get("Retry-After"))

    def parse(self, r: requests.Response, ok: tuple[int, ...] = (200,)) -> dict:
        """The JSON object of an ``ok`` answer; ``{}`` for a 204. Any other status raises."""
        body = self.body_of(r)
        if r.status_code not in ok:
            raise self.error_of(r, self.host)
        if r.status_code == 204:
            return {}
        if not isinstance(body, dict):
            raise BadAnswer(f"{self.host} sent an answer that is not a JSON object", status=r.status_code)
        return body
