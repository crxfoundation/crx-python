"""Typed errors. Branch on ``err.code``: the codes are stable."""

from __future__ import annotations

import html
import math
import re
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any


def clean(value: Any, limit: int = 300) -> str:
    """One printable line of at most ``limit`` characters."""
    return "".join(ch if ch.isprintable() else " " for ch in str(value))[:limit]


class CrxError(Exception):
    """Base error. ``code`` is the SDK's stable code.

    ``gateway_code`` is the gateway's own code, when the gateway sent one.
    ``status`` is the HTTP status, when there was one.
    """

    code = "crx_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        details: dict[str, Any] | None = None,
        gateway_code: str | None = None,
    ) -> None:
        super().__init__(clean(message, 500))
        if code:
            self.code = code
        self.status = status
        self.details = details or {}
        self.gateway_code = gateway_code

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, status={self.status!r}, message={str(self)!r})"


class ConfigError(CrxError):
    """Bad client setup: no key, bad key, unknown network."""

    code = "config"


class NetworkError(CrxError):
    """The gateway or the RPC did not answer."""

    code = "network"


class BadAnswer(CrxError):
    """The gateway or the RPC sent an answer the SDK cannot read."""

    code = "bad_answer"


class BadRequest(CrxError):
    code = "bad_request"


class QuoteFormatOutdated(BadRequest):
    """The gateway refused a quote signed in an older format. Update the SDK. Nothing was written."""

    code = "quote_format_outdated"


class AuthError(CrxError):
    code = "unauthorized"


class NotWhitelisted(CrxError):
    code = "not_whitelisted"


class SeatNotReady(CrxError):
    code = "seat_not_ready"


class MarketPaused(CrxError):
    """The pair is paused, or not offered on this chain. ``details['pair']`` names it."""

    code = "market_paused"


MarketClosed = MarketPaused


class BelowMin(CrxError):
    """Notional under the minimum. ``details['min']`` names it, when known."""

    code = "below_min"


class AboveMax(CrxError):
    """Notional over the maximum, or over a wallet cap."""

    code = "above_max"


class NoQuotes(CrxError):
    """No maker quoted before the wait ended. From ``rfqs(only=)``: the RFQ did not arrive before it.
    ``outcome`` and ``served_message`` are the gateway's line on the RFQ, when it served one: the
    message then carries it in brackets."""

    code = "no_quotes"

    @property
    def outcome(self) -> str | None:
        v = self.details.get("outcome")
        return v if isinstance(v, str) else None

    @property
    def served_message(self) -> str | None:
        v = self.details.get("message")
        return v if isinstance(v, str) else None


class RfqCancelled(NoQuotes):
    """The gateway cancelled the RFQ before an accept. ``reason`` names why: ``rate_out_of_band`` when no
    quote was inside the off-market band, ``mark_unavailable`` when no market price was read. The message
    is the gateway's sentence, when it sent one."""

    code = "rfq_cancelled"

    @property
    def reason(self) -> str | None:
        v = self.details.get("reason")
        return v if isinstance(v, str) else None


class Declined(CrxError):
    """The gateway declined the accept before it reserved anything: no trade, nothing armed or sent.
    ``details`` holds the gateway's fields. Ask for a new quote."""

    code = "declined"


class RateOutOfBand(Declined):
    """The quote's rate is outside the off-market band of the market price. ``details``: ``pair``, ``rate``,
    ``mark``, ``band_bps``."""

    code = "rate_out_of_band"


class MarkUnavailable(Declined):
    """The gateway has no market price to test the rate against."""

    code = "mark_unavailable"


class PositionMatured(Declined):
    """The position reached its maturity: it settles on its fixing and takes no close."""

    code = "position_matured"


class QuoteExpired(CrxError):
    """The quote or its round ended, or the maker refused it. Request a new quote."""

    code = "quote_expired"


class QuoteDropped(QuoteExpired):
    """The maker dropped the quote before the accept. Nothing was sent.

    ``best`` is the best live quote on the same RFQ, as a ``Quote`` for ``Client.trade``, or None.
    """

    code = "quote_dropped"
    best: Any = None


