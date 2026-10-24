"""Maker calls: read open RFQs, sign and post binding quotes, wait for the accept."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Iterator

from eth_utils import keccak

from . import _eip712 as e7
from ._bind import QUOTE_WINDOW, utc
from ._http import Gateway
from .errors import (
    AlreadyAccepted, BadAnswer, BadRequest, CrxError, LegIdTaken, LegLive, NetworkError, NoQuotes, QuoteLost,
    RateLimited, RefusedToSign, ServerError, UnknownOrEnded, clean,
)
from .models import Drop, MakerQuote, Rfq, Trade, _final, _provisional, _side_of, dec, ms_to_dt

if TYPE_CHECKING:
    from .client import Client

log = logging.getLogger("crx")

PAGE = 1000  # the gateway's cap on /trades rows per page
U64_MAX = 2**64 - 1
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MS = timedelta(milliseconds=1)
LIVE = ("open", "quoted")  # RFQ statuses that take a quote
AFTER_ACCEPT = ("accepted", "opened", "settled", "armed", "signing", "consenting")
TRANSIENT = (NetworkError, ServerError, RateLimited)
QUOTE_MIN_LIFE = 60  # seconds: the gateway refuses a quote that ends sooner
KEEPALIVE_S = 5.0  # default seconds between two /trades reads of a maker seat
KEEPALIVE_RANGE = (1.0, 10.0)  # the gateway counts a maker live for 15 s after a /trades read
LIVE_READ_S = 2.0  # seconds each quote_live() round reads new RFQs
LIVE_DROP_AFTER = timedelta(seconds=10)  # quote_live() drops an unaccepted quote this long after closes_at
LOST = {
    "another_maker": "the taker accepted another quote",
    "expired": "the RFQ ended with no accept",
    "cancelled": "the RFQ was cancelled",
    "dropped": "the quote was dropped: by your drop, by your later quote, or by a gateway restart",
}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _word(value: Any) -> str | None:
    s = value.lower() if isinstance(value, str) else ""
    return s if len(s) == 66 and s[:2] == "0x" and all(ch in "0123456789abcdef" for ch in s[2:]) else None


def _slash(pair: Any) -> str:
    s = "".join(ch for ch in str(pair or "").upper() if ch.isalpha())
    return f"{s[:3]}/{s[3:]}" if len(s) == 6 else str(pair or "")


def _plain(d: Decimal) -> str:
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def rfq_of(d: dict, seq: Any = None, quotes: Any = ()) -> Rfq | None:
    """Build an ``Rfq`` from an ``rfq.opened`` event or an RFQ view, or None without an RFQ id."""
    rid = _word(d.get("rfq_id"))
    if rid is None:
        return None
    qe_max = _int(d.get("quote_expiry_max"))
    rows = tuple(q for q in quotes if isinstance(q, dict)) if isinstance(quotes, (list, tuple)) else ()
    cid = d.get("client_rfq_id")
    return Rfq(
        rfq_id=rid, pair=_slash(d.get("pair")), side=_side_of(d.get("side")), notional=dec(d.get("notional")),
        expiry=ms_to_dt(_int(d.get("expiry"))),
        quote_expiry_max=ms_to_dt(qe_max * 1000) if qe_max is not None else None,
        premium_bps=_int(d.get("premium_bps")), kind=str(d.get("kind") or "open"),
        status=d.get("status") if isinstance(d.get("status"), str) else None,
        closes_at=ms_to_dt(_int(d.get("closes_at"))),
        client_rfq_id=cid if isinstance(cid, str) and cid else None,
        seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None, quotes=rows, raw=dict(d),
    )


def _quotable(r: Rfq, chain: str, now_ms: float, kind: str = "open") -> bool:
    """Return True for another seat's RFQ of ``kind`` on this chain that still takes quotes."""
    d = r.raw
    if r.kind != kind or d.get("chain") != chain or r.own:
        return False
    if r.status is not None and r.status not in LIVE:
        return False
    closes = r.closes_at.timestamp() * 1000 if r.closes_at else None
    qe = _int(d.get("quote_expiry_max"))
    return (closes is None or closes > now_ms) and (qe is None or qe * 1000 > now_ms)


