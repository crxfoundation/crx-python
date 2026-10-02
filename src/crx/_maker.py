"""The maker's calls: read open RFQs, post a signed binding quote, wait for the accept.

The maker signs one ``Quote`` per quote (SPEC v5 §3): its own half of the RFQ's terms,
a fresh salt and the RFQ's ``taker_ref``. The quote is its trade signature: nothing is
signed after the accept. Every value the gateway serves is checked before a signature
exists. A mismatch raises RefusedToSign and nothing is signed.
"""

from __future__ import annotations

import logging
import secrets
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Iterator

from eth_utils import keccak

from . import _eip712 as e7
from ._bind import QUOTE_WINDOW, utc
from .errors import (
    BadAnswer, BadRequest, LegIdTaken, LegLive, NetworkError, NoQuotes, QuoteLost, RateLimited,
    RefusedToSign, ServerError, UnknownOrEnded, clean,
)
from .models import Drop, MakerQuote, Rfq, Trade, _side_of, dec, ms_to_dt

if TYPE_CHECKING:
    from .client import Client

log = logging.getLogger("crx")

PAGE = 1000  # /trades rows per page; the gateway's cap
U64_MAX = 2**64 - 1
LIVE = ("open", "quoted")  # RFQ statuses that take a quote
AFTER_ACCEPT = ("accepted", "opened", "settled", "armed", "signing", "consenting")
TRANSIENT = (NetworkError, ServerError, RateLimited)
QUOTE_MIN_LIFE = 60  # s: the gateway takes a binding quote whose quote end is at least this far out
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
    """One RFQ from an ``rfq.opened`` frame or a ``GET /rfqs/{id}`` view. None when it names no RFQ id."""
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


def _quotable(r: Rfq, chain: str, now_ms: float) -> bool:
    """An open RFQ on this chain, another seat's, inside its quote window and before its quote end."""
    d = r.raw
    if r.kind != "open" or d.get("chain") != chain or r.own:
        return False
    if r.status is not None and r.status not in LIVE:
        return False
    closes = r.closes_at.timestamp() * 1000 if r.closes_at else None
    qe = _int(d.get("quote_expiry_max"))
    return (closes is None or closes > now_ms) and (qe is None or qe * 1000 > now_ms)


# ---------- the RFQ stream ----------

def _page(c: "Client", cursor: int) -> tuple[list, int]:
    page = c._gw.request("GET", "/trades", query={"since": cursor, "limit": PAGE})
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
    c: "Client", since: int | None, wait: float | None, poll: float, stop: Any = None, only: Any = None,
) -> Iterator[Rfq]:
    """Open RFQs off the REST tape (``GET /trades``), oldest first, each once.

    Reads the tape from ``since`` (default: its start) up to its head at once, before
    it returns. From there it yields the RFQs still inside their quote window, then
    each new one. A gateway that does not answer, or answers 5xx or 429, is read again
    after ``poll`` s. Ends when ``wait`` s pass (None: never), or once ``stop`` is set.
    ``only`` (an ``Ask``, or an RFQ id): see ``_only``.
    """
    rid = None
    if only is not None:
        rid = _word(getattr(only, "rfq_id", only))
        if rid is None:
            raise BadRequest("only is an Ask, or an RFQ id")
    cursor = int(since or 0)
    end = None if wait is None else c._clock() + max(float(wait), 0.0)
    backlog: list[Rfq] = []
    while True:
        rows, cursor = _page(c, cursor)
        backlog = [r for r in backlog if _quotable(r, c.chain_key, c._clock() * 1000)]
        backlog += [r for r in _opened(rows) if _quotable(r, c.chain_key, c._clock() * 1000)]
        if len(rows) < PAGE:
            break
    rfqs = _follow(c, cursor, backlog, end, poll, stop)
    return rfqs if rid is None else _only(rfqs, rid, stop)


def _only(rfqs: Iterator[Rfq], rfq_id: str, stop: Any) -> Iterator[Rfq]:
    """The RFQ ``rfq_id`` alone off ``rfqs``, then the end. ``NoQuotes`` when ``rfqs`` ends
    before it arrives, unless ``stop`` ended it."""
    for r in rfqs:
        if r.rfq_id == rfq_id:
            yield r
            return
    if stop is None or not stop.is_set():
        raise NoQuotes("the RFQ did not show in this account's open RFQs before the wait ended",
                       details={"rfq_id": rfq_id})


