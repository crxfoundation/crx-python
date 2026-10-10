"""The CRX client."""

from __future__ import annotations

import base64
import binascii
import logging
import os
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
from ._bind import Binder
from ._chain import Rpc, TxLog, send_tx
from . import _solana as sol
from ._http import Gateway
from ._keys import load_account
from . import _maker
from .signer import LocalSigner, as_signer, sign_typed
from .errors import (
    AboveMax, BadAnswer, BadRequest, BelowMin, ConfigError, CrxError,
    MarketPaused, NoQuotes, QuoteDropped, RefusedToSign, RfqCancelled, TxFailed, clean,
)
from .models import (
    Ask, Balance, Deposit, Drop, Event, MakerQuote, Market, Position, Quote, Rfq, Trade, Viewer, Withdraw, dec,
    ms_to_dt, side_word,
)

log = logging.getLogger("crx")

# settle_wait: s that trade(), deposit() and withdraw() poll their own status at most.
# check_minute: the minute past each hour of the hourly check.
# chain_id and core (Ethereum rows): the chain and the CRX core contract the SDK signs for. The EIP-712 domain
# is name "CRX", version "rulebook-1.0", this chain_id and this core. /health must name the same chain id, core
# and domain separator, else every signing call refuses. Client(core=) replaces the row's core.
NETWORKS = {
    "testnet": {
        "chain": "avax-fuji",
        "base_url": "https://api.sandbox.crxfx.com",
        "rpc_url": "https://api.avax-test.network/ext/bc/C/rpc",
        "settle_wait": 30.0,
        "check_minute": 5,
        "chain_id": 43113,
        "core": "0xa2F94aA752D4a703028eCfAE8686264dC0928B9c",
    },
    # Ethereum mainnet. Off unless the caller opts in; no default RPC.
    "mainnet": {
        "chain": "ethereum",
        "base_url": "https://api.crxfx.com",
        "rpc_url": None,
        "settle_wait": 90.0,
        "check_minute": 35,
        "chain_id": 1,
        "core": "0x90e32979611dB01CDFbA49C1446995EcB97a26bf",
    },
    # Solana mainnet. Off unless the caller opts in (allow_mainnet); no default RPC. program_id is the one
    # place the SDK names the CRX program: base58, in full, from the birth record (birth-pins.py emit).
    # With no program_id every signing call refuses and no wallet is read. Another cluster: add a copy of
    # this row to NETWORKS under a new name, with that cluster's base_url, rpc_url, genesis_hash, cluster_tag,
    # program_id and mint; it also needs allow_mainnet.
    "solana": {
        "chain": "solana",
        "family": "solana",
        "base_url": f"https://{sol.HOST}/api",
        "rpc_url": None,
        "settle_wait": 90.0,
        "check_minute": 5,
        "genesis_hash": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
        "cluster_tag": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
        "program_id": "A32Z1LwBwyE6UB8SmcF1mwHKQfEhtVQ95s9jfpqDFvWE",
        # The second RPC that reads a deposit's status before it reads as not sent.
        "check_rpc_url": "https://api.mainnet-beta.solana.com",
        "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    },
}
_ALIASES = {"fuji": "testnet"}
TESTNET_CHAIN_IDS = {43113, 84532, 11142220}
MAINNET_CHAIN_IDS = {1}
DEFAULT_STATE_DIR = "~/.crx-quickstart"  # shared with the CRX quickstart scripts: one nonce floor per seat
WAITING = ("", "sending", "pending")  # statuses _settle polls past
WITHDRAW_TTL = 22 * 3600  # s; the gateway takes a deadline at most 23 h out
OPEN_TIMEOUT = 30.0  # s; POST /rfqs answers after the 10 s window, the gateway stops at 30 s
# The 409 conflict of POST /withdraw while the gateway reads an older settlement.
UNREAD_SETTLEMENT = "the latest settlement is not read yet"
UNREAD_RETRY_S = 1200.0  # s that withdraw() posts the same signed intent again on that 409
UNREAD_BACKOFF = (15.0, 60.0)  # s: the first pause between posts, doubled up to the second
OPEN_STATUSES = ("open", "quoted")  # RFQ statuses that still take an accept


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


def _address(value: Any, what: str, err: type[CrxError] = BadRequest) -> str:
    """A 0x wallet address, lower case. A bad checksum or the zero address is refused."""
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
    """``/balance`` ``<key>.last``, or ``{}``."""
    d = b.get(key)
    last = d.get("last") if isinstance(d, dict) else None
    return last if isinstance(last, dict) else {}


def _word(value: Any) -> str | None:
    """A 0x 32-byte hex word, lower case; None for anything else."""
    s = value.lower() if isinstance(value, str) else ""
    return s if len(s) == 66 and s[:2] == "0x" and all(ch in "0123456789abcdef" for ch in s[2:]) else None


def _plain(d: Decimal) -> str:
    """A decimal string with no exponent."""
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _usdc(d: Decimal) -> str:
    """A USDC amount with 6 decimals, rounded down."""
    return format(d.quantize(Decimal("0.000001"), rounding=ROUND_DOWN), "f")


def _pair(pair: str) -> tuple[str, str]:
    """('USD/MXN', 'USDMXN') from any of USD/MXN, usdmxn, USD-MXN."""
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


def _no_worse(quote: Quote, other: Quote) -> bool:
    """True when ``other`` costs the taker no more than ``quote``: a buy pays no higher rate, a sell gets no lower."""
    return other.rate <= quote.rate if quote.side == "buy" else other.rate >= quote.rate


def _said(view: Any) -> dict:
    """The gateway's line on an RFQ view: ``outcome`` and ``message``, each when served."""
    out = {}
    if isinstance(view, dict):
        for key, limit in (("outcome", 64), ("message", 300)):
            v = view.get(key)
            if isinstance(v, str) and v.strip():
                out[key] = clean(v, limit)
    return out


def _no_quotes(text: str, view: Any, details: dict) -> NoQuotes:
    """``NoQuotes`` with ``text``. The gateway's line, when served, goes into ``details`` and, in
    brackets, after ``text``."""
    said = _said(view)
    details.update(said)
    if "message" in said:
        text += f" ({said['outcome']}: {said['message']})" if "outcome" in said else f" ({said['message']})"
    return NoQuotes(text, details=details)


def _ended(view: dict, rfq_id: str) -> CrxError | None:
    """The error for an RFQ that ended before an accept; None while it is open. A view with no ``status``
    reads as open. A cancelled RFQ carries the gateway's ``reason`` and ``message``; any other carries
    the gateway's ``outcome`` and ``message``, when served."""
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
    """A quote row that can still win: ``quoted`` and not past ``expires_at``."""
    return (isinstance(row, dict) and row.get("status") == "quoted" and isinstance(row.get("expires_at"), int)
            and not isinstance(row.get("expires_at"), bool) and row["expires_at"] > now_ms)


def _default_expiry(now: datetime) -> datetime:
    """One month out, a weekend rolled to Monday."""
    t = now + timedelta(days=30)
    while t.weekday() >= 5:
        t += timedelta(days=1)
    return t


class Client:
    """One seat on one CRX network.

    ``key`` is the seat wallet's private key. Without it, only ``health`` and
    ``markets`` work. The key is read from ``key``, ``key_file``,
    ``CRX_WALLET_PK`` or ``CRX_WALLET_PK_FILE``, in that order.

    ``signer`` replaces the key with a custodian (see ``crx.signer``): it signs the
    typed data the SDK builds, and EIP-191 for the gateway login. The client logs in by
    ``POST /session``: one login signature per 8 h, and again after a gateway restart
    or revoke. ``deposit``, ``send_quote`` and ``confirm`` need a local key.

    ``account`` is another seat this key may read (see ``add_viewer``). With it,
    ``balance``, ``positions`` and ``trades`` read that seat; every other call is refused.

    Each Ethereum network pins its chain id and core (``NETWORKS``); the signing domain is built from that
    pin. When /health names another chain id, core or domain, every signing call refuses and nothing is
    signed. ``core`` replaces the pinned core: pass it only with an address CRX publishes.

    ``network="mainnet"`` (Ethereum, chain 1) is off unless ``allow_mainnet=True``
    or ``CRX_ALLOW_MAINNET=1``. Its gateway is ``https://api.crxfx.com`` unless
    ``base_url`` or ``CRX_BASE`` names another. It has no default RPC: pass ``rpc_url``
    or set ``CRX_RPC``.

    On Solana, ``check_rpc_url`` or ``CRX_CHECK_RPC`` names a second RPC (default: the row's public one). A
    deposit tx with no confirmed status is "not sent" only when both RPCs read its own blockhash expired
    and then hold no status for it; else ``deposit()`` raises ``SendUnknown``. The same URL as the RPC counts
    as none.

    A GET that fails on the network or with a 5xx is sent once more after 0.5 s.
    POST, PUT and DELETE are sent once.

    ``keepalive`` (1 to 10 s, default 5): the first maker call (``rfqs``,
    ``send_quote``, ``confirm``, ``drop_quote``) starts a daemon thread. It reads
    ``/trades`` whenever this client read none for ``keepalive`` s: the gateway
    counts a maker live for 15 s after it reads ``/trades``. None: no thread.
    ``close()`` stops it.
    """

    def __init__(
        self,
        key: str | bytes | None = None,
        *,
        key_file: str | os.PathLike | None = None,
        network: str = "testnet",
        base_url: str | None = None,
        rpc_url: str | None = None,
        check_rpc_url: str | None = None,
        state_dir: str | os.PathLike | None = None,
        timeout: float = 10.0,
        session: requests.Session | None = None,
        account: str | None = None,
        allow_mainnet: bool = False,
        signer: Any = None,
        keepalive: float | None = _maker.KEEPALIVE_S,
        keypair: Any = None,
        core: str | None = None,
    ) -> None:
        self._account = None
        self._signer = None
        self._custody = None
        self._live: _maker.Keepalive | None = None
        if signer is not None and (key is not None or key_file is not None):
            key = keypair = None
            raise ConfigError("set key, key_file or signer: one only")
        lo, hi = _maker.KEEPALIVE_RANGE
        if keepalive is not None and (isinstance(keepalive, bool) or not isinstance(keepalive, (int, float))
                                      or not lo <= keepalive <= hi):
            key = keypair = None
            raise ConfigError(f"keepalive is None, or {lo:g} to {hi:g} s")
        self._keepalive_s = None if keepalive is None else float(keepalive)
        self._live_lock = threading.Lock()
        name = _ALIASES.get(network, network)
        net = NETWORKS.get(name)
        if net is None:
            key = keypair = None
            raise ConfigError(f"unknown network {clean(network, 20)!r}; known: {', '.join(NETWORKS)}")
        self.network = name
        self._net = net
        pin_core = net.get("core")
        if core is not None:
            if net.get("family") == "solana":
                key = keypair = None
                raise ConfigError("core= is for Ethereum networks; on Solana the SDK pins the program")
            try:
                pin_core = _address(core, "core", ConfigError)
            except ConfigError:
                key = keypair = None
                raise
        self._pin_core = pin_core.lower() if isinstance(pin_core, str) else None
        self._keypair = None
        seat = None
        if keypair is not None:
            if net.get("family") != "solana":
                key = keypair = None
                raise ConfigError("keypair= is for network='solana'")
            if signer is not None or key is not None or key_file is not None:
                key = keypair = None
                raise ConfigError("set keypair (the Solana wallet) or key, key_file, signer: one only")
        self.chain_key = net["chain"]
        self._settle_wait = net["settle_wait"]
        self._check_minute = net["check_minute"]
        gw_url = base_url or os.environ.get("CRX_BASE") or net["base_url"]
        rpc = rpc_url or os.environ.get("CRX_RPC") or net["rpc_url"]
        if name == "mainnet" or net.get("family") == "solana":
            refusal = None
            if not (allow_mainnet is True or os.environ.get("CRX_ALLOW_MAINNET") == "1"):
                refusal = "mainnet is off: pass allow_mainnet=True or set CRX_ALLOW_MAINNET=1"
            elif not rpc:
                refusal = f"{name} has no default RPC: pass rpc_url= or set CRX_RPC"
            if refusal:
                key = keypair = None
                raise ConfigError(refusal)
        if keypair is not None:
            # After the network refusals: an off network reads no wallet, and neither does one with no
            # program pinned. The wallet secret leaves this frame's locals once the Keypair holds it, or
            # fails to.
            try:
                sol.need_pin(net)
                self._keypair = sol.Keypair(keypair)
            finally:
                keypair = None
            # The seat key is never a local of this frame: load_account takes it and drops it.
            seat = load_account(sol.seat_secret(self._keypair))
        self._session = session or requests.Session()
        try:
            gateway = sol.SeatGateway if net.get("family") == "solana" else Gateway
            self._gw = gateway(gw_url, None, self._session, timeout)
        except ConfigError:
            key = None
            raise
        self._rpc = Rpc(rpc, self._session, max(timeout, 20.0))
        check = check_rpc_url or os.environ.get("CRX_CHECK_RPC") or net.get("check_rpc_url")
        self._check_rpc = Rpc(check, self._session, max(timeout, 20.0)) if check and check != rpc else None
        try:
            custody = None if account is None else _address(account, "account", ConfigError)
        except ConfigError:
            key = None
            raise
        # The key leaves this frame's locals as soon as it is loaded, or fails to load.
        try:
            self._account = seat if seat is not None else (load_account(key, key_file) if signer is None else None)
        except ConfigError:
            key = None
            raise
        key = None
        self._signer = as_signer(signer) if signer is not None else as_signer(self._account)
        self._gw._signer = self._signer
        self._gw.login = signer is not None and not isinstance(self._signer, LocalSigner)
        if self._signer is not None and custody != self.address:
            self._custody = self._gw.custody = custody
        self._state_dir = Path(state_dir or os.environ.get("CRX_STATE_DIR") or DEFAULT_STATE_DIR).expanduser()
        self._chain: dict | None = None
        self._sep: bytes | None = None
        self._rpc_checked = False
        self._gw.sleep = time.sleep
        self._clock = time.time
        self._legs: dict[str, str] = {}  # rfq_id -> this seat's binding leg id on it
        self._txlog = TxLog(self._state_dir / "txs.json",  # the txs this SDK sent: no key material
                            own_dir=not (state_dir or os.environ.get("CRX_STATE_DIR")))

    def __repr__(self) -> str:
        return f"crx.Client(network={getattr(self, 'network', None)!r}, address={self.address!r})"

    def __getstate__(self) -> Any:
        raise TypeError("crx.Client holds a key and cannot be pickled or copied")

    @property
    def address(self) -> str | None:
        """The key's (or the signer's) address, lower case. None without either."""
        signer = getattr(self, "_signer", None)
        return signer.address.lower() if signer is not None else None

    @property
    def account(self) -> str | None:
        """The seat this client reads: ``account=`` when set, else ``address``."""
        return getattr(self, "_custody", None) or self.address

    @property
    def _sleep(self) -> Callable[[float], None]:
        """The pause of this client and of its gateway's GET retry."""
        return self._gw.sleep

    @_sleep.setter
    def _sleep(self, fn: Callable[[float], None]) -> None:
        self._gw.sleep = fn

    def close(self) -> None:
        """Stop the maker keepalive (see ``Client``). Later maker calls do not start it again."""
        with self._live_lock:
            self._keepalive_s = None
            if self._live is not None:
                self._live.stop()

    def _keep_live(self) -> None:
        """Start the maker keepalive once: a daemon thread that reads ``/trades`` whenever this
        client read none for ``keepalive`` s. It stops on ``close()``, or once the client is freed."""
        with self._live_lock:
            if self._live is not None or self._keepalive_s is None:
                return
            self._live = _maker.Keepalive(self._gw, self._keepalive_s)
            weakref.finalize(self, self._live.stop)
            self._live.start()

    # ---------- setup checks ----------

    def _need_key(self) -> None:
        if self._signer is None:
            raise ConfigError("this call needs the seat key: set CRX_WALLET_PK or pass key=")

    def _need_local_key(self, what: str) -> None:
        """A call that signs a transaction or a maker hash: it needs a local key, not a custodian signer."""
        self._need_key()
        if self._account is None:
            raise ConfigError(f"{what} needs a local key (key= or CRX_WALLET_PK), not a signer")

    def _need_seat(self) -> None:
        """The key, acting for its own seat."""
        self._need_key()
        if self._custody is not None:
            raise ConfigError("a viewer (account=) only reads: balance, positions, trades")

    def _chain_info(self) -> dict:
        """This network's chain from /health, by the network's chain key. The served chain id and core must
        equal the pinned ones (the network row, or ``core=``), and the served domain separator must equal the
        one of the pinned chain id and core. The typed-data domain is the pinned chain id and core."""
        if self._chain is not None:
            return self._chain
        chains = self.health().get("chains")
        if not isinstance(chains, list):
            raise BadAnswer("/health lists no chains")
        c = next((c for c in chains if isinstance(c, dict)
                  and (c.get("key") or c.get("chain")) == self.chain_key), None)
        if c is None:
            raise ConfigError(f"{self._gw.host} does not serve {self.chain_key}")
        if self._net.get("family") == "solana":
            c = sol.check_cluster(self._rpc, self._net, c)
            self._chain, self._sep, self._rpc_checked = c, e7.domain_separator(None, c["core"]), True
            return self._chain
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
        """The chain, with the RPC checked: right chain id, and the core is a contract."""
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
        binder = sol.SeatBinder if self._net.get("family") == "solana" else Binder
        return binder(self._gw, self._signer, c, self._sep, self._state_dir, self._sleep, self._clock)

    # ---------- public reads ----------

    def health(self) -> dict:
        """The gateway's /health answer."""
        return self._gw.request("GET", "/health", auth=False)

    def next_check(self) -> datetime:
        """The time of the next hourly check, UTC: :05 past the hour on testnet and solana, :35 on mainnet.

        No call is made.
        """
        now = datetime.fromtimestamp(self._clock(), timezone.utc)
        at = now.replace(minute=self._check_minute, second=0, microsecond=0)
        return at if at > now else at + timedelta(hours=1)

    def markets(self) -> list[Market]:
        """Every pair: session open, paused, notional limits, the premium cap, the next long close.

        The request names this client's chain (``?chain=``). A row with ``chains`` is paused on this
        chain when it lists no entry for the chain, or the entry reads ``paused: true``; an entry with
        no ``paused`` reads as not paused, and the cap is the entry's. A row with no ``chains`` is this
        chain's: offered, the cap is the row's ``max_premium_bps``.
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
        """One pair. A pair /markets does not offer on this chain raises ``MarketPaused``: no row, or a
        row with ``chains`` and no entry for this chain."""
        slash, _ = _pair(pair)
        m = next((m for m in self.markets() if m.pair == slash), None)
        if m is None or ("chains" in m.raw and not any(
                isinstance(c, dict) and c.get("chain") == self.chain_key for c in m.raw.get("chains") or [])):
            raise MarketPaused(f"{slash} is not offered on {self.chain_key}", details={"pair": slash})
        return m

    # ---------- seat reads ----------

    def balance(self) -> Balance:
        """Collateral, margin and withdraw state."""
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
        """Your open positions."""
        self._need_key()
        body = self._gw.request("GET", "/positions", query={"chain": self.chain_key})
        rows = body.get("positions")
        if not isinstance(rows, list):
            raise BadAnswer("/positions lists no positions")
        return [
            Position(
                trade_id=str(p.get("trade_id")), rfq_id=str(p.get("rfq_id")), pair=str(p.get("pair")),
                side=side_word(p.get("side")), notional=dec(p.get("notional")), rate=dec(p.get("rate")),
                status=p.get("status"), raw=p,
            )
            for p in rows if isinstance(p, dict)
        ]

    def trades(self, after: int = 0, *, market: bool = False) -> list[Event]:
        """Your own event tape, oldest first. Trade events: ``trade.opened``, ``trade.refused``,
        ``trade.settled``, ``trade.closed``, ``trade.closed_out`` and ``trade.novated``. Margin events:
        ``margin.called`` and ``margin.cured``. ``rfq.accepted`` names ``rfq_id``, ``quote_id`` and
        ``client_quote_id`` only.

        Key trades on ``trade_id``. A ``trade.opened`` with ``provisional: true`` is followed, once final, by a
        ``trade.opened`` with no ``provisional`` key; that one is the final state. ``trade.retracted`` removes a
        provisional open.

        ``after`` is a seq of this client's tape: pass the last ``Event.seq`` to read only newer
        events. Seqs count per account and role (a viewer reads as a taker), so a seq read by
        another client, or by SDK 0.6, is not a cursor here. An ``after`` past your head raises
        (409 ``conflict``).
        A maker seat also receives every open RFQ on the venue (``rfq.opened``, no
        owner named). By default an ``rfq.opened`` is kept only when it carries a
        ``client_rfq_id``: the gateway serves that field to the RFQ's taker alone.
        ``quote()`` and ``ask()`` always send one; an RFQ opened elsewhere without one shows
        its other events only. ``market=True`` keeps every ``rfq.opened``.
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

    # ---------- trading ----------

    def quote(
        self,
        pair: str,
        side: str,
        notional: Any,
        *,
        expiry: datetime | timedelta | int | None = None,
        premium_bps: int = 0,
        wait: float = 30.0,
        client_rfq_id: str | None = None,
    ) -> Quote:
        """Open an RFQ and return the best firm quote. Nothing is accepted.

        ``side`` is your side in the base currency. ``expiry`` is the settlement
        instant: a datetime, a timedelta from now, or unix ms. Default: one
        month out, off the weekend.

        ``premium_bps`` is the upfront premium, -10000 to 10000: above 0 the taker
        pays, below 0 the maker pays. Default 0. ``trade()`` signs only when the
        served premium is this one and within the market's cap (``Market.max_premium_bps``).

        ``client_rfq_id`` is 1 to 128 printable ASCII characters.

        One call: the gateway answers after the 10 s window with the winner.
        An older gateway answers at once: then the SDK polls for ``wait`` s.
        ``trade()`` takes the quote until ``Quote.closes_at``, 120 s after the request.

        A closed market still takes the RFQ: the gateway decides. Its refusal
        raises the matching error; no quote raises ``NoQuotes``. An RFQ the gateway
        cancelled raises ``RfqCancelled`` (a ``NoQuotes``) with its ``reason``, e.g.
        ``rate_out_of_band``: no quote was inside the off-market band.

        On Solana a settlement outside the tenor band the pair's /markets row serves raises
        ``BadRequest`` (``details['limit']`` is ``tenor``): no RFQ is sent.
        """
        rfq, r = self._open(pair, side, notional, expiry, premium_bps, client_rfq_id, True)
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
        expiry: datetime | timedelta | int | None = None,
        premium_bps: int = 0,
        client_rfq_id: str | None = None,
    ) -> Ask:
        """Open an RFQ and return at once. ``Ask.quote()`` returns the winning quote.

        Takes the terms of ``quote()`` and makes the same checks. The gateway
        answers before the 10 s window ends, with no quote.
        """
        rfq, r = self._open(pair, side, notional, expiry, premium_bps, client_rfq_id, False)
        return Ask(rfq_id=rfq["rfq_id"], pair=rfq["pair"], side=rfq["side"], notional=Decimal(rfq["notional"]),
                   expiry=ms_to_dt(rfq["expiry"]), raw=r, rfq=rfq, _client=self)

    def _open(
        self, pair: str, side: str, notional: Any, expiry: datetime | timedelta | int | None, premium_bps: int,
        client_rfq_id: str | None, wait: bool,
    ) -> tuple[dict, dict]:
        """Check the terms and POST /rfqs. ``wait``: no ``wait`` is sent, and the gateway answers after
        the 10 s window, with the winner. Else ``wait: false`` is sent, and it answers at once.
        Returns (the RFQ's terms, the gateway's answer). ``closes_at`` (unix ms) is the RFQ's end:
        no accept after it."""
        self._need_seat()
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
        now = datetime.now(timezone.utc)
        if expiry is None:
            expiry = _default_expiry(now)
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
        if self._net.get("family") == "solana":
            sol.check_tenor(m.min_tenor_s, m.max_tenor_s, expiry_ms, now.timestamp())
        req = {
            "chain": self.chain_key, "pair": compact, "side": side, "notional": _plain(amount),
            "expiry": expiry_ms, "client_rfq_id": client_rfq_id or f"sdk-{uuid.uuid4().hex[:12]}",
        }
        if premium_bps:
            req["premium_bps"] = premium_bps
        if not wait:
            req["wait"] = False  # the gateway answers at once; with no `wait` it answers after the window
        r = self._gw.request("POST", "/rfqs", body=req,
                             timeout=max(self._gw._timeout, OPEN_TIMEOUT) if wait else None)
        rfq_id, leg_id, qe_max = _word(r.get("rfq_id")), _word(r.get("leg_id")), r.get("quote_expiry_max")
        if rfq_id is None or leg_id is None or type(qe_max) is not int:
            raise BadAnswer("/rfqs sent an answer this SDK cannot read")
        closes_at = r.get("closes_at") if type(r.get("closes_at")) is int else None
        log.info("rfq %s opened: %s %s %s", rfq_id, slash, side, _plain(amount))
        rfq = {"rfq_id": rfq_id, "leg_id": leg_id, "quote_expiry_max": qe_max, "pair": slash, "side": side,
               "notional": _plain(amount), "expiry": expiry_ms, "premium_bps": premium_bps,
               "max_premium_bps": m.max_premium_bps, "closes_at": closes_at}
        return rfq, r

    def _winner(self, rfq: dict, wait: float) -> Quote:
        """The winning quote on the open RFQ ``rfq``, polled for ``wait`` s at most."""
        return self._quote_of(rfq, self._await_quote(rfq["rfq_id"], wait))

    @staticmethod
    def _quote_of(rfq: dict, q: dict) -> Quote:
        """A quote row on the RFQ ``rfq`` as a ``Quote``: the rate and expires_at from the row, the terms
        and ``closes_at`` from the RFQ's own ask."""
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
        """The gateway's pick in a taker view, else its best live quote, else None. An RFQ that ended raises.

        A row counts only when live: ``quoted`` and not past ``expires_at``. Dropped, declined and
        expired rows never win. A pick that is not live counts as none.
        """
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
        """Poll GET /rfqs/{id}: the gateway's pick once named, else the best live quote at the end.
        A pick that is not ``quoted`` counts as none: the best live quote then goes at once.
        An RFQ that ended raises at once."""
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

    def trade(self, quote: Quote) -> Trade:
        """Accept a quote and open the trade: you sign a readable ``Trade``. CRX sends the tx and pays gas.

        The quote carries the trade template. The SDK builds its own ``Trade`` from your
        ask and the quote's rate, compares the served one with it member by member, checks
        the domain, the leg id's quote end and the nonce, signs its own typed data, and
        posts the accept with the signature: one call. Any difference raises
        ``RefusedToSign`` and nothing is signed. When the template is stale, the gateway
        answers with a fresh one, which is checked and signed in turn.

        When the maker dropped the quote, the gateway names the best live quote on the
        same RFQ. The SDK accepts it once when its rate is no worse than ``quote.rate``.
        Otherwise ``QuoteDropped`` raises, with that quote on ``.best`` (or None).

        Returns once the trade is ``open`` or ``refused``. When neither shows within
        30 s (testnet) or 90 s (mainnet), returns ``sending`` or ``pending``.
        ``ServiceUnavailable`` raises when the service takes no trade now: no trade opened.
        """
        self._need_seat()
        if not isinstance(quote, Quote):
            raise BadRequest("trade() takes the Quote that quote() returned")
        b = self._binder()
        try:
            return self._take(b, quote)
        except QuoteDropped as e:
            e.best = self._best_of(quote, e)
            if e.best is None or not _no_worse(quote, e.best):
                raise
            best = e.best
        log.info("rfq %s: quote %s dropped; accepting quote %s @ %s", quote.rfq_id, quote.quote_id,
                 best.quote_id, best.rate)
        try:
            return self._take(b, best)
        except QuoteDropped as e:
            e.best = self._best_of(best, e)
            raise

    def _best_of(self, dropped: Quote, e: QuoteDropped) -> Quote | None:
        """The best live quote a quote_dropped answer names: another live quote on the same RFQ, else None."""
        row = e.details.get("best")
        if not _live(row, self._clock() * 1000) or str(row.get("rfq_id") or dropped.rfq_id) != dropped.rfq_id:
            return None
        if str(row.get("quote_id")) == dropped.quote_id:
            return None
        try:
            return self._quote_of(dropped.rfq, row)
        except BadAnswer:
            return None

    def _take(self, b: Binder, quote: Quote) -> Trade:
        """Accept one quote, then read the trade status."""
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
        status, view = self._settle(lambda: self._gw.request("GET", f"/rfqs/{rfq_id}"), "trade_status")
        tx = _word(view.get("trade_tx"))
        log.info("rfq %s %s: tx %s", rfq_id, status, tx)
        return Trade(status=status, tx=tx, raw=r, **base)

    def _settle(self, read: Callable[[], Any], key: str = "status") -> tuple[str, dict]:
        """Poll ``read()`` for a ``key`` status other than ``sending`` or ``pending``, at most
        the network's ``settle_wait`` s. Returns (status, the answer that carried it).

        ``read`` returns a dict, or None when the gateway does not show it yet. A gateway
        error counts as None. Past the wait: the last ``sending`` or ``pending`` seen, else ``pending``.
        """
        deadline = self._clock() + self._settle_wait
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

    def _money_last(self, key: str, match: str, value: str) -> dict | None:
        """``/balance`` ``<key>.last`` when its ``match`` field is ``value``."""
        last = _last(self.balance().raw, key)
        return last if str(last.get(match) or "").lower() == value.lower() else None

    # ---------- money ----------

    def deposit(self, amount: Any, *, mint: bool = True, keypair: Any = None, unsigned: bool = False,
                authority: str | None = None) -> Deposit:
        """Deposit USDC to the core: approve (when short), then deposit. You pay gas.

        On a testnet, ``mint=True`` first mints the test USDC the wallet lacks. Otherwise a
        wallet that holds less than ``amount`` raises ``TxFailed`` and nothing is sent.
        The core call is ``deposit``, or ``depositFor`` to this seat's own address (an account CRX removed
        can still deposit to itself). Any other call, or ``depositFor`` to another address, raises
        ``RefusedToSign`` and nothing is sent.
        Returns once the deposit is ``credited`` or ``failed``; ``pending`` when
        neither shows within 30 s (testnet) or 90 s (mainnet).

        A tx that reverted on the chain raises ``TxFailed``. A tx with no receipt after 300 s raises
        ``SendUnknown`` with its hash: it can still be mined. Check it before you send again.

        On Solana the seat's bound authority signs one tx: pass its ``keypair`` (a keypair file, list or
        bytes; needs ``crx-python[solana]``), or ``unsigned=True`` and the wallet's ``authority`` (base58;
        default: the client's own keypair) for the checked, unsigned tx in ``txs`` (base64) with status
        ``unsigned``.
        """
        if self._net.get("family") != "solana":
            if keypair is not None or unsigned or authority is not None:
                keypair = None
                raise ConfigError("keypair=, unsigned= and authority= are for network='solana'")
        else:
            kp = None
            if keypair is not None:
                # No program pinned: the wallet is not read. The wallet secret leaves this frame's locals
                # once the Keypair holds it, or fails to.
                try:
                    sol.need_pin(self._net)
                    kp = sol.Keypair(keypair)
                finally:
                    keypair = None
            return self._deposit_solana(amount, kp, unsigned, authority)
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
            # deposit(raw), or depositFor(raw) to this seat's own address: the tx of an account CRX removed.
            calls = [e7.calldata("deposit(uint256)", ["uint256"], [raw]),
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
        status, _ = self._settle(lambda: self._money_last("deposit", "tx", hashes[-1]))
        log.info("deposit %s %s", _plain(amount), status)
        return Deposit(amount=amount, txs=hashes, status=status)

    def _need_solana(self, what: str) -> None:
        if self._net.get("family") != "solana":
            raise ConfigError(f"{what} is a Solana call; this client is on {self.network}")

    def _bind_state(self) -> dict:
        b = self._gw.request("GET", "/bind")
        if not isinstance(b, dict):
            raise BadAnswer("/bind sent an answer that is not an object")
        return b

    def bind_state(self) -> dict:
        """Solana: this seat's bind as CRX reads it, the ``GET /bind`` answer. ``bound`` is True once the chain
        holds the bind. ``status`` is ``unbound``, ``pending``, ``failed`` or ``bound``. ``authority``,
        ``payout_wallet`` and ``payout_ata`` are base58, None while unbound. A bound answer names the keys the
        chain holds: ``bound`` says the seat is bound, not to which keys. Compare the three with your own, or
        call ``bind()``. Nothing is posted."""
        self._need_seat()
        self._need_solana("bind_state()")
        return self._bind_state()

    def _await_bind(self, filed: dict) -> dict:
        last = sol.await_bind(self._bind_state, filed, self._clock, self._sleep)
        log.info("bind %s -> bound", self.address)
        return last

    def bind(self, authority: str | None = None, payout_wallet: str | None = None) -> dict:
        """Solana: bind this seat to its Ed25519 ``authority`` (signs deposits) and ``payout_wallet`` (default:
        the authority; withdrawals pay its USDC account). The seat signs ``BindSeat``; CRX sends the tx and
        pays its fee. Once only per seat: the bind fixes the payout account for good.

        Returns the ``GET /bind`` answer that reads the seat bound with the three keys of this bind: the
        authority, the payout wallet and its USDC account, each compared as 32 bytes. The status word alone
        ends nothing. The call reads ``GET /bind`` for 240 s at most: a bind lands in seconds, and after a
        fault on CRX's side its end can take 2 to 3 minutes.

        - A seat already bound to these three keys returns that answer: nothing is signed or posted.
        - A seat bound to another payout wallet or payout account raises ``SeatBoundOtherPayout``; to this
          payout and another authority, ``CrxError`` (``seat_already_bound``). Both carry
          ``details['bound']`` and ``details['filed']``.
        - A bind still in progress after the wait raises ``BindInProgress``: it has not failed, and
          ``bind()`` reads its end. So does a bind filed while another bind of the seat is in progress.
        - A bind that ended with the seat not bound raises ``BindFailed``.
        - ``ServiceUnavailable``: the service sends no bind now; the bind is not filed."""
        self._need_seat()
        self._need_solana("bind()")
        authority = authority or (self._keypair.pubkey if self._keypair is not None else None)
        if authority is None:
            raise BadRequest("bind() needs the authority: pass it, or make the client with keypair=")
        payout_wallet = payout_wallet or authority
        try:
            auth_b, pay_b = sol.key(authority), sol.key(payout_wallet)
        except ValueError:
            raise BadRequest("authority and payout_wallet are 32-byte base58 keys") from None
        sol.need_pin(self._net)
        filed = sol.bind_keys(authority, payout_wallet, self._net["mint"])
        held = self._bind_state()
        end = sol.bind_end(held, filed)
        if end is not None:
            return end
        if held.get("status") == "pending":
            # A bind of this seat is in progress, and the gateway takes no other. With these three keys its
            # end is this call's: nothing is signed or posted.
            if not sol.same_keys(held, filed):
                raise sol.BindInProgress(sol.BIND_LIVE_LINE, details={"filed": sol.shown_keys(held)})
            return self._await_bind(filed)
        c = self._chain_ready()
        r = sol.post_bind(self._gw, {"authority": authority, "payout_wallet": payout_wallet}, filed)
        if r is None:
            return self._await_bind(filed)
        now = int(self._clock())
        try:
            b = r["bind"]
            nonce, deadline = int(b["nonce"]), int(b["deadline"])
            same = (e7.address(b["seat"]) == self.address and b["authority"] == authority
                    and b["payout"] == payout_wallet)
            ata = str(r["payout_ata"])
        except (KeyError, TypeError, ValueError):
            raise RefusedToSign("/bind served a bind this SDK cannot read; nothing signed") from None
        if not same or ata != sol.ata(payout_wallet, self._net["mint"]):
            raise RefusedToSign("/bind served another seat, key or payout account; nothing signed")
        if not now < deadline <= now + 24 * 3600:
            raise RefusedToSign("/bind served a deadline out of range; nothing signed")
        if not 0 <= nonce < 2**64:
            raise RefusedToSign("/bind served a nonce out of range; nothing signed")
        msg = {"seat": self.address, "authority": e7.h0x(auth_b), "payout": e7.h0x(pay_b), "nonce": str(nonce),
               "deadline": str(deadline)}
        td = e7.typed_data("BindSeat", None, c["core"], msg)
        digest = e7.typed_digest(td)
        if e7.typed_mismatch(r.get("typed_data"), td) is not None or _word(r.get("digest")) != e7.h0x(digest):
            raise RefusedToSign("/bind served typed data other than the SDK's own; nothing signed")
        sig = sign_typed(self._signer, td, digest, self.address)
        sol.post_bind(self._gw, {"authority": authority, "payout_wallet": payout_wallet, "nonce": str(nonce),
                                 "deadline": deadline, "sig": sig}, filed)
        return self._await_bind(filed)

    def _deposit_solana(self, amount: Any, kp: Any, unsigned: bool, authority: str | None) -> Deposit:
        self._need_seat()
        if unsigned and kp is not None:
            raise ConfigError("Solana deposit(): pass keypair= or unsigned=True, not both")
        if not unsigned and kp is None and self._keypair is None:
            raise ConfigError("Solana deposit(): pass keypair= (the bound authority) or unsigned=True")
        if not unsigned and authority is not None:
            raise ConfigError("Solana deposit(): authority= goes with unsigned=True; the keypair names its own")
        amount = _amount(amount)
        # The wallet that signs: the keypair's key; for an unsigned tx, authority= or the client's keypair.
        if unsigned:
            wallet = authority if authority is not None else getattr(self._keypair, "pubkey", None)
            if wallet is None:
                raise ConfigError("Solana deposit(unsigned=True) needs authority= (the wallet that signs it)")
            try:
                sol.key(wallet)
            except ValueError:
                raise BadRequest("authority is a 32-byte base58 key") from None
        else:
            kp = kp if kp is not None else self._keypair
            wallet = kp.pubkey
        c = self._chain_ready()
        if c.get("base_token") != self._net["mint"] or int(c.get("base_decimals", -1)) != 6:
            raise RefusedToSign("/health names another token than this SDK pins; nothing signed")
        raw = e7.scaled6(amount)
        b = self._bind_state()
        if b.get("bound") is not True or not isinstance(b.get("authority"), str):
            raise RefusedToSign("this seat is not bound yet: call bind() first; nothing signed")
        if b["authority"] != wallet:
            raise ConfigError("the wallet is not this seat's bound authority; nothing signed")
        r = self._gw.request("POST", "/deposit", body={"chain": self.chain_key, "amount": _plain(amount)})
        try:
            (t,) = r["transactions"]
            ok = (t["family"], t["encoding"], t["version"]) == ("solana", "base64", "legacy")
            rawtx = base64.b64decode(t["tx"], validate=True)
            served_raw = int(r["amount_raw"])
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise RefusedToSign("the gateway served a deposit this SDK cannot read; nothing sent") from None
        if not ok or served_raw != raw:
            raise RefusedToSign("the gateway served another deposit than asked; nothing sent")
        parsed = sol.check_deposit(
            rawtx, program_id=c["program_id"], authority=wallet, source=sol.ata(wallet, self._net["mint"]),
            amount_raw=raw, row=None, seat20=bytes.fromhex(self.address[2:]))
        if kp is None:
            return Deposit(amount=amount, txs=[t["tx"]], status="unsigned")
        before = _last(self.balance().raw, "deposit")
        sig = sol.send_and_confirm(self._rpc, sol.signed_tx(parsed, kp), check_rpc=self._check_rpc,
                                   sleep=self._sleep, clock=self._clock)

        def credited() -> dict | None:
            # The gateway names the tx when it knows it; else a new record of this amount is this deposit.
            last = _last(self.balance().raw, "deposit")
            if last.get("tx") == sig:
                return last
            if last.get("tx") is None and last != before and str(last.get("amount")) == _plain(amount):
                return last
            return None

        status, _ = self._settle(credited)
        log.info("deposit %s %s", _plain(amount), status)
        return Deposit(amount=amount, txs=[sig], status=status)

    def withdraw(self, amount: Any) -> Withdraw:
        """Withdraw USDC to your own wallet. You sign the intent. CRX sends the tx and pays gas.

        Builds the intent from your next withdraw nonce, signs it and posts it in one request.
        The deadline is 22 h out. A 409 ``conflict`` "the latest settlement is not read yet" posts
        the same signed intent again, after 15 s, then 30 s, then every 60 s, for at most 20 minutes;
        then ``CrxError`` (``conflict``). Returns once the withdraw shows a status other than
        ``sending`` or ``pending``. When none shows within 30 s (testnet) or 90 s (mainnet),
        returns ``sending`` or ``pending``. The status is the one ``/balance`` shows for the signed item.
        """
        self._need_seat()
        amount = _amount(amount)
        c = self._chain_ready()
        if self._net.get("family") == "solana":
            # The chain pays the ATA fixed at bind (bind() checks it); WithdrawIntent names no payout.
            b = self._bind_state()
            if b.get("bound") is not True:
                raise RefusedToSign("this seat is not bound yet: call bind() first; nothing signed")
        nonce = self.balance().withdraw_nonce
        if nonce is None:
            raise RefusedToSign("/balance names no withdraw nonce for this seat; nothing signed")
        # The chain pays the account only. ``recipient`` is a member with drop_withdraw_recipient off only.
        w = {"account": self.address, "amount": e7.scaled6(amount), "recipient": self.address,
             "nonce": nonce, "deadline": int(self._clock()) + WITHDRAW_TTL}
        digest = e7.withdraw_digest(self._sep, w)
        td = e7.typed_data("WithdrawIntent", c["chain_id"], c["core"], e7.withdraw_message(w))
        sig = sign_typed(self._signer, td, digest, self.address)
        body = {"chain": self.chain_key, "amount": _plain(amount), "nonce": str(nonce), "deadline": w["deadline"],
                "sig": sig}
        until, pause = self._clock() + UNREAD_RETRY_S, UNREAD_BACKOFF[0]
        while True:
            try:
                r = self._gw.request("POST", "/withdraw", body=body, ok=(200, 202))
                break
            except CrxError as e:
                # crx-api writes its conflict as "conflict: <why>" in the body's error.
                text = str(e.details.get("error") or e).removeprefix("conflict: ")
                if not (e.status == 409 and e.code == "conflict" and text.startswith(UNREAD_SETTLEMENT)):
                    raise
                left = until - self._clock()
                if left <= 0:
                    raise CrxError("withdraw not sent: the latest settlement is not yet confirmed; "
                                   "the request was retried for 20 minutes.", code="conflict", status=409,
                                   details=e.details, gateway_code=e.gateway_code) from None
                # The same signed intent: the gateway answers a held item with the same body.
                self._sleep(min(pause, left))
                pause = min(pause * 2, UNREAD_BACKOFF[1])
        item = _word(r.get("item"))
        if item is None:
            raise BadAnswer("/withdraw named no item")
        if item != e7.h0x(e7.withdraw_item(w)):
            raise BadAnswer("/withdraw queued an item other than the signed withdraw")
        status, last = self._settle(lambda: self._money_last("withdraw", "item", item))
        tx = _word(last.get("tx"))
        log.info("withdraw %s %s, nonce %s, item %s", _plain(amount), status, nonce, item)
        return Withdraw(amount=amount, nonce=nonce, item=item, tx=tx, status=status)

    # ---------- viewers ----------

    def add_viewer(self, viewer: str) -> Viewer:
        """Let ``viewer`` read your balance, positions and trades. At most 5 viewers. A repeat is a no-op."""
        self._need_seat()
        return _viewer(self._gw.request("PUT", f"/viewers/{_address(viewer, 'viewer')}", ok=(200, 201)))

    def remove_viewer(self, viewer: str) -> None:
        """Take back ``viewer``'s read access. No error when it had none."""
        self._need_seat()
        self._gw.request("DELETE", f"/viewers/{_address(viewer, 'viewer')}", ok=(200, 204))

    def viewers(self) -> list[Viewer]:
        """The wallets that may read your seat."""
        self._need_seat()
        rows = self._gw.request("GET", "/viewers").get("viewers")
        if not isinstance(rows, list):
            raise BadAnswer("/viewers lists no viewers")
        return [_viewer(v) for v in rows if isinstance(v, dict)]

    # ---------- maker ----------

    def rfqs(
        self, *, after: int | None = None, wait: float | None = None, poll: float = 1.0,
        stop: threading.Event | None = None, only: Ask | str | None = None,
    ) -> Iterator[Rfq]:
        """Open RFQs you can quote, as they arrive: the ``rfq.opened`` frames of your tape.

        Needs a maker seat with collateral: the gateway sends no RFQ to a seat short of it.
        Yields open RFQs on this network, another seat's, inside their quote window, each
        once. The call reads your tape up to its head before it returns: after ``after``
        (an ``Rfq.seq`` this client read), else from the start. Reads again every ``poll`` s:
        the tape shares a per-IP read budget of 5 requests a second. Ends after ``wait`` s, or once ``stop``
        (a ``threading.Event``) is set; by default it never ends. Break out of the loop to stop.

        ``only`` (an ``Ask``, or an RFQ id) yields that RFQ alone, then ends. When ``wait``
        ends before it arrives, ``NoQuotes`` raises.

        The first maker call starts the keepalive (see ``Client``): the seat stays live
        between reads, also while ``confirm`` waits.
        """
        self._need_seat()
        self._keep_live()
        return _maker.stream(self, after, wait, poll, stop, only)

    def rfq(self, rfq_id: str) -> Rfq:
        """One RFQ as your seat reads it. As its taker, ``quotes`` holds the winning quote only
        and ``client_rfq_id`` is yours. As a maker, ``quotes`` holds your own quotes only."""
        self._need_seat()
        return _maker.read(self, rfq_id)

    def send_quote(
        self, rfq: Rfq, rate: Any, *, premium_bps: int = 0, expiry: datetime | timedelta | int | None = None,
        client_quote_id: str | None = None,
    ) -> MakerQuote:
        """Post a binding quote at ``rate`` on ``rfq``: you sign a ``Quote``. The taker gets the best quote only.

        The quote carries the RFQ's terms and your own side (the opposite of the taker's).
        Your rate prices a premium and a settlement: the SDK signs only those.
        ``premium_bps`` is your premium (default 0): the RFQ's must be the same. Above 0
        the taker pays, below 0 you pay. ``expiry`` is your settlement instant: by default
        ``rfq.expiry``, as you read it; a datetime or unix ms names another; a timedelta
        takes any settlement up to that long from now. Other terms raise ``RefusedToSign``
        before a signature exists.
        It is your trade signature: nothing is signed after the accept. It binds you until
        the RFQ's ``quote_expiry_max`` (at least 60 s out, else ``QuoteLost``), the end its
        leg id carries; ``drop_quote`` ends it sooner. A later quote on the same RFQ replaces
        the earlier one on the same leg. ``client_quote_id`` (optional) names the quote on
        your tape; the gateway defaults it to the quote id.

        Refusals: ``RefusedToSign`` (the RFQ's premium or expiry is not yours);
        ``InsufficientCollateral``; ``QuoteExpired`` when the RFQ ended;
        ``NotWhitelisted``; ``LegLive`` (your seat holds another live leg on the RFQ: quote
        again, or drop it); ``LegIdTaken`` (quote again); ``QuoteFillsFull``;
        ``QuoteFormatOutdated`` (the gateway takes a newer quote format: update the SDK).
        After a network error the quote may rest: ``drop_quote(rfq)`` ends it.
        Pass the result to ``confirm``.
        """
        self._need_seat()
        self._need_local_key("send_quote()")
        self._keep_live()
        return _maker.send(self, rfq, rate, client_quote_id, premium_bps, expiry)

    def confirm(self, quote: MakerQuote, *, timeout: float | None = None, poll: float = 1.0) -> Trade:
        """Wait for the taker's accept of your binding quote, then for the trade. You sign nothing more.
        CRX sends the tx and pays gas.

        Waits at most ``timeout`` s for the accept; by default until the RFQ's quote window
        closes. Returns once the trade is ``open`` or ``refused``; ``sending`` or ``pending``
        when neither shows within 30 s (testnet) or 90 s (mainnet).

        No trade raises ``QuoteLost``; ``reason`` is ``another_maker``, ``expired``,
        ``cancelled``, ``dropped`` or ``timeout``.
        """
        self._need_seat()
        self._need_local_key("confirm()")
        self._keep_live()
        return _maker.confirm(self, quote, timeout, poll)

    def drop_quote(self, quote: MakerQuote | Rfq, *, leg_id: str | None = None) -> Drop:
        """End your binding quote at once: its leg, with every quote you posted on it.

        Takes the ``MakerQuote``, or the ``Rfq`` with ``leg_id=`` (a ``LegLive`` error names
        one). A repeat answers the first drop's time. ``AlreadyAccepted`` when the taker
        accepted first: the trade stands, pass the quote to ``confirm``. ``UnknownOrEnded``
        when the RFQ ended or holds no such leg of yours. A dropped leg is final: the next
        ``send_quote`` on the RFQ makes a new one.
        """
        self._need_seat()
        self._keep_live()
        return _maker.drop(self, quote, leg_id)

    def _send(self, what: str, to: str, data: str) -> str:
        self._need_local_key("a transaction")
        tx = send_tx(self._rpc, self._account, self._chain_ready()["chain_id"], what, to, data,
                     sleep=self._sleep, log=self._txlog)
        log.info("%s tx %s", what, tx)
        return tx


__all__ = ["Client", "NETWORKS"]