class Keepalive:
    """Keep a maker seat live: read one ``/trades`` row when the client read none for ``every`` seconds."""

    def __init__(self, gw: Gateway, every: float) -> None:
        self._gw, self._every = gw, every
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="crx-keepalive", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        tried = 0.0
        while True:
            left = max(self._gw.tape_read, tried) + self._every - time.monotonic()
            if left > 0:
                if self._stop.wait(left):
                    return
                continue
            if self._stop.is_set():
                return
            tried = time.monotonic()
            try:
                r = self._gw.raw_request("GET", "/trades", query={"after": 0, "limit": 1})
                if r.status_code != 200:
                    log.info("keepalive: GET /trades answered %s", r.status_code)
            except Exception as e:  # noqa: BLE001 - the thread ends on stop() only
                log.info("keepalive: GET /trades failed (%s)", getattr(e, "code", type(e).__name__))


def _page(c: "Client", cursor: int) -> tuple[list, int]:
    page = c._gw.request("GET", "/trades", query={"after": cursor, "limit": PAGE})
    rows, seq = page.get("trades"), page.get("seq")
    if not isinstance(rows, list) or not isinstance(seq, int) or isinstance(seq, bool):
        raise BadAnswer("/trades sent a page this SDK cannot read")
    if len(rows) >= PAGE and seq <= cursor:
        raise BadAnswer("/trades did not advance its seq")
    return rows, max(cursor, seq)


def _opened(rows: list) -> Iterator[Rfq]:
    for row in rows:
        if isinstance(row, dict) and row.get("type") == "rfq.opened" and isinstance(row.get("data"), dict):
            r = rfq_of(row["data"], row.get("seq"))
            if r is not None:
                yield r


def stream(
    c: "Client", after: int | None, wait: float | None, poll: float, stop: Any = None, only: Any = None,
    kind: str = "open",
) -> Iterator[Rfq]:
    """Yield open RFQs from the ``/trades`` tape, oldest first, once each, until ``wait`` ends or ``stop`` is set."""
    rid = None
    if only is not None:
        rid = _word(getattr(only, "rfq_id", only))
        if rid is None:
            raise BadRequest("only is an Ask, or an RFQ id")
    cursor = int(after or 0)
    end = None if wait is None else c._clock() + max(float(wait), 0.0)
    backlog: list[Rfq] = []
    while True:
        rows, cursor = _page(c, cursor)
        backlog = [r for r in backlog if _quotable(r, c.chain_key, c._clock() * 1000, kind)]
        backlog += [r for r in _opened(rows) if _quotable(r, c.chain_key, c._clock() * 1000, kind)]
        if len(rows) < PAGE:
            break
    rfqs = _follow(c, cursor, backlog, end, poll, stop, kind)
    return rfqs if rid is None else _only(rfqs, rid, stop)


def _only(rfqs: Iterator[Rfq], rfq_id: str, stop: Any) -> Iterator[Rfq]:
    """Yield the RFQ ``rfq_id`` from ``rfqs``. Raise ``NoQuotes`` when the wait ends before it."""
    for r in rfqs:
        if r.rfq_id == rfq_id:
            yield r
            return
    if stop is None or not stop.is_set():
        raise NoQuotes("the RFQ did not show in this account's open RFQs before the wait ended",
                       details={"rfq_id": rfq_id})


