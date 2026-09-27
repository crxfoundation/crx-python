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
class Trade:
    """``status`` is ``bound`` (arm on chain; the next fold opens the position)
    or ``accepted`` (the relay binds off chain)."""

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
    pending_deposit: Decimal | None
    open_legs: int | None
    withdraw_live: bool | None
    withdraw_nonce: int | None
    as_of: datetime | None
    as_of_fold: Any
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
    amount: Decimal
    txs: list[str]


@dataclass(frozen=True)
class Withdraw:
    amount: Decimal
    nonce: int
    tx: str
