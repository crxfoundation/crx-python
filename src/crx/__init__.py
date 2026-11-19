"""CRX Python SDK: quote, trade, deposit and withdraw on the CRX FX forwards venue."""

from .client import NETWORKS, Client
from .errors import (
    AboveMax, AlreadyAccepted, AuthError, BadAnswer, BadRequest, BelowMin, ConfigError, CrxError, Declined,
    DepositFailed, EarlierOutcome, InsufficientCollateral, LegIdTaken, LegLive, MarkUnavailable, MarketClosed,
    MarketPaused, NetworkError, NoQuotes, NotWhitelisted, OwnRoundOpen, PositionMatured, QuoteDropped, QuoteExpired,
    QuoteFillsFull, QuoteFormatOutdated, QuoteLost, QuoteNotYours, QuoteRejected, RateLimited, RateOutOfBand,
    RefusedToSign, Rejected, RfqCancelled, SeatCannotSign, SeatNotReady, SendUnknown, ServerError,
    ServiceUnavailable, TradeInFlight, TradeUnknown, TxFailed, UnknownOrEnded, WithdrawInProgress, WithdrawUnknown,
)
from .models import (
    Ask, Balance, Close, Deposit, Drop, Event, MakerQuote, Market, Position, Quote, Rfq, Trade, Viewer, Withdraw,
    pair_label,
)

__version__ = "0.4.0"

__all__ = [
    "Client", "NETWORKS", "__version__", "pair_label",
    "Ask", "Balance", "Close", "Deposit", "Drop", "Event", "MakerQuote", "Market", "Position", "Quote", "Rfq",
    "Trade", "Viewer", "Withdraw",
    "CrxError", "AboveMax", "DepositFailed", "EarlierOutcome", "AlreadyAccepted", "AuthError", "BadAnswer",
    "BadRequest", "BelowMin", "ConfigError", "Declined", "MarkUnavailable", "PositionMatured", "RateOutOfBand",
    "InsufficientCollateral", "LegIdTaken", "LegLive", "MarketClosed", "MarketPaused", "NetworkError", "NoQuotes",
    "NotWhitelisted", "OwnRoundOpen", "QuoteDropped", "QuoteExpired", "QuoteFillsFull", "QuoteFormatOutdated",
    "QuoteLost", "QuoteNotYours", "QuoteRejected", "RateLimited", "RefusedToSign", "Rejected", "RfqCancelled",
    "SeatCannotSign", "SeatNotReady", "SendUnknown", "ServerError", "ServiceUnavailable",
    "TradeInFlight", "TradeUnknown", "TxFailed", "UnknownOrEnded", "WithdrawInProgress", "WithdrawUnknown",
]
