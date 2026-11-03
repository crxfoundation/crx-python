"""Objects the client returns. Most keep the gateway's full answer on ``raw``."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable


def dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def ms_to_dt(value: Any) -> datetime | None:
    """Return unix milliseconds as a UTC datetime, or None for a value that is not a positive int in range."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, ValueError, OSError):
        return None


def side_word(side: Any) -> str | None:
    return {1: "buy", -1: "sell"}.get(side) if isinstance(side, int) else None


@dataclass(frozen=True)
class Market:
    """One currency pair as this client's chain offers it.

    Attributes:
        pair_id: keccak256 of ``pair``, as 0x hex.
        paused: True when the pair is paused on this chain, or the chain does not offer it.
        max_premium_bps: The largest premium a quote can carry, in basis points.
        next_long_close: Start and end of the pair's next long close, UTC. The end includes the reopen period.
    """

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
    next_long_close: tuple[datetime, datetime] | None = None
    _clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)

    def next_valid_end(self, min_secs: int) -> datetime:
        """Return the time ``min_secs`` from now, UTC, moved to the end of ``next_long_close`` if inside it."""
        t = datetime.fromtimestamp(self._clock(), timezone.utc) + timedelta(seconds=min_secs)
        if self.next_long_close is not None and self.next_long_close[0] <= t < self.next_long_close[1]:
            return self.next_long_close[1]
        return t


@dataclass(frozen=True)
class Quote:
    """A firm maker quote on the client's RFQ. Pass it to ``Client.accept`` before ``closes_at``.

    Attributes:
        expiry: The settlement time, UTC. ``settles_at`` is the same value.
        expires_at: The quote end, UTC. The maker is bound until then.
        closes_at: The RFQ's end, 120 seconds after the request. The gateway takes no accept after it.
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

    @property
    def settles_at(self) -> datetime | None:
        """The settlement time, UTC. Same as ``expiry``."""
        return self.expiry


@dataclass(frozen=True)
class Ask:
    """An open RFQ from ``Client.ask``. Call ``quote()`` for its winning quote.

    Attributes:
        side: The client's side in the base currency.
        expiry: The settlement time, UTC. ``settles_at`` is the same value.
    """

    rfq_id: str
    pair: str
    side: str
    notional: Decimal
    expiry: datetime | None
    raw: dict = field(repr=False, compare=False)
    rfq: dict = field(repr=False, compare=False)
    _client: Any = field(repr=False, compare=False)

    @property
    def settles_at(self) -> datetime | None:
        """The settlement time, UTC. Same as ``expiry``."""
        return self.expiry

    def quote(self, wait: float = 30.0) -> Quote:
        """Return the winning quote. Nothing is accepted.

        The gateway names a winner after the 10-second quote window. With no winner when ``wait``
        ends, the best live quote returns. Pass the result to ``Client.accept``.

        Args:
            wait: Seconds to poll at most. Default 30.

        Raises:
            NoQuotes: No maker quoted before the wait ended.
            RfqCancelled: The gateway cancelled the RFQ. ``reason`` says why.
        """
        return self._client._winner(self.rfq, wait)


def _provisional(view: Any) -> bool:
    """Return True when an RFQ view marks its open trade ``trade_provisional``."""
    return (isinstance(view, dict) and view.get("trade_status") == "open"
            and view.get("trade_provisional") not in (None, False))


def _final(row: Any, status: str = "trade_status", mark: str = "trade_final") -> bool:
    """Return True when ``row`` is ``open`` and its ``mark`` key is exactly True. No retraction can follow."""
    return isinstance(row, dict) and row.get(status) == "open" and row.get(mark) is True


@dataclass(frozen=True)
class Trade:
    """A trade from ``Client.accept`` or ``Client.confirm``.

    Attributes:
        status: ``sending`` while CRX sends the transaction, ``open`` once the trade counts, ``pending``
            until ``trade.opened`` or ``trade.refused`` arrives, or ``refused``.
        tx: The transaction hash once it lands.
        provisional: True while the gateway marks the open as provisional.
        final: True once the gateway marks the trade final. Until then a reorg can retract an ``open`` trade.
    """

    status: str
    rfq_id: str
    quote_id: str
    pair: str
    side: str
    notional: Decimal
    rate: Decimal
    tx: str | None
    raw: dict = field(repr=False, compare=False)
    provisional: bool = False
    final: bool = False


@dataclass(frozen=True)
class Balance:
    """The seat's collateral, margin and withdraw state. Amounts are in USDC.

    Attributes:
        free: Collateral that new trades and withdraws can use.
        im: The initial margin the open trades hold.
        mm: The maintenance margin of the open trades.
        withdraw_nonce: The nonce the next withdraw signs.
    """

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
    """One row of ``Client.positions()``.

    The mark fields are None on a row with no mark: settled, not open yet, or no market price.

    Attributes:
        final: True once the row is ``open`` and final. No ``trade.retracted`` can follow.
        entry_price: The rate the position opened at.
        mark_price: The forward rate to the position's settlement on CRX's latest curve, at ``mark_time`` (UTC).
        unrealized_pnl: The signed P&L from ``entry_price`` to ``mark_price``, in USDC, from the seat's side.
        settles_at: The settlement time, UTC.
    """

    trade_id: str
    rfq_id: str
    pair: str
    side: str | None
    notional: Decimal | None
    rate: Decimal | None
    status: str | None
    raw: dict = field(repr=False, compare=False)
    final: bool = False
    entry_price: Decimal | None = None
    mark_price: Decimal | None = None
    mark_time: datetime | None = None
    unrealized_pnl: Decimal | None = None
    settles_at: datetime | None = None


@dataclass(frozen=True)
class Viewer:
    """A wallet with read access to the seat, granted by ``granted_by`` at ``granted_at``."""

    address: str
    granted_by: str | None
    granted_at: datetime | None
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class Event:
    """One event from ``Client.trades()``. Pass the last ``seq`` to ``trades(after=)`` to read newer events."""

    type: str
    seq: int | None
    ts: datetime | None
    data: dict
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class Deposit:
    """The result of ``Client.deposit``.

    Attributes:
        status: ``credited`` or ``pending``.
        counted: True once the deposit is final and counts in free collateral. A ``credited`` deposit can still wait.
    """

    amount: Decimal
    txs: list[str]
    status: str
    counted: bool = False


@dataclass(frozen=True)
class Withdraw:
    """The result of ``Client.withdraw``.

    Attributes:
        status: ``sending``, ``pending`` or ``accepted`` while in progress. Final: ``paid``, ``partial``,
            ``refused`` or ``returned``.
        item: The chain's id of the signed withdraw.
        tx: The transaction hash once it lands.
    """

    amount: Decimal
    nonce: int
    item: str
    tx: str | None
    status: str


def _side_of(value: Any) -> str | None:
    """Return ``buy`` or ``sell`` for a side of +1 or -1."""
    return side_word(value) if not isinstance(value, bool) else None


@dataclass(frozen=True)
class Rfq:
    """An RFQ as the seat sees it, from ``Client.rfqs()`` or ``Client.rfq()``.

    Attributes:
        side: The seat's side in the base currency. A maker's side is the opposite of the taker's.
        expiry: The settlement time, UTC. ``settles_at`` is the same value.
        quote_expiry_max: The latest quote end the gateway takes. A quote binds the maker until its end.
        closes_at: The RFQ's end. The gateway takes no accept after it.
        client_rfq_id: The taker's id for the RFQ. Only the taker sees it.
        quotes: The quote rows from ``Client.rfq()``: the winner for the taker, the maker's quotes for a maker.
        seq: The place of the ``rfq.opened`` event in the client's tape. Pass it to ``rfqs(after=)``.
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
    def settles_at(self) -> datetime | None:
        """The settlement time, UTC. Same as ``expiry``."""
        return self.expiry

    @property
    def test_request(self) -> bool:
        """True for a test request sent to one maker."""
        return self.raw.get("test_request") is True

    @property
    def request_expires_at(self) -> datetime | None:
        """The test request's deadline. A renewed RFQ keeps it."""
        return ms_to_dt(self.raw.get("request_expires_at"))

    @property
    def reference_rate(self) -> Decimal | None:
        """The reference rate from ``test_request()``, or None."""
        value = dec(self.raw.get("reference_rate"))
        return value if value is not None and value.is_finite() and value > 0 else None

    @property
    def own(self) -> bool:
        """True on the seat's own RFQ, the one that carries its ``client_rfq_id``."""
        cid = self.raw.get("client_rfq_id")
        return isinstance(cid, str) and cid != ""

    @property
    def taker_side(self) -> str | None:
        """The taker's side in the base currency."""
        if self.side is None:
            return None
        if self.own:
            return self.side
        return "sell" if self.side == "buy" else "buy"


