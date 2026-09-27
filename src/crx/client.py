"""The CRX client."""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests
from eth_utils import keccak, to_checksum_address

from . import _eip712 as e7
from ._bind import Binder
from ._chain import Rpc, send_tx
from ._http import Gateway
from ._keys import load_account
from .errors import (
    AboveMax, BadAnswer, BadRequest, BelowMin, ConfigError, CrxError, MarketClosed,
    MarketPaused, NoQuotes, RefusedToSign, clean,
)
from .models import (
    Balance, Deposit, Event, Market, Position, Quote, Trade, Withdraw, dec, ms_to_dt, side_word,
)

log = logging.getLogger("crx")

NETWORKS = {
    "testnet": {
        "chain": "avax-fuji",
        "base_url": "https://api.crxfx.com",
        "rpc_url": "https://api.avax-test.network/ext/bc/C/rpc",
    },
}
_ALIASES = {"fuji": "testnet"}
TESTNET_CHAIN_IDS = {43113, 84532, 11142220}
DEFAULT_STATE_DIR = "~/.crx-quickstart"  # shared with the CRX quickstart scripts: one Side nonce floor per seat


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
    ) -> None:
        self._account = None
        name = _ALIASES.get(network, network)
        net = NETWORKS.get(name)
        if net is None:
            key = None
            raise ConfigError(f"unknown network {clean(network, 20)!r}; known: {', '.join(NETWORKS)}")
        self.network = name
        self.chain_key = net["chain"]
        self._session = session or requests.Session()
        try:
            self._gw = Gateway(base_url or os.environ.get("CRX_BASE") or net["base_url"], None, self._session, timeout)
        except ConfigError:
            key = None
            raise
        self._rpc = Rpc(rpc_url or os.environ.get("CRX_RPC") or net["rpc_url"], self._session, max(timeout, 20.0))
        # The key leaves this frame's locals as soon as it is loaded, or fails to load.
        try:
            self._account = load_account(key, key_file)
        except ConfigError:
            key = None
            raise
        key = None
        self._gw._account = self._account
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
        """The seat address, lower case. None without a key."""
        account = getattr(self, "_account", None)
        return account.address.lower() if account is not None else None

    # ---------- setup checks ----------

    def _need_key(self) -> None:
        if self._account is None:
            raise ConfigError("this call needs the seat key: set CRX_WALLET_PK or pass key=")

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
        if chain_id not in TESTNET_CHAIN_IDS:
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
        return Binder(self._gw, self._rpc, self._account, c, self._sep, self._state_dir, self._sleep, self._clock)

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
        """Your own collateral, margin and withdraw state, as of the last fold."""
        self._need_key()
        b = self._gw.request("GET", "/balance", query={"chain": self.chain_key})
        if str(b.get("account") or "").lower() != self.address:
            raise BadAnswer("/balance answered for another account")
        w = b.get("withdraw") if isinstance(b.get("withdraw"), dict) else {}
        nonce = w.get("nonce")
        return Balance(
            account=self.address, state=b.get("state"), collateral=dec(b.get("collateral")), free=dec(b.get("free")),
            equity=dec(b.get("equity")), im=dec(b.get("im")), mm=dec(b.get("mm")),
            pending_deposit=dec(b.get("pending_deposit")),
            open_legs=b.get("open_legs") if isinstance(b.get("open_legs"), int) else None,
            withdraw_live=w.get("live") if isinstance(w.get("live"), bool) else None,
            withdraw_nonce=int(nonce) if isinstance(nonce, (str, int)) and str(nonce).isdigit() else None,
            as_of=ms_to_dt(b.get("as_of")), as_of_fold=b.get("as_of_fold"), raw=b,
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
        owner named). By default an ``rfq.opened`` is kept only when another event
        on your tape names the same RFQ, so your own RFQs show once they are quoted,
        filled or expired. ``market=True`` keeps them all.
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
            def rfq_of(r: dict) -> Any:
                d = r.get("data")
                return d.get("rfq_id") if isinstance(d, dict) else None
            mine = {rfq_of(r) for r in rows if r.get("type") != "rfq.opened"} - {None}
            rows = [r for r in rows if r.get("type") != "rfq.opened" or rfq_of(r) in mine]
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
        """
        self._need_key()
        slash, compact = _pair(pair)
        side = _side(side)
        amount = _amount(notional, "notional")
        m = self.market(slash)
        if m.paused:
            raise MarketPaused(f"{slash} is paused", details={"pair": slash})
        if not m.open:
            opens = int(m.next_open.timestamp() * 1000) if m.next_open else None
            when = f"; opens {m.next_open:%Y-%m-%d %H:%M} UTC" if m.next_open else ""
            raise MarketClosed(f"{slash} is closed{when}", details={"pair": slash, "opens_at": opens})
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
        """Accept a quote and bind it: sign your Side and send the arm tx (you pay gas).

        Returns once the arm is on chain. The next hourly fold opens the position.
        """
        self._need_key()
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
        if not isinstance(side, dict):
            return Trade(status="accepted", tx=None, raw=r, **base)
        try:
            opened = min(int(side["own_nonce"]) / 1000, answered)
        except (KeyError, TypeError, ValueError):
            raise RefusedToSign("refused to sign: the Side template cannot be read") from None
        exp = quote.raw.get("expires_at")
        maker_by = min((exp if isinstance(exp, int) else 10**13) / 1000, opened + 120) + 5
        tx = b.bind(rfq_id, arm, side, maker_by, log.info)
        log.info("rfq %s bound: arm %s", rfq_id, tx)
        return Trade(status="bound", tx=tx, raw=r, **base)

    # ---------- money ----------

    def deposit(self, amount: Any, *, mint: bool = True) -> Deposit:
        """Deposit USDC to the core: approve (when short), then deposit. You pay gas.

        On a testnet, ``mint=True`` first mints the test USDC the wallet lacks.
        The deposit is pending until the next fold, then counts as collateral.
        """
        self._need_key()
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
        log.info("deposited %s: pending until the next fold", _plain(amount))
        return Deposit(amount=amount, txs=hashes)

    def withdraw(self, amount: Any) -> Withdraw:
        """Withdraw USDC to your own wallet. Sign the intent, arm it on the core. You pay gas.

        The next fold serves it; the crank after that pays it.
        """
        self._need_key()
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
        sig = bytes(self._account.unsafe_sign_hash(digest).signature)
        data = e7.calldata("armWithdrawIntent(uint8,bytes)", ["uint8", "bytes"], [5, e7.withdraw_envelope(w, sig)])
        tx = self._send("armWithdrawIntent", c["core"], data)
        log.info("withdraw %s armed, nonce %s", _plain(amount), nonce)
        return Withdraw(amount=amount, nonce=nonce, tx=tx)

    def _send(self, what: str, to: str, data: str) -> str:
        tx = send_tx(self._rpc, self._account, self._chain_ready()["chain_id"], what, to, data, sleep=self._sleep)
        log.info("%s tx %s", what, tx)
        return tx


__all__ = ["Client", "NETWORKS"]
