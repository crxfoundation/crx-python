"""Take over a leg on a close round: bid a price, wait for the award, check and sign its failover form.

Unlisted. Each function takes a ``Client``. A failover form that differs from the bid in one word, and
any other form, is declined and not signed.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from eth_utils import keccak

from . import _eip712 as e7
from . import _maker
from .errors import (
    BadAnswer, BadRequest, CrxError, NetworkError, QuoteLost, RateLimited, RefusedToSign, ServerError, clean,
)
from .signer import sign_typed

if TYPE_CHECKING:
    from .client import Client

log = logging.getLogger("crx")

FAILOVER = "failover_consent"  # the one form this module signs
INCOMING = "incoming"
SIDE_TAG = b"CRX/side/v1"
OPEN_NONCE_TAG = b"crx/alloc-open-nonce/v1"
SALT_MIN_DISTINCT = 16  # a CSPRNG word shows about 30 distinct byte values
CLOCK_SKEW_S = 5
DEADLINE_S = 1200  # a failover form's deadline is at most this far past now
CLOSE_TIME_AGE_S = 3600  # a failover form's fix second is in the last hour
SIGN_GRACE_S = 330  # the award wait past the round's quote window end
WAIT_S = 600  # the award wait when the round names no end
POLL_S = 0.5
HTTP_TIMEOUT_S = 2.0  # seconds: the form read and the signature post
STATUS_EVERY = 4  # read the round's status once per this many template reads
SIGN_TRIES = 3
ENDED = ("expired", "cancelled", "settled")
U128 = 2**128 - 1
BPS_DENOM = 10_000
BEST_EXEC_FLOOR_BPS = 100
BEST_EXEC_K = 2
TRANSIENT = (NetworkError, ServerError, RateLimited)


@dataclass(frozen=True)
class Bid:
    """A price-only quote on a close round, with the terms its award must carry."""

    rfq_id: str
    quote_id: str
    client_quote_id: str
    rate: Decimal
    rate_e6: int
    side: int  # the bidder's own side of ``pair``: +1 buys the base currency
    pair: str
    pair_id: str
    notional_e6: int
    settlement_s: int
    chain: str
    seat: str
    closes_at: float | None  # unix seconds


@dataclass(frozen=True)
class Signed:
    """A form signed and posted, or found signed."""

    rfq_id: str
    digest_kind: str
    digest: str
    state: str | None
    already: bool = False


class _Mismatch(Exception):
    def __init__(self, word: str, why: str) -> None:
        super().__init__(f"{word}: {why}")
        self.word, self.why = word, why


def _need(ok: bool, word: str, why: str) -> None:
    if not ok:
        raise _Mismatch(word, why)


def _snake(name: str) -> str:
    return re.sub(r"[A-Z]", lambda m: "_" + m.group().lower(), name)


def _word_at(d: Any, key: str, kind: str, where: str) -> Any:
    """Return ``d[key]`` parsed as ``kind``. Raise _Mismatch when it is missing or does not parse."""
    if not isinstance(d, dict) or key not in d:
        raise _Mismatch(f"{where}{key}", "missing")
    try:
        return e7.word_of(kind, d[key])
    except ValueError:
        raise _Mismatch(f"{where}{key}", f"not a {kind}") from None


def strong(word: bytes) -> bool:
    """Return True when ``word`` shows at least ``SALT_MIN_DISTINCT`` distinct byte values."""
    return len(set(word)) >= SALT_MIN_DISTINCT


def incoming_side(incoming_c: bytes) -> bytes:
    """Return keccak256("CRX/side/v1" ‖ incomingC)."""
    return keccak(SIDE_TAG + incoming_c)


def open_nonce(old_id: bytes, nonce: int) -> int:
    """Return the leading 8 bytes, big endian, of keccak256("crx/alloc-open-nonce/v1" ‖ oldId ‖ nonce)."""
    return int.from_bytes(keccak(OPEN_NONCE_TAG + old_id + int(nonce).to_bytes(8, "big"))[:8], "big")


def fee_usd6(notional: int, mark: int, p: int, side: int, usd_is_base: bool) -> int:
    """Return the fee a failover close at ``p`` charges against ``mark``, USD at 6 dp, saturated at 2^128 - 1."""
    if side == 1:
        d = max(mark - p, 0)
    elif side == -1:
        d = max(p - mark, 0)
    else:
        return 0
    div = mark if usd_is_base else 10**6
    if d == 0 or div == 0:
        return 0
    return min(notional * d // div, U128)


def cap_usd6(notional: int, entry: int, usd_is_base: bool) -> int:
    """Return the largest fee a failover form carries on the leg: 2 * ceil(gross * 100 / 10 000), saturated."""
    gross = notional if usd_is_base else min(notional * entry // 10**6, U128)
    step = min(min(gross * BEST_EXEC_FLOOR_BPS, U128) + BPS_DENOM - 1, U128) // BPS_DENOM
    return min(BEST_EXEC_K * step, U128)


def _terms_id(taker_side: bytes, maker_side: bytes, old: dict) -> bytes:
    return e7.terms_id({
        "takerSide": e7.h0x(taker_side), "makerSide": e7.h0x(maker_side), "notional": old["notional"],
        "premiumBps": old["premium_bps"], "nonce": old["nonce"], "pair": e7.h0x(old["pair"]), "side": old["side"],
        "settlement": old["settlement"], "rate": old["rate"],
    })


def _old(t: dict, b: Bid) -> dict:
    """Parse the old leg's terms and check them against the bid."""
    o = t.get("old")
    _need(isinstance(o, dict), "old", "the template carries no old leg terms")
    old = {
        key: _word_at(o, key, kind, "old.") for key, kind in (
            ("notional", "uint256"), ("premium_bps", "int16"), ("nonce", "uint64"), ("pair", "bytes32"),
            ("side", "int8"), ("settlement", "uint40"), ("rate", "uint64"))
    }
    _need(e7.h0x(old["pair"]) == b.pair_id, "old.pair", "not the bid's pair")
    _need(old["notional"] == b.notional_e6, "old.notional", "not the bid's notional")
    _need(old["settlement"] == b.settlement_s, "old.settlement", "not the bid's settlement")
    _need(old["side"] in (1, -1), "old.side", "not a direction")
    return old


