"""The maker's calls: read open RFQs, post a signed quote, sign the maker Side after the accept.

Every value the gateway serves is checked before a signature exists. A mismatch
raises RefusedToSign and nothing is signed.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Iterator

from eth_utils import keccak

from . import _eip712 as e7
from ._bind import SIDE_WINDOW, Binder, obj, read_later, status_code, utc
from ._http import Gateway
from .errors import (
    BadAnswer, BadRequest, CrxError, NetworkError, QuoteLost, RateLimited, RefusedToSign, ServerError,
    TradeUnknown, clean, from_gateway,
)
from .models import MakerQuote, Rfq, Trade, _side_of, dec, ms_to_dt

if TYPE_CHECKING:
    from .client import Client

log = logging.getLogger("crx")

PAGE = 1000  # /trades rows per page; the gateway's cap
U64_MAX = 2**64 - 1
CONSENT_WINDOW_MS = 86_400_000  # the gateway takes an RFQ quote_expiry at most 24 h out
CLOCK_SLACK_MS = 60_000
LIVE = ("open", "quoted")  # RFQ statuses that take a quote
AFTER_ACCEPT = ("accepted", "opened", "settled", "armed", "signing", "consenting")
SIGN_WINDOW = 120.0  # s: the maker signs within 120 s of the accept (the gateway's round window)
SIGN_GRACE = 5.0  # s past the sign window the gateway still takes the Side
TRANSIENT = (NetworkError, ServerError, RateLimited)


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
    opened, window = _int(d.get("opened_at")), _int(d.get("quote_window_secs"))
    rows = tuple(q for q in quotes if isinstance(q, dict)) if isinstance(quotes, (list, tuple)) else ()
    cid = d.get("client_rfq_id")
    return Rfq(
        rfq_id=rid, pair=_slash(d.get("pair")), side=_side_of(d.get("side")), notional=dec(d.get("notional")),
        expiry=ms_to_dt(_int(d.get("expiry"))), quote_expiry=ms_to_dt(_int(d.get("quote_expiry"))),
        im_bps=_int(d.get("im_bps")), premium_bps=_int(d.get("premium_bps")),
        kind=str(d.get("kind") or "open"), status=d.get("status") if isinstance(d.get("status"), str) else None,
        opened_at=ms_to_dt(opened),
        closes_at=ms_to_dt(opened + window * 1000) if opened is not None and window is not None else None,
        client_rfq_id=cid if isinstance(cid, str) and cid else None,
        seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None, quotes=rows, raw=dict(d),
    )


def _quotable(r: Rfq, chain: str, now_ms: float) -> bool:
    """An open RFQ on this chain, another seat's, inside its quote window."""
    d = r.raw
    if r.kind != "open" or d.get("chain") != chain or d.get("join_ref") is None:
        return False
    if r.status is not None and r.status not in LIVE:
        return False
    closes = r.closes_at.timestamp() * 1000 if r.closes_at else None
    qe = _int(d.get("quote_expiry"))
    return (closes is None or closes > now_ms) and (qe is None or qe > now_ms)


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


def stream(c: "Client", since: int | None, wait: float | None, poll: float, stop: Any = None) -> Iterator[Rfq]:
    """Open RFQs off the REST tape (``GET /trades``), oldest first, each once.

    Reads the tape from ``since`` (default: its start) up to its head at once, before
    it returns. From there it yields the RFQs still inside their quote window, then
    each new one. A gateway that does not answer, or answers 5xx or 429, is read again
    after ``poll`` s. Ends when ``wait`` s pass (None: never), or once ``stop`` is set.
    """
    cursor = int(since or 0)
    end = None if wait is None else c._clock() + max(float(wait), 0.0)
    backlog: list[Rfq] = []
    while True:
        rows, cursor = _page(c, cursor)
        backlog = [r for r in backlog if _quotable(r, c.chain_key, c._clock() * 1000)]
        backlog += [r for r in _opened(rows) if _quotable(r, c.chain_key, c._clock() * 1000)]
        if len(rows) < PAGE:
            break
    return _follow(c, cursor, backlog, end, poll, stop)


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