class QuoteNotYours(CrxError):
    """The quote was made for another RFQ or another seat. Nothing was sent. Do not retry it."""

    code = "quote_not_yours"


class Rejected(CrxError):
    code = "rejected"


class OwnRoundOpen(CrxError):
    """Your own previous round is still open. ``details['until']`` is unix s."""

    code = "own_round_open"


class InsufficientCollateral(CrxError):
    code = "insufficient_collateral"


class RateLimited(CrxError):
    """Too many requests. ``retry_after`` is the wait in seconds, when the gateway sent one."""

    code = "rate_limited"
    retry_after: float | None = None


class ServerError(CrxError):
    code = "server_error"


class RelayUnavailable(ServerError):
    """CRX's relay takes no new item now. The gateway refused the call before it reserved or sent anything.
    On an accept: no trade opened, nothing armed or sent. Accept again later, or request a new quote."""

    code = "relay_unavailable"


class RefusedToSign(CrxError):
    """The SDK rebuilt what the gateway served and it did not match. Nothing was signed or sent."""

    code = "refused_to_sign"


class TxFailed(CrxError):
    """A transaction would revert, did revert, or has no receipt."""

    code = "tx_failed"


class QuoteLost(CrxError):
    """Your quote opened no trade. ``reason`` says why:

    - ``another_maker``: the taker accepted another quote.
    - ``expired``: the RFQ or your quote ended with no accept.
    - ``cancelled``: the RFQ was cancelled.
    - ``dropped``: a quote that can no longer be taken: you dropped its leg, your later
      quote on the RFQ replaced it, or the gateway restarted.
    - ``timeout``: no accept before the wait ended. The RFQ can still take one.
    """

    code = "quote_lost"

    def __init__(self, message: str, *, reason: str, **kw: Any) -> None:
        details = dict(kw.pop("details", None) or {})
        details["reason"] = reason
        super().__init__(message, details=details, **kw)
        self.reason = reason


class LegIdTaken(CrxError):
    """The quote's leg id is held: by another seat, another RFQ, or a dropped leg. Quote again: a new leg id is made."""

    code = "leg_id_taken"


class LegLive(CrxError):
    """Your seat holds another live leg on the RFQ. ``leg_id`` names it: quote again on it, or drop it."""

    code = "leg_live"

    @property
    def leg_id(self) -> str | None:
        v = self.details.get("leg_id")
        return v.lower() if isinstance(v, str) else None


class QuoteFillsFull(CrxError):
    """The gateway takes no more of your quotes for now: your seat's recently filled legs are at
    their limit. They clear as their quote end passes."""

    code = "quote_fills_full"


class AlreadyAccepted(CrxError):
    """The taker accepted the quote before the drop: the trade stands. ``trade_id`` names it, when sent."""

    code = "already_accepted"

    @property
    def trade_id(self) -> str | None:
        v = self.details.get("trade_id")
        return v.lower() if isinstance(v, str) else None


class UnknownOrEnded(CrxError):
    """Nothing to drop: the RFQ ended, or your seat has no such leg on it."""

    code = "unknown_or_ended"


class TradeUnknown(CrxError):
    """The trade may be on chain. Do not trade again: read ``positions()`` after the time the message names."""

    code = "trade_unknown"


_BY_GATEWAY_CODE: dict[str, type[CrxError]] = {
    "market_paused": MarketPaused,
    "notional_below_minimum": BelowMin,
    "pool_min_notional": BelowMin,
    "notional_above_maximum": AboveMax,
    "pool_cap": AboveMax,
    "not_whitelisted": NotWhitelisted,
    "key_not_bound": NotWhitelisted,
    "seat_not_ready": SeatNotReady,
    "unauthorized": AuthError,
    "invalid_signature": AuthError,
    "insufficient_collateral": InsufficientCollateral,
    "rejected": Rejected,
    "own_round_open": OwnRoundOpen,
    "quote_expired": QuoteExpired,
    "quote_dropped": QuoteDropped,
    "quote_not_yours": QuoteNotYours,
    "leg_id_taken": LegIdTaken,
    "leg_live": LegLive,
    "quote_fills_full": QuoteFillsFull,
    "already_accepted": AlreadyAccepted,
    "unknown_or_ended": UnknownOrEnded,
    "rfq_expired": QuoteExpired,
    "round_closed": QuoteExpired,
    "rate_limited": RateLimited,
    "rate_out_of_band": RateOutOfBand,
    "mark_unavailable": MarkUnavailable,
    "position_matured": PositionMatured,
    "bad_request": BadRequest,
    "quote_format_outdated": QuoteFormatOutdated,
    "unprocessable_entity": BadRequest,
    "viewer_invalid": BadRequest,
    "viewer_is_maker": BadRequest,
    "viewers_unavailable": ServerError,
    "relay_unavailable": RelayUnavailable,
    "upstream": ServerError,
    "internal": ServerError,
    "timeout": ServerError,
}


