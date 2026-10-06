"""The taker's bind: check the trade template, sign this seat's own ``Trade`` and post it with the
accept. CRX sends the tx and pays gas.

The template is ``{typed_data}``: the readable ``Trade`` (SPEC v5 §5.1). Before a signature
exists the SDK builds its own ``Trade`` from its own ask and the accepted quote's rate,
takes ``pairC``, ``ownLegId``, ``ownNonce`` and ``ownSalt`` from the served message, and
compares the served ``typed_data`` with its own member by member (SPEC v5 §5.2). Any
difference raises RefusedToSign and nothing is signed.
"""

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
    RefusedToSign, RelayUnavailable, TradeUnknown, clean, gateway_code,
)

QUOTE_WINDOW = 630  # s: the chain arms only while the quote end is at most 600 s ahead, plus 30 s of clock slack
NONCE_AHEAD_MS = 86_400_000  # the gateway's bound: ownNonce at most 24 h past now, in ms
NEW_QUOTE = "not opened; request a new quote"
MAX_POSTS = 3  # accept bodies per trade; a re-post of the same body after 409 rejected does not count
# Accept refusals that reserve nothing and send nothing, even after a signed post.
NOTHING_SENT = (QuoteExpired, OwnRoundOpen, QuoteNotYours, RateLimited, Declined, RelayUnavailable)
# The gateway's band declines: answered before the accept reserves anything, whatever the HTTP status.
DECLINES = ("rate_out_of_band", "mark_unavailable", "position_matured")
# The relay takes no new item: answered before the accept reserves anything, whatever the HTTP status.
RELAY = "relay_unavailable"
TEMPLATE = "trade_template"
STALE = "trade_stale"


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S UTC")


def read_later(end: int) -> str:
    """The chain arms no trade at or after its quote end."""
    return f"do not trade again; read positions() after {utc(end + 120)}"


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
    if k[1] == RELAY:
        return RelayUnavailable(f"CRX cannot send trades now: the trade is {NEW_QUOTE}", status=k[0],
                                gateway_code=RELAY, details=d)
    return refused(r)


def stale_template(r: Any, quote_id: str) -> dict | None:
    """The fresh template a 409 trade_stale answer carries in ``details.trade_template``; None for any
    other answer. A template for another quote raises RefusedToSign."""
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
    """The served Trade message of template ``t``. KeyError or TypeError when it has none."""
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

    def typed(self, primary: str, message: dict) -> dict:
        """Typed data on this chain's domain: the chain id and core that /health serves."""
        return e7.typed_data(primary, self.chain["chain_id"], self.chain["core"], message)

    # ---------- this seat's own Trade ----------

    def own_trade(self, ask: dict, served: dict) -> dict:
        """This seat's ``Trade`` message: the readable members from its own ask, ``rateE6`` from the
        accepted quote, ``pairC``, ``ownLegId``, ``ownNonce`` and ``ownSalt`` from ``served``."""
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

    # ---------- the nonce floor ----------

    def last_signed(self) -> int:
        """The last ownNonce this seat signed from this machine; 0 before the first."""
        try:
            return int(self.state_path.read_text())
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            raise RefusedToSign("refused to sign: the nonce file is unreadable") from None

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
            raise RefusedToSign("refused to sign: the nonce file is not writable") from None

    def below_floor(self, t: dict) -> bool:
        """True when this seat already signed an ownNonce at or above the template's."""
        try:
            return int(served_message(t)["ownNonce"]) <= self.last_signed()
        except (KeyError, TypeError, ValueError):
            return False  # check_trade refuses it

    # ---------- the checks (SPEC v5 §5.2) ----------

    def check_trade(self, t: Any, ask: dict) -> tuple[bytes, dict]:
        """Check template ``t`` against this seat's own ask. Returns (the digest, this seat's own typed
        data) to sign. Writes the nonce floor first.

        ``ask``: ``pair`` (``AAA/BBB``), ``side`` (+1 or -1), ``notional`` (decimal), ``expiry`` (unix ms),
        ``premium_bps``, ``rate`` (the accepted quote's), ``quote_expiry_max`` (unix s, the RFQ view's),
        ``max_premium_bps`` (the cap; None when not served) and ``leg_id`` (from the open answer, or None).
        """
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

    # ---------- the accept ----------

    def until(self, closes_at_ms: Any) -> float:
        """The last instant to post an accept: the RFQ's ``closes_at``, 120 s from now at most.
        The quote's ``expires_at`` is its quote end, not the accept deadline."""
        closes = closes_at_ms if isinstance(closes_at_ms, int) and not isinstance(closes_at_ms, bool) else 10**13
        return min(self.now() + 120, closes / 1000)

    def post_accept(self, rfq_id: str, body: dict, until: float) -> Any:
        """POST /accept. 409 rejected can be a busy maker: post the same body again every 3 s until ``until``."""
        while True:
            r = self.gw.raw_request("POST", f"/rfqs/{rfq_id}/accept", body=body)
            if status_code(r) == (409, "rejected") and self.now() + 3 < until:
                self.sleep(3)
                continue
            return r

    def accept_trade(
        self, rfq_id: str, quote_id: str, ask: dict, t: dict | None, closes_at_ms: Any,
    ) -> tuple[dict, dict, float]:
        """Accept in one call: check the trade template, sign this seat's own ``Trade``, post ``{quote_id, sig}``.

        ``t`` is the template on the quote row. With none, ``{quote_id}`` asks the gateway
        for it: 409 trade_stale carries it. A template whose ownNonce is not above this
        seat's last signed one is asked for again, while a signed post still fits.
        409 trade_stale on a signed post carries a fresh template: it is checked and signed
        in turn. At most MAX_POSTS bodies, before the RFQ's ``closes_at_ms``.

        Returns (the 200 answer, the template signed, the answer time). When the gateway
        does not answer a signed post, the answer is ``{}``: the gateway may hold the
        signature, so the trade status decides. A band decline or ``relay_unavailable`` raises as
        is: the gateway reserved nothing. Before the first signature, a refusal
        raises as is. After it, the trade can still open, so any failure other than a
        NOTHING_SENT refusal raises TradeUnknown.
        """
        until = self.until(closes_at_ms)
        posts, signed = 0, None
        try:
            ask_for = t is None
            while True:
                if ask_for:
                    r = self.post_accept(rfq_id, {"quote_id": quote_id}, until)
                    posts += 1
                    fresh = stale_template(r, quote_id)
                    if fresh is None:
                        raise accept_refused(r)
                    t = fresh
                if self.below_floor(t) and posts + 1 < MAX_POSTS and self.now() < until:  # room to sign after
                    ask_for = True
                    continue
                sig = self.sign_template(t, ask)
                signed = t
                try:
                    r = self.post_accept(rfq_id, {"quote_id": quote_id, "sig": sig}, until)
                except NetworkError:
                    r = None
                posts += 1
                if status_code(r)[1] in DECLINES + (RELAY,):
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
        except NOTHING_SENT:
            raise
        except Exception as e:  # noqa: BLE001 - once a Trade is signed, no failure may read as "nothing happened"
            if signed is None:
                raise
            why = f"{e.code}: {e}" if isinstance(e, CrxError) else type(e).__name__
        raise TradeUnknown(
            f"stopped after the Trade was signed ({clean(why, 200)}); the trade may still open; "
            + read_later(e7.leg_id_tail(served_message(signed)["ownLegId"])), details={"rfq_id": rfq_id})