def _head(t: Any, b: Bid, kind: str, sep: bytes) -> bytes:
    """Check the template's frame words and return its ``incoming_c``."""
    _need(isinstance(t, dict), "template", "not a JSON object")
    _need(t.get("digest_kind") == kind, "digest_kind", f"not {kind}")
    _need(t.get("role") == INCOMING, "role", f"not {INCOMING}")
    _need(_maker._word(t.get("rfq_id")) == b.rfq_id, "rfq_id", "not the bid's round")
    _need(t.get("chain") == b.chain, "chain", "not the bid's chain")
    _need(_maker._word(t.get("domain_separator")) == e7.h0x(sep), "domain_separator", "not this client's domain")
    incoming_c = _word_at(t, "incoming_c", "bytes32", "")
    _need(strong(incoming_c), "incoming_c", "weak")
    return incoming_c


def _item(t: dict, fields: Any) -> tuple[dict, dict, bytes]:
    """Return the item words parsed by camel name, the item in JSON form by camel name, and its salt."""
    item = t.get("item")
    _need(isinstance(item, dict), "item", "not a JSON object")
    words, raw = {}, {}
    for name, kind in fields:
        key = _snake(name)
        words[name] = _word_at(item, key, kind, "item.")
        raw[name] = item[key]
    salt = _word_at(item, "salt", "bytes32", "item.")
    _need(strong(salt), "item.salt", "weak")
    return words, raw, salt


def _tail(t: dict, primary: str, msg: dict, chain_id: int, core: str, commitment: bytes) -> tuple[dict, bytes]:
    """Check the commitment, the digest and the served typed data. Return the typed data and its digest."""
    _need(_maker._word(t.get("commitment")) == e7.h0x(commitment), "commitment", "not the item's own")
    td = e7.typed_data(primary, chain_id, core, msg)
    digest = e7.typed_digest(td)
    _need(_maker._word(t.get("digest")) == e7.h0x(digest), "digest", "not the item's own")
    served = t.get("typed_data")
    if served is not None:
        miss = e7.typed_mismatch(served, td)
        _need(miss is None, f"typed_data.{miss}", "not the item's own")
    return td, digest


