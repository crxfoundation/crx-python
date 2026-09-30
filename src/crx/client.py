"""The CRX client."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator

import requests
from eth_utils import is_checksum_address, is_hex_address, keccak, to_checksum_address

from . import _eip712 as e7
from ._bind import Binder
from ._chain import Rpc, send_tx
from ._http import Gateway
from ._keys import load_account
from . import _maker
from .errors import (
    AboveMax, BadAnswer, BadRequest, BelowMin, ConfigError, CrxError,
    MarketPaused, NoQuotes, RefusedToSign, clean,
)
from .models import (
    Balance, Deposit, Event, MakerQuote, Market, Position, Quote, Rfq, Trade, Viewer, Withdraw, dec, ms_to_dt,
    side_word,
)

log = logging.getLogger("crx")

# settle_wait: s that trade(), deposit() and withdraw() poll their own status at most.
NETWORKS = {
    "testnet": {
        "chain": "avax-fuji",
        "base_url": "https://api.crxfx.com",
        "rpc_url": "https://api.avax-test.network/ext/bc/C/rpc",
        "settle_wait": 30.0,
    },
    # Ethereum mainnet. Off unless the caller opts in; no default URLs.
    "mainnet": {
        "chain": "ethereum",
        "base_url": None,
        "rpc_url": None,
        "settle_wait": 90.0,
    },
}
_ALIASES = {"fuji": "testnet"}
TESTNET_CHAIN_IDS = {43113, 84532, 11142220}
MAINNET_CHAIN_IDS = {1}
DEFAULT_STATE_DIR = "~/.crx-quickstart"  # shared with the CRX quickstart scripts: one Side nonce floor per seat
WAITING = ("", "sending", "pending")  # statuses _settle polls past


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


def _pair(pair: str) -> tuple[str, str]:
    """('USD/MXN', 'USDMXN') from any of USD/MXN, usdmxn, USD-MXN."""
    s = "".join(ch for ch in str(pair).upper() if ch.isalpha())
    if len(s) != 6:
        raise BadRequest(f"unknown pair {clean(pair, 20)!r}")
    return f"{s[:3]}/{s[3:]}", s


def _side(side: str) -> str:
    s = str(side).strip().lower()
    if s not in ("buy", "sell"):
        raise BadRequest("side is 'buy' or 'sell'")
    return s


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

    ``account`` is another seat this key may read (see ``add_viewer``). With it,
    ``balance``, ``positions`` and ``trades`` read that seat; every other call is refused.

    ``network="mainnet"`` (Ethereum, chain 1) is off unless ``allow_mainnet=True``
    or ``CRX_ALLOW_MAINNET=1``. It has no default URLs: pass ``base_url`` and
    ``rpc_url``, or set ``CRX_BASE`` and ``CRX_RPC``.
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
    ) -> None:
        self._account = None
        self._custody = None
        name = _ALIASES.get(network, network)
        net = NETWORKS.get(name)
        if net is None:
            key = None
            raise ConfigError(f"unknown network {clean(network, 20)!r}; known: {', '.join(NETWORKS)}")
        self.network = name
        self.chain_key = net["chain"]
        self._settle_wait = net["settle_wait"]
        gw_url = base_url or os.environ.get("CRX_BASE") or net["base_url"]
        rpc = rpc_url or os.environ.get("CRX_RPC") or net["rpc_url"]
        if name == "mainnet":
            refusal = None
            if not (allow_mainnet is True or os.environ.get("CRX_ALLOW_MAINNET") == "1"):
                refusal = "mainnet is off: pass allow_mainnet=True or set CRX_ALLOW_MAINNET=1"
            elif not gw_url:
                refusal = "mainnet has no default gateway: pass base_url= or set CRX_BASE"
            elif not rpc:
                refusal = "mainnet has no default RPC: pass rpc_url= or set CRX_RPC"
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
        # The key leaves this frame's locals as soon as it is loaded, or fails to load.
        try:
            self._account = load_account(key, key_file)
        except ConfigError:
            key = None
            raise
        key = None
        self._gw._account = self._account
        if self._account is not None and custody != self.address:
            self._custody = self._gw.custody = custody
        self._state_dir = Path(state_dir or os.environ.get("CRX_STATE_DIR") or DEFAULT_STATE_DIR).expanduser()
        self._chain: dict | None = None
        self._sep: bytes | None = None
        self._rpc_checked = False
        self._sleep = time.sleep
        self._clock = time.time

    def __repr__(self) -> str:
        return f"crx.Client(network={getattr(self, 'network', None)!r}, address={self.address!r})"

    def __getstate__(self) -> Any:
        raise TypeError("crx.Client holds a key and cannot be pickled or copied")

    @property
    def address(self) -> str | None:
        """The key's address, lower case. None without a key."""
        account = getattr(self, "_account", None)
        return account.address.lower() if account is not None else None

    @property
    def account(self) -> str | None:
        """The seat this client reads: ``account=`` when set, else ``address``."""
        return getattr(self, "_custody", None) or self.address

    # ---------- setup checks ----------

    def _need_key(self) -> None:
        if self._account is None:
            raise ConfigError("this call needs the seat key: set CRX_WALLET_PK or pass key=")

    def _need_seat(self) -> None:
        """The key, acting for its own seat."""
        self._need_key()
        if self._custody is not None:
            raise ConfigError("a viewer (account=) only reads: balance, positions, trades")

    def _chain_info(self) -> dict:
        """This network's chain from /health, with its domain checked against the core."""
        if self._chain is not None:
            return self._chain
        chains = self.health().get("chains")
        if not isinstance(chains, list):
            raise BadAnswer("/health lists no chains")
        c = next((c for c in chains if isinstance(c, dict) and c.get("key") == self.chain_key), None)
        if c is None:
            raise ConfigError(f"{self._gw.host} does not serve {self.chain_key}")
        try:
            chain_id, core, domain = int(c["chain_id"]), str(c["core"]), str(c["domain"]).lower()
        except (KeyError, TypeError, ValueError):
            raise BadAnswer("/health sent a chain this SDK cannot read") from None
        if self.network == "mainnet":
            if chain_id not in MAINNET_CHAIN_IDS:
                raise ConfigError(f"{self.chain_key} is chain {chain_id}, not Ethereum mainnet (chain 1)")
        elif chain_id not in TESTNET_CHAIN_IDS:
            raise ConfigError(f"{self.chain_key} is chain {chain_id}, not a testnet; this SDK runs on testnets only")
        sep = e7.domain_separator(chain_id, core)
        if e7.h0x(sep) != domain:
            raise RefusedToSign("domain moved: /health does not match the core; nothing signed")
        self._chain, self._sep = dict(c, chain_id=chain_id), sep
        return self._chain

    def _chain_ready(self) -> dict:
        """The chain, with the RPC checked: right chain id, and the core is a contract."""
        c = self._chain_info()
        if not self._rpc_checked:
            if self._rpc.int("eth_chainId") != c["chain_id"]:
                raise ConfigError(f"the RPC is not on {self.chain_key} (chain {c['chain_id']})")
            if self._rpc("eth_getCode", c["core"], "latest") in ("0x", "", None):
                raise ConfigError(f"the core /health names on {self.chain_key} is not a contract; nothing sent")
            self._rpc_checked = True
        return c

    def _binder(self) -> Binder:
        c = self._chain_ready()
        return Binder(self._gw, self._account, c, self._sep, self._state_dir, self._sleep, self._clock)

    # ---------- public reads ----------

    def health(self) -> dict:
        """The gateway's /health answer."""
        return self._gw.request("GET", "/health", auth=False)

    def markets(self) -> list[Market]:
        """Every pair: session open, paused, notional limits."""
        body = self._gw.request("GET", "/markets", auth=False)
        rows = body.get("markets")
        if not isinstance(rows, list):
            raise BadAnswer("/markets lists no markets")
        out = []
        for m in rows:
            if not isinstance(m, dict) or not isinstance(m.get("pair"), str):
                continue
            on = [c for c in m.get("chains") or [] if isinstance(c, dict) and c.get("chain") == self.chain_key]
            session = m.get("session") if isinstance(m.get("session"), dict) else {}
            nb = session.get("next_boundary") if isinstance(session.get("next_boundary"), dict) else {}
            notional = m.get("notional") if isinstance(m.get("notional"), dict) else {}
            tenor = m.get("tenor") if isinstance(m.get("tenor"), dict) else {}
            out.append(Market(
                pair=m["pair"], pair_id=str(m.get("pair_id") or ""), base=str(m.get("base") or ""),
                quote=str(m.get("quote") or ""), open=session.get("open") is True,
                paused=not on or any(c.get("paused") is not False for c in on),
                min_notional=dec(notional.get("min")), max_notional=dec(notional.get("max")),
                next_open=ms_to_dt(nb.get("at")) if nb.get("kind") == "open" else None,
                min_tenor_s=tenor.get("min_secs"), max_tenor_s=tenor.get("max_secs"), raw=m,
            ))
        return out

    def market(self, pair: str) -> Market:
        slash, _ = _pair(pair)
        m = next((m for m in self.markets() if m.pair == slash), None)
        if m is None:
            raise BadRequest(f"unknown pair {slash}")
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

    def trades(self, since: int = 0, *, market: bool = False) -> list[Event]:
        """Your own event tape, oldest first: trade.opened, trade.settled, and the rest.

        ``since`` is a seq: pass the last ``Event.seq`` to read only newer events.
        A maker seat also receives every open RFQ on the venue (``rfq.opened``, no
        owner named). By default an ``rfq.opened`` is kept only when it carries a
        ``client_rfq_id``: the gateway serves that field to the RFQ's taker alone.
        ``quote()`` always sends one; an RFQ opened elsewhere without one shows
        its other events only. ``market=True`` keeps every ``rfq.opened``.
        """
        self._need_key()
        rows: list = []
        while True:
            page = self._gw.request("GET", "/trades", query={"since": since, "limit": 1000})
            got = page.get("trades")
            if not isinstance(got, list):
                raise BadAnswer("/trades lists no trades")
            rows += got
            if len(got) < 1000:
                break
            seq = page.get("seq")
            if not isinstance(seq, int) or seq <= since:
                raise BadAnswer("/trades did not advance its seq")
            since = seq
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
        im_bps: int = 100,
        wait: float = 30.0,
        client_rfq_id: str | None = None,
    ) -> Quote:
        """Open an RFQ and return the best firm quote. Nothing is accepted.

        ``side`` is your side in the base currency. ``expiry`` is the settlement
        instant: a datetime, a timedelta from now, or unix ms. Default: one
        month out, off the weekend.

        A closed market still takes the RFQ: the gateway decides. Its refusal
        raises the matching error (``MarketClosed`` for ``market_closed``);
        no quote before ``wait`` ends raises ``NoQuotes``.
        """
        self._need_seat()
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
        if not (isinstance(im_bps, int) and 1 <= im_bps <= 10_000):
            raise BadRequest("im_bps is an integer from 1 to 10000")
        req = {
            "chain": self.chain_key, "pair": compact, "side": side, "notional": _plain(amount),
            "expiry": expiry_ms, "im_bps": im_bps, "client_rfq_id": client_rfq_id or f"sdk-{uuid.uuid4().hex[:12]}",
        }
        r = self._gw.request("POST", "/rfqs", body=req)
        try:
            rfq_id, leg_id, quote_expiry = str(r["rfq_id"]), str(r["leg_id"]), int(r["quote_expiry"])
            echo_im = int(r.get("im_bps", im_bps))
        except (KeyError, TypeError, ValueError):
            raise BadAnswer("/rfqs sent an answer this SDK cannot read") from None
        log.info("rfq %s opened: %s %s %s", rfq_id, slash, side, _plain(amount))
        rfq = {"rfq_id": rfq_id, "leg_id": leg_id, "quote_expiry": quote_expiry, "im_bps": echo_im,
               "pair": slash, "side": side, "notional": _plain(amount), "expiry": expiry_ms, "req_im_bps": im_bps}
        q = self._await_quote(rfq_id, wait)
        try:
            return Quote(
                rfq_id=rfq_id, quote_id=str(q["quote_id"]), pair=slash, side=side, notional=Decimal(str(q["notional"])),
                rate=Decimal(str(q["rate"])), expiry=ms_to_dt(q["expiry"]) or ms_to_dt(expiry_ms),
                expires_at=ms_to_dt(q.get("expires_at")), house=q.get("house") is True, raw=q, rfq=rfq,
                expiry_ms=expiry_ms,
            )
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise BadAnswer("the quote row cannot be read") from None

    def _await_quote(self, rfq_id: str, wait: float) -> dict:
        """Poll GET /rfqs/{id}: the gateway's pick once named, else the best live quote at the end."""
        deadline = self._clock() + max(float(wait), 0.0)
        best = None
        while True:
            view = self._gw.request("GET", f"/rfqs/{rfq_id}")
            if isinstance(view.get("quote"), dict):
                return view["quote"]
            now_ms = self._clock() * 1000
            live = [q for q in view.get("quotes") or [] if isinstance(q, dict)
                    and isinstance(q.get("expires_at"), int) and q["expires_at"] > now_ms
                    and str(q.get("status") or "").lower() not in ("expired", "accepted", "lapsed", "rejected", "filled")]
            best = live[0] if live else None
            if self._clock() >= deadline:
                if best is not None:
                    return best
                raise NoQuotes("no quote before the wait ended: no maker online, or the market just closed",
                               details={"rfq_id": rfq_id})
            self._sleep(1.0)

    def trade(self, quote: Quote) -> Trade:
        """Accept a quote and open the trade: you sign your Side. CRX sends the tx and pays gas.

        Returns once the trade is ``open`` or ``refused``. When neither shows within
        30 s (testnet) or 90 s (mainnet), returns ``sending`` or ``pending``.
        """
        self._need_seat()
        if not isinstance(quote, Quote):
            raise BadRequest("trade() takes the Quote that quote() returned")
        b = self._binder()
        rfq = quote.rfq
        rfq_id = quote.rfq_id
        nonce = int.from_bytes(keccak(bytes.fromhex(self.address[2:]) + rfq_id.encode())[-8:], "big")
        try:
            arm = b.leg(quote.raw, rfq["leg_id"], nonce, rfq["quote_expiry"], rfq["im_bps"])
        except KeyError:
            raise RefusedToSign("refused to sign: the quote row lacks a term") from None
        want = {
            "pair_id": e7.pair_id(rfq["pair"]), "instrument_id": 1, "side": 1 if rfq["side"] == "buy" else -1,
            "notional": Decimal(rfq["notional"]), "expiry": int(rfq["expiry"]) // 1000,
            "im_bps": rfq["req_im_bps"], "premium_bps": 0,
        }
        b.check_terms(arm, want)
        sig = b.sign_leg(arm)
        r, answered = b.accept(rfq_id, quote.raw.get("expires_at"), {"quote_id": quote.quote_id, "leg": arm, "sig": sig})
        log.info("rfq %s accepted @ %s", rfq_id, quote.rate)
        base = dict(rfq_id=rfq_id, quote_id=quote.quote_id, pair=quote.pair, side=quote.side,
                    notional=quote.notional, rate=quote.rate)
        side = r.get("side")
        try:
            opened = min(int(side["own_nonce"]) / 1000, answered)
        except (KeyError, TypeError, ValueError):
            raise RefusedToSign("refused to sign: the Side template cannot be read") from None
        exp = quote.raw.get("expires_at")
        maker_by = min((exp if isinstance(exp, int) else 10**13) / 1000, opened + 120) + 5
        b.bind(rfq_id, arm, side, maker_by, log.info)
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

    def deposit(self, amount: Any, *, mint: bool = True) -> Deposit:
        """Deposit USDC to the core: approve (when short), then deposit. You pay gas.

        On a testnet, ``mint=True`` first mints the test USDC the wallet lacks.
        Returns once the deposit is ``credited`` or ``failed``; ``pending`` when
        neither shows within 30 s (testnet) or 90 s (mainnet).
        """
        self._need_seat()
        amount = _amount(amount)
        c = self._chain_ready()
        r = self._gw.request("POST", "/deposit", body={"chain": self.chain_key, "amount": _plain(amount)})
        token, core = c.get("base_token"), c["core"]
        if not token:
            raise CrxError(f"/health names no base token on {self.chain_key} yet; try again in a minute", code="not_ready")
        decimals = self._rpc.int("eth_call", {"to": token, "data": e7.calldata("decimals()", [], [])}, "latest")
        raw = amount.scaleb(decimals)
        if raw != raw.to_integral_value():
            raise BadRequest(f"amount has more than {decimals} decimals")
        raw = int(raw)
        try:
            txs = r["transactions"]
            if int(r["amount_raw"]) != raw:
                raise RefusedToSign(f"the gateway served amount_raw {clean(r['amount_raw'], 40)}, not {_plain(amount)}; nothing sent")
            want = [(core, e7.calldata("deposit(uint256)", ["uint256"], [raw]))]
            if len(txs) == 2:
                want.insert(0, (token, e7.calldata("approve(address,uint256)", ["address", "uint256"],
                                                   [to_checksum_address(core), raw])))
            served = [(t["to"].lower(), t["data"].lower(), int(t["chain_id"])) for t in txs]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise RefusedToSign("the gateway served a deposit this SDK cannot read; nothing sent") from None
        if served != [(to.lower(), data, c["chain_id"]) for to, data in want]:
            raise RefusedToSign("the gateway served a tx this SDK does not expect; nothing sent")
        hashes = []
        if mint and c["chain_id"] in TESTNET_CHAIN_IDS:
            held = self._rpc.int("eth_call", {"to": token, "data": e7.calldata(
                "balanceOf(address)", ["address"], [to_checksum_address(self.address)])}, "latest")
            if held < raw:
                hashes.append(self._send("mint", token, e7.calldata(
                    "mint(address,uint256)", ["address", "uint256"], [to_checksum_address(self.address), raw - held])))
        for (to, data), name in zip(want, ["approve", "deposit"][-len(want):]):
            hashes.append(self._send(name, to, data))
        status, _ = self._settle(lambda: self._money_last("deposit", "tx", hashes[-1]))
        log.info("deposit %s %s", _plain(amount), status)
        return Deposit(amount=amount, txs=hashes, status=status)

    def withdraw(self, amount: Any) -> Withdraw:
        """Withdraw USDC to your own wallet. You sign the intent. CRX sends the tx and pays gas.

        Returns once the withdraw shows a status other than ``sending`` or ``pending``.
        When none shows within 30 s (testnet) or 90 s (mainnet), returns ``sending`` or
        ``pending``. ``accepted`` leaves the balance at once; the payout follows.
        """
        self._need_seat()
        amount = _amount(amount)
        c = self._chain_ready()
        nonce = self.balance().withdraw_nonce
        r = self._gw.request("POST", "/withdraw", body={"chain": self.chain_key, "amount": _plain(amount)})
        try:
            w = r["intent"]
            mine = (w["account"].lower(), w["recipient"].lower(), int(w["amount"]), r["core"].lower(), int(r["chain_id"]))
            if mine != (self.address, self.address, e7.scaled6(amount), c["core"].lower(), c["chain_id"]):
                raise RefusedToSign("the intent is not this withdraw to this wallet; nothing signed")
            if nonce is None or int(w["nonce"]) != nonce:
                raise RefusedToSign("the intent's nonce is not the seat's next withdraw nonce; nothing signed")
            now = self._clock()
            if not now < int(w["deadline"]) <= now + 86_400:
                raise RefusedToSign("the intent's deadline is not within the next 24 h; nothing signed")
            digest = e7.withdraw_digest(self._sep, w)
            if e7.h0x(digest) != str(r["digest"]).lower():
                raise RefusedToSign("digest mismatch: the served digest is not this intent; nothing signed")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise RefusedToSign("the gateway served an intent this SDK cannot read; nothing signed") from None
        sig = "0x" + bytes(self._account.unsafe_sign_hash(digest).signature).hex()
        r = self._gw.request("POST", "/withdraw/sig", body={"chain": self.chain_key, "intent": w, "sig": sig},
                             ok=(200, 202))
        item = _word(r.get("item"))
        if item is None:
            raise BadAnswer("/withdraw/sig named no item")
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
        self, *, since: int | None = None, wait: float | None = None, poll: float = 0.5,
        stop: threading.Event | None = None,
    ) -> Iterator[Rfq]:
        """Open RFQs you can quote, as they arrive: the ``rfq.opened`` frames of your tape.

        Needs a maker seat with collateral: the gateway sends no RFQ to a seat short of it.
        Yields open RFQs on this network, another seat's, inside their quote window, each
        once. The call reads your tape up to its head before it returns: from ``since``
        (an ``Rfq.seq``), else from the start. Ends after ``wait`` s, or once ``stop`` (a
        ``threading.Event``) is set; by default it never ends. Break out of the loop to stop.
        """
        self._need_seat()
        return _maker.stream(self, since, wait, poll, stop)

    def rfq(self, rfq_id: str) -> Rfq:
        """One RFQ as your seat reads it. As its taker, ``quotes`` holds every desk's
        quote (``house_rate`` names the house desk's) and ``client_rfq_id`` is yours.
        As a maker, ``quotes`` holds your own quotes only."""
        self._need_seat()
        return _maker.read(self, rfq_id)

    def send_quote(
        self, rfq: Rfq, rate: Any, *, client_quote_id: str | None = None, expires_in: float | None = None,
    ) -> MakerQuote:
        """Post a firm quote at ``rate`` on ``rfq``: you sign your Leg. The taker gets the best quote only.

        The Leg carries the RFQ's terms, your own side (the opposite of the taker's) and
        your nonce from ``client_quote_id`` (a fresh one by default). ``expires_in`` ends
        the quote that many seconds from now; by default it lives as long as the RFQ.
        A refusal raises its error: ``InsufficientCollateral``, ``QuoteExpired`` when the
        RFQ ended, ``NotWhitelisted``. Pass the result to ``confirm``.
        """
        self._need_seat()
        return _maker.send(self, rfq, rate, client_quote_id, expires_in)

    def confirm(self, quote: MakerQuote, *, timeout: float | None = None, poll: float = 1.0) -> Trade:
        """Wait for the taker's accept, sign your Side, and open the trade. CRX sends the tx and pays gas.

        Waits at most ``timeout`` s for the accept; by default until the RFQ's quote window
        closes. Returns once the trade is ``open`` or ``refused``; ``sending`` or ``pending``
        when neither shows within 30 s (testnet) or 90 s (mainnet).

        No trade raises ``QuoteLost``; ``reason`` is ``another_maker``, ``expired``,
        ``cancelled``, ``round_closed`` or ``timeout``.
        """
        self._need_seat()
        return _maker.confirm(self, quote, timeout, poll)

    def _send(self, what: str, to: str, data: str) -> str:
        tx = send_tx(self._rpc, self._account, self._chain_ready()["chain_id"], what, to, data, sleep=self._sleep)
        log.info("%s tx %s", what, tx)
        return tx


__all__ = ["Client", "NETWORKS"]
