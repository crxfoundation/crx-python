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


class AuthError(CrxError):
    code = "unauthorized"


class NotWhitelisted(CrxError):
    code = "not_whitelisted"


class SeatNotReady(CrxError):
    code = "seat_not_ready"


class MarketPaused(CrxError):
    code = "market_paused"


class MarketClosed(CrxError):
    """The gateway refused: the pair's session is closed. ``details['opens_at']`` is unix ms, when sent."""

    code = "market_closed"


class BelowMin(CrxError):
    """Notional under the minimum. ``details['min']`` names it, when known."""

    code = "below_min"


class AboveMax(CrxError):
    """Notional over the maximum, or over a wallet cap."""

    code = "above_max"


class NoQuotes(CrxError):
    """No maker quoted before the wait ended."""

    code = "no_quotes"


class QuoteExpired(CrxError):
    """The quote or its round ended, or the maker refused it. Request a new quote."""

    code = "quote_expired"


class Rejected(CrxError):
    code = "rejected"


class OwnRoundOpen(CrxError):
    """Your own previous round is still open. ``details['until']`` is unix s."""

    code = "own_round_open"


class InsufficientCollateral(CrxError):
    code = "insufficient_collateral"


class RateLimited(CrxError):
    code = "rate_limited"


class ServerError(CrxError):
    code = "server_error"


class RefusedToSign(CrxError):
    """The SDK rebuilt what the gateway served and it did not match. Nothing was signed or sent."""

    code = "refused_to_sign"


class TxFailed(CrxError):
    """A transaction would revert, did revert, or has no receipt."""

    code = "tx_failed"


class TradeUnknown(CrxError):
    """The trade may be on chain. Do not trade again: read ``positions()`` after the next fold."""

    code = "trade_unknown"


_BY_GATEWAY_CODE: dict[str, type[CrxError]] = {
    "market_paused": MarketPaused,
    "market_closed": MarketClosed,
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
    "rfq_expired": QuoteExpired,
    "round_closed": QuoteExpired,
    "rate_limited": RateLimited,
    "bad_request": BadRequest,
    "unprocessable_entity": BadRequest,
    "viewer_invalid": BadRequest,
    "viewer_is_maker": BadRequest,
    "viewers_unavailable": ServerError,
    "upstream": ServerError,
    "internal": ServerError,
    "timeout": ServerError,
}


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


def from_gateway(status: int, body: Any, text: str = "", *, host: str = "", retry_after: Any = None) -> CrxError:
    """Map one gateway refusal to a typed error.

    A body that is not a JSON object gives the message ``HTTP <status> from <host>``;
    its first text line goes to ``details['body']``. A Retry-After header fills
    ``details['retry_after_secs']`` when the body has none.
    """
    if isinstance(body, dict):
        gw = clean(body.get("code") or "", 64) or None
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
    if cls is not None:
        return cls(msg, **kw)
    if status == 410:
        return QuoteExpired(msg, **kw)
    if status == 429:
        return RateLimited(msg, **kw)
    if status in (401, 403):
        return AuthError(msg, **kw)
    if status >= 500:
        return ServerError(msg, **kw)
    return CrxError(msg, code=gw or f"http_{status}", **kw)
