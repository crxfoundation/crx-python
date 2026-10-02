"""CRX Python SDK: quote, trade, deposit and withdraw on the CRX FX forwards venue."""

from .client import NETWORKS, Client
from .errors import (
    AboveMax, AlreadyAccepted, AuthError, BadAnswer, BadRequest, BelowMin, ConfigError, CrxError, Declined,
    InsufficientCollateral, LegIdTaken, LegLive, MarkUnavailable, MarketClosed, MarketPaused, NetworkError, NoQuotes,
    NotWhitelisted, OwnRoundOpen, PositionMatured, QuoteDropped, QuoteExpired, QuoteFillsFull, QuoteLost,
    QuoteNotYours, RateLimited, RateOutOfBand, RefusedToSign, Rejected, RfqCancelled, SeatNotReady, ServerError,
    TradeUnknown, TxFailed, UnknownOrEnded,
)
from .models import (
    Ask, Balance, Deposit, Drop, Event, MakerQuote, Market, Position, Quote, Rfq, Trade, Viewer, Withdraw,
)

__version__ = "0.5.0"

__all__ = [
    "Client", "NETWORKS", "__version__",
    "Ask", "Balance", "Deposit", "Drop", "Event", "MakerQuote", "Market", "Position", "Quote", "Rfq", "Trade",
    "Viewer", "Withdraw",
    "CrxError", "AboveMax", "AlreadyAccepted", "AuthError", "BadAnswer", "BadRequest", "BelowMin", "ConfigError",
    "Declined", "MarkUnavailable", "PositionMatured", "RateOutOfBand",
    "InsufficientCollateral", "LegIdTaken", "LegLive", "MarketClosed", "MarketPaused", "NetworkError", "NoQuotes",
    "NotWhitelisted", "OwnRoundOpen", "QuoteDropped", "QuoteExpired", "QuoteFillsFull", "QuoteLost", "QuoteNotYours",
    "RateLimited", "RefusedToSign", "Rejected", "RfqCancelled",
    "SeatNotReady", "ServerError", "TradeUnknown", "TxFailed", "UnknownOrEnded",
]
