"""The CRX client."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
import weakref
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator

import requests
from eth_utils import is_checksum_address, is_hex_address, to_checksum_address

from . import _eip712 as e7
from ._bind import Binder, read_later
from ._chain import Rpc, TxLog, send_tx
from . import _env
from ._http import Gateway
from ._keys import load_account
from . import _close
from . import _maker
from . import _settle
from .signer import LocalSigner, as_signer, sign_typed
from .errors import (
    AboveMax, AuthError, BadAnswer, BadRequest, BelowMin, ConfigError, CrxError, DepositFailed, EarlierOutcome,
    InsufficientCollateral, MarkUnavailable, MarketPaused, NetworkError, NoQuotes, QuoteDropped, QuoteRejected,
    RateLimited, RefusedToSign, RfqCancelled, ServerError, ServiceUnavailable, TradeUnknown, TxFailed,
    WithdrawInProgress, WithdrawUnknown, clean,
)
from .models import (
    Ask, Balance, Close, Deposit, Drop, Event, MakerQuote, Market, Position, Quote, Rfq, TakeOver, Trade, Viewer,
    Withdraw, dec, _final, _provisional, ms_to_dt, side_word,
)

log = logging.getLogger("crx")

# NETWORKS rows, times in seconds. settle_wait: how long withdraw() polls its status. default_wait: the wait of
# accept(), confirm(), deposit(), withdraw() and test_request() when the call passes no wait=. check_minute: the
# minute past the hour of the hourly check. chain_id and core: the EIP-712 domain the SDK signs for. When
# /health serves another chain id, core or domain separator, or core is not an address, signing calls refuse.
MAINNET_CORE = "0x0d716cA9Fc2BFD8c66D2AF2fB38b56D08809A8cc"
NETWORKS = {
    "testnet": {
        "chain": "avax-fuji",
        "base_url": "https://api.sandbox.crxfx.com",
        "rpc_url": "https://api.avax-test.network/ext/bc/C/rpc",
        "settle_wait": 30.0,
        "default_wait": 1800.0,
        "check_minute": 5,
        "chain_id": 43113,
        "core": "0x072000D0EB830F767DC5128ED28139bB731b1521",
    },
    # Needs allow_mainnet and an RPC URL.
    "mainnet": {
        "chain": "ethereum",
        "base_url": "https://api.crxfx.com",
        "rpc_url": None,
        "settle_wait": 90.0,
        "default_wait": 900.0,
        "check_minute": 35,
        "chain_id": 1,
        "core": MAINNET_CORE,
    },
}
_ALIASES = {"fuji": "testnet"}
TESTNET_CHAIN_IDS = {43113}
MAINNET_CHAIN_IDS = {1}
DEFAULT_STATE_DIR = "~/.crx-quickstart"  # shared with the quickstart scripts: one nonce floor per seat
WAITING = ("", "sending", "pending")  # statuses _settle polls past
# Withdraw statuses in progress: no next withdraw signs until the status leaves them.
WITHDRAW_LIVE = ("", "sending", "pending", "accepted")
WITHDRAW_TTL = 22 * 3600  # the gateway refuses a deadline more than 23 h out
OPEN_TIMEOUT = 30.0  # POST /rfqs answers only after the 10 s quote window
# Server text of the 409 on POST /withdraw while the gateway has not read the latest settlement.
UNREAD_SETTLEMENT = "the latest settlement is not read yet"
REPOST_MARGIN = 300  # no repost this close to the deadline: the gateway checks it before a held item
IN_FLIGHT_S = 300  # a withdraw post can still reach the gateway this late: 30 s request cap plus proxies
OPEN_STATUSES = ("open", "quoted")  # RFQ statuses that still take an accept
READ_AFTER_END = 120  # seconds past the quote end after which positions() is final
TRADES_KEPT = 64  # accepts a client remembers. One with no final answer is never dropped.
DEFAULT_WAIT = 900.0
TEST_REQUEST_COLLATERAL_RETRY = 30.0
# Server text, lower case, of the 403 on POST /test-requests while the maker's collateral is not counted yet.
TEST_REQUEST_SHORT = "the account has insufficient available collateral for this test request."
WITHDRAW_AGAIN_S = 5.0  # between posts of one signed withdraw while a refusal clears
MIN_WITHDRAW = Decimal("1")  # USDC: the least withdraw() takes when no amount is given
FREE_SHORT_S = 120.0  # withdraw() rereads a short free balance this long: it reads low after a trade opens
NONCE_AGAIN_S = 3.0  # between /balance reads while withdraw.nonce is null for a reason that clears
NONCE_WAIT_S = 90.0  # longest withdraw() waits for a withdraw nonce
# withdraw.nonce_reason values that clear with time. Any other value raises at once.
NONCE_CLEARS = ("settlement_unread", "unavailable")
REJECTED_KEPT = 1024  # rejected RFQs a client remembers
COUNTED_POLL_S = 5.0  # between /balance reads of a credited deposit not yet counted in free
SCREEN_AGAIN_S = 15.0  # between sends refused for collateral that /balance free covers
SCREEN_WAIT = 600.0  # default collateral_wait of quote() and ask()
# Reads the amount from the collateral 422 text when the body has no details['required'].
SCREEN_NEED = re.compile(r"does not cover the (\d+(?:\.\d+)?) USD")


def _amount(value: Any, what: str = "amount") -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BadRequest(f"{what} is not a number") from None
    if not d.is_finite() or d <= 0:
        raise BadRequest(f"{what} must be above zero")
    if d.scaleb(6) != d.scaleb(6).to_integral_value():
        raise BadRequest(f"{what} has more than 6 decimals")
    return d


def _wait(value: Any) -> float:
    """Return ``value`` as seconds: a finite number, 0 or more."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < float("inf"):
        raise BadRequest("wait is a number of seconds, 0 or more")
    return float(value)


def _address(value: Any, what: str, err: type[CrxError] = BadRequest) -> str:
    """Return a 0x wallet address in lower case. Refuse a bad checksum or the zero address."""
    s = value.strip() if isinstance(value, str) else ""
    body = s[2:]
    mixed = body not in (body.lower(), body.upper())
    if not (s[:2] == "0x" and is_hex_address(s)) or (mixed and not is_checksum_address(s)) or int(s, 16) == 0:
        raise err(f"{what} is not a wallet address")
    return s.lower()


def _viewer(v: dict) -> Viewer:
    by = v.get("granted_by")
    return Viewer(address=str(v.get("viewer") or "").lower(), granted_by=by.lower() if isinstance(by, str) else None,
                  granted_at=ms_to_dt(v.get("granted_at")), raw=v)


def _last(b: dict, key: str) -> dict:
    """Return ``/balance`` ``<key>.last``, or ``{}``."""
    d = b.get(key)
    last = d.get("last") if isinstance(d, dict) else None
    return last if isinstance(last, dict) else {}


def _nonce_reason(b: dict) -> str | None:
    """Return ``withdraw.nonce_reason`` from ``/balance``, or None when it is not a short string."""
    w = b.get("withdraw")
    r = w.get("nonce_reason") if isinstance(w, dict) else None
    return r if isinstance(r, str) and 0 < len(r) <= 64 else None


def _word(value: Any) -> str | None:
    """Return a 0x 32-byte hex word in lower case, or None."""
    s = value.lower() if isinstance(value, str) else ""
    return s if len(s) == 66 and s[:2] == "0x" and all(ch in "0123456789abcdef" for ch in s[2:]) else None


def _plain(d: Decimal) -> str:
    """Format a decimal with no exponent."""
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _usdc(d: Decimal) -> str:
    """Format a USDC amount with 6 decimals, rounded down."""
    return format(d.quantize(Decimal("0.000001"), rounding=ROUND_DOWN), "f")


def _pair(pair: str) -> tuple[str, str]:
    """Return ('USD/MXN', 'USDMXN') from USD/MXN, usdmxn or USD-MXN."""
    s = "".join(ch for ch in str(pair).upper() if ch.isalpha())
    if len(s) != 6:
        raise BadRequest(f"unknown pair {clean(pair, 20)!r}")
    return f"{s[:3]}/{s[3:]}", s


def _client_id(value: Any) -> bool:
    """True for 1 to 128 characters, each printable ASCII (0x20 to 0x7E)."""
    return isinstance(value, str) and 1 <= len(value) <= 128 and all(" " <= ch <= "~" for ch in value)


def _side(side: str) -> str:
    s = str(side).strip().lower()
    if s not in ("buy", "sell"):
        raise BadRequest("side is 'buy' or 'sell'")
    return s


def _said(view: Any) -> dict:
    """Return the gateway's ``outcome`` and ``message`` on an RFQ view, each when served."""
    out = {}
    if isinstance(view, dict):
        for key, limit in (("outcome", 64), ("message", 300)):
            v = view.get(key)
            if isinstance(v, str) and v.strip():
                out[key] = clean(v, limit)
    return out


def _no_quotes(text: str, view: Any, details: dict) -> NoQuotes:
    """Build ``NoQuotes`` from ``text`` and the gateway's outcome and message, when served."""
    said = _said(view)
    details.update(said)
    if "message" in said:
        text += f" ({said['outcome']}: {said['message']})" if "outcome" in said else f" ({said['message']})"
    return NoQuotes(text, details=details)


def _ended(view: dict, rfq_id: str) -> CrxError | None:
    """Return the error for an RFQ that ended, or None while it takes an accept."""
    st = view.get("status")
    if st is None or st in OPEN_STATUSES:
        return None
    details = {"rfq_id": rfq_id, "status": clean(st, 40)}
    if st != "cancelled":
        return _no_quotes(f"the RFQ is {clean(st, 40)}: it takes no accept", view, details)
    reason, message = view.get("reason"), view.get("message")
    reason = clean(reason, 64) if isinstance(reason, str) and reason else None
    message = clean(message, 300) if isinstance(message, str) and message else None
    details.update(reason=reason, message=message)
    if "outcome" in _said(view):
        details["outcome"] = _said(view)["outcome"]
    return RfqCancelled(message or f"the gateway cancelled the RFQ ({reason or 'no reason named'})", details=details)


def _live(row: Any, now_ms: float) -> bool:
    """True for a quote row that can still win: ``quoted`` and not past ``expires_at``."""
    return (isinstance(row, dict) and row.get("status") == "quoted" and isinstance(row.get("expires_at"), int)
            and not isinstance(row.get("expires_at"), bool) and row["expires_at"] > now_ms)


