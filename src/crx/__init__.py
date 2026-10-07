"""CRX Python SDK: quote, trade, deposit and withdraw on the CRX FX forwards venue."""

from .client import NETWORKS, Client
from .errors import (
    AboveMax, AlreadyAccepted, AuthError, BadAnswer, BadRequest, BelowMin, ConfigError, CrxError, Declined,
    InsufficientCollateral, LegIdTaken, LegLive, MarkUnavailable, MarketClosed, MarketPaused, NetworkError, NoQuotes,
    NotWhitelisted, OwnRoundOpen, PositionMatured, QuoteDropped, QuoteExpired, QuoteFillsFull, QuoteFormatOutdated,
    QuoteLost, QuoteNotYours, RateLimited, RateOutOfBand, RefusedToSign, Rejected, RelayUnavailable, RfqCancelled,
    SeatCannotSign, SeatNotReady, SendUnknown, ServerError, ServiceUnavailable, TradeUnknown, TxFailed,
    UnknownOrEnded,
)
from ._solana import BindFailed, BindInProgress, SeatBoundOtherPayout, SeatStopped
from .models import (
    Ask, Balance, Deposit, Drop, Event, MakerQuote, Market, Position, Quote, Rfq, Trade, Viewer, Withdraw,
)

__version__ = "0.2.2"

__all__ = [
    "Client", "NETWORKS", "__version__",
    "Ask", "Balance", "Deposit", "Drop", "Event", "MakerQuote", "Market", "Position", "Quote", "Rfq", "Trade",
    "Viewer", "Withdraw",
    "CrxError", "AboveMax", "AlreadyAccepted", "AuthError", "BadAnswer", "BadRequest", "BelowMin", "ConfigError",
    "Declined", "MarkUnavailable", "PositionMatured", "RateOutOfBand",
    "InsufficientCollateral", "LegIdTaken", "LegLive", "MarketClosed", "MarketPaused", "NetworkError", "NoQuotes",
    "NotWhitelisted", "OwnRoundOpen", "QuoteDropped", "QuoteExpired", "QuoteFillsFull", "QuoteFormatOutdated",
    "QuoteLost", "QuoteNotYours", "RateLimited", "RefusedToSign", "Rejected", "RfqCancelled",
    "SeatCannotSign", "SeatNotReady", "SeatStopped", "SendUnknown", "ServerError", "ServiceUnavailable",
    "TradeUnknown", "TxFailed", "UnknownOrEnded",
    "BindFailed", "BindInProgress", "SeatBoundOtherPayout",
]
