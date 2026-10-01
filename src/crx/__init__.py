"""CRX Python SDK: quote, trade, deposit and withdraw on the CRX FX forwards venue."""

from .client import NETWORKS, Client
from .errors import (
    AboveMax, AlreadyAccepted, AuthError, BadAnswer, BadRequest, BelowMin, ConfigError, CrxError,
    InsufficientCollateral, LegIdTaken, LegLive, MarketClosed, MarketPaused, NetworkError, NoQuotes, NotWhitelisted,
    OwnRoundOpen, QuoteDropped, QuoteExpired, QuoteFillsFull, QuoteLost, QuoteNotYours, RateLimited, RefusedToSign,
    Rejected, SeatNotReady, ServerError, TradeUnknown, TxFailed, UnknownOrEnded,
)
from .models import Balance, Deposit, Drop, Event, MakerQuote, Market, Position, Quote, Rfq, Trade, Viewer, Withdraw

__version__ = "0.4.1"

__all__ = [
    "Client", "NETWORKS", "__version__",
    "Balance", "Deposit", "Drop", "Event", "MakerQuote", "Market", "Position", "Quote", "Rfq", "Trade", "Viewer",
    "Withdraw",
    "CrxError", "AboveMax", "AlreadyAccepted", "AuthError", "BadAnswer", "BadRequest", "BelowMin", "ConfigError",
    "InsufficientCollateral", "LegIdTaken", "LegLive", "MarketClosed", "MarketPaused", "NetworkError", "NoQuotes",
    "NotWhitelisted", "OwnRoundOpen", "QuoteDropped", "QuoteExpired", "QuoteFillsFull", "QuoteLost", "QuoteNotYours",
    "RateLimited", "RefusedToSign", "Rejected",
    "SeatNotReady", "ServerError", "TradeUnknown", "TxFailed", "UnknownOrEnded",
]
