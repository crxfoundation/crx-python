"""CRX Python SDK: quote, trade, deposit and withdraw on the CRX FX forwards venue."""

from .client import NETWORKS, Client
from .errors import (
    AboveMax, AuthError, BadAnswer, BadRequest, BelowMin, ConfigError, CrxError, InsufficientCollateral,
    MarketClosed, MarketPaused, NetworkError, NoQuotes, NotWhitelisted, OwnRoundOpen, QuoteExpired,
    RateLimited, RefusedToSign, Rejected, SeatNotReady, ServerError, TradeUnknown, TxFailed, WithdrawInProgress,
)
from .models import Balance, Deposit, Event, Market, Position, Quote, Trade, Viewer, Withdraw

__version__ = "0.1.0"

__all__ = [
    "Client", "NETWORKS", "__version__",
    "Balance", "Deposit", "Event", "Market", "Position", "Quote", "Trade", "Viewer", "Withdraw",
    "CrxError", "AboveMax", "AuthError", "BadAnswer", "BadRequest", "BelowMin", "ConfigError",
    "InsufficientCollateral", "MarketClosed", "MarketPaused", "NetworkError", "NoQuotes",
    "NotWhitelisted", "OwnRoundOpen", "QuoteExpired", "RateLimited", "RefusedToSign", "Rejected",
    "SeatNotReady", "ServerError", "TradeUnknown", "TxFailed", "WithdrawInProgress",
]
