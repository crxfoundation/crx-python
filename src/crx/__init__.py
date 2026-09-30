"""CRX Python SDK: quote, trade, deposit and withdraw on the CRX FX forwards venue."""

from .client import NETWORKS, Client
from .errors import (
    AboveMax, AuthError, BadAnswer, BadRequest, BelowMin, ConfigError, CrxError, InsufficientCollateral,
    MarketClosed, MarketPaused, NetworkError, NoQuotes, NotWhitelisted, OwnRoundOpen, QuoteExpired, QuoteLost,
    RateLimited, RefusedToSign, Rejected, SeatNotReady, ServerError, TradeUnknown, TxFailed,
)
from .models import Balance, Deposit, Event, MakerQuote, Market, Position, Quote, Rfq, Trade, Viewer, Withdraw

__version__ = "0.3.0"

__all__ = [
    "Client", "NETWORKS", "__version__",
    "Balance", "Deposit", "Event", "MakerQuote", "Market", "Position", "Quote", "Rfq", "Trade", "Viewer", "Withdraw",
    "CrxError", "AboveMax", "AuthError", "BadAnswer", "BadRequest", "BelowMin", "ConfigError",
    "InsufficientCollateral", "MarketClosed", "MarketPaused", "NetworkError", "NoQuotes",
    "NotWhitelisted", "OwnRoundOpen", "QuoteExpired", "QuoteLost", "RateLimited", "RefusedToSign", "Rejected",
    "SeatNotReady", "ServerError", "TradeUnknown", "TxFailed",
]