def maker_leg(seat: str, r: Rfq, chain: str, rate: Decimal, nonce: int, now_ms: float) -> dict:
    """The Leg this seat signs on ``r``: the RFQ's terms, this seat's own credential and side."""
    d = r.raw
    if r.kind != "open":
        raise BadRequest("a close RFQ takes a price-only quote; send_quote() quotes open RFQs only")
    if d.get("chain") != chain:
        raise BadRequest(f"the RFQ is on {clean(d.get('chain'), 40)}; this client signs for {chain}")
    if d.get("join_ref") is None:
        raise BadRequest("this is your own RFQ; a seat does not quote its own RFQ")
    leg_id, join_ref, pair_id = _word(d.get("leg_id")), _word(d.get("join_ref")), _word(d.get("pair_id"))
    if leg_id is None or join_ref is None or pair_id is None:
        raise RefusedToSign("refused to sign: the RFQ names no leg_id, join_ref or pair_id for this seat")
    if pair_id != e7.h0x(e7.pair_id(r.pair)):
        raise RefusedToSign(f"refused to sign: pair_id is not keccak256({clean(r.pair, 20)})")
    side = d.get("side")
    if isinstance(side, bool) or side not in (1, -1):
        raise RefusedToSign("refused to sign: the RFQ names no side for this seat")
    inst = d.get("instrument_id", 1)
    if isinstance(inst, bool) or inst != 1:
        raise RefusedToSign("refused to sign: the RFQ is not an NDF (instrument 1)")
    notional = d.get("notional")
    try:
        if not isinstance(notional, str) or Decimal(notional) <= 0:
            raise ValueError
        e7.scaled6(notional)
    except (ValueError, ArithmeticError):
        raise RefusedToSign("refused to sign: the RFQ's notional cannot be read") from None
    im_bps, premium = _int(d.get("im_bps")), _int(d.get("premium_bps"))
    if im_bps is None or not 1 <= im_bps <= 10_000:
        raise RefusedToSign("refused to sign: the RFQ's im_bps is not 1 to 10000")
    if premium is None or not -32_768 <= premium <= 32_767:
        raise RefusedToSign("refused to sign: the RFQ's premium_bps cannot be read")
    expiry, qe = _int(d.get("expiry")), _int(d.get("quote_expiry"))
    if expiry is None or expiry <= now_ms:
        raise RefusedToSign("refused to sign: the RFQ's expiry is not in the future")
    if qe is None or qe <= now_ms:
        raise QuoteLost("the RFQ's quote_expiry has passed; nothing signed", reason="expired")
    if qe > now_ms + CONSENT_WINDOW_MS + CLOCK_SLACK_MS:
        raise RefusedToSign("refused to sign: the RFQ's quote_expiry is more than 24 h out")
    return {
        "seat": seat, "leg_id": leg_id, "join_ref": join_ref, "pair_id": pair_id, "instrument_id": 1,
        "side": side, "notional": notional, "rate": _plain(rate), "im_bps": im_bps, "premium_bps": premium,
        "expiry": expiry, "nonce": str(nonce), "quote_expiry": qe,
    }


def nonce_for(seat: str, client_quote_id: str) -> int:
    """The low 64 bits of keccak256(seat ‖ client_quote_id): the nonce of the maker's Leg."""
    return int.from_bytes(keccak(bytes.fromhex(seat[2:]) + client_quote_id.encode())[-8:], "big")


def send(c: "Client", r: Any, rate: Any, client_quote_id: str | None, expires_in: float | None) -> MakerQuote:
    if not isinstance(r, Rfq):
        raise BadRequest("send_quote() takes an Rfq from rfqs() or rfq()")
    rate = _rate(rate)
    cqid = client_quote_id or f"sdk-q-{uuid.uuid4().hex[:16]}"
    if not isinstance(cqid, str) or not 0 < len(cqid) <= 128 or not cqid.isprintable():
        raise BadRequest("client_quote_id is 1 to 128 printable characters")
    b = c._binder()
    now = c._clock()
    leg = maker_leg(c.address, r, c.chain_key, rate, nonce_for(c.address, cqid), now * 1000)
    try:
        digest = e7.leg_digest(b.sep, leg)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise RefusedToSign("refused to sign: the Leg cannot be encoded") from None
    body: dict[str, Any] = {"rate": leg["rate"], "client_quote_id": cqid}
    if expires_in is not None:
        if not 0 < float(expires_in) <= 86_400:
            raise BadRequest("expires_in is seconds, above 0 and at most 86400")
        body["expires_at"] = int((now + float(expires_in)) * 1000)
    body["sig"] = "0x" + bytes(c._account.unsafe_sign_hash(digest).signature).hex()
    q = c._gw.request("POST", f"/rfqs/{r.rfq_id}/quotes", body=body)
    try:
        quote_id = _word(q["quote_id"])
        echo_rate = e7.scaled6(q["rate"])
        leg_hash = q.get("leg_hash")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise BadAnswer("the quote answer cannot be read") from None
    if quote_id is None:
        raise BadAnswer("the quote answer names no quote_id")
    if q.get("rfq_id") is not None and str(q["rfq_id"]).lower() != r.rfq_id:
        raise BadAnswer("the quote answer names another RFQ")
    if echo_rate != e7.scaled6(leg["rate"]) or (leg_hash is not None and str(leg_hash).lower() != e7.h0x(digest)):
        raise BadAnswer("the gateway rested a quote other than the one signed")
    log.info("rfq %s: quoted %s", r.rfq_id, leg["rate"])
    return MakerQuote(
        rfq_id=r.rfq_id, quote_id=quote_id, pair=r.pair, side="buy" if leg["side"] == 1 else "sell",
        notional=Decimal(leg["notional"]), rate=rate, expiry=ms_to_dt(leg["expiry"]),
        expires_at=ms_to_dt(_int(q.get("expires_at"))), client_quote_id=cqid, raw=q, leg=leg, rfq=r,
    )