def check_failover(t: Any, b: Bid, *, chain_id: int, core: str, sep: bytes, max_notional_e6: int,
                   now: float) -> tuple[dict, bytes]:
    """Check a ``failover_consent`` template against bid ``b``. Return the typed data and digest to sign.

    Raises _Mismatch on the first word that differs. ``feed_id`` and the seat's allocation ``nonce`` are
    signed as served; the served mark prices the spread.
    """
    incoming_c = _head(t, b, FAILOVER, sep)
    w, raw, salt = _item(t, e7.CLOSEOUT_ITEM_FIELDS)
    _need(w["incoming"] == b.seat, "item.incoming", "not this seat")
    side_id = incoming_side(incoming_c)
    _need(w["incomingSide"] == side_id, "item.incoming_side", "not keccak(CRX/side/v1 ‖ incoming_c)")
    _need(len({w["closedOutSide"], w["remainingSide"], side_id}) == 3, "item.remaining_side",
          "the three side ids are not distinct")
    _need(w["subsidyMaxUsd"] == 0, "item.subsidy_max_usd", "not 0")
    _need(now < w["deadline"] <= now + DEADLINE_S + CLOCK_SKEW_S, "item.deadline",
          f"not within {DEADLINE_S + CLOCK_SKEW_S} s ahead")
    _need(now - CLOSE_TIME_AGE_S <= w["closeTime"] <= now + CLOCK_SKEW_S, "item.close_time",
          "not in the last hour")
    p = t.get("pricing")
    _need(isinstance(p, dict), "pricing", "missing")
    mark = _word_at(p, "mark", "uint64", "pricing.")
    notional = _word_at(p, "notional", "uint128", "pricing.")
    entry = _word_at(p, "entry_rate", "uint64", "pricing.")
    quote_rate = _word_at(p, "quote_rate", "uint64", "pricing.")
    c_side = _word_at(p, "side", "int8", "pricing.")
    usd_is_base = p.get("usd_is_base")
    _need(isinstance(usd_is_base, bool), "pricing.usd_is_base", "not a bool")
    if "USD" in (b.pair[:3], b.pair[4:]):
        _need(usd_is_base == (b.pair[:3] == "USD"), "pricing.usd_is_base", f"not the {b.pair} base")
    _need(quote_rate == b.rate_e6, "pricing.quote_rate", "not the bid's rate")
    _need(c_side == b.side, "pricing.side", "not the bid's side")
    _need(notional == b.notional_e6, "pricing.notional", "not the bid's notional")
    _need(notional <= max_notional_e6, "max_notional", "the notional is over the cap")
    old = _old(t, b)
    _need(old["rate"] == entry, "pricing.entry_rate", "not the old leg's rate")
    taker, maker = ((w["closedOutSide"], w["remainingSide"]) if c_side == old["side"]
                    else (w["remainingSide"], w["closedOutSide"]))
    _need(w["oldId"] == _terms_id(taker, maker, old), "item.old_id", "not the terms id of the old leg")
    _need(w["openNonce"] == open_nonce(w["oldId"], w["nonce"]), "item.open_nonce", "not the old id's open nonce")
    fee = fee_usd6(notional, mark, quote_rate, c_side, usd_is_base)
    _need(fee <= cap_usd6(notional, entry, usd_is_base), "item.spread", "the cap cuts the bid's fee")
    _need(w["spread"] == fee, "item.spread", "not the bid's fee against the mark")
    commitment = e7.hidden_commitment(e7.CLOSEOUT_ITEM_FIELDS, raw, e7.h0x(salt))
    msg = {
        "oldId": e7.h0x(w["oldId"]), "closedOutSide": e7.h0x(w["closedOutSide"]),
        "remainingSide": e7.h0x(w["remainingSide"]), "incomingSide": e7.h0x(side_id),
        "incomingC": e7.h0x(incoming_c), "nonce": str(w["nonce"]), "openNonce": str(w["openNonce"]),
        "deadline": str(w["deadline"]), "commitment": e7.h0x(commitment),
    }
    return _tail(t, "FailoverConsent", msg, chain_id, core, commitment)


def _text(value: Any) -> str | None:
    """Return served text as one printable line of at most 40 characters, or None for None."""
    return None if value is None else clean(value, 40)


def _rid(rfq_id: Any) -> str:
    rid = _maker._word(rfq_id)
    if rid is None:
        raise BadRequest("rfq_id is a 0x 32-byte hex word")
    return rid


def _cap(max_notional: Any) -> int:
    try:
        cap = e7.e6(max_notional)
    except ValueError as e:
        raise BadRequest(f"max_notional: {e}") from None
    if cap <= 0:
        raise BadRequest("max_notional must be above zero")
    return cap


