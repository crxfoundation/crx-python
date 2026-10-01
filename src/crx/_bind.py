"""The taker's bind: sign the template and post it with the accept, or after the accept on a
gateway that takes the leg body. CRX sends the tx and pays gas.

A template names what the taker signs (``digest_kind``). ``side``: a ``Side``. ``trade``: a
``Trade``, built from this seat's own RFQ and quote, with a readable ``summary``. Any other
kind is refused.

Every value the gateway serves is rebuilt locally before a signature exists: the SDK builds
its own typed data, compares it with the served ``typed_data`` member by member, and
recomputes the digest. A mismatch raises RefusedToSign and nothing is signed.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from eth_abi.exceptions import EncodingError

from . import _eip712 as e7
from ._http import Gateway
from .signer import as_signer, sign_typed
from .errors import (
    BadAnswer, CrxError, NetworkError, OwnRoundOpen, QuoteDropped, QuoteExpired, QuoteNotYours, RateLimited,
    RefusedToSign, TradeUnknown, clean, from_gateway, gateway_code,
)

KINDS = ("side", "trade")  # the template kinds a taker signs
SIDE_WINDOW = 630  # s: the core takes a Side quote_expiry at most 600 s past its block time, plus 30 s of clock slack
NEW_QUOTE = "not opened; request a new quote"
MAX_POSTS = 3  # accept bodies per trade; a re-post of the same body after 409 rejected does not count
# Accept refusals that reserve nothing and send nothing, even after a signed post.
NOTHING_SENT = (QuoteExpired, OwnRoundOpen, QuoteNotYours, RateLimited)


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S UTC")


def read_later(qe: int) -> str:
    """The core takes no trade at or after the Side quote_expiry; positions() shows a landed trade within a minute."""
    return f"do not trade again; read positions() after {utc(qe + 120)}"


def status_code(r: Any) -> tuple[int, str]:
    if r is None:
        return (0, "")
    return (r.status_code, gateway_code(Gateway.body_of(r)))


def obj(r: Any) -> dict:
    body = Gateway.body_of(r) if r is not None else None
    return body if isinstance(body, dict) else {}


def refused(r: Any) -> CrxError:
    """A bind refusal: a closed round asks for a new quote."""
    k = status_code(r)
    if k[0] == 410 or k in ((409, "rejected"), (409, "round_closed"), (409, "conflict")):
        return QuoteExpired(NEW_QUOTE, status=k[0], gateway_code=k[1] or None)
    return Gateway.error_of(r)


def accept_refused(r: Any) -> CrxError:
    """An accept refusal. 409 rejected here is the end of the busy-maker retries.
    409 quote_dropped keeps the served best live quote in ``details["best"]`` (a row, or None)."""
    k = status_code(r)
    d = obj(r).get("details")
    d = d if isinstance(d, dict) else {}
    if k == (409, "quote_dropped"):
        best = d.get("best", obj(r).get("best"))
        return QuoteDropped(f"the maker dropped this quote; {NEW_QUOTE}", status=409, gateway_code="quote_dropped",
                            details={"best": best if isinstance(best, dict) else None})
    if k == (409, "own_round_open"):
        return OwnRoundOpen("your previous round is still open; no new trade before it ends",
                            status=409, gateway_code="own_round_open", details=d)
    if k == (409, "rejected"):
        return QuoteExpired(f"the maker refused the accept; {NEW_QUOTE}", status=409, gateway_code="rejected",
                            details=d)
    return refused(r)


def stale_template(r: Any, quote_id: str) -> dict | None:
    """The fresh Side template a 409 side_stale answer carries; None for any other answer.
    A template for another quote raises RefusedToSign."""
    if status_code(r) != (409, "side_stale"):
        return None
    d = obj(r).get("details")
    t = d.get("side_template") if isinstance(d, dict) else None
    if not isinstance(t, dict):
        return None
    q = d.get("quote_id")
    if not isinstance(q, str) or q.lower() != quote_id.lower():
        raise RefusedToSign("refused to sign: the fresh Side template is for another quote")
    return t


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
        """``account``: the signer (``crx.signer``), or a local account."""
        self.gw = gateway
        self.signer = as_signer(account)
        self.seat = self.signer.address.lower()
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
        """Sign this seat's ``Leg`` as typed data."""
        try:
            digest = e7.leg_digest(self.sep, a)
            td = self.typed("Leg", e7.leg_message(a))
        except (KeyError, TypeError, ValueError, ArithmeticError, EncodingError):
            raise RefusedToSign("refused to sign: the quote's leg cannot be encoded") from None
        return sign_typed(self.signer, td, digest, self.seat)

    def typed(self, primary: str, message: dict) -> dict:
        """Typed data on this chain's domain: the chain id and core that /health serves."""
        return e7.typed_data(primary, self.chain["chain_id"], self.chain["core"], message)

    def own_trade(self, t: dict, arm: dict, pair: str) -> dict:
        """This seat's ``Trade`` message: the terms from its own RFQ and quote, rows 9 to 14 from ``t``."""
        if not e7.pair_text_ok(pair):
            raise RefusedToSign("refused to sign: the pair is not AAA/BBB")
        if e7.pair_id(pair) != e7.hx(arm["pair_id"]):
            raise RefusedToSign("refused to sign: the pair does not hash to the quote's pair_id")
        try:
            notional = e7.e6(arm["notional"], 128)
        except ValueError:
            raise RefusedToSign("refused to sign: the notional is not a 6-decimal amount below 2^128") from None
        try:
            rate = e7.e6(arm["rate"], 64)
        except ValueError:
            raise RefusedToSign("refused to sign: the rate is not a 6-decimal amount below 2^64") from None
        expiry = arm["expiry"]
        if isinstance(expiry, bool) or not isinstance(expiry, int) or expiry < 0:
            raise RefusedToSign("refused to sign: the expiry is not unix ms")
        try:
            return e7.trade_message(
                pair, arm["side"], notional, rate, arm["premium_bps"], arm["im_bps"], expiry // 1000,
                t["pair_c"], t["own_leg_id"], t["quote_expiry"], t["own_nonce"], t["own_salt"], t["wraps_hash"])
        except ValueError as e:
            raise RefusedToSign(f"refused to sign: {e}") from None

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

    def check_side(self, t: dict, arm: dict, pair: str) -> tuple[bytes, dict]:
        """Rebuild this seat's half with the gateway's nonce and the Side quote expiry, then this seat's
        own typed data. Returns (the digest, the typed data) to sign. Writes the nonce floor first.

        ``trade``: the served ``typed_data`` and ``digest`` must equal this seat's own. ``side``:
        the served ``digest`` and ``typed_data``, when present, must equal this seat's own.
        """
        kind = t.get("digest_kind") if isinstance(t, dict) else None
        if kind not in KINDS:
            raise RefusedToSign(f"refused to sign: unknown digest_kind {clean(kind, 40)!r}")
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
            if kind == "trade":
                msg = self.own_trade(t, arm, pair)
                digest, td = e7.trade_digest(self.sep, msg), self.typed("Trade", msg)
                if "typed_data" not in t:
                    raise RefusedToSign("refused to sign: the trade template carries no typed_data")
            else:
                digest, td = e7.side_digest(self.sep, t), self.typed("Side", e7.side_message(t))
            if "typed_data" in t:
                diff = e7.typed_mismatch(t["typed_data"], td)
                if diff is not None:
                    raise RefusedToSign(f"refused to sign: the served typed_data differs at {diff}")
            if (kind == "trade" or t.get("digest")) and e7.h0x(digest) != str(t["digest"]).lower():
                raise RefusedToSign(f"refused to sign: the served digest is not this {kind.capitalize()}")
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError, EncodingError):
            raise RefusedToSign("refused to sign: the template cannot be read") from None
        self.keep_signed(own_nonce)
        return digest, td

    def sign_template(self, t: dict, arm: dict, pair: str) -> str:
        """Check template ``t`` and sign this seat's own typed data for it."""
        digest, td = self.check_side(t, arm, pair)
        return sign_typed(self.signer, td, digest, self.seat)

    def bind(
        self, rfq_id: str, arm: dict, t: dict, maker_by: float | None, log: Callable[[str], None], pair: str,
    ) -> None:
        """Sign this seat's Side and post it. CRX sends the trade once both Sides are in.
        ``maker_by`` is the maker's last instant to sign its Side; None where the maker's quote binds it.
        Raises QuoteExpired (no bind) or TradeUnknown (maybe bound). Before the Side
        signature exists, a refusal is RefusedToSign. After it, the trade can still
        open, so any other failure is TradeUnknown."""
        sig = self.sign_template(t, arm, pair)
        why = None
        try:
            try:
                r = self.gw.raw_request("POST", f"/rfqs/{rfq_id}/side", body={"sig": sig})
            except NetworkError:
                r = None  # 5xx or no answer: the gateway may hold the signature
            if r is not None and r.status_code != 200 and r.status_code < 500:
                raise refused(r)
            log(f"side signed, nonce {clean(t['own_nonce'])}"
                + (f"; the maker signs by {utc(maker_by)}" if maker_by is not None else ""))
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

    def until(self, expires_at_ms: Any) -> float:
        """The last instant to post an accept: the quote's expires_at, 120 s from now at most."""
        return min(self.now() + 120, (expires_at_ms if isinstance(expires_at_ms, int) else 10**13) / 1000)

    def post_accept(self, rfq_id: str, body: dict, until: float) -> Any:
        """POST /accept. 409 rejected can be a busy maker: post the same body again every 3 s until ``until``."""
        while True:
            r = self.gw.raw_request("POST", f"/rfqs/{rfq_id}/accept", body=body)
            if status_code(r) == (409, "rejected") and self.now() + 3 < until:
                self.sleep(3)
                continue
            return r

    def accept(self, rfq_id: str, expires_at_ms: Any, body: dict) -> tuple[dict, float]:
        """The leg body accept. 409 rejected can be a busy maker: retry until the quote expires (120 s at most).
        The reject code is opaque; when the retries end, the round is over."""
        r = self.post_accept(rfq_id, body, self.until(expires_at_ms))
        if r.status_code == 200:
            out = obj(r)
            if not out:
                raise BadAnswer("the gateway sent an accept answer that is not an object")
            return out, self.now()
        raise accept_refused(r)

    def below_floor(self, t: dict) -> bool:
        """True when this seat already signed a Side nonce at or above the template's own_nonce."""
        try:
            return int(t["own_nonce"]) <= self.last_signed()
        except (KeyError, TypeError, ValueError):
            return False  # check_side refuses it

    def accept_side(
        self, rfq_id: str, quote_id: str, arm: dict, t: dict | None, expires_at_ms: Any, pair: str,
    ) -> tuple[dict, dict, float] | None:
        """Accept in one call: check and sign the Side template, post ``{quote_id, sig}``.

        ``t`` is the template on the quote row. With none, ``{quote_id}`` asks the
        gateway for it: 409 side_stale carries it. 400 bad_request means the gateway
        takes the leg body only: this returns None and nothing is signed. A template
        whose own_nonce is not above this seat's last signed one is asked for again,
        while a signed post still fits.
        409 side_stale on a signed post carries a fresh template: it is checked and
        signed in turn. At most MAX_POSTS bodies, while the quote lives.

        Returns (the 200 answer, the template signed, the answer time). When the
        gateway does not answer a signed post, the answer is ``{}``: the gateway may
        hold the signature, so the trade status decides. Before the first signature,
        a refusal raises as is. After it, the trade can still open, so any failure
        other than a NOTHING_SENT refusal raises TradeUnknown.
        """
        until = self.until(expires_at_ms)
        posts, signed = 0, None
        try:
            ask = t is None
            while True:
                if ask:
                    r = self.post_accept(rfq_id, {"quote_id": quote_id}, until)
                    posts += 1
                    fresh = stale_template(r, quote_id)
                    if fresh is None:
                        if signed is None and status_code(r) == (400, "bad_request"):
                            return None
                        raise accept_refused(r)
                    t = fresh
                if self.below_floor(t) and posts + 1 < MAX_POSTS and self.now() < until:  # room to sign after
                    ask = True
                    continue
                sig = self.sign_template(t, arm, pair)
                signed = t
                try:
                    r = self.post_accept(rfq_id, {"quote_id": quote_id, "sig": sig}, until)
                except NetworkError:
                    r = None
                posts += 1
                if r is None or r.status_code >= 500:
                    return {}, t, self.now()
                if r.status_code == 200:
                    return obj(r), t, self.now()
                fresh = stale_template(r, quote_id)
                if fresh is None:
                    raise accept_refused(r)
                if posts >= MAX_POSTS or self.now() >= until:
                    raise QuoteExpired(f"the Side template went stale; {NEW_QUOTE}", status=409,
                                       gateway_code="side_stale")
                t, ask = fresh, False
        except NOTHING_SENT:
            raise
        except Exception as e:  # noqa: BLE001 - once a Side is signed, no failure may read as "nothing happened"
            if signed is None:
                raise
            why = f"{e.code}: {e}" if isinstance(e, CrxError) else type(e).__name__
        raise TradeUnknown(
            f"stopped after the Side was signed ({clean(why, 200)}); the trade may still open; "
            + read_later(int(signed["quote_expiry"])), details={"rfq_id": rfq_id})
