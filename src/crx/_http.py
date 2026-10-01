"""Gateway transport: signed REST calls (CRX-REST-LOGIN)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests
from eth_account.messages import encode_defunct
from eth_utils import keccak

from .errors import BadAnswer, ConfigError, CrxError, NetworkError, clean, from_gateway


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


class Gateway:
    def __init__(self, base_url: str, account: Any, session: requests.Session, timeout: float = 10) -> None:
        u = urlsplit(base_url)
        local = u.hostname in ("localhost", "127.0.0.1", "::1")
        if u.scheme != "https" and not (u.scheme == "http" and local):
            raise ConfigError("the gateway URL must be https (http only for localhost)")
        if u.username or u.password:
            raise ConfigError("the gateway URL must not carry credentials")
        self.base_url = base_url.rstrip("/")
        self._account = account
        self.custody: str | None = None  # the account read as a viewer; None = the signer's own seat
        self._session = session
        self._timeout = timeout

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
        if self._account is None:
            raise ConfigError("this call needs the seat key: set CRX_WALLET_PK or pass key=")
        signer = self._account.address.lower()
        custody = self.custody or signer
        ts, nonce = int(time.time() * 1000), uuid.uuid4().hex
        msg = rest_message(method, path, custody, signer, ts, nonce, raw)
        sig = self._account.sign_message(encode_defunct(text=msg)).signature
        return {
            "x-crx-address": custody, "x-crx-signer": signer, "x-crx-ts": str(ts),
            "x-crx-nonce": nonce, "x-crx-sig": "0x" + bytes(sig).hex(),
        }

    def raw_request(
        self, method: str, path: str, *, body: Any = None, query: dict | None = None, auth: bool = True
    ) -> requests.Response:
        raw = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"accept": "application/json"}
        if body is not None:
            headers["content-type"] = "application/json"
        if auth:
            headers.update(self.headers(method, path, raw))  # the signed path carries no query string
        url = self.base_url + path + ("?" + urlencode(query) if query else "")
        failed = None
        try:
            # No redirects: signed headers go to this host only.
            return self._session.request(
                method, url, headers=headers, data=raw or None, timeout=self._timeout, allow_redirects=False)
        except requests.RequestException as e:
            failed = type(e).__name__
        # Raised outside the except block: no context carries the URL.
        raise NetworkError(f"{self.host} did not answer ({failed})")

    def request(
        self, method: str, path: str, *, body: Any = None, query: dict | None = None, auth: bool = True,
        ok: tuple[int, ...] = (200,),
    ) -> dict:
        return self.parse(self.raw_request(method, path, body=body, query=query, auth=auth), ok)

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