def _follow(
    c: "Client", cursor: int, backlog: list[Rfq], end: float | None, poll: float, stop: Any, kind: str = "open",
) -> Iterator[Rfq]:
    seen: set[str] = set()
    pending = list(backlog)
    while True:
        for r in pending:
            if r.rfq_id in seen or not _quotable(r, c.chain_key, c._clock() * 1000, kind):
                continue
            seen.add(r.rfq_id)
            if len(seen) > 10_000:
                seen.clear()
            yield r
        pending = []
        if (end is not None and c._clock() >= end) or (stop is not None and stop.is_set()):
            return
        c._sleep(poll)
        try:
            while True:
                rows, cursor = _page(c, cursor)
                pending += list(_opened(rows))
                if len(rows) < PAGE:
                    break
        except TRANSIENT as e:
            log.warning("rfqs: %s; reading again in %s s", e.code, poll)


def read(c: "Client", rfq_id: Any) -> Rfq:
    rid = _word(rfq_id)
    if rid is None:
        raise BadRequest("rfq_id is a 0x 32-byte hex word")
    view = c._gw.request("GET", f"/rfqs/{rid}")
    r = rfq_of(view, None, view.get("quotes") or ())
    if r is None or r.rfq_id != rid:
        raise BadAnswer(f"/rfqs/{rid} answered for another RFQ")
    return r


def _rate(value: Any) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BadRequest("rate is not a number") from None
    if not d.is_finite() or d <= 0:
        raise BadRequest("rate must be above zero")
    if d.scaleb(6) != d.scaleb(6).to_integral_value():
        raise BadRequest("rate has more than 6 decimals")
    if int(d.scaleb(6)) > U64_MAX:
        raise BadRequest("rate is out of range")
    return d


def own_terms(premium_bps: Any, expiry: Any) -> None:
    """Raise ``BadRequest`` when the maker's premium or expiry, as ``send_quote`` takes them, is not valid."""
    if isinstance(premium_bps, bool) or not isinstance(premium_bps, int) or not -32_768 <= premium_bps <= 32_767:
        raise BadRequest("premium_bps is an integer from -32768 to 32767")
    if isinstance(expiry, datetime):
        if expiry.utcoffset() is None:
            raise BadRequest("expiry needs a timezone, e.g. datetime(..., tzinfo=timezone.utc)")
        return
    if expiry is None or (isinstance(expiry, timedelta) and expiry > timedelta(0)):
        return
    if isinstance(expiry, int) and not isinstance(expiry, bool) and expiry > 0:
        return
    raise BadRequest("expiry is a datetime, a timedelta above zero, or unix ms")


def _ms(t: datetime) -> int:
    """Return an aware datetime as whole unix milliseconds."""
    return (t - EPOCH) // MS


def _day(ms: int) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OverflowError, OSError):
        return f"unix ms {ms}"


def _check_expiry(r: Rfq, served: int, own: Any, now_ms: float) -> None:
    """Raise ``RefusedToSign`` when the served expiry differs from the maker's expiry."""
    if own is None:
        if r.expiry is None:
            log.warning("rfq %s: the Rfq shows no expiry; the served expiry is %s", r.rfq_id, _day(served))
        elif not isinstance(r.expiry, datetime) or r.expiry.utcoffset() is None:
            raise RefusedToSign("refused to sign: the Rfq's expiry is not a datetime with a timezone")
        elif served != _ms(r.expiry):
            raise RefusedToSign(f"refused to sign: the RFQ settles at {_day(served)}, not at the Rfq's expiry "
                                f"({_day(_ms(r.expiry))})")
    elif isinstance(own, timedelta):
        latest = int(now_ms) + own // MS
        if served > latest:
            raise RefusedToSign(f"refused to sign: the RFQ settles at {_day(served)}, after your expiry bound "
                                f"({_day(latest)})")
    else:
        want = _ms(own) if isinstance(own, datetime) else own
        if served != want:
            raise RefusedToSign(f"refused to sign: the RFQ settles at {_day(served)}, not at your expiry ({_day(want)})")