def _text(value: Any) -> str | None:
    """Return a served status word, cleaned, or None."""
    return clean(value, 40) if isinstance(value, str) and value else None


def _trade_word(view: Any) -> str:
    """Return the trade status an RFQ view shows, read from the RFQ status when none is served."""
    v = view if isinstance(view, dict) else {}
    ts, st = _text(v.get("trade_status")), _text(v.get("status"))
    if ts is None and st in ("opened", "settled"):
        return "open"
    if ts is None and st in ("expired", "cancelled"):
        return "refused"
    return ts or "pending"


def _answered(word: str) -> bool:
    """True when the accept has an answer: ``open`` or ``refused``. A ``pending`` trade can still be refused."""
    return word in ("open", "refused")


def _same(row: dict, held: dict) -> bool:
    """True when the /balance withdraw row stands at the held intent's nonce and amount."""
    try:
        return str(row.get("nonce")) == str(held["nonce"]) and Decimal(str(row.get("amount"))) == held["amount"]
    except (InvalidOperation, ValueError):
        return False


def _unread(e: CrxError) -> bool:
    """True for the 409 ``conflict`` of POST /withdraw while the gateway reads an older settlement."""
    return (e.status == 409 and (e.gateway_code or e.code) == "conflict"
            and str(e.details.get("error") or e).removeprefix("conflict: ").startswith(UNREAD_SETTLEMENT))


def _short_said(e: CrxError) -> str | None:
    """Return the gateway's text when ``e`` is the 403 ``TEST_REQUEST_SHORT`` refusal, else None."""
    if e.status != 403 or (e.gateway_code or "forbidden") != "forbidden":
        return None
    for text in (e.details.get("error"), str(e)):
        said = str(text or "").strip()
        if said.lower().startswith("forbidden:"):
            said = said[len("forbidden:"):].strip()
        if said.lower() == TEST_REQUEST_SHORT:
            return said
    return None


def _uncounted(e: CrxError) -> CrxError | None:
    """Return the error to raise for a test request refused on uncounted maker collateral, else None."""
    if isinstance(e, InsufficientCollateral):
        return e if e.details.get("side") == "maker" else None
    if isinstance(e, ServerError):
        return e if e.gateway_code == "collateral_unverified" else None
    said = _short_said(e)
    if said is None:
        return None
    return InsufficientCollateral(said, status=e.status, details={**e.details, "side": "maker"},
                                  gateway_code=e.gateway_code)


def _screen_need(e: CrxError, side: str) -> Decimal | None:
    """Return the USD amount the collateral 422 names for ``side`` (``taker`` or ``maker``), else None."""
    if not isinstance(e, InsufficientCollateral) or e.status != 422 or e.details.get("side", side) != side:
        return None
    need = dec(e.details.get("required"))
    if need is None:
        for text in (e.details.get("error"), str(e)):
            m = SCREEN_NEED.search(str(text or ""))
            if m:
                need = dec(m.group(1))
                break
    return need if need is not None and need.is_finite() and need > 0 else None


def _clears(e: CrxError) -> bool:
    """True for a POST /withdraw refusal that clears with time."""
    return (e.status == 409 and (e.gateway_code or e.code) == "trade_in_flight") or _unread(e)


def _past_held_check(e: CrxError) -> bool:
    """True for a POST /withdraw refusal the gateway gives only after it found no held item."""
    code, d = e.gateway_code or e.code, e.details
    if e.status == 409 and code == "withdraw_in_progress":
        return True
    if e.status == 409 and code == "conflict":
        return not _unread(e)
    return e.status == 422 and ("expected" in d and "got" in d or "free" in d and "requested" in d)


def _position(p: dict) -> Position:
    """Build a ``Position`` from a ``/positions`` row."""
    entry = dec(p.get("entry_price"))
    return Position(
        trade_id=str(p.get("trade_id")), rfq_id=str(p.get("rfq_id")), pair=str(p.get("pair")),
        side=side_word(p.get("side")), notional=dec(p.get("notional")), rate=dec(p.get("rate")),
        status=p.get("status"), raw=p, final=_final(p, "status", "final"),
        entry_price=entry if entry is not None else dec(p.get("rate")), mark_price=dec(p.get("mark_price")),
        mark_time=ms_to_dt(p.get("mark_time")), unrealized_pnl=dec(p.get("unrealized_pnl")),
        settles_at=ms_to_dt(p.get("settlement")),
    )


def _refused(e: CrxError) -> dict:
    """Summarize a refused post: code, HTTP status, message and any retry time."""
    out = {"code": e.code, "status": e.status, "message": str(e)}
    out.update({k: e.details[k] for k in ("retry_after_secs", "time_utc") if k in e.details})
    return out


