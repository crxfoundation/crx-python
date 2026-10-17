"""Fixed test-request terms shared by the maker client and acceptance worker."""

from decimal import Decimal, InvalidOperation
import re

from ._bind import QUOTE_WINDOW
from .errors import BadAnswer

COLLECTION_SECONDS = 3 * 3600
SETTLEMENT_SECONDS = 5 * 3600
WORD = re.compile(r"0x[0-9a-fA-F]{64}\Z")
DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")


def word(value) -> bool:
    return isinstance(value, str) and WORD.fullmatch(value) is not None


def rate(value) -> Decimal:
    """A positive decimal rate that the standard quote signer can encode."""
    if not isinstance(value, str) or DECIMAL.fullmatch(value) is None:
        raise BadAnswer("the test request has no decimal reference rate")
    try:
        result = Decimal(value)
        scaled = result.scaleb(6)
        if not result.is_finite() or result <= 0 or scaled != scaled.to_integral_value() or scaled > 2**64 - 1:
            raise ValueError
    except (ValueError, InvalidOperation):
        raise BadAnswer("the test request reference rate is out of range") from None
    return result


def validate(d: dict, chain: str, now: float, *, maker: bool, event: bool = False,
             require_reference: bool = True) -> None:
    """Reject changed terms before any trade signature. Maker responses require a reference rate."""
    if not isinstance(d, dict) or d.get("test_request") is not True:
        raise BadAnswer("the response is not a test request")
    if (not word(d.get("rfq_id")) or d.get("chain") != chain or d.get("pair") not in ("USD/MXN", "USDMXN")
            or type(d.get("side")) is not int or d["side"] != (-1 if maker else 1)
            or d.get("kind", "open") != "open"):
        raise BadAnswer("the test request has different trade terms")
    amount = d.get("notional")
    if not isinstance(amount, str) or DECIMAL.fullmatch(amount) is None or Decimal(amount) != 100:
        raise BadAnswer("the test request notional must be 100 USD")
    premium = d.get("premium_bps", 0)
    if type(premium) is not int or premium != 0:
        raise BadAnswer("the test request premium must be zero")
    expiry, closes = d.get("expiry"), d.get("closes_at")
    if (type(expiry) is not int or type(closes) is not int or closes <= now * 1000
            or closes > (now + COLLECTION_SECONDS + 30) * 1000
            or expiry - closes != (SETTLEMENT_SECONDS - COLLECTION_SECONDS) * 1000):
        raise BadAnswer("the test request has an invalid time window")
    if d.get("status", "open" if event else None) not in ("open", "quoted"):
        raise BadAnswer("the test request is closed")
    end = d.get("quote_expiry_max")
    if type(end) is not int or end <= 0 or not word(d.get("taker_ref")):
        raise BadAnswer("the test request has invalid quote terms")
    if not event and not now < end <= now + QUOTE_WINDOW:
        raise BadAnswer("the test request quote window is not current")
    cid = d.get("client_rfq_id")
    if maker:
        if cid is not None:
            raise BadAnswer("the test request is not a maker request")
        if require_reference or "reference_rate" in d:
            rate(d.get("reference_rate"))
    elif "client_rfq_id" not in d or (cid is not None and (not isinstance(cid, str) or not cid)):
        raise BadAnswer("the test request does not belong to this account")