def maker_terms(seat: str, r: Rfq, chain: str, rate: Decimal, now_ms: float,
                premium_bps: int = 0, expiry: Any = None) -> dict:
    """Build the seat's half of ``r``. Refuse to sign when a served term differs from the maker's terms."""
    d = r.raw
    if r.kind != "open":
        raise BadRequest("a close RFQ takes a price-only quote; send_quote() quotes open RFQs only")
    if d.get("chain") != chain:
        raise BadRequest(f"the RFQ is on {clean(d.get('chain'), 40)}; this client signs for {chain}")
    if r.own:
        raise BadRequest("this is your own RFQ; a seat does not quote its own RFQ")
    if not e7.pair_text_ok(r.pair):
        raise RefusedToSign("refused to sign: the RFQ names no AAA/BBB pair")
    side = d.get("side")
    if isinstance(side, bool) or side not in (1, -1):
        raise RefusedToSign("refused to sign: the RFQ names no side for this seat")
    notional = d.get("notional")
    try:
        if not isinstance(notional, str) or Decimal(notional) <= 0:
            raise ValueError
        e7.scaled6(notional)
    except (ValueError, ArithmeticError):
        raise RefusedToSign("refused to sign: the RFQ's notional cannot be read") from None
    premium = _int(d.get("premium_bps", 0))
    if premium is None or not -32_768 <= premium <= 32_767:
        raise RefusedToSign("refused to sign: the RFQ's premium_bps cannot be read")
    if premium != premium_bps:
        raise RefusedToSign(f"refused to sign: the RFQ's premium is {premium} bps, not your premium_bps "
                            f"({premium_bps} bps)")
    served = _int(d.get("expiry"))
    if served is None or served <= now_ms:
        raise RefusedToSign("refused to sign: the RFQ's expiry is not in the future")
    _check_expiry(r, served, expiry, now_ms)
    return {
        "seat": seat, "pair_id": e7.h0x(e7.pair_id(r.pair)), "side": side, "notional": notional,
        "rate": _plain(rate), "premium_bps": premium, "expiry": served,
    }


def nonce_for(seat: str, client_quote_id: str) -> int:
    """Return the low 64 bits of keccak256(seat ‖ client_quote_id), the nonce when DERIVE_MAKER_NONCE is off."""
    return int.from_bytes(keccak(bytes.fromhex(seat[2:]) + client_quote_id.encode())[-8:], "big")