# ---------- the maker Side ----------

def check_side(b: Binder, t: dict, leg: dict) -> bytes:
    """Rebuild this seat's maker half with the gateway's nonce and the Side quote expiry.

    Returns the digest to sign. Writes the nonce floor first.
    """
    try:
        if t.get("digest_kind") != "side":
            raise RefusedToSign("refused to sign: the template is not a Side template")
        if t["own_leg_id"].lower() != leg["leg_id"].lower():
            raise RefusedToSign("refused to sign: own_leg_id is not this leg")
        c_maker = e7.half_commitment(e7.arm_words(leg, t["own_nonce"], t["quote_expiry"]), t["own_salt"])
        if e7.h0x(c_maker) != t["c_maker"].lower():
            raise RefusedToSign("refused to sign: c_maker is not this leg")
        if e7.h0x(e7.pair_commitment(e7.hx(t["c_taker"]), c_maker)) != t["pair_c"].lower():
            raise RefusedToSign("refused to sign: pair_c is not keccak(0x03, c_taker, c_maker)")
        if str(t["domain_separator"]).lower() != e7.h0x(b.sep):
            raise RefusedToSign("refused to sign: the template's domain is not this core's")
        own_nonce, qe = int(t["own_nonce"]), int(t["quote_expiry"])
        now = b.now()
        last = b.last_signed()
        if own_nonce >= U64_MAX:
            raise RefusedToSign("refused to sign: own_nonce is out of range")
        if own_nonce > max(int(now * 1000), last) + 86_400_000:
            raise RefusedToSign("refused to sign: own_nonce is more than one day ahead")
        if own_nonce <= last:
            raise RefusedToSign("refused to sign: own_nonce is not above the last one this seat signed")
        if qe <= now:
            raise QuoteLost("the Side window has passed; no trade", reason="round_closed")
        if qe > now + SIDE_WINDOW:
            raise RefusedToSign(f"refused to sign: the Side quote_expiry is more than {SIDE_WINDOW} s ahead")
        digest = e7.side_digest(b.sep, t)
        if e7.h0x(digest) != str(t["digest"]).lower():
            raise RefusedToSign("refused to sign: the served digest is not this Side")
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        raise RefusedToSign("refused to sign: the Side template cannot be read") from None
    b.keep_signed(own_nonce)
    return digest


def lost(view: dict, quote_id: str) -> str | None:
    """Why the quote opened no trade, from the seat's own view of the RFQ; None while it still can."""
    mine = next((q for q in view.get("quotes") or [] if isinstance(q, dict)
                 and str(q.get("quote_id") or "").lower() == quote_id), None)
    own = str(mine.get("status") or "").lower() if mine else ""
    if own == "accepted":
        return None
    st = str(view.get("status") or "").lower()
    if st == "cancelled":
        return "cancelled"
    if st in AFTER_ACCEPT:
        return "another_maker"
    if st == "expired" or own == "expired":
        return "expired"
    return None


