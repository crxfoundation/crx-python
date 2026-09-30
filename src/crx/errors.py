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
    - ``dropped``: a binding quote that can no longer be taken: you dropped its leg, your
      later quote on the RFQ replaced it, or the gateway restarted.
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
    their limit. They clear as their quote_expiry passes."""

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
    "bad_request": BadRequest,
    "unprocessable_entity": BadRequest,
    "viewer_invalid": BadRequest,
    "viewer_is_maker": BadRequest,
    "viewers_unavailable": ServerError,
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


def retry_after(details: dict, headers: Any = None) -> float | None:
    """Seconds to wait: ``details.retry_after_secs``, else a numeric ``Retry-After`` header."""
    for v in (details.get("retry_after_secs"), (headers or {}).get("Retry-After")):
        try:
            s = float(v)
        except (TypeError, ValueError):
            continue
        if 0 <= s < 86_400:
            return s
    return None


def from_gateway(status: int, body: Any, text: str = "", headers: Any = None) -> CrxError:
    """Map one gateway refusal to a typed error. ``headers`` gives a 429 its ``Retry-After``."""
    body = body if isinstance(body, dict) else {}
    gw = gateway_code(body) or None
    msg = clean(body.get("error") or body.get("detail") or text or f"HTTP {status}")
    details = body.get("details") if isinstance(body.get("details"), dict) else {}
    kw = {"status": status, "details": details, "gateway_code": gw}
    cls = _BY_GATEWAY_CODE.get(gw or "")
    if cls is None and status == 410:
        cls = QuoteExpired
    if cls is None and status == 429:
        cls = RateLimited
    if cls is not None:
        e = cls(msg, **kw)
        if isinstance(e, RateLimited):
            e.retry_after = retry_after(details, headers)
        return e
    if status in (401, 403):
        return AuthError(msg, **kw)
    if status >= 500:
        return ServerError(msg, **kw)
    return CrxError(msg, code=gw or f"http_{status}", **kw)
