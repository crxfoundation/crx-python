"""Gateway transport: signed REST calls, or a session token in custodian mode."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests
from eth_utils import keccak

from .errors import (
    BadAnswer, ConfigError, CrxError, MarkUnavailable, NetworkError, ServerError, clean, from_gateway,
)
from .signer import as_signer, sign_login

log = logging.getLogger("crx")
SESSION_HEADER = "x-crx-session"
SESSION_MARGIN = 60.0  # seconds before expiry to mint a new token
GET_RETRY_PAUSE = 0.5  # seconds before the single GET retry
# Errors that retry a GET once. MarkUnavailable means the gateway has no market price yet.
GET_RETRIED = (NetworkError, ServerError, MarkUnavailable)
_TOKEN = re.compile(r"[0-9a-f]{64}")


def host_of(url: str) -> str:
    """Return the host only, so a secret in the path, query or userinfo never prints."""
    return clean(urlsplit(url).hostname or "?", 100)


# signer_line: the login message names the signer. no_signer_line: the gateway recovers it.
AUTH_SHAPE = "signer_line"
_STAMP_LOCK = threading.Lock()
_last_stamp = 0


def stamp(now_ms: int) -> int:
    """Return a Unix time in milliseconds above the last one in this process, so each signed call is unique."""
    global _last_stamp
    with _STAMP_LOCK:
        _last_stamp = max(int(now_ms), _last_stamp + 1)
        return _last_stamp


def rest_message(
    method: str, path: str, custody: str, signer: str, ts: int, raw: bytes, chain_id: int | None = None,
) -> str:
    """Build the CRX-REST-LOGIN message. ``Body`` is the keccak256 of the exact body bytes.
    With ``chain_id``, ``Chain: <id>`` follows ``Audience``."""
    lines = ["CRX-REST-LOGIN", "Audience: crx-gateway"]
    if chain_id is not None:
        lines.append(f"Chain: {int(chain_id)}")
    lines += [f"Method: {method}", f"Path: {path}", f"Custody: {custody.lower()}"]
    if AUTH_SHAPE == "signer_line":
        lines.append(f"Signer: {signer.lower()}")
    lines += [f"Timestamp: {ts}", f"Body: 0x{keccak(raw).hex()}"]
    return "\n".join(lines)


def takes_session(method: str, path: str) -> bool:
    """Return True for calls a session token covers: all but the mint and the viewer grants."""
    return path != "/session" and not (method in ("PUT", "DELETE") and path.startswith("/viewers/"))


class Gateway:
    """Gateway HTTP client. With ``login``, one signed ``POST /session`` mints a token for later calls.
    A gateway with session tokens off gets signed headers on every call."""

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
        self.custody: str | None = None  # account read as a viewer, None for the signer's own seat
        self.chain_id: int | None = None  # the signed Chain line and x-crx-chain; None on chain 1
        self._session = session
        self._timeout = timeout
        self.login = login
        self._clock = time.time
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_until = 0.0
        self._sessions_off = False
        self.sleep = time.sleep
        self.tape_read = 0.0  # time.monotonic() of the last 200 to GET /trades

    def __repr__(self) -> str:
        return f"Gateway({host_of(getattr(self, 'base_url', ''))})"

    def __getstate__(self) -> Any:
        raise TypeError("a Gateway holds a key and cannot be pickled or copied")

    @property
    def host(self) -> str:
        return host_of(self.base_url)

    def headers(self, method: str, path: str, raw: bytes = b"") -> dict[str, str]:
        """Return the signed auth headers, with ``x-crx-signer`` when the signer is not the custody and
        ``x-crx-chain`` when ``chain_id`` is set."""
        if self._signer is None:
            raise ConfigError("this call needs the seat key: set CRX_WALLET_PK or pass key=")
        signer = self._signer.address.lower()
        custody = self.custody or signer
        ts = stamp(int(self._clock() * 1000))
        msg = rest_message(method, path, custody, signer, ts, raw, self.chain_id)
        out = {"x-crx-address": custody, "x-crx-ts": str(ts)}
        if self.chain_id is not None:
            out["x-crx-chain"] = str(int(self.chain_id))
        if signer != custody:
            out["x-crx-signer"] = signer
        out["x-crx-sig"] = sign_login(self._signer, msg)
        return out

    def _uses_session(self, method: str, path: str) -> bool:
        return self.login and self.custody is None and not self._sessions_off and takes_session(method, path)

    def token(self) -> str | None:
        """Return the live session token, minting one when needed, or None when sessions are off."""
        with self._lock:
            if self._token is not None and self._clock() < self._token_until:
                return self._token
            self._token = None
            if self._sessions_off:
                return None
            try:
                r = self._send("POST", "/session", b"", None, None, self.headers("POST", "/session"))
                if r.status_code == 404:
                    self._sessions_off = True
                    log.warning("%s has session tokens off: every call signs its own login", self.host)
                    return None
                body = self.parse(r)
            except GET_RETRIED as e:
                e.login = True  # request() does not retry a failed POST /session
                raise
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
        """Forget ``token``: the next call mints another."""
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
            r = self._session.request(
                method, url, headers=headers, data=raw or None, timeout=timeout or self._timeout,
                allow_redirects=False)
        except requests.RequestException as e:
            failed = type(e).__name__
        else:
            if method == "GET" and path == "/trades" and r.status_code == 200:
                self.tape_read = time.monotonic()
            return r
        # Raised outside the except block: no context carries the URL.
        raise NetworkError(f"{self.host} did not answer ({failed})")

    def raw_request(
        self, method: str, path: str, *, body: Any = None, query: dict | None = None, auth: bool = True,
        timeout: float | None = None,
    ) -> requests.Response:
        """Send one call. A 401 on a session token mints a new token and resends once.

        The resend is safe: the gateway refuses a bad token before it reads the call.
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
        """Send one call and parse the answer. Retry a GET once on a ``GET_RETRIED`` error."""
        try:
            return self.parse(self.raw_request(method, path, body=body, query=query, auth=auth, timeout=timeout), ok)
        except GET_RETRIED as e:
            if method != "GET" or getattr(e, "login", False):
                raise
            log.info("GET %s: %s; sending it once more in %s s", clean(path, 100), e.code, GET_RETRY_PAUSE)
        self.sleep(GET_RETRY_PAUSE)
        return self.parse(self.raw_request(method, path, body=body, query=query, auth=auth, timeout=timeout), ok)

    @staticmethod
    def body_of(r: requests.Response) -> Any:
        try:
            return r.json()
        except ValueError:
            return None

    @staticmethod
    def error_of(r: requests.Response, host: str = "") -> CrxError:
        """Return the typed error of a refused answer."""
        body = Gateway.body_of(r)
        text = "" if isinstance(body, dict) else (r.text or "")[:8192]
        headers = getattr(r, "headers", None) or {}
        return from_gateway(
            r.status_code, body, text, host=host or host_of(getattr(r, "url", "") or ""),
            retry_after=headers.get("Retry-After"))

    def parse(self, r: requests.Response, ok: tuple[int, ...] = (200,)) -> dict:
        """Return the JSON object of an ``ok`` answer, or ``{}`` for a 204. Raise on any other status."""
        body = self.body_of(r)
        if r.status_code not in ok:
            raise self.error_of(r, self.host)
        if r.status_code == 204:
            return {}
        if not isinstance(body, dict):
            raise BadAnswer(f"{self.host} sent an answer that is not a JSON object", status=r.status_code)
        return body