class Client:
    """One seat on one CRX network.

    ``CRX_*`` settings come from the environment, then from a ``.env`` file in the working directory.
    A name in the environment wins. A key file, or a ``.env`` file with a ``CRX_*`` line, that other
    users can open is set to owner-only (0600) when the current user owns it; else the client refuses it.

    Args:
        key: Seat wallet private key. When absent, read from ``key_file``, ``CRX_WALLET_PK`` or
            ``CRX_WALLET_PK_FILE``. Without a key, only ``health()``, ``markets()``, ``market()`` and ``next_check()`` work.
        network: ``"testnet"`` (default) or ``"mainnet"``.
        allow_mainnet: Required for mainnet, or set ``CRX_ALLOW_MAINNET=1``.
        rpc_url: Chain RPC URL, or ``CRX_RPC``. Mainnet has no default.
        signer: A custodian signer in place of the key (see ``crx.signer``). ``deposit``, ``send_quote``
            and ``confirm`` still need a local key.
        account: Another seat this key may read (see ``add_viewer``). Then only ``balance``, ``positions``
            and ``trades`` work.
        core: Core contract that replaces the network's pinned one. Pass only an address CRX publishes.
            Signing calls refuse when /health serves another core or domain.
        keepalive: Seconds between maker ``/trades`` reads that keep the seat live, 1 to 10 (default 5), or None.
    """

    def __init__(
        self,
        key: str | bytes | None = None,
        *,
        key_file: str | os.PathLike | None = None,
        network: str = "testnet",
        base_url: str | None = None,
        rpc_url: str | None = None,
        state_dir: str | os.PathLike | None = None,
        timeout: float = 10.0,
        session: requests.Session | None = None,
        account: str | None = None,
        allow_mainnet: bool = False,
        signer: Any = None,
        keepalive: float | None = _maker.KEEPALIVE_S,
        core: str | None = None,
    ) -> None:
        self._account = None
        self._signer = None
        self._custody = None
        self._live: _maker.Keepalive | None = None
        if signer is not None and (key is not None or key_file is not None):
            key = None
            raise ConfigError("set key, key_file or signer: one only")
        lo, hi = _maker.KEEPALIVE_RANGE
        if keepalive is not None and (isinstance(keepalive, bool) or not isinstance(keepalive, (int, float))
                                      or not lo <= keepalive <= hi):
            key = None
            raise ConfigError(f"keepalive is None, or {lo:g} to {hi:g} s")
        self._keepalive_s = None if keepalive is None else float(keepalive)
        self._live_lock = threading.Lock()
        name = _ALIASES.get(network, network)
        net = NETWORKS.get(name)
        if net is None:
            key = None
            raise ConfigError(f"unknown network {clean(network, 20)!r}; known: {', '.join(NETWORKS)}")
        self.network = name
        self._net = net
        pin_core = net.get("core")
        if core is not None:
            try:
                pin_core = _address(core, "core", ConfigError)
            except ConfigError:
                key = None
                raise
        try:
            self._pin_core = _address(pin_core, "core", ConfigError)
        except ConfigError:
            self._pin_core = None  # signing calls refuse later
        self.chain_key = net["chain"]
        self._settle_wait = net["settle_wait"]
        self._default_wait = float(net.get("default_wait", DEFAULT_WAIT))
        self._check_minute = net["check_minute"]
        try:
            env = _env.load()
        except ConfigError:
            key = None
            raise
        gw_url = base_url or env.get("CRX_BASE") or net["base_url"]
        rpc = rpc_url or env.get("CRX_RPC") or net["rpc_url"]
        if name == "mainnet":
            refusal = None
            if not (allow_mainnet is True or env.get("CRX_ALLOW_MAINNET") == "1"):
                refusal = "mainnet is off: pass allow_mainnet=True or set CRX_ALLOW_MAINNET=1"
            elif not rpc:
                refusal = f"{name} has no default RPC: pass rpc_url= or set CRX_RPC"
            if refusal:
                key = None
                raise ConfigError(refusal)
        self._session = session or requests.Session()
        try:
            self._gw = Gateway(gw_url, None, self._session, timeout)
        except ConfigError:
            key = None
            raise
        self._rpc = Rpc(rpc, self._session, max(timeout, 20.0))
        try:
            custody = None if account is None else _address(account, "account", ConfigError)
        except ConfigError:
            key = None
            raise
        # Drop the key from locals once it loads, or fails to.
        try:
            self._account = load_account(key, key_file, env.get) if signer is None else None
        except ConfigError:
            key = None
            raise
        key = None
        self._signer = as_signer(signer) if signer is not None else as_signer(self._account)
        self._gw._signer = self._signer
        self._gw.login = signer is not None and not isinstance(self._signer, LocalSigner)
        if self._signer is not None and custody != self.address:
            self._custody = self._gw.custody = custody
        self._state_dir = Path(state_dir or env.get("CRX_STATE_DIR") or DEFAULT_STATE_DIR).expanduser()
        self._chain: dict | None = None
        self._sep: bytes | None = None
        self._rpc_checked = False
        self._gw.sleep = time.sleep
        self._clock = time.time
        self._legs: dict[str, str] = {}  # rfq_id -> this seat's leg id on it
        # One accept() and one withdraw() at a time. _accepts: rfq_id -> the accept signed on it.
        # _held_withdraw: the signed withdraw whose final answer the caller has not read.
        self._trade_mx = threading.Lock()
        self._withdraw_mx = threading.Lock()
        self._accepts: dict[str, dict] = {}
        self._rejected: dict[str, None] = {}  # rfq_ids reject() took, oldest first
        self._held_withdraw: dict | None = None
        self._txlog = TxLog(self._state_dir / "txs.json",  # the txs this SDK sent: no key material
                            own_dir=not (state_dir or env.get("CRX_STATE_DIR")))

    def __repr__(self) -> str:
        return f"crx.Client(network={getattr(self, 'network', None)!r}, address={self.address!r})"

    def __getstate__(self) -> Any:
        raise TypeError("crx.Client holds a key and cannot be pickled or copied")

    @property
    def address(self) -> str | None:
        """The key's or signer's address in lower case, or None without either."""
        signer = getattr(self, "_signer", None)
        return signer.address.lower() if signer is not None else None

    @property
    def account(self) -> str | None:
        """The seat this client reads: ``account`` when set, else ``address``."""
        return getattr(self, "_custody", None) or self.address

    @property
    def _sleep(self) -> Callable[[float], None]:
        """The pause of this client and of its gateway's GET retry."""
        return self._gw.sleep

    @_sleep.setter
    def _sleep(self, fn: Callable[[float], None]) -> None:
        self._gw.sleep = fn

    def close(self) -> None:
        """Stop the maker keepalive thread. Later maker calls do not start it again."""
        with self._live_lock:
            self._keepalive_s = None
            if self._live is not None:
                self._live.stop()

    def _keep_live(self) -> None:
        """Start the maker keepalive thread once. It stops on ``close()`` or when the client is freed."""
        with self._live_lock:
            if self._live is not None or self._keepalive_s is None:
                return
            self._live = _maker.Keepalive(self._gw, self._keepalive_s)
            weakref.finalize(self, self._live.stop)
            self._live.start()

    def _wait_of(self, wait: Any) -> float:
        """Return ``wait`` in seconds, or this network's default wait for None."""
        return self._default_wait if wait is None else _wait(wait)

    def _need_key(self) -> None:
        if self._signer is None:
            raise ConfigError("this call needs the seat key: set CRX_WALLET_PK or pass key=")

    def _need_local_key(self, what: str) -> None:
        """Refuse a custodian signer: ``what`` signs a transaction or a maker hash with a local key."""
        self._need_key()
        if self._account is None:
            raise ConfigError(f"{what} needs a local key (key= or CRX_WALLET_PK), not a signer")

    def _need_seat(self) -> None:
        """Refuse a viewer: the call needs the key acting for its own seat."""
        self._need_key()
        if self._custody is not None:
            raise ConfigError("a viewer (account=) only reads: balance, positions, trades")

    def _chain_info(self) -> dict:
        """Return this network's chain from /health, checked against the pinned chain id, core and domain."""
        if self._chain is not None:
            return self._chain
        chains = self.health().get("chains")
        if not isinstance(chains, list):
            raise BadAnswer("/health lists no chains")
        c = next((c for c in chains if isinstance(c, dict)
                  and (c.get("key") or c.get("chain")) == self.chain_key), None)
        if c is None:
            raise ConfigError(f"{self._gw.host} does not serve {self.chain_key}")
        pin_id, pin_core = self._net.get("chain_id"), self._pin_core
        if type(pin_id) is not int or pin_core is None:
            raise ConfigError(f"network {self.network!r} pins no chain id or core; nothing signed. Pass core=")
        try:
            chain_id, core, domain = int(c["chain_id"]), str(c["core"]).lower(), str(c["domain"]).lower()
        except (KeyError, TypeError, ValueError):
            raise BadAnswer("/health sent a chain this SDK cannot read") from None
        if self.network == "mainnet":
            if chain_id not in MAINNET_CHAIN_IDS:
                raise ConfigError(f"{self.chain_key} is chain {chain_id}, not Ethereum mainnet (chain 1)")
        elif chain_id not in TESTNET_CHAIN_IDS:
            raise ConfigError(f"{self.chain_key} is chain {chain_id}, not a testnet; this SDK runs on testnets only")
        if chain_id != pin_id:
            raise ConfigError(f"{self.chain_key} is chain {chain_id}, not the pinned chain {pin_id}; nothing signed")
        if core != pin_core:
            raise RefusedToSign(
                f"core moved: /health names {clean(core, 44)}, the SDK pins {pin_core} on {self.chain_key}; "
                "nothing signed. Update crx-python, or pass core= with the address CRX publishes",
                details={"chain": self.chain_key, "served_core": clean(core, 44), "pinned_core": pin_core})
        sep = e7.domain_separator(pin_id, pin_core)
        if e7.h0x(sep) != domain:
            raise RefusedToSign("domain moved: /health does not match the core; nothing signed")
        self._chain, self._sep = dict(c, chain_id=pin_id, core=pin_core), sep
        return self._chain

    def _chain_ready(self) -> dict:
        """Return the chain after checking the RPC's chain id and that the core is a contract."""
        c = self._chain_info()
        if not self._rpc_checked:
            if self._rpc.int("eth_chainId") != c["chain_id"]:
                raise ConfigError(f"the RPC is not on {self.chain_key} (chain {c['chain_id']})")
            if self._rpc("eth_getCode", c["core"], "latest") in ("0x", "", None):
                raise ConfigError(f"the pinned core on {self.chain_key} is not a contract; nothing sent")
            self._rpc_checked = True
        return c

    def _binder(self) -> Binder:
        c = self._chain_ready()
        return Binder(self._gw, self._signer, c, self._sep, self._state_dir, clock=self._clock)

    def health(self) -> dict:
        """Return the gateway's /health answer."""
        return self._gw.request("GET", "/health", auth=False)

    def next_check(self) -> datetime:
        """Return the time of the next hourly check in UTC, without a network call.

        The check runs at :05 past the hour on testnet, and at :35 on mainnet.
        """
        now = datetime.fromtimestamp(self._clock(), timezone.utc)
        at = now.replace(minute=self._check_minute, second=0, microsecond=0)
        return at if at > now else at + timedelta(hours=1)

    def markets(self) -> list[Market]:
        """List every pair with its session, pause state, notional limits and premium cap.

        ``paused`` and ``max_premium_bps`` apply to this client's chain.
        """
        body = self._gw.request("GET", "/markets", query={"chain": self.chain_key}, auth=False)
        rows = body.get("markets")
        if not isinstance(rows, list):
            raise BadAnswer("/markets lists no markets")
        out = []
        for m in rows:
            if not isinstance(m, dict) or not isinstance(m.get("pair"), str):
                continue
            if "chains" in m:
                on = [c for c in m.get("chains") or [] if isinstance(c, dict) and c.get("chain") == self.chain_key]
                caps = [c.get("max_premium_bps") for c in on]
                cap = min(caps) if caps and all(type(v) is int and v >= 0 for v in caps) else None
                paused = not on or any(c.get("paused") is True for c in on)
            else:
                v = m.get("max_premium_bps")
                cap = v if type(v) is int and v >= 0 else None
                paused = False
            session = m.get("session") if isinstance(m.get("session"), dict) else {}
            nb = session.get("next_boundary") if isinstance(session.get("next_boundary"), dict) else {}
            notional = m.get("notional") if isinstance(m.get("notional"), dict) else {}
            tenor = m.get("tenor") if isinstance(m.get("tenor"), dict) else {}
            lc = m.get("next_long_close") if isinstance(m.get("next_long_close"), dict) else {}
            start, end = ms_to_dt(lc.get("starts_at")), ms_to_dt(lc.get("ends_at"))
            pair = m["pair"]
            slash = e7.pair_text_ok(pair)
            out.append(Market(
                pair=pair, pair_id=e7.h0x(e7.pair_id(pair)) if slash else "",
                base=pair[:3] if slash else "", quote=pair[4:] if slash else "", open=session.get("open") is True,
                paused=paused, max_premium_bps=cap,
                min_notional=dec(notional.get("min")), max_notional=dec(notional.get("max")),
                next_open=ms_to_dt(nb.get("at")) if nb.get("kind") == "open" else None,
                min_tenor_s=tenor.get("min_secs"), max_tenor_s=tenor.get("max_secs"), raw=m,
                next_long_close=(start, end) if start and end and start < end else None, _clock=self._clock,
            ))
        return out

    def market(self, pair: str) -> Market:
        """Return the market for one pair.

        Args:
            pair: Currency pair, e.g. ``"USD/MXN"`` or ``"usdmxn"``.

        Raises:
            BadRequest: ``pair`` is not a currency pair.
            MarketPaused: The pair is not offered on this client's chain.
        """
        slash, _ = _pair(pair)
        m = next((m for m in self.markets() if m.pair == slash), None)
        if m is None or ("chains" in m.raw and not any(
                isinstance(c, dict) and c.get("chain") == self.chain_key for c in m.raw.get("chains") or [])):
            raise MarketPaused(f"{slash} is not offered on {self.chain_key}", details={"pair": slash})
        return m

    def balance(self) -> Balance:
        """Return the seat's collateral, margin and withdraw state."""
        self._need_key()
        b = self._gw.request("GET", "/balance", query={"chain": self.chain_key})
        if str(b.get("account") or "").lower() != self.account:
            raise BadAnswer("/balance answered for another account")
        w = b.get("withdraw") if isinstance(b.get("withdraw"), dict) else {}
        nonce = w.get("nonce")
        return Balance(
            account=self.account, state=b.get("state"), collateral=dec(b.get("collateral")), free=dec(b.get("free")),
            equity=dec(b.get("equity")), im=dec(b.get("im")), mm=dec(b.get("mm")),
            open_legs=b.get("open_legs") if isinstance(b.get("open_legs"), int) else None,
            withdraw_live=w.get("live") if isinstance(w.get("live"), bool) else None,
            withdraw_nonce=int(nonce) if isinstance(nonce, (str, int)) and str(nonce).isdigit() else None,
            as_of=ms_to_dt(b.get("as_of")),
            as_of_block=b["as_of_block"] if type(b.get("as_of_block")) is int else None, raw=b,
        )

    def positions(self) -> list[Position]:
        """List the seat's positions with entry price, mark price and unrealized P&L.

        ``unrealized_pnl`` is signed USDC from the seat's side. The mark fields and the P&L are None when a
        position has no mark: settled, not open yet, or no market price.
        """
        self._need_key()
        body = self._gw.request("GET", "/positions", query={"chain": self.chain_key})
        rows = body.get("positions")
        if not isinstance(rows, list):
            raise BadAnswer("/positions lists no positions")
        return [_position(p) for p in rows if isinstance(p, dict)]

    def trades(self, after: int = 0, *, market: bool = False) -> list[Event]:
        """List the seat's event tape, oldest first.

        Key trades on ``trade_id``. ``trade.retracted`` can take back a ``trade.opened`` until ``trade.final``
        arrives, or a ``trade.opened`` carries ``final: true``.

        Args:
            after: Return only events after this ``Event.seq``. Sequence numbers count per account and role.
            market: Keep every ``rfq.opened`` a maker seat receives. By default only the seat's own RFQs show.

        Raises:
            CrxError: ``after`` is past the end of the tape (code ``conflict``).
        """
        self._need_key()
        rows: list = []
        while True:
            page = self._gw.request("GET", "/trades", query={"after": after, "limit": 1000})
            got = page.get("trades")
            if not isinstance(got, list):
                raise BadAnswer("/trades lists no trades")
            rows += got
            if len(got) < 1000:
                break
            seq = page.get("seq")
            if not isinstance(seq, int) or seq <= after:
                raise BadAnswer("/trades did not advance its seq")
            after = seq
        rows = [r for r in rows if isinstance(r, dict)]
        if not market:
            def own_rfq(r: dict) -> bool:
                d = r.get("data")
                cid = d.get("client_rfq_id") if isinstance(d, dict) else None
                return isinstance(cid, str) and cid != ""
            rows = [r for r in rows if r.get("type") != "rfq.opened" or own_rfq(r)]
        return [
            Event(type=str(r.get("type")), seq=r.get("seq") if isinstance(r.get("seq"), int) else None,
                  ts=ms_to_dt(r.get("ts")),
                  data=r.get("data") if isinstance(r.get("data"), dict) else {}, raw=r)
            for r in rows
        ]

    def quote(
        self,
        pair: str,
        side: str,
        notional: Any,
        *,
        settles: Any = None,
        expiry: datetime | timedelta | int | None = None,
        premium_bps: int = 0,
        wait: float = 30.0,
        client_rfq_id: str | None = None,
        collateral_wait: float = SCREEN_WAIT,
    ) -> Quote:
        """Open an RFQ and return the best firm quote, without accepting it.

        Accept the quote with ``accept()`` before ``Quote.closes_at``, 120 s after the request.

        Args:
            side: ``"buy"`` or ``"sell"`` the base currency.
            notional: Amount in the base currency, at most 6 decimals.
            settles: Exact UTC time: an aware datetime, ISO 8601 with Z or an offset, or Unix seconds. No default.
            expiry: Older form of ``settles``: a datetime, a timedelta from now, or unix ms. Pass one of the two.
            premium_bps: Upfront premium, -10000 to 10000 (default 0). Above 0 the taker pays.
            wait: Seconds to poll when the gateway answers before the quote window ends (default 30).
            client_rfq_id: 1 to 128 printable ASCII characters.
            collateral_wait: Seconds to send again while a new deposit is not counted yet (default 600).

        Raises:
            NoQuotes: No quote arrived. ``RfqCancelled`` carries the gateway's ``reason``.
            BadRequest: Invalid terms, or a settlement time that does not settle (``details['next_time']``).
        """
        rfq, r = self._open(pair, side, notional, settles, expiry, premium_bps, client_rfq_id, True,
                            collateral_wait)
        if not isinstance(r.get("quotes"), list):
            return self._winner(rfq, wait)
        q = self._pick(r, rfq["rfq_id"])  # the answer came after the window
        if q is None:
            raise _no_quotes("no quote in the window: the request is still open", r,
                             {"rfq_id": rfq["rfq_id"]})
        return self._quote_of(rfq, q)

    def ask(
        self,
        pair: str,
        side: str,
        notional: Any,
        *,
        settles: Any = None,
        expiry: datetime | timedelta | int | None = None,
        premium_bps: int = 0,
        client_rfq_id: str | None = None,
        collateral_wait: float = SCREEN_WAIT,
    ) -> Ask:
        """Open an RFQ and return at once, before any quote arrives.

        Takes the arguments of ``quote()``. ``Ask.quote()`` returns the winning quote.
        """
        rfq, r = self._open(pair, side, notional, settles, expiry, premium_bps, client_rfq_id, False,
                            collateral_wait)
        return Ask(rfq_id=rfq["rfq_id"], pair=rfq["pair"], side=rfq["side"], notional=Decimal(rfq["notional"]),
                   expiry=ms_to_dt(rfq["expiry"]), raw=r, rfq=rfq, _client=self)

    def _open(
        self, pair: str, side: str, notional: Any, settles: Any, expiry: datetime | timedelta | int | None,
        premium_bps: int, client_rfq_id: str | None, wait: bool, collateral_wait: Any = SCREEN_WAIT,
    ) -> tuple[dict, dict]:
        """Check the terms, POST /rfqs, and return the RFQ's terms and the gateway's answer."""
        self._need_seat()
        collateral_wait = _wait(collateral_wait)
        if client_rfq_id is not None and not _client_id(client_rfq_id):
            raise BadRequest("client_rfq_id must be 1 to 128 printable characters",
                             details={"field": "client_rfq_id", "max": 128})
        slash, compact = _pair(pair)
        side = _side(side)
        amount = _amount(notional, "notional")
        m = self.market(slash)
        if m.paused:
            raise MarketPaused(f"{slash} is paused", details={"pair": slash})
        if m.min_notional is not None and amount < m.min_notional:
            raise BelowMin(f"minimum notional on {slash} is {m.min_notional}", details={"min": str(m.min_notional)})
        if m.max_notional is not None and amount > m.max_notional:
            raise AboveMax(f"maximum notional on {slash} is {m.max_notional}", details={"max": str(m.max_notional)})
        now, t0 = datetime.now(timezone.utc), self._clock()
        if settles is not None and expiry is not None:
            raise BadRequest("pass settles or expiry, not both", details={"field": "settles"})
        if expiry is None:
            expiry = _settle.settlement(settles, m, now)  # raises when settles is missing
        if isinstance(expiry, timedelta):
            expiry = now + expiry
        if isinstance(expiry, datetime):
            if expiry.tzinfo is None:
                raise BadRequest("expiry needs a timezone, e.g. datetime(..., tzinfo=timezone.utc)")
            expiry_ms = int(expiry.timestamp() * 1000)
        elif isinstance(expiry, int) and not isinstance(expiry, bool):
            expiry_ms = expiry
        else:
            raise BadRequest("expiry is a datetime, a timedelta or unix ms")
        if isinstance(premium_bps, bool) or not (isinstance(premium_bps, int) and -10_000 <= premium_bps <= 10_000):
            raise BadRequest("premium_bps is an integer from -10000 to 10000")
        req = {
            "chain": self.chain_key, "pair": compact, "side": side, "notional": _plain(amount),
            "expiry": expiry_ms, "client_rfq_id": client_rfq_id or f"sdk-{uuid.uuid4().hex[:12]}",
        }
        if premium_bps:
            req["premium_bps"] = premium_bps
        if not wait:
            req["wait"] = False  # else the gateway answers only after the quote window
        sent = []

        def send() -> Any:
            # A resend keeps the settlement time; it must still clear the shortest tenor and the open margin.
            if sent and settles is not None:
                _settle.settlement(settles, m, now + timedelta(seconds=self._clock() - t0))
            sent.append(True)
            return self._gw.request("POST", "/rfqs", body=req,
                                    timeout=max(self._gw._timeout, OPEN_TIMEOUT) if wait else None)

        r = self._screen_again(send, "taker", self._clock() + collateral_wait)
        expiry_ms = req["expiry"]
        rfq_id, leg_id, qe_max = _word(r.get("rfq_id")), _word(r.get("leg_id")), r.get("quote_expiry_max")
        if rfq_id is None or leg_id is None or type(qe_max) is not int:
            raise BadAnswer("/rfqs sent an answer this SDK cannot read")
        closes_at = r.get("closes_at") if type(r.get("closes_at")) is int else None
        log.info("rfq %s opened: %s %s %s", rfq_id, slash, side, _plain(amount))
        rfq = {"rfq_id": rfq_id, "leg_id": leg_id, "quote_expiry_max": qe_max, "pair": slash, "side": side,
               "notional": _plain(amount), "expiry": expiry_ms, "premium_bps": premium_bps,
               "max_premium_bps": m.max_premium_bps, "closes_at": closes_at}
        return rfq, r

    def _screen_again(self, send: Callable[[], Any], side: str, end: float) -> Any:
        """Call ``send()`` again while the collateral screen refuses an amount that /balance free covers."""
        while True:
            try:
                return send()
            except InsufficientCollateral as e:
                need = _screen_need(e, side)
                if need is None or self._clock() + SCREEN_AGAIN_S > end:
                    raise
                try:
                    free = self.balance().free
                except CrxError as read:
                    raise e from read
                if free is None or not free.is_finite() or free < need:
                    raise e
                log.info("%s refused for collateral that /balance free %s covers (%s USD); sending again in %g s",
                         side, free, need, SCREEN_AGAIN_S)
                self._sleep(SCREEN_AGAIN_S)

    def _winner(self, rfq: dict, wait: float) -> Quote:
        """Return the winning quote on ``rfq``, polling for ``wait`` seconds at most."""
        return self._quote_of(rfq, self._await_quote(rfq["rfq_id"], wait))

    @staticmethod
    def _quote_of(rfq: dict, q: dict) -> Quote:
        """Build a ``Quote`` from a quote row and the RFQ's own terms."""
        try:
            return Quote(
                rfq_id=rfq["rfq_id"], quote_id=str(q["quote_id"]), pair=rfq["pair"], side=rfq["side"],
                notional=Decimal(rfq["notional"]), rate=Decimal(str(q["rate"])), expiry=ms_to_dt(rfq["expiry"]),
                expires_at=ms_to_dt(q.get("expires_at")), raw=q, rfq=rfq, expiry_ms=rfq["expiry"],
                closes_at=ms_to_dt(rfq.get("closes_at")),
            )
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise BadAnswer("the quote row cannot be read") from None

    def _pick(self, view: dict, rfq_id: str) -> dict | None:
        """Return the gateway's live pick, else the best live quote, else None. Raise when the RFQ ended."""
        err = _ended(view, rfq_id)
        if err is not None:
            raise err
        now_ms = self._clock() * 1000
        pick = view.get("quote")
        if _live(pick, now_ms):
            return pick
        live = [q for q in view.get("quotes") or [] if _live(q, now_ms)]
        return live[0] if live else None

    def _await_quote(self, rfq_id: str, wait: float) -> dict:
        """Poll GET /rfqs/{id} until the gateway names a pick, or return the best live quote when ``wait`` ends."""
        deadline = self._clock() + max(float(wait), 0.0)
        while True:
            view = self._gw.request("GET", f"/rfqs/{rfq_id}")
            err = _ended(view, rfq_id)
            if err is not None:
                raise err
            if isinstance(view.get("quote"), dict):
                best = self._pick(view, rfq_id)
                if best is not None:
                    return best
            if self._clock() >= deadline:
                best = self._pick(view, rfq_id)
                if best is not None:
                    return best
                raise _no_quotes("no quote before the wait ended: the request is still open", view,
                                 {"rfq_id": rfq_id})
            self._sleep(1.0)

    def accept(self, quote: Quote, *, wait: float | None = None) -> Trade:
        """Accept a quote and open the trade. ``trade()`` is the same call.

        The SDK signs only typed data it builds from the quote. CRX sends the transaction and pays the network fee.

        Args:
            wait: Seconds to wait for ``open`` or ``refused``. Default 30 minutes on testnet, 15 on mainnet.

        Returns:
            The trade once ``open`` or ``refused``, else ``sending`` or ``pending`` when ``wait`` ends.
            A second call with the same quote signs nothing and returns the current status.

        Raises:
            RefusedToSign: The served trade differs from the quote. Nothing is signed.
            QuoteDropped: The maker dropped the quote. ``best`` is the best live quote, or None.
            QuoteRejected: ``reject()`` took this RFQ.
            TradeUnknown: An earlier accept has no answer yet.
            EarlierOutcome: An earlier accept has its final answer now.
        """
        self._need_seat()
        if not isinstance(quote, Quote):
            raise BadRequest("accept() takes the Quote that quote() returned")
        wait = self._wait_of(wait)
        with self._trade_mx:
            if quote.rfq_id in self._rejected:
                raise QuoteRejected(f"rfq {quote.rfq_id} was rejected on this client; nothing signed. Request a "
                                    "new quote", details={"rfq_id": quote.rfq_id})
            rec = self._accepts.get(quote.rfq_id)
            if rec is not None:
                return self._trade_again(rec, wait)
            self._earlier_trades()
            b = self._binder()
            rec = {"rfq_id": quote.rfq_id, "quote": quote, "end": quote.rfq["quote_expiry_max"], "final": False,
                   "told": False, "raw": {}, "word": "pending", "tx": None, "provisional": False}
            self._accepts[quote.rfq_id] = rec
            self._keep_accepts()
            try:
                t, view = self._trade_once(b, quote, wait)
            except TradeUnknown:
                raise
            except MarkUnavailable as e:
                if not getattr(e, "after_sig", False):
                    self._accepts.pop(quote.rfq_id, None)
                raise  # after a signature the gateway can keep the round: its status decides
            except CrxError:
                self._accepts.pop(quote.rfq_id, None)  # refused before anything was reserved
                raise
            rec.update(raw=t.raw, word=t.status, told=_answered(t.status), final=_final(view),
                       provisional=t.provisional, tx=t.tx)
            return t

    trade = accept

    def reject(self, quote: Quote) -> None:
        """Reject a quote's RFQ on this client. Nothing is sent to the gateway.

        A later ``accept()`` of any quote on the RFQ raises ``QuoteRejected``. The quote lapses at ``expires_at``.

        Raises:
            BadRequest: This client already signed an accept on the RFQ.
        """
        if not isinstance(quote, Quote):
            raise BadRequest("reject() takes the Quote that quote() returned")
        with self._trade_mx:
            if quote.rfq_id in self._accepts:
                raise BadRequest(f"rfq {quote.rfq_id} has an accept signed on this client; the accept stands. "
                                 "Read it with accept()", details={"rfq_id": quote.rfq_id})
            self._rejected[quote.rfq_id] = None
            for rid in list(self._rejected)[: max(len(self._rejected) - REJECTED_KEPT, 0)]:
                del self._rejected[rid]
        log.info("rfq %s rejected on this client; nothing sent", quote.rfq_id)

    def close_trade(self, trade_id: str, *, to: str | None = None, limit: Any = None,
                    wait: float | None = None) -> Close:
        """Close an open trade with its counterparty, or let another seat take over this seat's side.

        The request goes to the counterparty and to the makers, or to the one seat ``to`` names. An answer of
        the counterparty inside the price band wins and the trade ends for both seats. Else the best quote of
        another seat wins and that seat takes over this seat's side. The gateway refuses a rate outside its
        price band. The SDK checks the close against the trade, then signs it. CRX sends the transaction and
        pays the network fee.

        Args:
            trade_id: The trade, as ``positions()`` lists it.
            to: The one seat to ask. Defaults to every maker.
            limit: The worst close rate this seat takes: the lowest on a buy, the highest on a sell. Defaults
                to the price band.
            wait: Seconds to wait for ``closed`` or ``refused``. Default 30 minutes on testnet, 15 on mainnet.

        Raises:
            NoQuotes: No quote before the request ended.
            RateOutOfBand: The winning rate is outside the price band.
            QuoteExpired: The winning maker did not sign in time.
            OwnRoundOpen: Another close of this seat is still open at the accept.
            CrxError: Code ``conflict`` when a close of this trade is already open.
            RefusedToSign: The best rate is past ``limit``, or the close differs from the trade. Nothing is signed.
        """
        self._need_seat()
        return _close.close_trade(self, trade_id, to, self._wait_of(wait), limit=limit)

    def close_requests(
        self, *, after: int | None = None, wait: float | None = None, poll: float = 1.0,
        stop: threading.Event | None = None,
    ) -> Iterator[Rfq]:
        """Yield each request of a counterparty to close a trade with this seat, as it arrives.

        Answer one with ``answer_close()`` before ``Rfq.closes_at``.

        Args:
            after: Start after this ``Rfq.seq``. Defaults to the start of the tape.
            wait: Seconds until the stream ends. Defaults to no end.
            poll: Seconds between reads. Defaults to 1.
            stop: A ``threading.Event`` that ends the stream when set.
        """
        self._need_seat()
        return _close.requests(self, after, wait, poll, stop)

    def answer_close(self, request: Rfq, rate: Any, *, wait: float | None = None,
                     client_quote_id: str | None = None) -> Close:
        """Answer a request from ``close_requests()`` at ``rate``. When the answer wins, sign the close.

        An answer inside the price band wins over the quotes of other seats, and the trade ends for both
        seats. The SDK checks the close against the trade and the rate, then signs it.

        Args:
            rate: The close rate for the trade's pair, up to 6 decimals.
            wait: Seconds to wait for ``closed`` or ``refused``. Default 30 minutes on testnet, 15 on mainnet.
            client_quote_id: Label for the answer. Defaults to a new id.

        Raises:
            QuoteExpired: The round ended without this answer.
            RefusedToSign: The close differs from the trade or the rate. The SDK declines it. Nothing is signed.
        """
        self._need_seat()
        return _close.answer(self, request, rate, self._wait_of(wait), client_quote_id=client_quote_id)

    def _trade_once(self, b: Binder, quote: Quote, wait: float) -> tuple[Trade, dict]:
        """Accept ``quote``. A ``QuoteDropped`` carries the best other live quote on the RFQ in ``.best``."""
        try:
            return self._take(b, quote, wait)
        except QuoteDropped as e:
            e.best = self._best_of(quote, e)
            raise

    def _keep_accepts(self) -> None:
        """Drop the oldest accepts with an answer the caller read, past TRADES_KEPT."""
        for rid in [k for k, r in self._accepts.items() if r["told"]][: max(len(self._accepts) - TRADES_KEPT, 0)]:
            del self._accepts[rid]

    def _trade_read(self, rec: dict) -> tuple[str, bool, dict]:
        """Read the accept ``rec`` once. Past its quote end, a view with no answer defers to ``positions()``."""
        rid, past = rec["rfq_id"], self._clock() >= rec["end"] + READ_AFTER_END
        try:
            view, read = self._gw.request("GET", f"/rfqs/{rid}"), True
        except CrxError:
            view, read = {}, False
        view = view if isinstance(view, dict) else {}
        word = _trade_word(view)
        if _answered(word) or not past:
            return word, _final(view), view
        try:
            mine = [p for p in self.positions() if p.rfq_id.lower() == rid.lower()]
        except CrxError:
            return word, False, view
        if mine:
            return _text(mine[0].status) or "pending", mine[0].final, view
        if read and _text(view.get("trade_status")) is None:
            return "refused", False, view
        return word, False, view

    def _trade_again(self, rec: dict, wait: float) -> Trade:
        """Read the trade of an accept this client signed already, for ``wait`` s at most. Sign nothing."""
        q = rec["quote"]
        if rec["told"] and not rec["final"]:
            try:
                view = self._gw.request("GET", f"/rfqs/{rec['rfq_id']}")
            except CrxError:
                view = {}
            view = view if isinstance(view, dict) else {}
            if _text(view.get("trade_status")) is not None:
                word = _trade_word(view)
                rec.update(word=word, told=_answered(word), final=_final(view), provisional=_provisional(view),
                           tx=_word(view.get("trade_tx")) or rec.get("tx"))
        if not rec["told"]:
            deadline = self._clock() + wait
            while True:
                word, final, view = self._trade_read(rec)
                if _answered(word) or self._clock() >= deadline:
                    break
                self._sleep(1.0)
            rec.update(word=word, told=_answered(word), final=final, provisional=_provisional(view),
                       tx=_word(view.get("trade_tx")) or rec.get("tx"))
        log.info("rfq %s: accept signed already; read %s, nothing signed", q.rfq_id, rec["word"])
        return Trade(status=rec["word"], tx=rec.get("tx"), raw=rec["raw"], rfq_id=q.rfq_id,
                     quote_id=q.quote_id, pair=q.pair, side=q.side, notional=q.notional, rate=q.rate,
                     provisional=rec.get("provisional", False), final=rec["final"])

    def _earlier_trades(self) -> None:
        """Raise while an earlier accept has no answer, or once for an answer the caller has not read."""
        for rec in [r for r in self._accepts.values() if not r["told"]]:
            word, final, view = self._trade_read(rec)
            rid = rec["rfq_id"]
            if not _answered(word):
                raise TradeUnknown(f"the trade on rfq {rid} has no final answer yet; nothing signed for this "
                                   "quote; " + read_later(rec["end"]), details={"rfq_id": rid, "status": word})
            rec.update(word=word, told=True, final=final, provisional=_provisional(view))
            if word != "refused":
                raise EarlierOutcome(f"the trade on rfq {rid} is {word}; nothing signed for this quote; "
                                     "call accept() again to take it", details={"rfq_id": rid, "status": word})

    def _best_of(self, dropped: Quote, e: QuoteDropped) -> Quote | None:
        """Return the other live quote on the same RFQ that a ``QuoteDropped`` names, else None."""
        row = e.details.get("best")
        if not _live(row, self._clock() * 1000) or str(row.get("rfq_id") or dropped.rfq_id) != dropped.rfq_id:
            return None
        if str(row.get("quote_id")) == dropped.quote_id:
            return None
        try:
            return self._quote_of(dropped.rfq, row)
        except BadAnswer:
            return None

    def _take(self, b: Binder, quote: Quote, wait: float) -> tuple[Trade, dict]:
        """Accept one quote and read the trade for ``wait`` s at most. Return the trade and the last view."""
        rfq = quote.rfq
        rfq_id = quote.rfq_id
        ask = {
            "pair": rfq["pair"], "side": 1 if rfq["side"] == "buy" else -1, "notional": rfq["notional"],
            "expiry": rfq["expiry"], "premium_bps": rfq["premium_bps"], "rate": quote.rate,
            "quote_expiry_max": rfq["quote_expiry_max"], "max_premium_bps": rfq.get("max_premium_bps"),
            "leg_id": rfq.get("leg_id"),
        }
        base = dict(rfq_id=rfq_id, quote_id=quote.quote_id, pair=quote.pair, side=quote.side,
                    notional=quote.notional, rate=quote.rate)
        template = quote.raw.get("trade_template")
        r, t, _ = b.accept_trade(rfq_id, quote.quote_id, ask, template if isinstance(template, dict) else None,
                                 rfq.get("closes_at"))
        log.info("rfq %s @ %s: trade signed and posted with the accept, nonce %s", rfq_id, quote.rate,
                 clean(t["typed_data"]["message"]["ownNonce"]))
        status, view = self._settle(lambda: self._gw.request("GET", f"/rfqs/{rfq_id}"), "trade_status", wait)
        tx = _word(view.get("trade_tx"))
        log.info("rfq %s %s: tx %s", rfq_id, status, tx)
        return Trade(status=status, tx=tx, raw=r, provisional=_provisional(view), final=_final(view), **base), view

    def _settle(self, read: Callable[[], Any], key: str = "status", wait: float | None = None) -> tuple[str, dict]:
        """Poll ``read()`` until ``key`` is past ``pending``, for ``wait`` s (default ``settle_wait``)."""
        deadline = self._clock() + (self._settle_wait if wait is None else wait)
        word, seen = "pending", {}
        while True:
            try:
                got = read()
            except CrxError:
                got = None
            w = got.get(key) if isinstance(got, dict) else None
            if isinstance(w, str) and w:
                word, seen = clean(w, 40), got
                if w not in WAITING:
                    return word, seen
            if self._clock() >= deadline:
                return word, seen
            self._sleep(1.0)

    def deposit(self, amount: Any, *, wait: float | None = None, mint: bool = True) -> Deposit:
        """Deposit USDC from the seat wallet into collateral. The wallet pays the network fees.

        Args:
            amount: USDC to deposit, up to 6 decimals.
            wait: Seconds to wait for the deposit to count in free collateral. Defaults to 30 minutes on
                testnet and 15 minutes on mainnet.
            mint: On testnet, first mint the test USDC the wallet lacks. Defaults to True.

        Returns:
            Status ``credited`` with ``counted`` True once free collateral includes the deposit. When ``wait``
            ends first, ``counted`` is False.

        Raises:
            TxFailed: The wallet holds too little USDC, or a transaction reverted.
            SendUnknown: A transaction has no receipt after 300 s. Check its hash before sending again.
            DepositFailed: The deposit failed.
            RefusedToSign: The gateway served an unexpected transaction. Nothing was sent.
        """
        wait = self._wait_of(wait)
        self._need_seat()
        self._need_local_key("deposit()")
        amount = _amount(amount)
        c = self._chain_ready()
        r = self._gw.request("POST", "/deposit", body={"chain": self.chain_key, "amount": _plain(amount)})
        token, core = c.get("base_token"), c["core"]
        if not token:
            raise CrxError(f"the base token on {self.chain_key} is not ready yet; it becomes readable once /health names it.",
                           code="not_ready")
        decimals = self._rpc.int("eth_call", {"to": token, "data": e7.calldata("decimals()", [], [])}, "latest")
        raw = amount.scaleb(decimals)
        if raw != raw.to_integral_value():
            raise BadRequest(f"amount has more than {decimals} decimals")
        raw = int(raw)
        try:
            txs = r["transactions"]
            if int(r["amount_raw"]) != raw:
                raise RefusedToSign(f"the gateway served amount_raw {clean(r['amount_raw'], 40)}, not {_plain(amount)}; nothing sent")
            # depositFor to the seat itself is the transaction of an account CRX removed.
            calls =[e7.calldata("deposit(uint256)", ["uint256"], [raw]),
                     e7.calldata("depositFor(address,uint256)", ["address", "uint256"],
                                 [to_checksum_address(self.address), raw])]
            first = []
            if len(txs) == 2:
                first = [(token, e7.calldata("approve(address,uint256)", ["address", "uint256"],
                                             [to_checksum_address(core), raw]))]
            served = [(t["to"].lower(), t["data"].lower(), int(t["chain_id"])) for t in txs]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise RefusedToSign("the gateway served a deposit this SDK cannot read; nothing sent") from None
        want = next((w for w in ([*first, (core, d)] for d in calls)
                     if served == [(to.lower(), data, c["chain_id"]) for to, data in w]), None)
        if want is None:
            raise RefusedToSign("the gateway served a tx this SDK does not expect; nothing sent")
        hashes = []
        held = self._rpc.int("eth_call", {"to": token, "data": e7.calldata(
            "balanceOf(address)", ["address"], [to_checksum_address(self.address)])}, "latest")
        if held < raw and mint and c["chain_id"] in TESTNET_CHAIN_IDS:
            hashes.append(self._send("mint", token, e7.calldata(
                "mint(address,uint256)", ["address", "uint256"], [to_checksum_address(self.address), raw - held])))
        elif held < raw:
            have, need = _usdc(Decimal(held).scaleb(-decimals)), _usdc(amount)
            raise TxFailed(f"not enough USDC: you have {have}, need {need}",
                           details={"step": "deposit", "have": have, "need": need})
        for (to, data), name in zip(want, ["approve", "deposit"][-len(want):]):
            hashes.append(self._send(name, to, data))
        status, counted = self._deposit_counted(hashes[-1], wait, amount)
        return self._deposited(amount, hashes, status, counted)

    def _deposit_counted(self, tx: str, wait: float, amount: Decimal) -> tuple[str, bool]:
        """Poll ``/balance`` until deposit ``tx`` counts in free collateral, fails, or ``wait`` s end."""
        deadline = self._clock() + wait
        word = "pending"
        while True:
            try:
                raw = self.balance().raw
            except CrxError:
                raw = {}
            last = _last(raw, "deposit")
            if str(last.get("tx") or "").lower() != tx.lower():
                last = None
            w = _text(last.get("status")) if isinstance(last, dict) else None
            if w is not None:
                word = w
                if w == "failed":
                    return w, False
                free = dec(raw.get("free"))
                if w == "credited" and last.get("final") is True and free is not None and free >= amount:
                    return w, True
            if self._clock() >= deadline:
                return word, False
            self._sleep(min(COUNTED_POLL_S if word == "credited" else 1.0, deadline - self._clock()))

    @staticmethod
    def _deposited(amount: Decimal, txs: list[str], status: str, counted: bool = False) -> Deposit:
        """Build the ``Deposit``. Raise ``DepositFailed`` when ``status`` is ``failed``."""
        log.info("deposit %s %s%s", _plain(amount), status, ", counts in free collateral" if counted else "")
        if status == "failed":
            raise DepositFailed(f"the deposit of {_plain(amount)} USDC failed: it is not credited. Read balance() "
                                "before you deposit again", details={"amount": _plain(amount), "txs": list(txs)})
        return Deposit(amount=amount, txs=txs, status=status, counted=counted)

    def withdraw(self, amount: Any = None, *, wait: float | None = None) -> Withdraw:
        """Withdraw USDC from free collateral to the seat's own wallet. CRX pays the network fee.

        Call again with the same amount after an error to read that withdraw.

        Args:
            amount: USDC to withdraw, up to 6 decimals. None withdraws all free collateral, rounded down.
            wait: Seconds to retry while the withdraw cannot be signed or posted yet. Defaults to 30 minutes
                on testnet and 15 minutes on mainnet.

        Returns:
            The withdraw. ``paid``, ``partial``, ``refused`` and ``returned`` are final. ``sending``,
            ``pending`` and ``accepted`` are in progress. The next call waits for the final status.

        Raises:
            InsufficientCollateral: Free collateral stays below ``amount``.
            BelowMin: With no amount, free collateral stays below 1 USDC.
            MarkUnavailable: Free collateral is unknown while the open positions have no market price.
            WithdrawUnknown: This or an earlier withdraw has no known status yet. Call again with its amount.
            WithdrawInProgress: Another withdraw of the seat is still in progress when ``wait`` ends.
            EarlierOutcome: Reports once how an earlier withdraw ended. The call signs nothing.
        """
        self._need_seat()
        amount = None if amount is None else _amount(amount)
        wait = self._wait_of(wait)
        c = self._chain_ready()
        with self._withdraw_mx:
            held = self._held_withdraw
            end = self._clock() + wait
            if held is not None and held["returned"]:
                self._await_returned(held, end)
                held = None
            if held is not None:
                same = held.get("all", False) if amount is None else amount == held["amount"]
                if same:
                    return self._withdraw_again(held)
                word, final, _ = self._withdraw_read(held)
                what = f"the withdraw of {_plain(held['amount'])}, item {held['item']}"
                if not final:
                    raise WithdrawUnknown(f"{what}, is {word} with no final answer yet; nothing signed; call "
                                          f"withdraw({_plain(held['amount'])}) to read it",
                                          details=self._held_details(word))
                self._held_withdraw = None
                raise EarlierOutcome(f"{what}, is {word}; nothing signed for this amount; call withdraw() again",
                                     details=self._held_details(word, held))
            return self._withdraw_new(amount, c, max(end - self._clock(), 0.0))

    def _await_returned(self, held: dict, end: float) -> None:
        """Read a returned withdraw with no final status until it is final or ``end`` passes."""
        while True:
            word, final, _ = self._withdraw_read(held)
            if final:
                self._held_withdraw = None
                if word in ("refused", "returned"):
                    raise EarlierOutcome(f"the withdraw of {_plain(held['amount'])}, item {held['item']}, is {word}; "
                                         "nothing signed for this amount; call withdraw() again",
                                         details=self._held_details(word, held))
                log.info("withdraw nonce %s: %s; signing the next one", held["nonce"], word)
                return
            if self._clock() + WITHDRAW_AGAIN_S > end:
                raise WithdrawInProgress(
                    f"the withdraw of {_plain(held['amount'])}, item {held['item']}, is {word}; nothing signed. "
                    "Call withdraw() again once it is paid", details=self._held_details(word))
            self._sleep(WITHDRAW_AGAIN_S)

    def _withdraw_new(self, amount: Decimal | None, c: dict, wait: float) -> Withdraw:
        """Sign and post a new intent. Sign again while another withdraw of the seat is in progress."""
        end = self._clock() + wait
        while True:
            try:
                return self._withdraw_sign(amount, c, end)
            except WithdrawInProgress as e:
                if e.status != 409 or self._clock() + WITHDRAW_AGAIN_S > end:
                    raise
                log.info("withdraw: another withdraw of this seat is in progress; signing again in %g s",
                         WITHDRAW_AGAIN_S)
                self._sleep(WITHDRAW_AGAIN_S)

    def _withdraw_sign(self, amount: Decimal | None, c: dict, end: float) -> Withdraw:
        """Read the balance, then sign one intent and post it until ``end``."""
        everything = amount is None
        short_end = min(self._clock() + FREE_SHORT_S, end)
        nonce_end = min(self._clock() + NONCE_WAIT_S, end)
        bal = self.balance()
        while True:
            # /balance names no free collateral for a moment after a settlement update.
            while bal.free is None and self._clock() + WITHDRAW_AGAIN_S <= end:
                log.info("withdraw: /balance names no free collateral; reading it again in %g s", WITHDRAW_AGAIN_S)
                self._sleep(WITHDRAW_AGAIN_S)
                bal = self.balance()
            free = bal.free
            if free is None:
                raise MarkUnavailable("free collateral is unknown: the open positions have no market price now; "
                                      "nothing signed. Withdraw again later")
            if not free.is_finite():
                raise BadAnswer("/balance names a free collateral this SDK cannot read")
            want = free.quantize(Decimal("0.000001"), rounding=ROUND_DOWN) if everything else amount
            if (want >= MIN_WITHDRAW) if everything else (want <= free):
                if bal.withdraw_nonce is not None:
                    break
                # After a settlement, /balance names no nonce until the gateway reads it.
                reason = _nonce_reason(bal.raw)
                if reason not in NONCE_CLEARS or self._clock() + NONCE_AGAIN_S > nonce_end:
                    why = f" ({reason})" if reason else ""
                    raise RefusedToSign(f"/balance names no withdraw nonce for this seat{why}; nothing signed",
                                        details={"nonce_reason": reason} if reason else None)
                log.info("withdraw: /balance names no withdraw nonce (%s); reading it again in %g s", reason,
                         NONCE_AGAIN_S)
                self._sleep(NONCE_AGAIN_S)
                bal = self.balance()
                continue
            if self._clock() + WITHDRAW_AGAIN_S > short_end:
                if everything:
                    raise BelowMin(f"free collateral is {_usdc(free)} USDC, under the {MIN_WITHDRAW} USDC minimum "
                                   "of withdraw(); nothing signed",
                                   details={"min": str(MIN_WITHDRAW), "free": _usdc(free)})
                raise InsufficientCollateral(
                    f"withdraw of {_plain(amount)} USDC is above free collateral of {_usdc(free)} USDC; nothing "
                    "signed", details={"free": _usdc(free), "requested": _usdc(amount)})
            log.info("withdraw: free collateral %s USDC is short; reading it again in %g s", _usdc(free),
                     WITHDRAW_AGAIN_S)
            self._sleep(WITHDRAW_AGAIN_S)
            bal = self.balance()
        amount = want
        nonce = bal.withdraw_nonce
        if nonce is None:
            raise RefusedToSign("/balance names no withdraw nonce for this seat; nothing signed")
        # The chain pays the account. ``recipient`` is signed only when DROP_WITHDRAW_RECIPIENT is off.
        w = {"account": self.address, "amount": e7.scaled6(amount), "recipient": self.address,
             "nonce": nonce, "deadline": int(self._clock()) + WITHDRAW_TTL}
        digest = e7.withdraw_digest(self._sep, w)
        td = e7.typed_data("WithdrawIntent", c["chain_id"], c["core"], e7.withdraw_message(w))
        sig = sign_typed(self._signer, td, digest, self.address)
        body = {"chain": self.chain_key, "amount": _plain(amount), "nonce": str(nonce), "deadline": w["deadline"],
                "sig": sig}
        item = e7.h0x(e7.withdraw_item(w))
        # returned: a Withdraw went back with no final status. all: the call named no amount.
        held = self._held_withdraw = {"amount": amount, "nonce": nonce, "deadline": w["deadline"], "item": item,
                                      "body": body, "returned": False, "sent": self._clock(), "all": everything}
        while True:
            held["sent"] = self._clock()
            try:
                r = self._gw.request("POST", "/withdraw", body=body, ok=(200, 202))
                break
            except ServiceUnavailable:
                self._held_withdraw = None  # CRX sends nothing
                raise
            except (NetworkError, ServerError) as e:
                # No answer: the gateway may hold the intent, so its status decides.
                log.info("withdraw nonce %s: %s on the post; reading its status", nonce, e.code)
                return self._withdraw_settle(held, posted=False)
            except BadAnswer:
                raise  # the hold stays: the next call reads the status
            except CrxError as e:
                # Nothing queued. A refusal that clears with time gets the same signed intent again.
                if not _clears(e) or self._clock() + WITHDRAW_AGAIN_S > end:
                    self._held_withdraw = None
                    raise
                log.info("withdraw nonce %s: %s; posting the same signed intent in %g s", nonce, e.code,
                         WITHDRAW_AGAIN_S)
                self._sleep(WITHDRAW_AGAIN_S)
        got = _word(r.get("item"))
        if got is None:
            raise BadAnswer("/withdraw named no item")
        if got != item:
            raise BadAnswer("/withdraw queued an item other than the signed withdraw")
        return self._withdraw_settle(held, posted=True)

    def _withdraw_read(self, held: dict) -> tuple[str, bool, dict | None | bool]:
        """Read the held intent once. ``row`` is None when /balance does not show it, False when unread."""
        try:
            bal = self.balance()
        except CrxError:
            return "pending", False, False  # not read: never "not shown"
        last = _last(bal.raw, "withdraw")
        it = _word(last.get("item"))
        moved = bal.withdraw_nonce is not None and bal.withdraw_nonce > held["nonce"]
        # Some rows name no item. Once the nonce passed, the row at the intent's nonce and amount is its own.
        if it == held["item"].lower() or (moved and it is None and _same(last, held)):
            st = _text(last.get("status")) or "pending"
            return st, st not in WITHDRAW_LIVE, last
        if moved:
            return "unknown", True, None
        if self._clock() > held["deadline"]:
            return "refused", True, None
        return "pending", False, None

    def _withdraw_settle(self, held: dict, posted: bool, refused: dict | None = None) -> Withdraw:
        """Poll the held intent for ``settle_wait`` s at most, until it is ``accepted`` or final."""
        deadline = self._clock() + self._settle_wait
        while True:
            word, final, last = self._withdraw_read(held)
            if final or word not in WAITING or self._clock() >= deadline:
                break
            self._sleep(1.0)
        if not last and not posted and not final:
            details = self._held_details(word)
            if refused is not None:
                details["refusal"] = refused
            raise WithdrawUnknown(f"the withdraw of {_plain(held['amount'])} may still be sent; nothing else "
                                  f"signed; call withdraw({_plain(held['amount'])}) to read it", details=details)
        if final:
            self._held_withdraw = None
        if word == "unknown":
            raise EarlierOutcome(f"the withdraw of {_plain(held['amount'])}, item {held['item']}, was paid or never "
                                 "will be: its nonce is spent; read balance() before another withdraw",
                                 details=self._held_details(word, held))
        if final and last is None:
            raise EarlierOutcome(f"the withdraw of {_plain(held['amount'])}, item {held['item']}, is {word}: its "
                                 "deadline passed; call withdraw() again for a new one",
                                 details=self._held_details(word, held))
        tx = _word((last or {}).get("tx"))
        if not final:
            held["returned"] = True
        log.info("withdraw %s %s, nonce %s, item %s", _plain(held["amount"]), word, held["nonce"], held["item"])
        return Withdraw(amount=held["amount"], nonce=held["nonce"], item=held["item"], tx=tx, status=word)

    def _withdraw_again(self, held: dict) -> Withdraw:
        """Read the held intent. Post it again, same signature, when /balance does not show it."""
        word, final, last = self._withdraw_read(held)
        now = self._clock()
        refused = None
        # The gateway can still queue an earlier post after it refuses a repeat: repeat once none is in flight.
        if last is None and not final and held["sent"] + IN_FLIGHT_S <= now < held["deadline"] - REPOST_MARGIN:
            log.info("withdraw nonce %s: not shown; posting the same signed intent", held["nonce"])
            held["sent"] = now
            try:
                self._gw.request("POST", "/withdraw", body=held["body"], ok=(200, 202))
            except CrxError as e:
                if _past_held_check(e):
                    self._held_withdraw = None  # a held intent gets 200, so the gateway holds none
                    raise
                log.info("withdraw nonce %s: %s on the repeat post; reading its status", held["nonce"], e.code)
                refused = _refused(e) if e.status is not None else None
        return self._withdraw_settle(held, posted=False, refused=refused)

    def _held_details(self, word: str, held: dict | None = None) -> dict:
        h = held or self._held_withdraw or {}
        return {"item": h.get("item"), "nonce": str(h.get("nonce")), "amount": _plain(h["amount"]) if h else None,
                "status": word}

    def add_viewer(self, viewer: str) -> Viewer:
        """Give the wallet ``viewer`` read access to this seat's balance, positions and trades.

        A seat has at most 5 viewers. Adding the same wallet again has no effect.
        """
        self._need_seat()
        return _viewer(self._gw.request("PUT", f"/viewers/{_address(viewer, 'viewer')}", ok=(200, 201)))

    def remove_viewer(self, viewer: str) -> None:
        """Remove the read access of the wallet ``viewer``. A wallet without access raises no error."""
        self._need_seat()
        self._gw.request("DELETE", f"/viewers/{_address(viewer, 'viewer')}", ok=(200, 204))

    def viewers(self) -> list[Viewer]:
        """List the wallets with read access to this seat."""
        self._need_seat()
        rows = self._gw.request("GET", "/viewers").get("viewers")
        if not isinstance(rows, list):
            raise BadAnswer("/viewers lists no viewers")
        return [_viewer(v) for v in rows if isinstance(v, dict)]

    def test_request(self, *, wait: float | None = None) -> Rfq:
        """Request a test RFQ for this maker: 100 USD of USD/MXN at zero premium.

        The request lasts 15 minutes and each RFQ closes after 120 s. Call again to renew an expired RFQ
        that no one accepted, except in the last minute. A live or accepted RFQ comes back unchanged. Each
        maker starts one request per UTC day, within a global limit of ten. ``reference_rate`` is an
        example quote rate.

        Args:
            wait: Seconds to retry while rate limited or while new collateral does not count yet. Defaults
                to 30 minutes on testnet and 15 minutes on mainnet.

        Raises:
            InsufficientCollateral: The seat's collateral still does not count when ``wait`` ends.
            RateLimited: The rate limit outlasts ``wait``.
            CrxError: Code ``test_requests_off``. This network offers no test requests.
        """
        from ._test_requests import validate

        self._need_seat()
        self._keep_live()
        wait = self._wait_of(wait)
        end = self._clock() + wait
        while True:
            try:
                answer = self._gw.request("POST", "/test-requests", body={"chain": self.chain_key})
                break
            except RateLimited as e:
                pause = None if e.retry_after is None else max(e.retry_after, 1.0)
                if pause is None or self._clock() + pause > end:
                    raise
                log.info("test request: rate limited; sending again in %g s", pause)
                self._sleep(pause)
            except (InsufficientCollateral, ServerError, AuthError) as e:
                # A refusal for uncounted collateral opens no request, so the day's request stays unused.
                last = _uncounted(e)
                if last is None:
                    raise
                if self._clock() + TEST_REQUEST_COLLATERAL_RETRY > end:
                    if last is e:
                        raise
                    raise last from e
                log.info("test request: collateral not counted yet; sending again in %g s", TEST_REQUEST_COLLATERAL_RETRY)
                self._sleep(TEST_REQUEST_COLLATERAL_RETRY)
            except CrxError as e:
                if e.status != 404:
                    raise
                raise CrxError(f"test requests are not offered on {self.chain_key}", code="test_requests_off",
                               status=404, details={**e.details, "message": str(e)},
                               gateway_code=e.gateway_code) from None
        validate(answer, self.chain_key, self._clock(), maker=True, allow_accepted=True)
        return _maker.rfq_of(answer)

    def rfqs(
        self, *, after: int | None = None, wait: float | None = None, poll: float = 1.0,
        stop: threading.Event | None = None, only: Ask | str | None = None, kind: str = "open",
    ) -> Iterator[Rfq]:
        """Yield the open RFQs this seat can quote, as they arrive.

        The gateway sends RFQs only to a maker seat with collateral. Each RFQ comes once, from another
        seat, inside its quote window. The first maker call starts the keepalive (see ``Client``).

        Args:
            after: Start after this ``Rfq.seq``. Defaults to the start of the tape.
            wait: Seconds until the stream ends. Defaults to no end.
            poll: Seconds between reads. Defaults to 1. Reads share a limit of 5 requests a second per IP.
            stop: A ``threading.Event`` that ends the stream when set.
            only: An ``Ask`` or RFQ id. Yield that RFQ alone, then end.
            kind: ``"open"`` (default), or ``"close"`` for the close RFQs ``take_over()`` quotes.

        Raises:
            NoQuotes: With ``only``, ``wait`` ends before the RFQ arrives.
        """
        self._need_seat()
        if kind not in ("open", "close"):
            raise BadRequest('kind is "open" or "close"')
        self._keep_live()
        return _maker.stream(self, after, wait, poll, stop, only, kind)

    def rfq(self, rfq_id: str) -> Rfq:
        """Return one RFQ as this seat sees it.

        For the taker, ``quotes`` contains the winning quote only. For a maker, it contains that maker's quotes.
        """
        self._need_seat()
        return _maker.read(self, rfq_id)

    def send_quote(
        self, rfq: Rfq, rate: Any, *, premium_bps: int = 0, expiry: datetime | timedelta | int | None = None,
        client_quote_id: str | None = None,
    ) -> MakerQuote:
        """Post a binding quote at ``rate`` on ``rfq``. Pass the result to ``confirm()``.

        The quote is the trade signature. The maker signs nothing after the accept. It binds until the
        RFQ's ``quote_expiry_max``, or until ``drop_quote()``. A later quote on the same RFQ replaces it.
        After a network error the quote may still rest: ``drop_quote(rfq)`` ends it.

        Args:
            rate: The quoted rate for ``rfq.pair``, up to 6 decimals.
            premium_bps: The maker's premium, equal to the RFQ's. Above 0 the taker pays. Defaults to 0.
            expiry: Settlement time as a datetime or unix ms, or a timedelta that accepts any settlement up
                to that far from now. Defaults to ``rfq.expiry``.
            client_quote_id: Label for the quote on the tape. Defaults to the quote id.

        Raises:
            RefusedToSign: The RFQ's premium or expiry differs from these terms.
            InsufficientCollateral: Free collateral does not cover the quote before the RFQ closes.
            QuoteExpired: The RFQ has ended.
            QuoteLost: Less than 60 s of the RFQ's quote window is left.
            LegLive: The seat has another live leg on the RFQ. Quote again, or drop it.
        """
        self._need_seat()
        self._need_local_key("send_quote()")
        self._keep_live()
        return _maker.send(self, rfq, rate, client_quote_id, premium_bps, expiry)

    def confirm(self, quote: MakerQuote, *, timeout: float | None = None, poll: float = 1.0,
                wait: float | None = None) -> Trade:
        """Wait for the taker to accept a binding quote, then return the trade.

        The maker signs nothing more, and CRX pays the network fee.

        Args:
            timeout: Seconds to wait for the accept. Defaults to the end of the RFQ's quote window.
            poll: Seconds between reads. Defaults to 1.
            wait: Seconds to read the trade after the accept. Defaults to 30 minutes on testnet and 15
                minutes on mainnet.

        Returns:
            The trade once it is ``open`` or ``refused``, else ``sending`` or ``pending`` when ``wait`` ends.

        Raises:
            QuoteLost: No trade opened. ``reason`` is ``another_maker``, ``expired``, ``cancelled``,
                ``dropped`` or ``timeout``.
        """
        self._need_seat()
        self._need_local_key("confirm()")
        wait = self._wait_of(wait)
        self._keep_live()
        return _maker.confirm(self, quote, timeout, poll, wait)

    def drop_quote(self, quote: MakerQuote | Rfq, *, leg_id: str | None = None) -> Drop:
        """End a binding quote now, with every quote this seat posted on its leg.

        A dropped leg is final. The next ``send_quote()`` on the RFQ opens a new one.

        Args:
            quote: The ``MakerQuote``, or the ``Rfq`` it quotes.
            leg_id: With an ``Rfq``, the leg to drop, as a ``LegLive`` error names it. Defaults to the leg
                this client last quoted on the RFQ.

        Raises:
            AlreadyAccepted: The taker accepted first. The trade stands: pass the quote to ``confirm()``.
            UnknownOrEnded: The RFQ has ended, or holds no such leg of this seat.
        """
        self._need_seat()
        self._keep_live()
        return _maker.drop(self, quote, leg_id)

    def take_over(self, rfq: Rfq | str, rate: Any, *, wait: float | None = None,
                  client_quote_id: str | None = None) -> TakeOver:
        """Quote a close RFQ at ``rate``. When the quote wins, take over the closing seat's side of the trade.

        The quote is a price only. When the round picks it, the SDK checks the item against the RFQ and the
        rate, then signs it within the round's sign window. CRX sends the transaction and pays the network fee.

        Args:
            rfq: A close ``Rfq`` from ``rfqs(kind="close")``, or its id.
            rate: The rate for ``rfq.pair``, up to 6 decimals.
            wait: Seconds to wait for ``open`` or ``refused``. Default 30 minutes on testnet, 15 on mainnet.
            client_quote_id: Label for the quote. Defaults to a new id.

        Raises:
            QuoteExpired: The round picked another quote, or ended.
            RefusedToSign: The item differs from the RFQ or the rate. The SDK declines it. Nothing is signed.
            InsufficientCollateral: Free collateral does not cover the trade at ``rate``.
        """
        self._need_seat()
        self._keep_live()
        return _close.take_over(self, rfq, rate, self._wait_of(wait), client_quote_id=client_quote_id)

    def quote_live(self, price: Callable[[Rfq], Any], seconds: float = 3600, *,
                   log: Callable[[str], None] | None = print) -> list[Trade]:
        """Quote each new RFQ at ``price(rfq)`` for ``seconds``, and return the trades that opened.

        Each round reads new RFQs for 2 s, posts one binding quote per priced RFQ, then reads each live
        quote once. It drops a quote that has no accept 10 s after its RFQ closes. At the end, on Ctrl-C
        or on an error, it drops every live quote.

        Args:
            price: Takes an ``Rfq`` and returns a rate with at most 6 decimals, or None to skip the RFQ.
                ``rfq.side`` is the maker's side of ``rfq.pair``.
            seconds: How long to quote. Defaults to 3600.
            log: Takes one line per event, local time first. Defaults to ``print``. None logs nothing.
        """
        self._need_seat()
        self._need_local_key("quote_live()")
        return _maker.live(self, price, seconds, log)

    def _send(self, what: str, to: str, data: str) -> str:
        self._need_local_key("a transaction")
        tx = send_tx(self._rpc, self._account, self._chain_ready()["chain_id"], what, to, data,
                     sleep=self._sleep, log=self._txlog)
        log.info("%s tx %s", what, tx)
        return tx


__all__ = ["Client", "MAINNET_CORE", "NETWORKS"]