def _follow(
    c: "Client", cursor: int, backlog: list[Rfq], end: float | None, poll: float, stop: Any,
) -> Iterator[Rfq]:
    seen: set[str] = set()
    pending = list(backlog)
    while True:
        for r in pending:
            if r.rfq_id in seen or not _quotable(r, c.chain_key, c._clock() * 1000):
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


# ---------- the quote ----------

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


def maker_terms(seat: str, r: Rfq, chain: str, rate: Decimal, now_ms: float) -> dict:
    """The terms of this seat's half on ``r``: the RFQ's terms, this seat's own side and rate.
    The pair id is keccak256 of the RFQ's pair."""
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
    expiry = _int(d.get("expiry"))
    if expiry is None or expiry <= now_ms:
        raise RefusedToSign("refused to sign: the RFQ's expiry is not in the future")
    return {
        "seat": seat, "pair_id": e7.h0x(e7.pair_id(r.pair)), "side": side, "notional": notional,
        "rate": _plain(rate), "premium_bps": premium, "expiry": expiry,
    }


def nonce_for(seat: str, client_quote_id: str) -> int:
    """The low 64 bits of keccak256(seat ‖ client_quote_id): the Quote nonce with derive_maker_nonce off."""
    return int.from_bytes(keccak(bytes.fromhex(seat[2:]) + client_quote_id.encode())[-8:], "big")


def send(c: "Client", r: Any, rate: Any, client_quote_id: str | None) -> MakerQuote:
    """Post a binding quote on ``r``: this seat signs a ``Quote`` over its own half, a fresh
    salt and the RFQ's ``taker_ref``. Nothing is signed after the accept.

    The leg id is this seat's own: 24 random bytes, then the quote end, the RFQ's
    ``quote_expiry_max``. A later quote on the same RFQ keeps it, so one leg fills at most
    once. The Quote nonce is the u64 of leg id bytes 16 to 23. The salt goes to the gateway
    only and is not kept.
    """
    if not isinstance(r, Rfq):
        raise BadRequest("send_quote() takes an Rfq from rfqs() or rfq()")
    rate = _rate(rate)
    cqid = client_quote_id
    if cqid is not None and (not isinstance(cqid, str) or not 0 < len(cqid) <= 128
                             or not all(" " <= ch <= "~" for ch in cqid)):
        raise BadRequest("client_quote_id is 1 to 128 printable ASCII characters")
    if cqid is None and not e7.DERIVE_MAKER_NONCE:
        cqid = f"sdk-q-{secrets.token_hex(8)}"
    b = c._binder()
    now = c._clock()
    d = r.raw
    terms = maker_terms(c.address, r, c.chain_key, rate, now * 1000)
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
    try:
        q = c._gw.request("POST", f"/rfqs/{r.rfq_id}/quotes", body=body)
    except LegIdTaken:
        c._legs.pop(r.rfq_id, None)
        raise
    except LegLive as e:
        # The seat's live leg on the RFQ: the next quote goes on it while it has the life left.
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
    """DELETE /rfqs/{id}/quotes/{leg_id}: end this seat's binding leg, every quote on it."""
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
    """Wait for the taker's accept of a binding quote. Returns once the seat's own quote reads accepted.

    Reads the RFQ view only, on every third poll and at the end of the wait: the view's
    per-IP budget is shared with the taker's own polls.
    """
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


# ---------- the accept ----------

def own_status(view: dict, quote_id: str) -> str:
    """The status of the seat's own quote ``quote_id`` in its view of the RFQ; "" when the view lacks it."""
    mine = next((q for q in view.get("quotes") or [] if isinstance(q, dict)
                 and str(q.get("quote_id") or "").lower() == quote_id), None)
    return str(mine.get("status") or "").lower() if mine else ""


def lost(view: dict, quote_id: str) -> str | None:
    """Why the quote opened no trade, from the seat's own view of the RFQ; None while it still can.

    The RFQ's status is read before the quote's own. Once an RFQ is accepted, expired or
    cancelled, every other binding quote on it reads ``dropped``. A maker's view holds its
    own quotes only: another of them accepted means this one was replaced.
    """
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


def confirm(c: "Client", q: Any, timeout: float | None, poll: float) -> Trade:
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
    status, view = c._settle(lambda: c._gw.request("GET", f"/rfqs/{q.rfq_id}"), "trade_status")
    tx = _word(view.get("trade_tx"))
    log.info("rfq %s %s: tx %s", q.rfq_id, status, tx)
    return Trade(status=status, rfq_id=q.rfq_id, quote_id=q.quote_id, pair=q.pair, side=q.side,
                 notional=q.notional, rate=q.rate, tx=tx, raw=view)
