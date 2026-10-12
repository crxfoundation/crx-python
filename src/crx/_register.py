"""Wallet registration: the firm's wallet asks CRX for a seat with a firm token. No browser.

The wallet signs one fixed text (EIP-191) and the SDK posts it once to Discover with the firm token as a
Bearer header. The private key never leaves the client. The token goes to the Discover host only.
"""

from __future__ import annotations

import json
import re
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct

from . import _eip712 as e7
from ._chain import Rpc, RpcError
from ._http import Gateway
from .errors import BadAnswer, ConfigError, RefusedToSign, SeatCannotSign, clean, from_register
from .models import Registration

DISCOVER_URL = "https://discover.crxfx.com"
ENV_DISCOVER = "CRX_DISCOVER"
ENV_TOKEN = "CRX_FIRM_TOKEN"
PATH = "/api/v1/onboarding/wallet"
TIMEOUT = 30.0  # s; the route reads the chain and the firm's wallets before it answers
HEADER = "CRX-WALLET-REGISTER"
CHAIN_ID = 1
# The proof the body carries: "wallet-signature" (the wallet signs the text below) or "address-only"
# (the body names the address alone; nothing is signed).
PROOF = "wallet-signature"
DELEGATION = "0xef0100"  # EIP-7702: the code of an account that delegates to a contract
TOKEN_RE = re.compile(r"crx_ft_([0-9a-f]{12})_[0-9a-f]{64}")


def token_id(value: Any) -> tuple[str, str]:
    """(the firm token, its 12-hex id). Anything but ``crx_ft_<12 hex>_<64 hex>`` is refused, unread."""
    s = value.strip() if isinstance(value, str) else ""
    m = TOKEN_RE.fullmatch(s)
    if m is None:
        raise ConfigError(f"the firm token is missing or malformed: pass token= or set {ENV_TOKEN}")
    return s, m.group(1)


def message(tid: str, address: str, ts_ms: int) -> str:
    """The text the wallet signs: five lines joined by ``\\n``, no trailing newline. ``address`` lower case."""
    return "\n".join([HEADER, f"Token: {tid}", f"Address: {address.lower()}", f"Chain: {CHAIN_ID}",
                      f"Timestamp: {ts_ms}"])


def check_eoa(rpc: Rpc, address: str) -> None:
    """Refuse unless ``address`` holds no code on chain 1. An EIP-7702 delegation is code. Any read that
    fails refuses: nothing is sent."""
    try:
        on = rpc.int("eth_chainId")
        code = rpc("eth_getCode", address, "latest")
    except RpcError:
        raise BadAnswer("the RPC refused the code read; nothing sent") from None
    if on != CHAIN_ID:
        raise ConfigError(f"the RPC is on chain {on}, not chain {CHAIN_ID}; nothing sent")
    if not isinstance(code, str) or not code.startswith("0x"):
        raise BadAnswer("the RPC sent code this SDK cannot read; nothing sent")
    if code.lower().startswith(DELEGATION):
        raise SeatCannotSign("this address delegates to a contract (EIP-7702); a seat is a plain EOA or MPC "
                             "address; nothing sent", details={"address": address, "delegation": True})
    if code != "0x":
        raise SeatCannotSign("this address is a smart-contract wallet; a seat is a plain EOA or MPC address; "
                             "nothing sent", details={"address": address, "delegation": False})


def sign(signer: Any, text: str, address: str) -> str:
    """The EIP-191 signature of ``text``: 0x hex, low ``s``, ``v`` 27 or 28. Refused unless it recovers
    to ``address``."""
    data = text.encode()
    try:
        sig = e7.normalize_sig(signer.sign_message(data))
        who = Account.recover_message(encode_defunct(primitive=data), signature=sig).lower()
    except Exception as e:  # noqa: BLE001 - the wallet's failure: nothing was sent
        why = type(e).__name__
    else:
        why = None
    if why is not None:
        raise RefusedToSign(f"the wallet did not sign the registration ({why}); nothing sent")
    if who != address.lower():
        raise RefusedToSign("the signature does not recover to this address; nothing sent")
    return e7.h0x(sig)


def body(signer: Any, tid: str, address: str, ts_ms: int) -> dict:
    """The request body. ``address`` lower case. With ``PROOF`` "wallet-signature" the wallet signs."""
    if PROOF == "address-only":
        return {"address": address, "chain": "ethereum"}
    sig = sign(signer, message(tid, address, ts_ms), address)
    return {"address": address, "timestamp": ts_ms, "signature": sig, "chain": "ethereum"}


def post(gw: Gateway, token: str, payload: dict, address: str) -> Registration:
    """POST once. 201: CRX holds the request. 200: the repeat of a saved request (same token, same address).
    Any other answer raises its typed error."""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    r = gw._send("POST", PATH, raw, None, TIMEOUT, {"authorization": f"Bearer {token}"})
    answer = Gateway.body_of(r)
    if r.status_code not in (200, 201):
        text = "" if isinstance(answer, dict) else (r.text or "")[:8192]
        raise from_register(r.status_code, answer, text, host=gw.host,
                            retry_after=(getattr(r, "headers", None) or {}).get("Retry-After"))
    st = answer.get("status") if isinstance(answer, dict) else None
    served = answer.get("address") if isinstance(answer, dict) else None
    if not isinstance(st, str) or not st or not isinstance(served, str) or served.lower() != address:
        raise BadAnswer(f"{gw.host} sent an answer this SDK cannot read; register again with the same token to "
                        "read it", status=r.status_code)
    return Registration(address=address, status=clean(st, 40), repeat=r.status_code == 200, raw=answer)
