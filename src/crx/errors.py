"""Errors the SDK raises. Branch on ``err.code``, which is stable across releases."""

from __future__ import annotations

import html
import math
import re
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any


def clean(value: Any, limit: int = 300) -> str:
    """Return ``value`` as one printable line of at most ``limit`` characters."""
    return "".join(ch if ch.isprintable() else " " for ch in str(value))[:limit]


class CrxError(Exception):
    """Base class of every SDK error.

    Attributes:
        code: The SDK's error code. Branch on it.
        status: The HTTP status, or None.
        gateway_code: The gateway's own code, or None.
        details: Extra fields. ``retry_after_secs`` and ``time_utc`` give the time to call again, when known.
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
    """The client setup is not valid: a missing or bad key, or an unknown network. Fix the setup."""
    code = "config"


class NetworkError(CrxError):
    """The gateway or the RPC did not answer. Read the state before you send a write again."""
    code = "network"


class BadAnswer(CrxError):
    """The gateway or the RPC sent an answer the SDK cannot read. Read ``balance()`` before you act."""
    code = "bad_answer"


class BadRequest(CrxError):
    """The SDK or the gateway refused an argument. The message names it."""
    code = "bad_request"


class QuoteFormatOutdated(BadRequest):
    """The gateway refused a quote in an older format and wrote nothing. Update the SDK."""
    code = "quote_format_outdated"


class AuthError(CrxError):
    """The gateway refused the key or its signature (HTTP 401 or 403). Check the key."""
    code = "unauthorized"


class NotWhitelisted(CrxError):
    """The seat is not onboarded, or the key is not bound to it. Finish onboarding first."""
    code = "not_whitelisted"


class SeatNotReady(CrxError):
    """The seat's onboarding is not finished. Wait for it to finish."""
    code = "seat_not_ready"


class MarketPaused(CrxError):
    """The pair is paused or this chain does not offer it. ``details['pair']`` names the pair."""
    code = "market_paused"


MarketClosed = MarketPaused


class BelowMin(CrxError):
    """The amount is under the minimum, which ``details['min']`` names when known. Raise the amount."""
    code = "below_min"


class AboveMax(CrxError):
    """The amount is over the maximum or over a wallet cap. Lower the amount."""
    code = "above_max"


class NoQuotes(CrxError):
    """No maker quoted before the wait ended. Request a new quote."""
    code = "no_quotes"

    @property
    def outcome(self) -> str | None:
        """The gateway's outcome for the RFQ, when it sent one."""
        v = self.details.get("outcome")
        return v if isinstance(v, str) else None

    @property
    def served_message(self) -> str | None:
        """The gateway's message for the RFQ, when it sent one."""
        v = self.details.get("message")
        return v if isinstance(v, str) else None


class RfqCancelled(NoQuotes):
    """The gateway cancelled the RFQ before an accept. Request a new quote."""
    code = "rfq_cancelled"

    @property
    def reason(self) -> str | None:
        """``rate_out_of_band`` when no quote was inside the band, ``mark_unavailable`` with no market price."""
        v = self.details.get("reason")
        return v if isinstance(v, str) else None


class Declined(CrxError):
    """The gateway declined the accept before it reserved anything. No trade opened. Request a new quote."""
    code = "declined"


class RateOutOfBand(Declined):
    """The quote's rate is outside the off-market band. ``details`` holds ``rate``, ``mark`` and ``band_bps``."""
    code = "rate_out_of_band"


class MarkUnavailable(Declined):
    """No market price is available to check the rate or to price free collateral. Nothing was signed."""
    code = "mark_unavailable"


class PositionMatured(Declined):
    """The position reached maturity. It settles at its fixing and takes no close."""
    code = "position_matured"


class QuoteExpired(CrxError):
    """The quote or its round ended, or the maker refused it. Request a new quote."""
    code = "quote_expired"


class QuoteDropped(QuoteExpired):
    """The maker dropped the quote before the accept. Pass ``best``, when set, to ``Client.accept``."""
    code = "quote_dropped"
    best: Any = None


class QuoteRejected(CrxError):
    """``reject()`` rejected this RFQ on this client, so ``accept()`` signs nothing for it."""
    code = "quote_rejected"


