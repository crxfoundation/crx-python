"""Taker accept: check the served trade template, sign the seat's own ``Trade`` and post it."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from eth_abi.exceptions import EncodingError

from . import _eip712 as e7
from ._http import Gateway
from .signer import as_signer, sign_typed
from .errors import (
    CrxError, Declined, NetworkError, OwnRoundOpen, QuoteDropped, QuoteExpired, QuoteNotYours, RateLimited,
    RefusedToSign, ServiceUnavailable, TradeUnknown, clean, gateway_code,
)

QUOTE_WINDOW = 630  # seconds: the chain takes a quote end at most 600 s ahead, plus 30 s of clock slack
NONCE_AHEAD_MS = 86_400_000  # the gateway's limit: ownNonce at most 24 h ahead of now
NEW_QUOTE = "not opened; request a new quote"
MAX_POSTS = 3  # accept bodies per trade, each posted once
# Accept refusals that reserve nothing and send nothing, even after a signed post.
NOTHING_SENT = (QuoteExpired, OwnRoundOpen, QuoteNotYours, RateLimited, Declined, ServiceUnavailable)
# Declines sent before the accept reserves anything, whatever the HTTP status.
DECLINES = ("rate_out_of_band", "mark_unavailable", "position_matured")
# The service takes no new trade and reserves nothing.
UNAVAILABLE = ("service_unavailable",)
TEMPLATE = "trade_template"
STALE = "trade_stale"


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S UTC")


def read_later(end: int) -> str:
    """Return the hint to read positions() after the quote end, past which no trade opens."""
    return f"do not trade again; read positions() after {utc(end + 120)}"


def status_code(r: Any) -> tuple[int, str]:
    if r is None:
        return (0, "")
    return (r.status_code, gateway_code(Gateway.body_of(r)))


def obj(r: Any) -> dict:
    body = Gateway.body_of(r) if r is not None else None
    return body if isinstance(body, dict) else {}


def served(r: Any) -> dict:
    """Return the refusal's details, plus the gateway's ``message`` and ``outcome`` when sent."""
    if r is None:
        return {}
    d = dict(Gateway.error_of(r).details)
    for key in ("message", "outcome"):
        v = obj(r).get(key)
        if isinstance(v, str) and v.strip():
            d.setdefault(key, clean(v, 300))
    return d


def refused(r: Any) -> CrxError:
    """Map a refusal to its error. A closed round gives QuoteExpired."""
    k = status_code(r)
    if k[0] == 410 or k in ((409, "rejected"), (409, "round_closed"), (409, "conflict")):
        return QuoteExpired(NEW_QUOTE, status=k[0], gateway_code=k[1] or None, details=served(r))
    return Gateway.error_of(r)


def accept_refused(r: Any) -> CrxError:
    """Map an accept refusal to its error. On quote_dropped, ``details['best']`` holds the best live quote."""
    k = status_code(r)
    d = served(r)
    if k == (409, "quote_dropped"):
        best = d.get("best", obj(r).get("best"))
        d["best"] = best if isinstance(best, dict) else None
        return QuoteDropped(f"the maker dropped this quote; {NEW_QUOTE}", status=409, gateway_code="quote_dropped",
                            details=d)
    if k == (409, "own_round_open"):
        return OwnRoundOpen("your previous round is still open; no new trade before it ends",
                            status=409, gateway_code="own_round_open", details=d)
    if k == (409, "rejected"):
        return QuoteExpired(f"the accept was refused; {NEW_QUOTE}", status=409, gateway_code="rejected", details=d)
    if k[1] in UNAVAILABLE:
        return ServiceUnavailable(f"service temporarily unavailable: the trade is {NEW_QUOTE}", status=k[0],
                                  gateway_code=k[1], details=d)
    return refused(r)


def stale_template(r: Any, quote_id: str) -> dict | None:
    """Return the fresh template of a 409 trade_stale answer, or None. Refuse a template for another quote."""
    if status_code(r) != (409, STALE):
        return None
    d = obj(r).get("details")
    t = d.get(TEMPLATE) if isinstance(d, dict) else None
    if not isinstance(t, dict):
        return None
    q = d.get("quote_id")
    if not isinstance(q, str) or q.lower() != quote_id.lower():
        raise RefusedToSign("refused to sign: the fresh trade template is for another quote")
    return t


def served_message(t: Any) -> dict:
    """Return the Trade message of template ``t``. Raises KeyError or TypeError when it has none."""
    return t["typed_data"]["message"]


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
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``account`` is a ``crx.signer`` signer or a local account."""
        self.gw = gateway
        self.signer = as_signer(account)
        self.seat = self.signer.address.lower()
        self.chain = chain
        self.sep = separator
        self.state_path = Path(state_dir).expanduser() / f"side-nonce-{self.seat}"
        self.now = clock

    def typed(self, primary: str, message: dict) -> dict:
        """Build typed data on the chain id and core that /health serves."""
        return e7.typed_data(primary, self.chain["chain_id"], self.chain["core"], message)

    def own_trade(self, ask: dict, served: dict) -> dict:
        """Build this seat's ``Trade`` from its own ask, with ``pairC``, ``ownLegId``, ``ownNonce`` and ``ownSalt``
        from ``served``."""
        pair = ask["pair"]
        if not e7.pair_text_ok(pair):
            raise RefusedToSign("refused to sign: the pair is not AAA/BBB")
        try:
            notional = e7.e6(ask["notional"], 128)
        except ValueError:
            raise RefusedToSign("refused to sign: the notional is not a 6-decimal amount below 2^128") from None
        try:
            rate = e7.e6(ask["rate"], 64)
        except ValueError:
            raise RefusedToSign("refused to sign: the rate is not a 6-decimal amount below 2^64") from None
        expiry = ask["expiry"]
        if isinstance(expiry, bool) or not isinstance(expiry, int) or expiry < 0:
            raise RefusedToSign("refused to sign: the expiry is not unix ms")
        try:
            return e7.trade_message(
                pair, ask["side"], notional, rate, ask["premium_bps"], expiry // 1000,
                served["pairC"], served["ownLegId"], served["ownNonce"], served["ownSalt"], served.get("quoteExpiry"))
        except ValueError as e:
            raise RefusedToSign(f"refused to sign: {e}") from None

    def last_signed(self) -> int:
        """Return the last ownNonce this seat signed on this machine, or 0."""
        try:
            return int(self.state_path.read_text())
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            raise RefusedToSign("refused to sign: the nonce file is unreadable") from None

    def keep_signed(self, nonce: int) -> None:
        """Persist ``nonce`` before signing: temporary file, fsync, rename."""
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(tmp, "w") as f:
                f.write(str(nonce))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        except OSError:
            raise RefusedToSign("refused to sign: the nonce file is not writable") from None

    def below_floor(self, t: dict) -> bool:
        """Return True when this seat already signed an ownNonce at or above the template's."""
        try:
            return int(served_message(t)["ownNonce"]) <= self.last_signed()
        except (KeyError, TypeError, ValueError):
            return False  # check_trade refuses it

    def check_trade(self, t: Any, ask: dict) -> tuple[bytes, dict]:
        """Check template ``t`` against this seat's own ask and return (digest, typed data) to sign.

        The seat rebuilds its own ``Trade`` and compares the served ``typed_data`` member by member. Any
        difference raises RefusedToSign. The nonce floor is written before the return.

        ``ask`` holds ``pair``, ``side`` (+1 or -1), ``notional``, ``expiry`` (unix ms), ``premium_bps``,
        ``rate``, ``quote_expiry_max`` (unix s), ``max_premium_bps`` (None when not served) and ``leg_id``."""
        if not isinstance(t, dict) or set(t) != {"typed_data"}:
            raise RefusedToSign("refused to sign: the trade template is not {typed_data}")
        premium, cap = ask["premium_bps"], ask.get("max_premium_bps")
        if premium != 0 and cap is None:
            raise RefusedToSign("refused to sign: the premium is not 0 and the market serves no premium cap")
        if abs(premium) > (cap or 0):
            raise RefusedToSign(f"refused to sign: the premium {premium} bps is above the cap ({cap} bps)")
        try:
            served = served_message(t)
            if not isinstance(served, dict):
                raise TypeError
            msg = self.own_trade(ask, served)
            td = self.typed("Trade", msg)
            diff = e7.typed_mismatch(t["typed_data"], td)
            if diff is not None:
                raise RefusedToSign(f"refused to sign: the served typed_data differs at {diff}")
            own_leg_id, own_nonce = msg["ownLegId"], int(msg["ownNonce"])
            if ask.get("leg_id") is not None and own_leg_id != str(ask["leg_id"]).lower():
                raise RefusedToSign("refused to sign: ownLegId is not this RFQ's leg")
            end = e7.leg_id_tail(own_leg_id)
            if end != ask["quote_expiry_max"]:
                raise RefusedToSign("refused to sign: the ownLegId tail is not the RFQ's quote_expiry_max")
            now = self.now()
            if own_nonce > now * 1000 + NONCE_AHEAD_MS:
                raise RefusedToSign("refused to sign: ownNonce is more than 24 h ahead")
            if own_nonce <= self.last_signed():
                raise RefusedToSign("refused to sign: ownNonce is not above the last one this seat signed")
            if end <= now:
                raise QuoteExpired(f"the quote end has passed; {NEW_QUOTE}")
            if end > now + QUOTE_WINDOW:
                raise RefusedToSign(f"refused to sign: the quote end is more than {QUOTE_WINDOW} s ahead")
            digest = e7.trade_digest(self.sep, msg)
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError, EncodingError):
            raise RefusedToSign("refused to sign: the template cannot be read") from None
        self.keep_signed(own_nonce)
        return digest, td

    def sign_template(self, t: Any, ask: dict) -> str:
        """Check template ``t`` and sign this seat's own typed data for it."""
        digest, td = self.check_trade(t, ask)
        return sign_typed(self.signer, td, digest, self.seat)

    def until(self, closes_at_ms: Any) -> float:
        """Return the accept deadline: the RFQ's ``closes_at``, capped at 120 s from now. The quote's
        ``expires_at`` is its quote end and does not bound the accept."""
        closes = closes_at_ms if isinstance(closes_at_ms, int) and not isinstance(closes_at_ms, bool) else 10**13
        return min(self.now() + 120, closes / 1000)

    def post_accept(self, rfq_id: str, body: dict) -> Any:
        """Post one accept body and return the raw answer."""
        return self.gw.raw_request("POST", f"/rfqs/{rfq_id}/accept", body=body)

    def accept_trade(
        self, rfq_id: str, quote_id: str, ask: dict, t: dict | None, closes_at_ms: Any,
    ) -> tuple[dict, dict, float]:
        """Check the template, sign this seat's ``Trade`` and post ``{quote_id, sig}``.

        With no template ``t``, or one below the nonce floor, an unsigned post asks for one: the 409
        trade_stale answer carries it. A 409 trade_stale on a signed post reserves nothing, and its fresh
        template is checked and signed in turn. Each body is posted once, at most MAX_POSTS bodies before
        ``closes_at_ms``.

        Return (the 200 answer, the signed template, the answer time). The answer is ``{}`` when a signed
        post gets no answer: the gateway may hold the signature, so the trade status decides. A NOTHING_SENT
        refusal raises as received, with ``after_sig`` set. Any other failure after a signature, or a 200 to
        the unsigned post, raises TradeUnknown: the trade can still open."""
        until = self.until(closes_at_ms)
        posts, signed = 0, None
        try:
            ask_for = t is None
            while True:
                if ask_for:
                    r = self.post_accept(rfq_id, {"quote_id": quote_id})
                    posts += 1
                    if r is not None and r.status_code == 200:
                        # A 200 here is an accept the gateway already holds, and that trade can still open.
                        raise TradeUnknown("the gateway holds an accept on this RFQ already; the trade may still "
                                           "open; " + read_later(ask["quote_expiry_max"]), details={"rfq_id": rfq_id})
                    fresh = stale_template(r, quote_id)
                    if fresh is None:
                        raise accept_refused(r)
                    t = fresh
                if self.below_floor(t) and posts + 1 < MAX_POSTS and self.now() < until:  # room for a signed post
                    ask_for = True
                    continue
                sig = self.sign_template(t, ask)
                signed = t
                try:
                    r = self.post_accept(rfq_id, {"quote_id": quote_id, "sig": sig})
                except NetworkError:
                    r = None
                posts += 1
                if status_code(r)[1] in DECLINES + UNAVAILABLE:
                    raise accept_refused(r)
                if r is None or r.status_code >= 500:
                    return {}, t, self.now()
                if r.status_code == 200:
                    return obj(r), t, self.now()
                fresh = stale_template(r, quote_id)
                if fresh is None:
                    raise accept_refused(r)
                if posts >= MAX_POSTS or self.now() >= until:
                    raise QuoteExpired(f"the trade template went stale; {NEW_QUOTE}", status=409, gateway_code=STALE)
                t, ask_for = fresh, False
        except NOTHING_SENT as e:
            e.after_sig = signed is not None  # after a signature, mark_unavailable can keep the round
            raise
        except Exception as e:  # noqa: BLE001 - once a Trade is signed, no failure may read as "nothing happened"
            if signed is None:
                raise
            why = f"{e.code}: {e}" if isinstance(e, CrxError) else type(e).__name__
        raise TradeUnknown(
            f"stopped after the Trade was signed ({clean(why, 200)}); the trade may still open; "
            + read_later(e7.leg_id_tail(served_message(signed)["ownLegId"])), details={"rfq_id": rfq_id})