def _template(c: "Client", q: MakerQuote, end: float, poll: float) -> dict | None:
    """Poll GET /rfqs/{id}/side until the maker's template shows. None when the pair already armed."""
    path = f"/rfqs/{q.rfq_id}/side"
    while True:
        try:
            r = c._gw.raw_request("GET", path)
        except NetworkError:
            r = None
        k = status_code(r)
        if r is not None and r.status_code == 200:
            t = obj(r)
            if not t:
                raise BadAnswer("the Side template is not an object")
            return t
        if k == (409, "round_closed"):
            raise QuoteLost("the Side round closed before the pair armed; no trade", reason="round_closed",
                            status=409, gateway_code="round_closed")
        if k == (409, "conflict"):
            return None  # the pair armed: this seat signed before
        if r is not None and r.status_code not in (404, 429) and r.status_code < 500:
            raise from_gateway(r.status_code, Gateway.body_of(r), r.text[:300] if r.text else "")
        try:
            why = lost(c._gw.request("GET", f"/rfqs/{q.rfq_id}"), q.quote_id)
        except TRANSIENT:
            why = None
        if why is not None:
            raise QuoteLost({
                "another_maker": "the taker accepted another quote",
                "expired": "the RFQ ended with no accept",
                "cancelled": "the RFQ was cancelled",
            }[why], reason=why, details={"rfq_id": q.rfq_id})
        if c._clock() >= end:
            raise QuoteLost("no accept before the wait ended", reason="timeout", details={"rfq_id": q.rfq_id})
        c._sleep(poll)


def _post_side(c: "Client", q: MakerQuote, sig: str, t: dict, sign_by: float) -> None:
    """Post the maker Side signature; the same bytes again on no answer or 5xx, until ``sign_by``."""
    why = None
    try:
        while True:
            try:
                r = c._gw.raw_request("POST", f"/rfqs/{q.rfq_id}/side", body={"sig": sig})
            except NetworkError:
                r = None
            if r is not None and r.status_code == 200:
                return
            k = status_code(r)
            if k == (409, "round_closed"):
                raise QuoteLost("the Side round closed before the pair armed; no trade", reason="round_closed",
                                status=409, gateway_code="round_closed")
            if k == (409, "conflict"):
                return  # already signed, or the pair armed: the trade status decides
            if r is not None and r.status_code < 500 and r.status_code != 429:
                raise from_gateway(r.status_code, Gateway.body_of(r), r.text[:300] if r.text else "")
            if c._clock() + 1 >= sign_by:
                why = "no answer" if r is None else f"HTTP {r.status_code}"
                break
            c._sleep(1.0)
    except QuoteLost:
        raise
    except CrxError as e:
        if e.status is not None and e.status < 500:
            raise  # the gateway refused the signature: it holds none
        why = f"{e.code}: {e}"
    raise TradeUnknown(
        f"stopped after the Side was signed ({clean(why, 200)}); the trade may still open; "
        + read_later(int(t["quote_expiry"])), details={"rfq_id": q.rfq_id})


def confirm(c: "Client", q: Any, timeout: float | None, poll: float) -> Trade:
    if not isinstance(q, MakerQuote):
        raise BadRequest("confirm() takes the MakerQuote that send_quote() returned")
    b = c._binder()
    now = c._clock()
    if timeout is None:
        closes = q.rfq.closes_at.timestamp() if q.rfq.closes_at else now + 120
        end = max(closes, now) + 5
    else:
        end = now + max(float(timeout), 0.0)
    t = _template(c, q, end, poll)
    if t is not None and t.get("signed") is not True:
        accepted = c._clock()
        exp = q.raw.get("expires_at")
        sign_by = min((exp if isinstance(exp, int) else 10**13) / 1000, accepted - poll + SIGN_WINDOW) + SIGN_GRACE
        digest = check_side(b, t, q.leg)
        sig = "0x" + bytes(c._account.unsafe_sign_hash(digest).signature).hex()
        _post_side(c, q, sig, t, sign_by)
        log.info("rfq %s: maker side signed, nonce %s, by %s", q.rfq_id, clean(t["own_nonce"]), utc(sign_by))
    status, view = c._settle(lambda: c._gw.request("GET", f"/rfqs/{q.rfq_id}"), "trade_status")
    tx = _word(view.get("trade_tx"))
    log.info("rfq %s %s: tx %s", q.rfq_id, status, tx)
    return Trade(status=status, rfq_id=q.rfq_id, quote_id=q.quote_id, pair=q.pair, side=q.side,
                 notional=q.notional, rate=q.rate, tx=tx, raw=view)