class QuoteNotYours(CrxError):
    """The quote belongs to another RFQ or another seat. Nothing was sent. Do not send it again."""
    code = "quote_not_yours"


class Rejected(CrxError):
    """The gateway rejected the request. The message gives its reason."""
    code = "rejected"


class OwnRoundOpen(CrxError):
    """The seat's previous round is open until ``details['until']``, in unix seconds. Wait for it to end."""
    code = "own_round_open"


class InsufficientCollateral(CrxError):
    """Free collateral does not cover the call. Deposit more or lower the amount."""
    code = "insufficient_collateral"


class RateLimited(CrxError):
    """Too many requests. Wait ``retry_after`` seconds, when set, before the next call."""
    code = "rate_limited"
    retry_after: float | None = None


class ServerError(CrxError):
    """The gateway failed to serve the call. Read the state before you send a write again."""
    code = "server_error"


class ServiceUnavailable(ServerError):
    """The service takes no new trade or withdraw now and reserved nothing. Call again later."""
    code = "service_unavailable"


class SeatCannotSign(CrxError):
    """The address is a smart-contract wallet and cannot sign trades. Use an EOA or MPC address."""
    code = "seat_cannot_sign"


class RefusedToSign(CrxError):
    """What the gateway served does not match what the SDK rebuilt. Nothing was signed or sent."""
    code = "refused_to_sign"


class TxFailed(CrxError):
    """A transaction would revert, did revert, or has no receipt."""
    code = "tx_failed"


class SendUnknown(CrxError):
    """A transaction was sent and its outcome is unknown. Check ``tx`` on the chain before you send again."""
    code = "send_unknown"

    @property
    def tx(self) -> str | None:
        """The transaction hash."""
        v = self.details.get("tx")
        return v if isinstance(v, str) else None


class QuoteLost(CrxError):
    """The maker quote opened no trade. ``reason`` is one of:

    - ``another_maker``: the taker accepted another quote.
    - ``expired``: the RFQ or the quote ended with no accept.
    - ``cancelled``: the RFQ was cancelled.
    - ``dropped``: the seat dropped the leg, a later quote replaced it, or the gateway restarted.
    - ``timeout``: the wait ended with no accept. The RFQ can still take one.
    """

    code = "quote_lost"

    def __init__(self, message: str, *, reason: str, **kw: Any) -> None:
        details = dict(kw.pop("details", None) or {})
        details["reason"] = reason
        super().__init__(message, details=details, **kw)
        self.reason = reason


class LegIdTaken(CrxError):
    """The quote's leg id is in use. Quote again: the SDK makes a new leg id."""
    code = "leg_id_taken"


class LegLive(CrxError):
    """The seat holds another live leg on the RFQ. Quote again on ``leg_id``, or drop it."""
    code = "leg_live"

    @property
    def leg_id(self) -> str | None:
        """The id of the live leg, in lower case."""
        v = self.details.get("leg_id")
        return v.lower() if isinstance(v, str) else None


class QuoteFillsFull(CrxError):
    """The seat's recently filled legs are at their limit. Quote again after their quote ends pass."""
    code = "quote_fills_full"


class AlreadyAccepted(CrxError):
    """The taker accepted the quote before the drop, and the trade stands. ``trade_id`` names it."""
    code = "already_accepted"

    @property
    def trade_id(self) -> str | None:
        """The trade's id, when the gateway sent it."""
        v = self.details.get("trade_id")
        return v.lower() if isinstance(v, str) else None


class UnknownOrEnded(CrxError):
    """The RFQ ended, or the seat has no such leg on it. There is nothing to drop."""
    code = "unknown_or_ended"


class TradeUnknown(CrxError):
    """The trade may be on the chain. Do not trade again. Read ``positions()`` after the time in the message."""
    code = "trade_unknown"


class DepositFailed(CrxError):
    """The deposit in ``details['txs']`` failed and is not credited. Read ``balance()`` before you deposit again."""
    code = "deposit_failed"


