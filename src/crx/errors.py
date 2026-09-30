"""Typed errors. Branch on ``err.code``: the codes are stable."""

from __future__ import annotations

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


class QuoteLost(CrxError):
    """Your quote opened no trade. ``reason`` says why:

    - ``another_maker``: the taker accepted another quote.
    - ``expired``: the RFQ or your quote ended with no accept.
    - ``cancelled``: the RFQ was cancelled.
    - ``round_closed``: the taker accepted, and the Side round closed before the pair armed.
    - ``timeout``: no accept before the wait ended. The RFQ can still take one.
    """

    code = "quote_lost"

    def __init__(self, message: str, *, reason: str, **kw: Any) -> None:
        details = dict(kw.pop("details", None) or {})
        details["reason"] = reason
        super().__init__(message, details=details, **kw)
        self.reason = reason


class TradeUnknown(CrxError):
    """The trade may be on chain. Do not trade again: read ``positions()`` after the time the message names."""

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


def from_gateway(status: int, body: Any, text: str = "") -> CrxError:
    """Map one gateway refusal to a typed error."""
    body = body if isinstance(body, dict) else {}
    gw = clean(body.get("code") or "", 64) or None
    msg = clean(body.get("error") or body.get("detail") or text or f"HTTP {status}")
    details = body.get("details") if isinstance(body.get("details"), dict) else {}
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