def send(c: "Client", r: Any, rate: Any, client_quote_id: str | None,
         premium_bps: Any = 0, expiry: Any = None) -> MakerQuote:
    """Sign and post a binding quote on ``r``. Later quotes on the RFQ reuse its leg, so the leg fills once."""
    if not isinstance(r, Rfq):
        raise BadRequest("send_quote() takes an Rfq from rfqs() or rfq()")
    if r.test_request:
        from ._test_requests import validate

        try:
            validate(r.raw, c.chain_key, c._clock(), maker=True, event=r.status is None,
                     require_reference=False)
            if (r.pair != _slash(r.raw["pair"]) or r.notional != dec(r.raw["notional"])
                    or r.side != "sell" or r.kind != "open"):
                raise BadAnswer("the test request differs from its displayed terms")
        except BadAnswer as e:
            raise RefusedToSign(str(e)) from None
    rate = _rate(rate)
    own_terms(premium_bps, expiry)
    cqid = client_quote_id
    if cqid is not None and (not isinstance(cqid, str) or not 0 < len(cqid) <= 128
                             or not all(" " <= ch <= "~" for ch in cqid)):
        raise BadRequest("client_quote_id is 1 to 128 printable ASCII characters")
    if cqid is None and not e7.DERIVE_MAKER_NONCE:
        cqid = f"sdk-q-{secrets.token_hex(8)}"
    b = c._binder()
    now = c._clock()
    d = r.raw
    terms = maker_terms(c.address, r, c.chain_key, rate, now * 1000, premium_bps, expiry)
    taker_ref, qe_max = _word(d.get("taker_ref")), _int(d.get("quote_expiry_max"))
    if taker_ref is None or qe_max is None:
        raise RefusedToSign("refused to sign: the RFQ names no taker_ref or quote_expiry_max")
    for rid in [rid for rid, leg in c._legs.items() if e7.leg_id_tail(leg) < now]:
        del c._legs[rid]
    leg_id = c._legs.get(r.rfq_id)
    if leg_id is None or e7.leg_id_tail(leg_id) < now + QUOTE_MIN_LIFE:
        if qe_max < now + QUOTE_MIN_LIFE:
            raise QuoteLost(f"under {QUOTE_MIN_LIFE} s of the RFQ's quote window is left; nothing signed",
                            reason="expired")
        leg_id = e7.leg_id_for(secrets.token_bytes(24), qe_max)
    end = e7.leg_id_tail(leg_id)
    if end > now + QUOTE_WINDOW or end > qe_max:
        raise RefusedToSign("refused to sign: the quote end is past the RFQ's quote_expiry_max, or more than "
                            f"{QUOTE_WINDOW} s ahead")
    if end != qe_max:
        raise RefusedToSign("refused to sign: the leg id's quote end is not the RFQ's quote_expiry_max")
    nonce = e7.maker_nonce(leg_id) if e7.DERIVE_MAKER_NONCE else nonce_for(c.address, cqid)
    salt = e7.h0x(secrets.token_bytes(32))
    half = dict(terms, leg_id=leg_id, nonce=str(nonce), quote_expiry=end)
    try:
        digest = e7.quote_digest(b.sep, e7.leg_words(half), salt, taker_ref)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise RefusedToSign("refused to sign: the Quote cannot be encoded") from None
    body: dict[str, Any] = {"rate": half["rate"], "leg_id": leg_id, "salt": salt}
    if not e7.DERIVE_MAKER_NONCE:
        body["nonce"] = str(nonce)
    if not e7.DROP_QUOTE_EXPIRY:
        body["quote_expiry"] = end
    if cqid is not None:
        body["client_quote_id"] = cqid
    body["sig"] = "0x" + bytes(c._account.unsafe_sign_hash(digest).signature).hex()
    c._legs[r.rfq_id] = leg_id
    # Send the same signed body again on a collateral refusal that /balance free covers, until the RFQ closes.
    last = r.closes_at.timestamp() if r.closes_at is not None else end
    try:
        q = c._screen_again(lambda: c._gw.request("POST", f"/rfqs/{r.rfq_id}/quotes", body=body), "maker",
                            min(last, end))
    except LegIdTaken:
        c._legs.pop(r.rfq_id, None)
        raise
    except LegLive as e:
        # The next quote reuses the seat's live leg while that leg has enough life left.
        live = _word(e.leg_id)
        if live is not None and e7.leg_id_tail(live) >= now + QUOTE_MIN_LIFE:
            c._legs[r.rfq_id] = live
        else:
            c._legs.pop(r.rfq_id, None)
        raise
    try:
        quote_id = _word(q["quote_id"])
        echo_rate = e7.scaled6(q["rate"])
        leg_hash, echo_leg = q.get("leg_hash"), q.get("leg_id")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise BadAnswer("the quote answer cannot be read") from None
    if quote_id is None:
        raise BadAnswer("the quote answer names no quote_id")
    if q.get("rfq_id") is not None and str(q["rfq_id"]).lower() != r.rfq_id:
        raise BadAnswer("the quote answer names another RFQ")
    if (echo_rate != e7.scaled6(half["rate"]) or (leg_hash is not None and str(leg_hash).lower() != e7.h0x(digest))
            or (echo_leg is not None and str(echo_leg).lower() != leg_id)):
        raise BadAnswer("the gateway rested a quote other than the one signed")
    log.info("rfq %s: quoted %s, binding until %s", r.rfq_id, half["rate"], utc(end))
    return MakerQuote(
        rfq_id=r.rfq_id, quote_id=quote_id, pair=r.pair, side="buy" if half["side"] == 1 else "sell",
        notional=Decimal(half["notional"]), rate=rate, expiry=ms_to_dt(half["expiry"]),
        expires_at=ms_to_dt(_int(q.get("expires_at"))), client_quote_id=cqid, raw=q, leg=half, rfq=r,
    )


