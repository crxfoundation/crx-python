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
    confirmation: Confirmation | None = None

    @classmethod
    def from_wire(cls, raw: dict) -> Event:
        """Parse an event frame and its typed confirmation, when present."""
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        confirmation = None
        if raw.get("type") == "trade.confirmation":
            try:
                confirmation = Confirmation.from_wire(data)
            except ValueError:
                pass
        seq = raw.get("seq")
        return cls(
            type=str(raw.get("type")), seq=seq if isinstance(seq, int) else None,
            ts=ms_to_dt(raw.get("ts")), data=data, raw=raw, confirmation=confirmation,
        )


@dataclass(frozen=True)
class Confirmation:
    """One party's trade confirmation. ``raw`` keeps every field from the gateway."""

    confirmation_id: str
    trade_id: str
    rfq_id: str | None
    leg: str | None
    status: str
    voided_at: int | None
    suspended_text: str | None
    void_text: str | None
    revision: int | None
    content_hash: str | None
    template_version: str | None
    issued_at: int | None
    issued_at_iso: str | None
    party: dict
    counterparty: dict
    dealer_text: str | None
    opening_final: bool | None
    opening_text: str | None
    trade_date: str | None
    execution_time: int | None
    execution_time_iso: str | None
    product: str | None
    settlement_type: str | None
    product_note_text: str | None
    pair: str | None
    base_currency: str | None
    quote_currency: str | None
    reference_currency: str | None
    reference_currency_text: str | None
    notional: dict
    reference_notional: dict | None
    usd_notional: str | None
    usd_notional_text: str | None
    forward_rate: str | None
    premium_bps: int | None
    premium_text: str | None
    buyer: str | None
    seller: str | None
    buyer_seller_text: str | None
    settlement_currency: str | None
    settlement_currency_text: str | None
    settlement_time: int | None
    settlement_time_iso: str | None
    settlement_time_text: str | None
    valuation_date: str | None
    valuation_date_text: str | None
    settlement_date: str | None
    settlement_date_text: str | None
    settlement_rate: dict
    business_days: dict
    settlement_amount: Any
    settlement_amount_direction: str | None
    settlement_payment_text: str | None
    settlement_shortfall_text: str | None
    fallback: str | None
    delisting_text: str | None
    early_end_text: str | None
    calculation_agent: str | None
    governing_terms: str | None
    rules_version_text: str | None
    confirmation_statement: str | None
    not_provided: str | None
    open_inputs: list
    open_input_labels: dict
    uti: str | None
    uti_text: str | None
    reporting_status: str | None
    reporting_counterparty: str | None
    clearing_status: str | None
    delivery: dict
    raw: dict = field(repr=False, compare=False)

    @classmethod
    def from_wire(cls, raw: dict) -> Confirmation:
        """Parse a confirmation while retaining unknown fields in ``raw``."""
        if not isinstance(raw, dict):
            raise ValueError("confirmation is not an object")
        cid, tid, status = raw.get("confirmation_id"), raw.get("trade_id"), raw.get("status")
        if not isinstance(cid, str) or not isinstance(tid, str) or not isinstance(status, str):
            raise ValueError("confirmation is missing its id, trade id or status")
        def text(name: str) -> str | None:
            value = raw.get(name)
            return value if isinstance(value, str) else None
        def integer(name: str) -> int | None:
            value = raw.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) else None
        revision = integer("revision")
        premium = integer("premium_bps")
        opening_final = raw.get("opening_final") if isinstance(raw.get("opening_final"), bool) else None
        return cls(
            confirmation_id=cid, trade_id=tid, rfq_id=text("rfq_id"), leg=text("leg"), status=status,
            voided_at=integer("voided_at"), suspended_text=text("suspended_text"), void_text=text("void_text"),
            revision=revision, content_hash=text("content_hash"),
            template_version=text("template_version"), issued_at=integer("issued_at"), issued_at_iso=text("issued_at_iso"),
            party=raw.get("party") if isinstance(raw.get("party"), dict) else {},
            counterparty=raw.get("counterparty") if isinstance(raw.get("counterparty"), dict) else {},
            dealer_text=text("dealer_text"), opening_final=opening_final, opening_text=text("opening_text"),
            trade_date=text("trade_date"), execution_time=integer("execution_time"), execution_time_iso=text("execution_time_iso"),
            product=text("product"), settlement_type=text("settlement_type"), product_note_text=text("product_note_text"), pair=text("pair"),
            base_currency=text("base_currency"), quote_currency=text("quote_currency"), reference_currency=text("reference_currency"),
            reference_currency_text=text("reference_currency_text"), notional=raw.get("notional") if isinstance(raw.get("notional"), dict) else {},
            reference_notional=raw.get("reference_notional") if isinstance(raw.get("reference_notional"), dict) else None,
            usd_notional=text("usd_notional"), usd_notional_text=text("usd_notional_text"), forward_rate=text("forward_rate"),
            premium_bps=premium, premium_text=text("premium_text"), buyer=text("buyer"), seller=text("seller"),
            buyer_seller_text=text("buyer_seller_text"), settlement_currency=text("settlement_currency"),
            settlement_currency_text=text("settlement_currency_text"), settlement_time=integer("settlement_time"),
            settlement_time_iso=text("settlement_time_iso"), settlement_time_text=text("settlement_time_text"),
            valuation_date=text("valuation_date"), valuation_date_text=text("valuation_date_text"),
            settlement_date=text("settlement_date"), settlement_date_text=text("settlement_date_text"),
            settlement_rate=raw.get("settlement_rate") if isinstance(raw.get("settlement_rate"), dict) else {},
            business_days=raw.get("business_days") if isinstance(raw.get("business_days"), dict) else {},
            settlement_amount=raw.get("settlement_amount"), settlement_amount_direction=text("settlement_amount_direction"),
            settlement_payment_text=text("settlement_payment_text"), settlement_shortfall_text=text("settlement_shortfall_text"),
            fallback=text("fallback"), delisting_text=text("delisting_text"), early_end_text=text("early_end_text"),
            calculation_agent=text("calculation_agent"),
            governing_terms=text("governing_terms"), rules_version_text=text("rules_version_text"),
            confirmation_statement=text("confirmation_statement"), not_provided=text("not_provided"),
            open_inputs=raw.get("open_inputs") if isinstance(raw.get("open_inputs"), list) else [],
            open_input_labels=raw.get("open_input_labels") if isinstance(raw.get("open_input_labels"), dict) else {},
            uti=text("uti"), uti_text=text("uti_text"), reporting_status=text("reporting_status"),
            reporting_counterparty=text("reporting_counterparty"), clearing_status=text("clearing_status"),
            delivery=raw.get("delivery") if isinstance(raw.get("delivery"), dict) else {}, raw=raw,
        )


@dataclass(frozen=True)
class ConfirmationPage:
    """One page of confirmations. Pass ``cursor`` as ``after`` for the next page."""

    confirmations: list[Confirmation]
    cursor: int

    def __iter__(self):
        return iter(self.confirmations)

    def __len__(self) -> int:
        return len(self.confirmations)


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