def bid(client: "Client", rfq_id: Any, price: Any, *, max_notional: Any = None,
        client_quote_id: str | None = None) -> Bid:
    """Post a price-only quote at ``price`` on the close round ``rfq_id``.

    ``max_notional`` refuses a round over that base notional. Nothing is signed.
    """
    client._need_seat()
    client._keep_live()
    rid = _rid(rfq_id)
    rate = _maker._rate(price)
    cqid = client_quote_id or f"tko-{secrets.token_hex(8)}"
    if not isinstance(cqid, str) or not 0 < len(cqid) <= 128 or not all(" " <= ch <= "~" for ch in cqid):
        raise BadRequest("client_quote_id is 1 to 128 printable ASCII characters")
    client._chain_info()  # the domain the form is checked against, read before the award
    r = _maker.read(client, rid)
    d = r.raw
    now = client._clock()
    if r.kind != "close":
        raise BadRequest("the RFQ is not a close round")
    if d.get("chain") != client.chain_key:
        raise BadRequest(f"the round is on {clean(d.get('chain'), 40)!r}; this client signs for {client.chain_key}")
    if r.status is not None and r.status not in _maker.LIVE:
        raise QuoteLost(f"the round is {clean(r.status, 40)}", reason="expired", details={"rfq_id": rid})
    side = d.get("side")
    if isinstance(side, bool) or side not in (1, -1):
        raise BadAnswer("the round names no side for this seat")
    if not e7.pair_text_ok(r.pair):
        raise BadAnswer("the round names no AAA/BBB pair")
    try:
        notional_e6 = e7.scaled6(d.get("notional"))
        expiry = int(d["expiry"])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise BadAnswer("the round's notional or expiry cannot be read") from None
    if notional_e6 <= 0 or expiry <= 0:
        raise BadAnswer("the round's notional or expiry is not above zero")
    closes = r.closes_at.timestamp() if r.closes_at is not None else None
    if closes is not None and closes <= now:
        raise QuoteLost("the round's quote window has ended", reason="expired", details={"rfq_id": rid})
    if max_notional is not None and notional_e6 > _cap(max_notional):
        raise BadRequest("the round's notional is over max_notional")
    q = client._gw.request("POST", f"/rfqs/{rid}/quotes",
                           body={"rate": _maker._plain(rate), "client_quote_id": cqid})
    quote_id = _maker._word(q.get("quote_id"))
    try:
        echo = e7.scaled6(q.get("rate"))
    except (TypeError, ValueError, ArithmeticError):
        echo = None
    if quote_id is None or echo != e7.scaled6(rate):
        raise BadAnswer("the quote answer names no quote_id, or another rate")
    log.info("takeover bid %s on %s at %s", quote_id, rid, _maker._plain(rate))
    return Bid(
        rfq_id=rid, quote_id=quote_id, client_quote_id=cqid, rate=rate, rate_e6=e7.scaled6(rate), side=side,
        pair=r.pair, pair_id=e7.h0x(e7.pair_id(r.pair)), notional_e6=notional_e6, settlement_s=expiry // 1000,
        chain=client.chain_key, seat=client.address, closes_at=closes,
    )


def read_template(client: "Client", rfq_id: Any) -> dict | None:
    """Return this seat's form on the round, or None while the gateway serves it none."""
    rid = _rid(rfq_id)
    r = client._gw.raw_request("GET", f"/rfqs/{rid}/close", timeout=HTTP_TIMEOUT_S)
    if r.status_code == 404:
        return None
    return client._gw.parse(r)


def _ended(client: "Client", b: Bid) -> None:
    """Raise QuoteLost when the round ended or the bid is no longer live."""
    view = client._gw.request("GET", f"/rfqs/{b.rfq_id}")
    status = view.get("status")
    if status in ENDED:
        raise QuoteLost(f"the round is {status}", reason="cancelled" if status == "cancelled" else "expired",
                        details={"rfq_id": b.rfq_id})
    for q in view.get("quotes") or ():
        if isinstance(q, dict) and _maker._word(q.get("quote_id")) == b.quote_id and q.get("status") in (
                "declined", "dropped", "expired"):
            raise QuoteLost(f"the bid is {q['status']}", reason="dropped" if q["status"] == "dropped" else "expired",
                            details={"rfq_id": b.rfq_id, "quote_id": b.quote_id, "status": q["status"]})