def drop(c: "Client", q: Any, leg_id: Any = None) -> Drop:
    """End the seat's binding leg on the RFQ and every quote on it."""
    if isinstance(q, MakerQuote):
        rfq_id, leg = q.rfq_id, _word(q.leg_id)
    elif isinstance(q, Rfq):
        rfq_id, leg = q.rfq_id, _word(leg_id) if leg_id is not None else c._legs.get(q.rfq_id)
    else:
        raise BadRequest("drop_quote() takes the MakerQuote that send_quote() returned, or an Rfq and leg_id=")
    if leg is None:
        raise BadRequest("leg_id is a 0x 32-byte hex word")
    try:
        a = c._gw.request("DELETE", f"/rfqs/{rfq_id}/quotes/{leg}")
    except UnknownOrEnded:
        if c._legs.get(rfq_id) == leg:
            del c._legs[rfq_id]
        raise
    if a.get("dropped") is not True or (a.get("leg_id") is not None and str(a["leg_id"]).lower() != leg):
        raise BadAnswer("the drop answer does not name this leg")
    if c._legs.get(rfq_id) == leg:
        del c._legs[rfq_id]
    log.info("rfq %s: leg %s dropped", rfq_id, leg)
    return Drop(rfq_id=rfq_id, leg_id=leg, at=ms_to_dt(_int(a.get("at_ms"))), raw=a)


def _accepted(c: "Client", q: MakerQuote, end: float, poll: float) -> None:
    """Wait for the accept. Read the RFQ view every third poll, because the taker shares its rate limit."""
    n = -1
    while True:
        n += 1
        if n % 3 == 0 or c._clock() >= end:
            try:
                view = c._gw.request("GET", f"/rfqs/{q.rfq_id}")
            except TRANSIENT:
                view = None
            if view is not None:
                if own_status(view, q.quote_id) == "accepted":
                    return
                why = lost(view, q.quote_id)
                if why is not None:
                    raise QuoteLost(LOST[why], reason=why, details={"rfq_id": q.rfq_id})
        if c._clock() >= end:
            raise QuoteLost("no accept before the wait ended", reason="timeout", details={"rfq_id": q.rfq_id})
        c._sleep(poll)


def own_status(view: dict, quote_id: str) -> str:
    """Return the status of the seat's quote ``quote_id`` in an RFQ view, or "" when absent."""
    mine = next((q for q in view.get("quotes") or [] if isinstance(q, dict)
                 and str(q.get("quote_id") or "").lower() == quote_id), None)
    return str(mine.get("status") or "").lower() if mine else ""


def lost(view: dict, quote_id: str) -> str | None:
    """Return why the quote opened no trade, or None while it still can. A maker's view lists its quotes only."""
    own = own_status(view, quote_id)
    if own == "accepted":
        return None
    st = str(view.get("status") or "").lower()
    if st == "cancelled":
        return "cancelled"
    if st in AFTER_ACCEPT:
        won = any(isinstance(q, dict) and str(q.get("status") or "").lower() == "accepted"
                  for q in view.get("quotes") or [])
        return "dropped" if won else "another_maker"
    if st == "expired" or own == "expired":
        return "expired"
    if own == "dropped":
        return "dropped"
    return None


