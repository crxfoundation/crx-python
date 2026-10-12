"""CRX Python SDK: quote, trade, deposit and withdraw on the CRX FX forwards venue."""

from .client import NETWORKS, Client
from .errors import (
    AboveMax, AlreadyAccepted, AuthError, BadAnswer, BadRequest, BelowMin, ConfigError, CrxError, Declined,
    EarlierOutcome,
    InsufficientCollateral, InvalidSignature, LegIdTaken, LegLive, MarkUnavailable, MarketClosed, MarketPaused,
    NetworkError, NoQuotes, NotWhitelisted, OneWallet, OwnRoundOpen, PositionMatured, QuoteDropped,
    QuoteExpired, QuoteFillsFull, QuoteFormatOutdated,
    QuoteLost, QuoteNotYours, RateLimited, RateOutOfBand, RefusedToSign, Rejected, RelayUnavailable, RfqCancelled,
    SeatCannotSign, SeatNotReady, SendUnknown, ServerError, ServiceUnavailable, TradeUnknown, TxFailed,
    UnknownOrEnded, WalletHeld, WalletSame, WalletSigning, WithdrawUnknown,
)
from ._solana import BindFailed, BindInProgress, SeatBoundOtherPayout, SeatStopped
from .models import (
    Ask, Balance, Deposit, Drop, Event, MakerQuote, Market, Position, Quote, Registration, Rfq, Trade, Viewer,
    Withdraw,
)

__version__ = "0.2.8"

__all__ = [
    "Client", "NETWORKS", "__version__",
    "Ask", "Balance", "Deposit", "Drop", "Event", "MakerQuote", "Market", "Position", "Quote", "Registration", "Rfq",
    "Trade", "Viewer", "Withdraw",
    "CrxError", "AboveMax", "EarlierOutcome", "AlreadyAccepted", "AuthError", "BadAnswer", "BadRequest", "BelowMin", "ConfigError",
    "Declined", "MarkUnavailable", "PositionMatured", "RateOutOfBand",
    "InsufficientCollateral", "InvalidSignature", "LegIdTaken", "LegLive", "MarketClosed", "MarketPaused",
    "NetworkError", "NoQuotes", "NotWhitelisted", "OneWallet", "OwnRoundOpen", "QuoteDropped",
    "QuoteExpired", "QuoteFillsFull", "QuoteFormatOutdated",
    "QuoteLost", "QuoteNotYours", "RateLimited", "RefusedToSign", "Rejected", "RfqCancelled",
    "SeatCannotSign", "SeatNotReady", "SeatStopped", "SendUnknown", "ServerError", "ServiceUnavailable",
    "TradeUnknown", "TxFailed", "UnknownOrEnded", "WalletHeld", "WalletSame", "WalletSigning", "WithdrawUnknown",
    "BindFailed", "BindInProgress", "SeatBoundOtherPayout",
]
