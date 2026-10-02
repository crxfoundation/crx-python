"""Return types. Each keeps the gateway's full answer on ``raw``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def ms_to_dt(value: Any) -> datetime | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc)


def side_word(side: Any) -> str | None:
    return {1: "buy", -1: "sell"}.get(side) if isinstance(side, int) else None


@dataclass(frozen=True)
class Market:
    """One pair on this client's chain. ``pair_id`` is keccak256 of ``pair``. ``paused`` is True when the
    pair is not offered on this chain, or its chain row reads ``paused: true``; a row with no ``paused``
    reads as not paused. ``max_premium_bps`` is the chain's premium cap; None when not served."""

    pair: str
    pair_id: str
    base: str
    quote: str
    open: bool
    paused: bool
    min_notional: Decimal | None
    max_notional: Decimal | None
    next_open: datetime | None
    min_tenor_s: int | None
    max_tenor_s: int | None
    raw: dict = field(repr=False, compare=False)
    max_premium_bps: int | None = None


@dataclass(frozen=True)
class Quote:
    """A firm maker quote on your open RFQ. Pass it to ``Client.trade`` before ``closes_at``.

    ``expires_at`` is the quote end: the RFQ's quote end. ``closes_at`` is the RFQ's end, 120 s
    after the request: the gateway takes no accept after it. None when the gateway did not serve it.
    """

    rfq_id: str
    quote_id: str
    pair: str
    side: str
    notional: Decimal
    rate: Decimal
    expiry: datetime
    expires_at: datetime | None
    raw: dict = field(repr=False, compare=False)
    rfq: dict = field(repr=False, compare=False)
    expiry_ms: int = field(repr=False, default=0)
    closes_at: datetime | None = None


@dataclass(frozen=True)
class Ask:
    """Your open RFQ, as ``Client.ask`` opened it. ``quote()`` returns its winning quote.

    ``side`` is your side in the base currency. ``expiry`` is the settlement instant.
    """

    rfq_id: str
    pair: str
    side: str
    notional: Decimal
    expiry: datetime | None
    raw: dict = field(repr=False, compare=False)
    rfq: dict = field(repr=False, compare=False)
    _client: Any = field(repr=False, compare=False)

    def quote(self, wait: float = 30.0) -> Quote:
        """The winning quote, once the gateway names it after the 10 s window. Nothing is accepted.

        Polls for ``wait`` s at most; the best live quote goes at the end. No quote
        raises ``NoQuotes``; an RFQ the gateway cancelled raises ``RfqCancelled`` with its
        ``reason``. Pass the result to ``Client.trade``.
        """
        return self._client._winner(self.rfq, wait)


@dataclass(frozen=True)
class Trade:
    """``status`` is ``sending`` (CRX is sending the tx), ``open`` (the trade counts now),
    ``pending`` (not final yet; ``trade.opened`` or ``trade.refused`` follows) or ``refused``.
    ``tx`` is the landed tx; None before it lands."""

    status: str
    rfq_id: str
    quote_id: str
    pair: str
    side: str
    notional: Decimal
    rate: Decimal
    tx: str | None
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class Balance:
    account: str
    state: str | None
    collateral: Decimal | None
    free: Decimal | None
    equity: Decimal | None
    im: Decimal | None
    mm: Decimal | None
    open_legs: int | None
    withdraw_live: bool | None
    withdraw_nonce: int | None
    as_of: datetime | None
    as_of_block: int | None
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class Position:
    trade_id: str
    rfq_id: str
    pair: str
    side: str | None
    notional: Decimal | None
    rate: Decimal | None
    status: str | None
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class Viewer:
    """A wallet that may read your seat: ``address``, granted by ``granted_by`` at ``granted_at``."""

    address: str
    granted_by: str | None
    granted_at: datetime | None
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class Event:
    type: str
    seq: int | None
    ts: datetime | None
    data: dict
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class Deposit:
    """``status`` is ``credited``, ``pending`` or ``failed``."""

    amount: Decimal
    txs: list[str]
    status: str


@dataclass(frozen=True)
class Withdraw:
    """``status`` is ``sending``, ``accepted``, ``pending``, ``paid``, ``partial``, ``refused`` or ``returned``.
    ``item`` is the chain item id. ``tx`` is the landed tx; None before it lands."""

    amount: Decimal
    nonce: int
    item: str
    tx: str | None
    status: str


def _side_of(value: Any) -> str | None:
    """``buy`` or ``sell`` from a signed side (+1 or -1)."""
    return side_word(value) if not isinstance(value, bool) else None


@dataclass(frozen=True)
class Rfq:
    """An RFQ as this seat reads it: from ``Client.rfqs()`` or ``Client.rfq()``.

    ``side`` is this seat's own side in the base currency: a maker's side is the
    opposite of the taker's. ``taker_side`` is the taker's side. ``expiry`` is the
    settlement instant. ``closes_at`` is the end of the quote window: no accept after it.
    ``quote_expiry_max`` is the latest quote end the gateway takes: a quote binds until it.
    ``client_rfq_id`` is served to the RFQ's own taker only. ``quotes`` holds the quote
    rows ``Client.rfq()`` reads: the winning quote only for the RFQ's taker, the seat's own
    quotes for a maker. ``seq`` is the tape position of an ``rfq.opened`` frame.
    """

    rfq_id: str
    pair: str
    side: str | None
    notional: Decimal | None
    expiry: datetime | None
    quote_expiry_max: datetime | None
    premium_bps: int | None
    kind: str
    status: str | None
    closes_at: datetime | None
    client_rfq_id: str | None
    seq: int | None
    quotes: tuple = field(repr=False, compare=False, default=())
    raw: dict = field(repr=False, compare=False, default_factory=dict)

    @property
    def own(self) -> bool:
        """True on this seat's own RFQ: the gateway serves ``client_rfq_id`` to the RFQ's taker only."""
        cid = self.raw.get("client_rfq_id")
        return isinstance(cid, str) and cid != ""

    @property
    def taker_side(self) -> str | None:
        """The taker's side."""
        if self.side is None:
            return None
        if self.own:
            return self.side
        return "sell" if self.side == "buy" else "buy"


@dataclass(frozen=True)
class MakerQuote:
    """Your binding quote on an open RFQ, as ``Client.send_quote`` posted it. Pass it to ``Client.confirm``.

    ``side`` is your own side. ``expiry`` is the settlement instant. ``expires_at`` is
    the quote's own expiry, as the gateway answers it. ``leg`` is the half you signed.
    The quote binds you until ``quote_expiry``, the end its leg id carries:
    ``Client.drop_quote`` ends it sooner.
    """

    rfq_id: str
    quote_id: str
    pair: str
    side: str
    notional: Decimal
    rate: Decimal
    expiry: datetime | None
    expires_at: datetime | None
    client_quote_id: str | None
    raw: dict = field(repr=False, compare=False)
    leg: dict = field(repr=False, compare=False)
    rfq: Rfq = field(repr=False, compare=False)

    @property
    def leg_id(self) -> str:
        """The id of the leg this quote is on."""
        return self.leg["leg_id"]

    @property
    def quote_expiry(self) -> datetime | None:
        """The quote end: the last instant the signed half is good for, from its leg id."""
        end = self.leg.get("quote_expiry")
        return ms_to_dt(end * 1000) if isinstance(end, int) and not isinstance(end, bool) else None


@dataclass(frozen=True)
class Drop:
    """A dropped leg, from ``Client.drop_quote``: every quote on it is ended. ``at`` is the drop time."""

    rfq_id: str
    leg_id: str
    at: datetime | None
    raw: dict = field(repr=False, compare=False)
