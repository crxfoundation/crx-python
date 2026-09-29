"""The taker's bind: accept a quote, sign the Side, post it. CRX sends the tx and pays gas.

Every value the gateway serves is rebuilt locally before a signature exists.
A mismatch raises RefusedToSign and nothing is signed.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from . import _eip712 as e7
from ._http import Gateway
from .errors import (
    BadAnswer, CrxError, NetworkError, OwnRoundOpen, QuoteExpired, RefusedToSign,
    TradeUnknown, clean, from_gateway,
)

SIDE_WINDOW = 630  # s: the core takes a Side quote_expiry at most 600 s past its block time, plus 30 s of clock slack
NEW_QUOTE = "not opened; request a new quote"


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S UTC")


def read_later(qe: int) -> str:
    """The core takes no trade at or after the Side quote_expiry; positions() shows a landed trade within a minute."""
    return f"do not trade again; read positions() after {utc(qe + 120)}"


def status_code(r: Any) -> tuple[int, str]:
    if r is None:
        return (0, "")
    body = Gateway.body_of(r)
    code = clean(body.get("code") or "", 64) if isinstance(body, dict) else ""
    return (r.status_code, code)


def obj(r: Any) -> dict:
    body = Gateway.body_of(r) if r is not None else None
    return body if isinstance(body, dict) else {}


def refused(r: Any) -> CrxError:
    """A bind refusal: a closed round asks for a new quote."""
    k = status_code(r)
    if k[0] == 410 or k in ((409, "rejected"), (409, "round_closed"), (409, "conflict")):
        return QuoteExpired(NEW_QUOTE, status=k[0], gateway_code=k[1] or None)
    return Gateway.error_of(r)


class Binder:
    def __getstate__(self) -> Any:
        raise TypeError("a Binder holds a key and cannot be pickled or copied")

    def __init__(
        self,
        gateway: Gateway,
        account: Any,
        chain: dict,
        separator: bytes,
        state_dir: Path,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.gw = gateway
        self.account = account
        self.seat = account.address.lower()
        self.chain = chain
        self.sep = separator
        self.state_path = Path(state_dir).expanduser() / f"side-nonce-{self.seat}"
        self.sleep = sleep
        self.now = clock

    # ---------- the taker's own half ----------

    def leg(self, quote: dict, leg_id: str, nonce: int, quote_expiry: int, im_bps: int) -> dict:
        """The joined fields ride on the quote row; leg_id, nonce and quote_expiry are this seat's own."""
        return {
            "seat": self.seat, "leg_id": leg_id, "join_ref": quote["join_ref"],
            "pair_id": quote["pair_id"], "instrument_id": quote.get("instrument_id", 1),
            "side": quote["side"], "notional": quote["notional"], "rate": quote["rate"],
            "im_bps": im_bps, "premium_bps": quote["premium_bps"], "expiry": quote["expiry"],
            "nonce": str(nonce), "quote_expiry": quote_expiry,
        }

    @staticmethod
    def check_terms(a: dict, want: dict) -> None:
        """Sign only the terms this request asked for."""
        try:
            got = {
                "pair_id": e7.hx(a["pair_id"]), "instrument_id": a["instrument_id"], "side": a["side"],
                "notional": Decimal(str(a["notional"])), "expiry": int(a["expiry"]) // 1000,
                "im_bps": a["im_bps"], "premium_bps": a["premium_bps"],
            }
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise RefusedToSign("refused to sign: the quote's terms cannot be read") from None
        bad = [k for k in want if got[k] != want[k]]
        if bad:
            raise RefusedToSign(f"refused to sign: the quote's {', '.join(bad)} is not this request's")

    def sign_leg(self, a: dict) -> str:
        try:
            digest = e7.leg_digest(self.sep, a)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise RefusedToSign("refused to sign: the quote's leg cannot be encoded") from None
        return "0x" + bytes(self.account.unsafe_sign_hash(digest).signature).hex()

    # ---------- the Side round ----------

    def last_signed(self) -> int:
        """The last own_nonce this seat signed from this machine; 0 before the first."""
        try:
            return int(self.state_path.read_text())
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            raise RefusedToSign("refused to sign: the Side nonce file is unreadable") from None

    def keep_signed(self, nonce: int) -> None:
        """Written before the signature exists: a temporary file, an fsync, a rename."""
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(tmp, "w") as f:
                f.write(str(nonce))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        except OSError:
            raise RefusedToSign("refused to sign: the Side nonce file is not writable") from None

    def check_side(self, t: dict, arm: dict) -> bytes:
        """Rebuild this seat's half with the gateway's nonce and the Side quote expiry."""
        try:
            if t["own_leg_id"].lower() != arm["leg_id"].lower():
                raise RefusedToSign("refused to sign: own_leg_id is not this leg")
            c_taker = e7.half_commitment(e7.arm_words(arm, t["own_nonce"], t["quote_expiry"]), t["own_salt"])
            if e7.h0x(c_taker) != t["c_taker"].lower():
                raise RefusedToSign("refused to sign: c_taker is not this leg")
            if e7.h0x(e7.pair_commitment(c_taker, e7.hx(t["c_maker"]))) != t["pair_c"].lower():
                raise RefusedToSign("refused to sign: pair_c is not keccak(0x03, c_taker, c_maker)")
            own_nonce, qe = int(t["own_nonce"]), int(t["quote_expiry"])
            now = self.now()
            if own_nonce > now * 1000 + 86_400_000:
                raise RefusedToSign("refused to sign: own_nonce is more than one day ahead")
            if own_nonce <= self.last_signed():
                raise RefusedToSign("refused to sign: own_nonce is not above the last one this seat signed")
            if qe <= now:
                raise QuoteExpired(f"the Side window has passed; {NEW_QUOTE}")
            if qe > now + SIDE_WINDOW:
                raise RefusedToSign(f"refused to sign: the Side quote_expiry is more than {SIDE_WINDOW} s ahead")
            digest = e7.side_digest(self.sep, t)
            if t.get("digest") and e7.h0x(digest) != str(t["digest"]).lower():
                raise RefusedToSign("refused to sign: the served digest is not this Side")
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            raise RefusedToSign("refused to sign: the Side template cannot be read") from None
        self.keep_signed(own_nonce)
        return digest

    def bind(self, rfq_id: str, arm: dict, t: dict, maker_by: float, log: Callable[[str], None]) -> None:
        """Sign this seat's Side and post it. CRX sends the trade once both Sides are in.
        Raises QuoteExpired (no bind) or TradeUnknown (maybe bound). Before the Side
        signature exists, a refusal is RefusedToSign. After it, the trade can still
        open, so any other failure is TradeUnknown."""
        sig = "0x" + bytes(self.account.unsafe_sign_hash(self.check_side(t, arm)).signature).hex()
        why = None
        try:
            try:
                r = self.gw.raw_request("POST", f"/rfqs/{rfq_id}/side", body={"sig": sig})
            except NetworkError:
                r = None  # 5xx or no answer: the gateway may hold the signature
            if r is not None and r.status_code != 200 and r.status_code < 500:
                raise refused(r)
            log(f"side signed, nonce {clean(t['own_nonce'])}; the maker signs by {utc(maker_by)}")
            return
        except QuoteExpired:
            raise
        except CrxError as e:
            why = f"{e.code}: {e}"
        except Exception as e:  # noqa: BLE001 - the Side is signed: no failure may read as "nothing happened"
            why = type(e).__name__
        raise TradeUnknown(
            f"stopped after the Side was signed ({clean(why, 200)}); the trade may still open; "
            + read_later(int(t["quote_expiry"])), details={"rfq_id": rfq_id})

    def accept(self, rfq_id: str, expires_at_ms: Any, body: dict) -> tuple[dict, float]:
        """409 rejected can be a busy maker: retry until the quote expires (120 s at most).
        The reject code is opaque; when the retries end, the round is over."""
        until = min(self.now() + 120, (expires_at_ms if isinstance(expires_at_ms, int) else 10**13) / 1000)
        while True:
            r = self.gw.raw_request("POST", f"/rfqs/{rfq_id}/accept", body=body)
            k = status_code(r)
            if k == (409, "own_round_open"):
                d = obj(r).get("details") or {}
                raise OwnRoundOpen(
                    "your previous round is still open; no new trade before it ends",
                    status=409, gateway_code="own_round_open", details=d if isinstance(d, dict) else {})
            if k == (409, "rejected") and self.now() + 3 < until:
                self.sleep(3)
                continue
            if r.status_code == 200:
                out = obj(r)
                if not out:
                    raise BadAnswer("the gateway sent an accept answer that is not an object")
                return out, self.now()
            if k == (409, "rejected"):
                d = obj(r).get("details")
                raise QuoteExpired(f"the maker refused the accept; {NEW_QUOTE}", status=409, gateway_code="rejected",
                                   details=d if isinstance(d, dict) else {})
            raise refused(r)