def gateway_code(body: Any) -> str:
    """The gateway's code: ``code``, else an ``error`` that is itself a known code."""
    if not isinstance(body, dict):
        return ""
    code, error = body.get("code"), body.get("error")
    if not code and isinstance(error, str) and error in _BY_GATEWAY_CODE:
        code = error
    return clean(code or "", 64)


_NOT_TEXT = re.compile(r"<(script|style)\b.*?</\1\s*>|<!--.*?-->|<[^>]*>", re.I | re.S)


def first_line(text: str, limit: int = 200) -> str:
    """The first non-blank line of a text or HTML body, tags removed, at most ``limit`` characters."""
    if text.lstrip().startswith("<"):
        text = html.unescape(_NOT_TEXT.sub("\n", text))
    for line in text.splitlines():
        line = clean(line, 10_000).strip()
        if line:
            return line[:limit]
    return ""


RETRY_AFTER_MAX = 86400  # s


def retry_after_secs(value: Any) -> int | None:
    """Seconds from a Retry-After header: delta-seconds or an HTTP date, at most ``RETRY_AFTER_MAX``.

    None when absent or unreadable. A date without a zone is UTC.
    """
    v = str(value or "").strip()
    if v.isascii() and v.isdigit():
        return RETRY_AFTER_MAX if len(v) > 9 else min(int(v), RETRY_AFTER_MAX)
    try:
        dt = parsedate_to_datetime(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return min(max(0, math.ceil(dt.timestamp() - time.time())), RETRY_AFTER_MAX)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def wait_secs(details: dict) -> float | None:
    """``details['retry_after_secs']`` as seconds; None unless a number from 0 to ``RETRY_AFTER_MAX``."""
    try:
        s = float(details.get("retry_after_secs"))
    except (TypeError, ValueError):
        return None
    return s if 0 <= s <= RETRY_AFTER_MAX else None


def from_gateway(status: int, body: Any, text: str = "", *, host: str = "", retry_after: Any = None) -> CrxError:
    """Map one gateway refusal to a typed error.

    A body that is not a JSON object gives the message ``HTTP <status> from <host>``;
    its first text line goes to ``details['body']``. A Retry-After header fills
    ``details['retry_after_secs']`` when the body has none.
    """
    if isinstance(body, dict):
        gw = gateway_code(body) or None
        msg = clean(body.get("error") or body.get("detail") or f"HTTP {status}")
        details = dict(body["details"]) if isinstance(body.get("details"), dict) else {}
    else:
        gw = None
        msg = f"HTTP {status} from {clean(host, 100)}" if host else f"HTTP {status}"
        line = first_line(text or "")
        details = {"body": line} if line else {}
    secs = retry_after_secs(retry_after)
    if secs is not None:
        details.setdefault("retry_after_secs", secs)
    kw = {"status": status, "details": details, "gateway_code": gw}
    cls = _BY_GATEWAY_CODE.get(gw or "")
    if cls is None and status == 410:
        cls = QuoteExpired
    if cls is None and status == 429:
        cls = RateLimited
    if cls is not None:
        e = cls(msg, **kw)
        if isinstance(e, RateLimited):
            e.retry_after = wait_secs(details)
        return e
    if status in (401, 403):
        return AuthError(msg, **kw)
    if status >= 500:
        return ServerError(msg, **kw)
    return CrxError(msg, code=gw or f"http_{status}", **kw)