def await_award(client: "Client", b: Bid, *, wait: float | None = None, poll: float = POLL_S) -> dict:
    """Read the round until it serves this seat a form, and return the form.

    ``wait`` defaults to the round's window end plus ``SIGN_GRACE_S``. Raises QuoteLost on the round's
    end, on the bid's end, and with reason ``timeout`` when ``wait`` ends first. The bid still rests then.
    """
    now = client._clock()
    if wait is None:
        wait = (b.closes_at - now + SIGN_GRACE_S) if b.closes_at is not None else WAIT_S
    end = now + max(float(wait), 0.0)
    n = 0
    while True:
        try:
            t = read_template(client, b.rfq_id)
            if t is not None:
                return t
            if n % STATUS_EVERY == 0:
                _ended(client, b)
        except TRANSIENT as e:
            log.info("takeover %s: %s; reading again", b.rfq_id, e.code)
        n += 1
        if client._clock() >= end:
            raise QuoteLost("no award before the wait ended", reason="timeout",
                            details={"rfq_id": b.rfq_id, "quote_id": b.quote_id})
        client._sleep(poll)


def decline(client: "Client", rfq_id: Any, quote_id: str) -> dict:
    """Decline this seat's award on the round when it is the award of ``quote_id``."""
    return client._gw.request("POST", f"/rfqs/{_rid(rfq_id)}/close/decline", body={"quote_id": quote_id})


def _decline_quietly(client: "Client", b: Bid) -> bool:
    try:
        decline(client, b.rfq_id, b.quote_id)
    except CrxError as e:
        log.warning("takeover %s: the decline failed (%s)", b.rfq_id, e.code)
        return False
    return True


def sign_award(client: "Client", rfq_id: Any, *, bid: Bid, max_notional: Any, template: dict | None = None,
               poll: float = POLL_S) -> Signed:
    """Check the award's failover form against ``bid`` and ``max_notional``, sign it and post the signature.

    A form that differs in one word, any other form and a signer failure are declined: RefusedToSign
    names the word and ``details['declined']``. ``template`` defaults to a read of the round.
    """
    client._need_seat()
    rid = _rid(rfq_id)
    if rid != bid.rfq_id:
        raise BadRequest("the bid is on another round")
    cap = _cap(max_notional)
    t = template if template is not None else read_template(client, rid)
    if t is None:
        raise QuoteLost("the round serves this seat no form", reason="another_maker", details={"rfq_id": rid})
    kind = clean(t.get("digest_kind"), 40) if isinstance(t, dict) else "unread"
    if isinstance(t, dict) and t.get("signed") is True and kind == FAILOVER:
        return Signed(rid, kind, clean(t.get("digest"), 66), _text(t.get("state")), already=True)
    c = client._chain_info()
    try:
        td, digest = check_failover(t, bid, chain_id=c["chain_id"], core=c["core"], sep=client._sep,
                                    max_notional_e6=cap, now=client._clock())
        sig = sign_typed(client._signer, td, digest, bid.seat)
    except (_Mismatch, RefusedToSign, ValueError, KeyError, TypeError) as e:
        word, why = ((e.word, e.why) if isinstance(e, _Mismatch) else ("signature", str(e))
                     if isinstance(e, RefusedToSign) else ("template", "cannot be read"))
        declined = _decline_quietly(client, bid)
        raise RefusedToSign(f"refused to sign the {kind} form: {word}: {why}; "
                            + ("the award is declined" if declined else "the decline failed"),
                            details={"rfq_id": rid, "word": word, "declined": declined}) from None
    for i in range(SIGN_TRIES):
        try:
            out = client._gw.request("POST", f"/rfqs/{rid}/close/sign", body={"sig": sig}, timeout=HTTP_TIMEOUT_S)
        except TRANSIENT as e:
            if i + 1 == SIGN_TRIES:
                raise
            log.info("takeover %s: the signature post failed (%s); sending it again", rid, e.code)
            client._sleep(poll)
            continue
        except CrxError as e:
            e.details = {**e.details, "declined": _decline_quietly(client, bid)}
            raise
        state = _text(out.get("state"))
        log.info("takeover %s: %s signed, state %s", rid, kind, state)
        return Signed(rid, kind, e7.h0x(digest), state)
    raise BadRequest("SIGN_TRIES is below 1")


def take_over(client: "Client", rfq_id: Any, price: Any, *, max_notional: Any, wait: float | None = None,
              poll: float = POLL_S) -> Signed:
    """Bid ``price`` on the close round, wait for the award, then check and sign its failover form.

    See ``bid``, ``await_award`` and ``sign_award``.
    """
    b = bid(client, rfq_id, price, max_notional=max_notional)
    t = await_award(client, b, wait=wait, poll=poll)
    return sign_award(client, b.rfq_id, bid=b, max_notional=max_notional, template=t, poll=poll)