@dataclass(frozen=True)
class MakerQuote:
    """A binding quote from ``Client.send_quote``. Pass it to ``Client.confirm``.

    The quote binds the maker until ``quote_expiry``. ``Client.drop_quote`` ends it sooner.

    Attributes:
        side: The maker's side in the base currency.
        expiry: The settlement time, UTC. ``settles_at`` is the same value.
        expires_at: The quote's expiry as the gateway reports it.
        leg: The half of the trade the maker signed.
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
    def settles_at(self) -> datetime | None:
        """The settlement time, UTC. Same as ``expiry``."""
        return self.expiry

    @property
    def leg_id(self) -> str:
        """The id of the leg this quote is on."""
        return self.leg["leg_id"]

    @property
    def quote_expiry(self) -> datetime | None:
        """The last instant the quote binds the maker, read from its leg id."""
        end = self.leg.get("quote_expiry")
        return ms_to_dt(end * 1000) if isinstance(end, int) and not isinstance(end, bool) else None


@dataclass(frozen=True)
class Drop:
    """A leg dropped by ``Client.drop_quote``, with every quote on it. ``at`` is the drop time."""

    rfq_id: str
    leg_id: str
    at: datetime | None
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class Close:
    """The result of ``Client.close_trade``.

    Attributes:
        to: The seat that takes the trade over, or the counterparty that closes it.
        rate: The close rate.
        status: ``sending`` while CRX sends the transaction, ``pending`` until the hourly check counts it,
            ``closed``, or ``refused``. ``unsigned`` when the gateway took no signature before the wait ended.
        item: The chain's id of the signed close.
        tx: The transaction hash once it lands.
        final: True once the close is final.
        counterparty: True when the trade closes with its counterparty and ends for both seats.
    """

    trade_id: str
    rfq_id: str
    to: str
    rate: Decimal
    status: str
    item: str | None
    tx: str | None
    raw: dict = field(repr=False, compare=False)
    final: bool = False
    counterparty: bool = False


@dataclass(frozen=True)
class TakeOver:
    """The result of ``Client.take_over``.

    Attributes:
        trade_id: The trade the seat takes over, once the tape names it. ``positions()`` lists it once ``open``.
        side: The seat's side in the base currency.
        rate: The seat's quoted rate.
        status: ``pending`` until the trade moves to the seat, ``open``, or ``refused``. ``unsigned`` when the
            gateway took no signature inside the round's sign window.
    """

    rfq_id: str
    quote_id: str
    trade_id: str | None
    pair: str
    side: str
    notional: Decimal
    rate: Decimal
    status: str
    raw: dict = field(repr=False, compare=False)