class TradeInFlight(CrxError):
    """A trade of the seat is still opening. Withdraw again once it is ``open`` or ``refused``."""
    code = "trade_in_flight"


class WithdrawInProgress(CrxError):
    """Another withdraw is in progress. Withdraw again once it is paid."""
    code = "withdraw_in_progress"


class WithdrawUnknown(CrxError):
    """The withdraw may still be sent. Call ``withdraw()`` with the same amount to read its status."""
    code = "withdraw_unknown"


class EarlierOutcome(CrxError):
    """An earlier ``accept()`` or ``withdraw()`` got its final answer, in ``details``. This call signed nothing."""
    code = "earlier_outcome"


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
    "service_unavailable": ServiceUnavailable,
    "seat_cannot_sign": SeatCannotSign,
    "trade_in_flight": TradeInFlight,
    "withdraw_in_progress": WithdrawInProgress,
    "upstream": ServerError,
    "internal": ServerError,
    "timeout": ServerError,
}


def gateway_code(body: Any) -> str:
    """Return the gateway's ``code``, else an ``error`` that is a known code."""
    if not isinstance(body, dict):
        return ""
    code, error = body.get("code"), body.get("error")
    if not code and isinstance(error, str) and error in _BY_GATEWAY_CODE:
        code = error
    return clean(code or "", 64)


_NOT_TEXT = re.compile(r"<(script|style)\b.*?</\1\s*>|<!--.*?-->|<[^>]*>", re.I | re.S)


def first_line(text: str, limit: int = 200) -> str:
    """Return the first non-blank line of a text or HTML body, without tags, cut to ``limit``."""
    if text.lstrip().startswith("<"):
        text = html.unescape(_NOT_TEXT.sub("\n", text))
    for line in text.splitlines():
        line = clean(line, 10_000).strip()
        if line:
            return line[:limit]
    return ""


RETRY_AFTER_MAX = 86400  # seconds


def retry_after_secs(value: Any) -> int | None:
    """Return the seconds a Retry-After value names, capped at ``RETRY_AFTER_MAX``. A date without a zone is UTC."""
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


# Matches ``17:05 UTC``, ``Tue 02:34 UTC`` and ``Sun 4 Oct 23:00 UTC``.
_UTC_TIME = re.compile(
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) )?)?"
    r"\d{2}:\d{2}(?::\d{2})? UTC")


def utc_time(*texts: Any) -> str | None:
    """Return the first UTC time that ``texts`` name, as written, or None."""
    for t in texts:
        m = _UTC_TIME.search(t) if isinstance(t, str) else None
        if m:
            return m.group(0)
    return None


def wait_secs(details: dict) -> float | None:
    """Return ``details['retry_after_secs']`` when it is a number from 0 to ``RETRY_AFTER_MAX``, else None."""
    try:
        s = float(details.get("retry_after_secs"))
    except (TypeError, ValueError):
        return None
    return s if 0 <= s <= RETRY_AFTER_MAX else None


def from_gateway(status: int, body: Any, text: str = "", *, host: str = "", retry_after: Any = None) -> CrxError:
    """Map one gateway refusal to a typed error, with the gateway's message and the time to call again."""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        # A JSON-RPC error object: its message is the gateway's words; its code goes to details['rpc_code'].
        rpc = body["error"]
        code = rpc.get("code")
        body = {"message": rpc.get("message"),
                "details": {"rpc_code": code} if isinstance(code, int) and not isinstance(code, bool) else {}}
    if isinstance(body, dict):
        gw = gateway_code(body) or None
        said, why = (v if isinstance(v, str) and v.strip() else None for v in (body.get("message"), body.get("error")))
        if said and why and said.strip() != why.strip():
            msg = f"{clean(said)} ({clean(why)})"
        else:
            msg = clean(said or why or body.get("detail") or f"HTTP {status}")
        details = dict(body["details"]) if isinstance(body.get("details"), dict) else {}
        if said and why:
            details.setdefault("error", clean(why))
        if "retry_after_secs" not in details and wait_secs(body) is not None:
            details["retry_after_secs"] = body["retry_after_secs"]
        at = utc_time(said, why)
        if at is not None:
            details.setdefault("time_utc", at)
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