def confirm(c: "Client", q: Any, timeout: float | None, poll: float, wait: float | None = None) -> Trade:
    """Wait for the accept until ``timeout`` (default: the RFQ's close plus 5 s), then read the trade."""
    if not isinstance(q, MakerQuote):
        raise BadRequest("confirm() takes the MakerQuote that send_quote() returned")
    now = c._clock()
    if timeout is None:
        closes = q.rfq.closes_at.timestamp() if q.rfq.closes_at else now + 120
        end = max(closes, now) + 5
    else:
        end = now + max(float(timeout), 0.0)
    _accepted(c, q, end, poll)
    log.info("rfq %s: accepted; the quote binds, nothing more to sign", q.rfq_id)
    status, view = c._settle(lambda: c._gw.request("GET", f"/rfqs/{q.rfq_id}"), "trade_status", wait)
    tx = _word(view.get("trade_tx"))
    log.info("rfq %s %s: tx %s", q.rfq_id, status, tx)
    return Trade(status=status, rfq_id=q.rfq_id, quote_id=q.quote_id, pair=q.pair, side=q.side,
                 notional=q.notional, rate=q.rate, tx=tx, raw=view, provisional=_provisional(view),
                 final=_final(view))


def live(c: "Client", price: Any, seconds: Any, out: Any) -> list[Trade]:
    """Run ``Client.quote_live``: quote new RFQs, follow each quote to its end, drop live quotes on exit."""
    if not callable(price):
        raise BadRequest("price is a function: price(rfq) returns a rate, or None to pass")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds < float("inf"):
        raise BadRequest("seconds is a number of seconds above 0")
    if out is not None and not callable(out):
        raise BadRequest("log is a function that takes one line, or None")

    def say(*values: Any) -> None:
        if out is not None:
            out(" ".join([datetime.fromtimestamp(c._clock()).strftime("%H:%M:%S"), *(str(v) for v in values)]))

    def closed(q: MakerQuote) -> bool:
        end = q.rfq.closes_at
        return end is not None and datetime.fromtimestamp(c._clock(), timezone.utc) > end + LIVE_DROP_AFTER

    def drop_live(q: MakerQuote) -> bool:
        try:
            c.drop_quote(q)
        except UnknownOrEnded:
            pass
        except CrxError:
            return False
        return True

    def post(r: Rfq, rate: Any) -> MakerQuote | None:
        rid = r.rfq_id[:10]
        try:
            return c.send_quote(r, rate)
        except TRANSIENT as e:
            # With no answer the quote may rest on the book, so drop its leg.
            say("no answer", rid, e.code)
            try:
                c.drop_quote(r)
            except (UnknownOrEnded, BadRequest):
                pass
            except AlreadyAccepted:
                say("accepted", rid, "read positions()")
            except CrxError as e2:
                say("not dropped", rid, e2.code)
        except CrxError as e:
            say("not quoted", rid, e.code)
        return None

    quotes: dict[str, MakerQuote] = {}
    opened: list[Trade] = []
    try:
        say(c.address)
        events = c.trades(market=True)
        after = events[-1].seq if events else None
        end = c._clock() + seconds
        while c._clock() < end:
            try:
                for r in c.rfqs(after=after, wait=LIVE_READ_S):
                    after = r.seq or after
                    rid = r.rfq_id
                    rate = price(r)
                    say("rfq", rid[:10], r.pair, r.side, r.notional, "pass" if rate is None else rate)
                    if rate is not None and rid not in quotes:
                        q = post(r, rate)
                        if q is not None:
                            quotes[rid] = q
                            say("quoted", rid[:10], q.rate)
            except TRANSIENT as e:
                say("read dropped", e.code)
                c._sleep(1.0)

            for rid, q in list(quotes.items()):
                try:
                    t = c.confirm(q, timeout=0, wait=c._settle_wait)  # the next round reads it again
                except QuoteLost as e:
                    if e.reason == "timeout" and (not closed(q) or not drop_live(q)):
                        continue
                    say("lost", rid[:10], e.reason)
                    del quotes[rid]
                    continue
                except CrxError as e:
                    say("read failed", rid[:10], e.code)
                    continue
                if t.status in ("pending", "sending"):
                    continue
                say("trade", rid[:10], t.pair, t.side, t.notional, t.rate, t.status)
                del quotes[rid]
                if t.status == "open":
                    opened.append(t)
    finally:
        for q in quotes.values():
            if not drop_live(q):
                say("not dropped", q.rfq_id[:10], "read positions()")
    return opened
