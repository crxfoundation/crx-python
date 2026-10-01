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


@dataclass(frozen=True)
class Quote:
    """A firm maker quote on your open RFQ. Pass it to ``Client.trade``."""

    rfq_id: str
    quote_id: str
    pair: str
    side: str
    notional: Decimal
    rate: Decimal
    expiry: datetime
    expires_at: datetime | None
    house: bool
    raw: dict = field(repr=False, compare=False)
    rfq: dict = field(repr=False, compare=False)
    expiry_ms: int = field(repr=False, default=0)


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
    ``client_rfq_id`` is served to the RFQ's own taker only. ``quotes`` holds the quote
    rows ``Client.rfq()`` reads: every desk's quote for the RFQ's taker, the seat's own
    quotes for a maker. ``seq`` is the tape position of an ``rfq.opened`` frame.
    """

    rfq_id: str
    pair: str
    side: str | None
    notional: Decimal | None
    expiry: datetime | None
    quote_expiry: datetime | None
    im_bps: int | None
    premium_bps: int | None
    kind: str
    status: str | None
    opened_at: datetime | None
    closes_at: datetime | None
    client_rfq_id: str | None
    seq: int | None
    quotes: tuple = field(repr=False, compare=False, default=())
    raw: dict = field(repr=False, compare=False, default_factory=dict)

    @property
    def taker_side(self) -> str | None:
        """The taker's side. The taker's own view carries no ``join_ref``: it has no counterparty yet."""
        if self.side is None:
            return None
        if self.raw.get("join_ref") is None:
            return self.side
        return "sell" if self.side == "buy" else "buy"

    @property
    def sign_mode(self) -> str:
        """What a maker signs on this RFQ: ``quote`` (a binding quote, nothing after the accept)
        or ``side`` (a Leg, then a Side after the accept). An RFQ that names none is ``side``."""
        return "quote" if self.raw.get("sign_mode") == "quote" else "side"

    @property
    def house_rate(self) -> Decimal | None:
        """The rate of the best live house quote in ``quotes``; None when no house quote shows.

        Only the RFQ's taker reads other desks' quotes.
        """
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        for q in self.quotes:
            if not isinstance(q, dict) or q.get("house") is not True:
                continue
            exp = q.get("expires_at")
            if str(q.get("status") or "").lower() != "quoted" or not isinstance(exp, int) or exp <= now_ms:
                continue
            rate = dec(q.get("rate"))
            if rate is not None and rate > 0:
                return rate
        return None


@dataclass(frozen=True)
class MakerQuote:
    """Your firm quote on an open RFQ, as ``Client.send_quote`` posted it. Pass it to ``Client.confirm``.

    ``side`` is your own side. ``expiry`` is the settlement instant. ``expires_at`` is
    the quote's own expiry. ``leg`` is the half you signed: your Leg, or the terms of
    your binding quote. ``sign_mode`` is ``quote`` for a binding quote, else ``side``.
    A binding quote binds you until ``quote_expiry``: ``Client.drop_quote`` ends it.
    """

    rfq_id: str
    quote_id: str
    pair: str
    side: str
    notional: Decimal
    rate: Decimal
    expiry: datetime | None
    expires_at: datetime | None
    client_quote_id: str
    raw: dict = field(repr=False, compare=False)
    leg: dict = field(repr=False, compare=False)
    rfq: Rfq = field(repr=False, compare=False)
    sign_mode: str = "side"

    @property
    def leg_id(self) -> str:
        """The id of the leg this quote is on."""
        return self.leg["leg_id"]

    @property
    def quote_expiry(self) -> datetime | None:
        """The last instant the signed half is good for."""
        return ms_to_dt(self.leg.get("quote_expiry"))


@dataclass(frozen=True)
class Drop:
    """A dropped leg, from ``Client.drop_quote``: every quote on it is ended. ``at`` is the drop time."""

    rfq_id: str
    leg_id: str
    at: datetime | None
    raw: dict = field(repr=False, compare=False)
