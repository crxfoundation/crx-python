"""Close rounds: close a trade with another seat, or take over another seat's trade.

The SDK signs only typed data it builds from the round's item, after it checks the item against the trade and
the quote.
"""

from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterator

from eth_utils import keccak

from . import _eip712 as e7
from ._maker import PAGE, TRANSIENT, _page, _plain, _rate, _slash, _word, read, rfq_of
from .errors import (
    BadAnswer, BadRequest, CrxError, NetworkError, NoQuotes, QuoteExpired, RefusedToSign, ServerError, clean,
)
from .models import Close, Rfq, TakeOver, dec, side_word
from .signer import sign_typed

if TYPE_CHECKING:
    from .client import Client

log = logging.getLogger("crx")

SIDE_TAG = b"CRX/side/v1"
OPEN_NONCE_TAG = b"crx/alloc-open-nonce/v1"
EXIT_WINDOW = 600  # seconds: the core refuses an exit deadline further past its block time
CLOCK_SLACK = 60  # seconds the gateway clock may run ahead of this one
HOUR = 3600  # a close-out item's deadline falls inside the hour of its round
BEST_EXEC_K, BEST_EXEC_FLOOR_BPS = 2, 100  # the close-out fee cap: K * ceil(gross * floor bps / 10_000)
ACCEPTANCE, CONSENT, FAILOVER = "allocation_acceptance", "allocation_consent", "failover_consent"
ENDED = ("expired", "cancelled")  # round statuses with no close
PICK_SLACK = 30  # seconds past a request's answer end that its pick may still arrive
ROUND_WINDOW = 120  # seconds an elective close round takes quotes and its accept
ELECTIVE_SIGN, FORCED_SIGN = 60, 5  # seconds a round's winner has to sign after its award


def close_quote_id(rfq_id: str, maker: str, rate_e6: int, client_quote_id: str) -> str:
    """Return a close quote's id: keccak256("crx/close-quote/v1" ‖ rfq id ‖ maker ‖ rate as 8 bytes ‖ client id)."""
    raw = b"crx/close-quote/v1" + _w("bytes32", rfq_id) + bytes.fromhex(maker[2:]) + rate_e6.to_bytes(8, "big")
    return e7.h0x(keccak(raw + client_quote_id.encode()))


def _refuse(why: str) -> RefusedToSign:
    return RefusedToSign(f"refused to sign: {why}")


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value) else None


def _words(item: Any, fields: Any) -> dict:
    """Return the item's words in JSON form, keyed by member name. Raise RefusedToSign on a word that does not read."""
    if not isinstance(item, dict):
        raise _refuse("the close template has no item")
    out = {}
    for name, kind in fields:
        try:
            e7.word_of(kind, item[_snake(name)])
        except (KeyError, ValueError):
            raise _refuse(f"the item's {_snake(name)} cannot be read") from None
        out[name] = item[_snake(name)]
    return out


def _w(kind: str, value: Any) -> Any:
    return e7.word_of(kind, value)


def side_id(incoming_c: Any) -> bytes:
    """Return the side id of a commitment: keccak256("CRX/side/v1" ‖ incomingC)."""
    return keccak(SIDE_TAG + _w("bytes32", incoming_c))


def open_nonce(old_id: Any, nonce: int) -> int:
    """Return the terms nonce of the leg a close seats: the first 8 bytes of
    keccak256("crx/alloc-open-nonce/v1" ‖ oldId ‖ nonce as 8 bytes), big endian."""
    h = keccak(OPEN_NONCE_TAG + _w("bytes32", old_id) + int(nonce).to_bytes(8, "big"))
    return int.from_bytes(h[:8], "big")


def gross_e6(notional_e6: int, entry_e6: int, usd_is_base: bool) -> int:
    """Return the leg's gross in USD at 6 decimals."""
    return notional_e6 if usd_is_base else notional_e6 * entry_e6 // 10**6


def fee_cap_e6(gross: int) -> int:
    """Return the largest close-out fee on a leg of ``gross``, USD at 6 decimals."""
    return BEST_EXEC_K * -(-gross * BEST_EXEC_FLOOR_BPS // 10_000)


def uncapped_fee(notional_e6: int, mark_e6: int, rate_e6: int, side: int, usd_is_base: bool) -> int:
    """Return the fee a close-out at ``rate_e6`` costs the closed-out seat against ``mark_e6``, USD at 6 decimals.

    ``side`` is the incoming seat's own side. A rate at or better than the mark for the closed-out seat costs 0.
    """
    d = mark_e6 - rate_e6 if side == 1 else rate_e6 - mark_e6 if side == -1 else 0
    div = mark_e6 if usd_is_base else 10**6
    return notional_e6 * d // div if d > 0 and div > 0 else 0


def forced_spread(notional_e6: int, mark_e6: int, rate_e6: int, side: int, usd_is_base: bool, entry_e6: int) -> int:
    """Return the fee of a close-out at ``rate_e6``, capped at 2 % of the leg's gross."""
    fee = uncapped_fee(notional_e6, mark_e6, rate_e6, side, usd_is_base)
    return min(fee, fee_cap_e6(gross_e6(notional_e6, entry_e6, usd_is_base)))


def _old_terms(old: Any) -> dict:
    """Return the old leg's terms as numbers. Raise RefusedToSign when they do not read."""
    if not isinstance(old, dict):
        raise _refuse("the close template has no old leg terms")
    try:
        return {
            "notional": _w("uint256", old["notional"]), "premium_bps": _w("int16", old["premium_bps"]),
            "nonce": _w("uint64", old["nonce"]), "pair": _w("bytes32", old["pair"]), "side": _w("int8", old["side"]),
            "settlement": _w("uint40", old["settlement"]), "rate": _w("uint64", old["rate"]),
        }
    except (KeyError, ValueError):
        raise _refuse("the old leg terms cannot be read") from None


def _terms_id(old: dict, taker_side: bytes, maker_side: bytes) -> bytes:
    return e7.terms_id({
        "takerSide": e7.h0x(taker_side), "makerSide": e7.h0x(maker_side), "notional": str(old["notional"]),
        "premiumBps": str(old["premium_bps"]), "nonce": str(old["nonce"]), "pair": e7.h0x(old["pair"]),
        "side": str(old["side"]), "settlement": str(old["settlement"]), "rate": str(old["rate"]),
    })


def check_leg(old: dict, old_id: Any, leaving: Any, remaining: Any, side: int) -> None:
    """Check that ``old_id`` is the old leg's terms id with the leaving and the remaining side ids.

    ``side`` is the leaving seat's own side. As taker it equals the terms' side; as maker it is the opposite.
    """
    want, out, stay = _w("bytes32", old_id), _w("bytes32", leaving), _w("bytes32", remaining)
    if want == _terms_id(old, out, stay):
        own = old["side"]
    elif want == _terms_id(old, stay, out):
        own = -old["side"]
    else:
        raise _refuse("the item's old_id is not the old leg's terms id")
    if own != side:
        raise _refuse("the old leg's side is not the side that moves")


def check_trade(old: dict, item_side: int, *, pair: str, side: int, notional_e6: int, settlement_ms: int) -> None:
    """Check the old leg against the trade the seat expects: pair, side, notional and settlement."""
    if old["pair"] != e7.pair_id(pair):
        raise _refuse(f"the old leg is not {pair}")
    if item_side != side:
        raise _refuse(f"the side that moves is not {side_word(side)}")
    if old["notional"] != notional_e6:
        raise _refuse("the old leg's notional is not the trade's")
    if old["settlement"] * 1000 != settlement_ms:
        raise _refuse("the old leg's settlement is not the trade's")


def check_deadline(deadline: int, now: float, longest: int) -> None:
    if not now < deadline <= now + longest + CLOCK_SLACK:
        raise _refuse("the item's deadline is not in its window")


def check_commitment(t: dict, fields: Any, words: dict, counterparty: bool = False) -> None:
    """Check the side id and the commitment of the item's hidden words and salt.

    On a close with the counterparty the incoming side is the remaining side.
    """
    if counterparty:
        if _w("bytes32", words["incomingSide"]) != _w("bytes32", words["remainingSide"]):
            raise _refuse("the item's incoming_side is not its remaining_side")
    else:
        try:
            own = side_id(t.get("incoming_c"))
        except ValueError:
            raise _refuse("the close template's incoming_c cannot be read") from None
        if _w("bytes32", words["incomingSide"]) != own:
            raise _refuse("the item's incoming_side is not the side id of incoming_c")
    try:
        salt = e7.h0x(_w("bytes32", t["item"]["salt"]))
    except (KeyError, ValueError):
        raise _refuse("the item's salt cannot be read") from None
    if salt == e7.h0x(bytes(32)):
        raise _refuse("the item's salt is zero")
    try:
        served = _w("bytes32", t.get("commitment"))
    except ValueError:
        raise _refuse("the close template's commitment cannot be read") from None
    if e7.hidden_commitment(fields, words, salt) != served:
        raise _refuse("the commitment is not the hash of the item and its salt")


def sign_form(c: "Client", t: dict, primary: str, message: dict) -> str:
    """Sign ``primary`` over ``message`` with the client's pinned domain, once the template's digest and typed data match it."""
    ch = c._chain_info()
    td = e7.typed_data(primary, ch["chain_id"], ch["core"], message)
    digest = e7.typed_digest(td)
    try:
        served = _w("bytes32", t.get("digest"))
    except ValueError:
        raise _refuse("the close template has no digest") from None
    if served != digest:
        raise _refuse(f"the served digest is not the digest of this {primary}")
    if t.get("typed_data") is not None:
        diff = e7.typed_mismatch(t["typed_data"], td)
        if diff is not None:
            raise _refuse(f"the served typed data differs at {diff}")
    return sign_typed(c._signer, td, digest, c.address)


def _hexes(words: dict, names: Any) -> dict:
    out = {}
    for name in names:
        v = words[name]
        out[name] = e7.h0x(_w("bytes32", v)) if isinstance(v, str) and v.startswith("0x") else str(_int(v))
    return out


def consent_message(words: dict, commitment: Any, salt: Any) -> dict:
    """Return the ``AllocationConsent`` message of an allocation item."""
    m = _hexes(words, ("oldId", "exitingSide", "remainingSide", "incomingSide", "nonce", "deadline"))
    return {**m, "commitment": e7.h0x(_w("bytes32", commitment)), "salt": e7.h0x(_w("bytes32", salt))}


def acceptance_message(words: dict, incoming_c: Any, commitment: Any) -> dict:
    """Return the ``AllocationAcceptance`` message of an allocation item."""
    m = _hexes(words, ("oldId", "incomingSide"))
    return {**m, "incomingC": e7.h0x(_w("bytes32", incoming_c)), **_hexes(words, ("nonce", "deadline")),
            "commitment": e7.h0x(_w("bytes32", commitment))}


def failover_message(words: dict, incoming_c: Any, commitment: Any) -> dict:
    """Return the ``FailoverConsent`` message of a close-out item."""
    m = _hexes(words, ("oldId", "closedOutSide", "remainingSide", "incomingSide"))
    return {**m, "incomingC": e7.h0x(_w("bytes32", incoming_c)), **_hexes(words, ("nonce", "openNonce", "deadline")),
            "commitment": e7.h0x(_w("bytes32", commitment))}


def check_allocation(t: Any, *, role: str, seat: str, rfq_id: str, rate_e6: int, pair: str, side: int,
                     notional_e6: int, settlement_ms: int, now: float, entry: tuple | None = None,
                     to: str | None = None) -> dict:
    """Check an allocation template for ``role`` and return its words.

    ``role`` is ``exiting``, ``incoming`` (the seat that takes the side over) or ``remaining`` (the
    counterparty that closes the trade with the exiting seat). ``side`` is the exiting seat's own side.
    ``entry`` is the seat's own (rate at 6 decimals, premium bps) of the trade; ``to`` the incoming seat
    the exiting seat named.
    """
    if not isinstance(t, dict):
        raise _refuse("the close template cannot be read")
    kind = ACCEPTANCE if role == "incoming" else CONSENT
    if t.get("role") != role or t.get("digest_kind") != kind:
        raise _refuse(f"the template is not the {role} seat's {kind}")
    path = t.get("path", "take_over")
    if path not in {"exiting": ("take_over", "close"), "remaining": ("close",)}.get(role, ("take_over",)):
        raise _refuse(f"the template's path is not the {role} seat's")
    if str(t.get("rfq_id", "")).lower() != rfq_id:
        raise _refuse("the template is for another round")
    w = _words(t.get("item"), e7.ALLOCATION_ITEM_FIELDS)
    incoming = _w("address", w["incoming"])
    if (incoming == seat) != (role != "exiting"):
        raise _refuse("the item's incoming seat is not the seat's role")
    if to is not None and (path == "close" or incoming != to):
        raise _refuse("the item's incoming seat is not the seat to= names")
    plain = path == "close"
    try:
        close_rate, spread = _w("uint128", w["closeRate"]), _w("uint128", w["spread"])
    except ValueError:
        raise _refuse("the item's close rate or spread is out of range") from None
    if close_rate != rate_e6:
        raise _refuse(f"the item's close rate is not {_plain(Decimal(rate_e6).scaleb(-6))}")
    if spread != 0:
        raise _refuse("the item carries a spread")
    leg = (_w("bytes32", w["pairId"]), _w("int8", w["side"]), _w("uint256", w["notional"]), _w("uint40", w["settlement"]))
    if plain:
        # A close with the counterparty names the trade through old_id alone: its leg words are zero.
        if leg != (bytes(32), 0, 0, 0):
            raise _refuse("the close with the counterparty carries leg words")
    else:
        if leg[0] != e7.pair_id(pair) or leg[2] != notional_e6:
            raise _refuse("the item's pair or notional is not the trade's")
        if leg[3] * 1000 != settlement_ms:
            raise _refuse("the item's settlement is not the trade's")
    old = _old_terms(t.get("old"))
    check_trade(old, side if plain else leg[1], pair=pair, side=side, notional_e6=notional_e6,
                settlement_ms=settlement_ms)
    if not plain and (old["settlement"] != leg[3] or old["notional"] != notional_e6):
        raise _refuse("the item and the old leg differ")
    if entry is not None and (old["rate"], old["premium_bps"]) != entry:
        raise _refuse("the old leg's rate or premium is not the trade's")
    check_leg(old, w["oldId"], w["exitingSide"], w["remainingSide"], side)
    check_deadline(_w("uint64", w["deadline"]), now, EXIT_WINDOW)
    check_commitment(t, e7.ALLOCATION_ITEM_FIELDS, w, counterparty=path == "close")
    return w


def check_failover(t: Any, *, seat: str, rfq_id: str, rate_e6: int, pair: str, side: int, notional_e6: int,
                   settlement_ms: int, now: float) -> dict:
    """Check a close-out template for the incoming seat and return its words.

    ``side`` is the incoming seat's own side. The fee must be the API's: the quoted rate against the mark, capped.
    """
    if not isinstance(t, dict):
        raise _refuse("the close template cannot be read")
    if t.get("role") != "incoming" or t.get("digest_kind") != FAILOVER:
        raise _refuse(f"the template is not the incoming seat's {FAILOVER}")
    if str(t.get("rfq_id", "")).lower() != rfq_id:
        raise _refuse("the template is for another round")
    w = _words(t.get("item"), e7.CLOSEOUT_ITEM_FIELDS)
    if _w("address", w["incoming"]) != seat:
        raise _refuse("the item's incoming seat is not this seat")
    p = t.get("pricing")
    try:
        mark, entry, quoted = (_w("uint64", p[k]) for k in ("mark", "entry_rate", "quote_rate"))
        n = _w("uint128", p["notional"])
        p_side, usd_is_base = _w("int8", p["side"]), p["usd_is_base"]
    except (KeyError, TypeError, ValueError):
        raise _refuse("the close-out pricing cannot be read") from None
    if quoted != rate_e6:
        raise _refuse(f"the priced rate is not {_plain(Decimal(rate_e6).scaleb(-6))}")
    if p_side != side or n != notional_e6 or usd_is_base is not (pair[:3] == "USD"):
        raise _refuse("the close-out pricing is not this trade's")
    old = _old_terms(t.get("old"))
    check_trade(old, p_side, pair=pair, side=side, notional_e6=notional_e6, settlement_ms=settlement_ms)
    if entry != old["rate"]:
        raise _refuse("the priced entry rate is not the old leg's")
    try:
        spread, subsidy = _w("uint128", w["spread"]), _w("uint128", w["subsidyMaxUsd"])
    except ValueError:
        raise _refuse("the item's fee is out of range") from None
    if spread != forced_spread(n, mark, quoted, side, usd_is_base, entry):
        raise _refuse("the item's fee is not the fee of the quoted rate")
    if forced_spread(n, mark, quoted, side, usd_is_base, entry) != uncapped_fee(n, mark, quoted, side, usd_is_base):
        raise _refuse("the fee of the quoted rate passes the close-out cap: the seat would enter worse than its rate")
    if not now - HOUR <= _w("uint64", w["closeTime"]) <= now + CLOCK_SLACK:
        raise _refuse("the item's close time is not this hour's fix")
    if subsidy != 0:
        raise _refuse("the close-out carries a term this SDK does not sign")
    nonce = _w("uint64", w["nonce"])
    if _w("uint64", w["openNonce"]) != open_nonce(w["oldId"], nonce):
        raise _refuse("the item's open_nonce is not the old leg's")
    check_leg(old, w["oldId"], w["closedOutSide"], w["remainingSide"], side)
    check_deadline(_w("uint64", w["deadline"]), now, HOUR)
    check_commitment(t, e7.CLOSEOUT_ITEM_FIELDS, w)
    return w


# ── the exiting seat ──────────────────────────────────────────────────────────


def _position(c: "Client", trade_id: str) -> dict:
    body = c._gw.request("GET", "/positions", query={"chain": c.chain_key})
    rows = body.get("positions")
    if not isinstance(rows, list):
        raise BadAnswer("/positions lists no positions")
    for p in rows:
        if isinstance(p, dict) and str(p.get("trade_id", "")).lower() == trade_id:
            return p
    raise BadRequest(f"trade {trade_id} is not an open position of this seat", details={"trade_id": trade_id})


def _post_once_more(c: "Client", path: str, body: dict) -> dict:
    """Post ``body``. After a network error, post the same body once more: its client id makes the repeat safe."""
    try:
        return c._gw.request("POST", path, body=body)
    except (NetworkError, ServerError) as e:
        log.info("POST %s: %s; sending the same body once more", path, e.code)
    c._sleep(1.0)
    return c._gw.request("POST", path, body=body)


def _post_quote(c: "Client", rfq_id: str, path: str, body: dict, quote_id: str, until: float) -> None:
    """Post a close quote or answer. After a transient error, post the same body again until ``until``: a
    repeat under the same client id answers the quote that rests. A refused repeat raises unless the round
    lists the quote."""
    first = True
    while True:
        try:
            q = c._gw.request("POST", path, body=body)
            break
        except TRANSIENT as e:
            _backoff(c, e, 1.0, f"POST {path}", until)
            if c._clock() >= until:
                log.info("POST %s: %s; the award decides whether the quote rests", path, e.code)
                return
            first = False
        except CrxError as e:
            try:
                view = None if first else _soft(c, 1.0, f"/rfqs/{rfq_id}", until)
            except CrxError:
                raise e from None
            rows = view.get("quotes") if isinstance(view, dict) and isinstance(view.get("quotes"), list) else []
            if any(isinstance(x, dict) and _word(x.get("quote_id")) == quote_id for x in rows):
                return
            raise e
    if _word(q.get("quote_id")) != quote_id:
        raise BadAnswer("the gateway named another quote id for this quote")


def _post_sign(c: "Client", rfq_id: str, sig: str, due: str, until: float) -> bool:
    """Post a signature. After a transient error, post it again while the round still waits for this seat's
    form (round status ``due``) and ``until`` has not passed: a repeat of the same signature is safe. A
    refusal raises. Return False when ``until`` passed with no answer."""
    path = f"/rfqs/{rfq_id}/close/sign"
    first = True
    while True:
        if not first and c._clock() >= until:
            log.info("POST %s: the form's window ended; the round's status decides", path)
            return False
        first = False
        try:
            c._gw.request("POST", path, body={"sig": sig})
            return True
        except TRANSIENT as e:
            _backoff(c, e, 1.0, f"POST {path}", until)
        view = _soft(c, 1.0, f"/rfqs/{rfq_id}", until)
        if view is not None and view.get("status") != due:
            return True


def _backoff(c: "Client", e: CrxError, poll: float, what: str, until: float | None = None) -> None:
    """Pause after a transient error: the ``Retry-After`` time when sent, else ``poll``, at most 300 s and
    never past ``until``."""
    after = getattr(e, "retry_after", None)
    if after is None:
        after = e.details.get("retry_after_secs")
    pause = min(max(float(after) if isinstance(after, (int, float)) and not isinstance(after, bool) else poll,
                    poll), 300.0)
    if until is not None:
        pause = max(min(pause, until - c._clock()), 0.0)
    log.info("%s: %s; reading again in %s s", what, e.code, pause)
    c._sleep(pause)


def _soft(c: "Client", poll: float, path: str, until: float | None = None) -> dict | None:
    """GET ``path`` for a poll loop. On a transient error, pause and return None."""
    try:
        return c._gw.request("GET", path)
    except TRANSIENT as e:
        _backoff(c, e, poll, f"GET {path}", until)
        return None


def _moved_on(c: "Client", rfq_id: str, end: float) -> bool:
    """Return True when one read shows the round past the winner's form: its signature landed."""
    view = _soft(c, 1.0, f"/rfqs/{rfq_id}", end) or {}
    return view.get("status") in ("consenting", "armed", "settled")


def _template(c: "Client", rfq_id: str, until: float) -> dict:
    """Read the seat's close template, through transient errors, until ``until``."""
    while True:
        try:
            return c._gw.request("GET", f"/rfqs/{rfq_id}/close")
        except TRANSIENT as e:
            if c._clock() >= until:
                raise
            _backoff(c, e, 0.5, f"GET /rfqs/{rfq_id}/close", until)


def _await_winner(c: "Client", rfq_id: str, end: float) -> dict:
    """Poll the round until the gateway names its winner. Raise NoQuotes when the round or the wait ends first."""
    from .client import _ended
    while True:
        view = _soft(c, 1.0, f"/rfqs/{rfq_id}", end)
        if view is not None and view.get("status") in ENDED:
            raise _ended(view, rfq_id)
        q = view.get("quote") if view is not None else None
        if isinstance(q, dict) and q.get("status") == "quoted" and q.get("quote_id"):
            return q
        if c._clock() >= end:
            raise NoQuotes("no quote before the wait ended: the round is still open", details={"rfq_id": rfq_id})
        if view is not None:
            c._sleep(1.0)


def _await_status(c: "Client", rfq_id: str, want: tuple, end: float, poll: float) -> dict:
    """Poll the round until its status is one of ``want``. Raise QuoteExpired when it ends first."""
    while True:
        view = _soft(c, poll, f"/rfqs/{rfq_id}", end)
        if view is not None and view.get("status") in want:
            return view
        if view is not None and view.get("status") in ENDED:
            raise QuoteExpired("the close round ended before its forms were signed",
                               details={"rfq_id": rfq_id, "status": clean(view.get("status"), 40)})
        if c._clock() >= end:
            raise QuoteExpired("the wait ended before the winner signed", details={"rfq_id": rfq_id})
        if view is not None:
            c._sleep(poll)


def _arm(c: "Client", rfq_id: str, end: float, poll: float) -> dict:
    """Poll the item's status until it is ``closed`` and final, or ``refused``, or until ``end``. A round
    that ended with nothing sent reads ``refused``; a close not final by ``end`` reads ``pending``."""
    last: dict = {}
    while True:
        a = _soft(c, poll, f"/rfqs/{rfq_id}/close/arm-tx", end)
        if a is not None:
            last = a
            if a.get("status") == "refused" or (a.get("status") == "closed" and a.get("final") is True):
                return a
            if a.get("status") is None:
                view = _soft(c, poll, f"/rfqs/{rfq_id}", end)
                if view is not None and view.get("status") in ENDED:
                    return dict(a, status="refused")
        if c._clock() >= end:
            return dict(last, status="pending") if last.get("status") == "closed" else last
        if a is not None:
            c._sleep(poll)


def _own_trade(pos: dict) -> tuple:
    """Return (pair, side, notional at 6 dp, settlement ms, (entry rate at 6 dp, premium bps)) of a /positions row."""
    try:
        pair, side = _slash(pos["pair"]), _w("int8", pos["side"])
        notional_e6, settlement_ms = e7.scaled6(pos["notional"]), int(pos["settlement"])
        entry = (e7.scaled6(pos["rate"]), _w("int16", pos.get("premium_bps", 0)))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise BadAnswer("/positions sent a row this SDK cannot read") from None
    return pair, side, notional_e6, settlement_ms, entry


def close_trade(c: "Client", trade_id: Any, to: Any, wait: float, poll: float = 1.0, limit: Any = None) -> Close:
    tid = _word(trade_id)
    if tid is None:
        raise BadRequest("trade_id is a 0x 32-byte hex word")
    limit_e6 = None if limit is None else e7.scaled6(_rate(limit))
    seat = c.address
    pos = _position(c, tid)
    pair, side, notional_e6, settlement_ms, entry = _own_trade(pos)
    if not e7.pair_text_ok(pair) or side not in (1, -1):
        raise BadAnswer("/positions sent a row this SDK cannot read")
    c._chain_info()
    end = c._clock() + wait
    cid = "close-" + uuid.uuid4().hex
    if to is None:
        opened = _post_once_more(c, "/rfqs", {"kind": "close", "position_id": tid, "client_rfq_id": cid})
    else:
        try:
            incoming = e7.address(to)
        except ValueError:
            raise BadRequest("to= is a 0x 20-byte address") from None
        if incoming == seat:
            raise BadRequest("to= names this seat; name the seat that takes the trade over")
        opened = _post_once_more(c, "/novations", {"trade_id": tid, "incoming": incoming, "client_novation_id": cid})
    rfq_id = _word(opened.get("rfq_id"))
    if rfq_id is None or str(opened.get("position_id", "")).lower() != tid:
        raise BadAnswer("the close round answer names another trade")
    q = _await_winner(c, rfq_id, end)
    rate = dec(q.get("rate"))
    if rate is None:
        raise BadAnswer("the winning quote has no rate")
    rate_e6 = e7.scaled6(rate)
    if limit_e6 is not None and (rate_e6 < limit_e6 if side == 1 else rate_e6 > limit_e6):
        raise RefusedToSign(f"refused to accept: the best close rate {_plain(rate)} is past the limit "
                            f"{_plain(Decimal(limit_e6).scaleb(-6))}; nothing accepted",
                            details={"rfq_id": rfq_id, "rate": _plain(rate)})
    try:
        c._gw.request("POST", f"/rfqs/{rfq_id}/accept", body={"quote_id": q["quote_id"]})
    except (NetworkError, ServerError) as e:
        log.info("close accept on rfq %s: %s; the round's status decides", rfq_id, e.code)
    _await_status(c, rfq_id, ("consenting", "armed"), end, poll)
    t = _template(c, rfq_id, end)
    if t.get("signed") is not True:
        if str(t.get("trade_id", "")).lower() != tid:
            raise _refuse("the template is for another trade")
        w = check_allocation(t, role="exiting", seat=seat, rfq_id=rfq_id, rate_e6=rate_e6, pair=pair, side=side,
                             notional_e6=notional_e6, settlement_ms=settlement_ms, now=c._clock(), entry=entry,
                             to=None if to is None else incoming)
        sig = sign_form(c, t, "AllocationConsent", consent_message(w, t["commitment"], t["item"]["salt"]))
        until = min(end, _w("uint64", w["deadline"]))
        if not _post_sign(c, rfq_id, sig, "consenting", until) and (
                (_soft(c, poll, f"/rfqs/{rfq_id}", end) or {}).get("status") not in ("armed", "settled")):
            return Close(trade_id=tid, rfq_id=rfq_id, to=str(t["item"].get("incoming", "")).lower(), rate=rate,
                         status="unsigned", item=None, tx=None, raw=t, counterparty=t.get("path") == "close")
    a = _arm(c, rfq_id, end, poll)
    item = t.get("item") if isinstance(t.get("item"), dict) else {}
    return Close(
        trade_id=tid, rfq_id=rfq_id, to=str(item.get("incoming", "")).lower(), rate=rate,
        status=clean(a.get("status") or "sending", 40), item=_word(a.get("item")), tx=_word(a.get("tx")), raw=a,
        final=a.get("final") is True, counterparty=t.get("path") == "close",
    )


# ── the incoming seat ─────────────────────────────────────────────────────────


def _tape(c: "Client", cursor: int) -> tuple[list, int]:
    """Read the tape after ``cursor``. A transient error after the first page returns the pages read."""
    rows, out = [], cursor
    while True:
        try:
            got, out = _page(c, out)
        except TRANSIENT:
            if out == cursor:
                raise
            return rows, out
        rows += [r for r in got if isinstance(r, dict)]
        if len(got) < PAGE:
            return rows, out


def _head(c: "Client", end: float) -> int:
    """Return the tape's head seq, read through transient errors."""
    cursor = 0
    while True:
        try:
            rows, after = _tape(c, cursor)
        except TRANSIENT as e:
            if c._clock() >= end:
                raise
            _backoff(c, e, 1.0, "close round tape", end)
            continue
        if after == cursor and not rows:
            return cursor
        cursor = after


def _of(row: dict, rfq_id: str, key: str = "rfq_id") -> bool:
    d = row.get("data")
    return isinstance(d, dict) and str(d.get(key, "")).lower() == rfq_id


def _await_award(c: "Client", rfq_id: str, quote_id: str, cursor: int, end: float, poll: float) -> tuple[dict, int]:
    """Read the tape until the round awards this quote. Raise QuoteExpired when the round ends or the wait ends.

    The award's data carries ``_at``: the award frame's time, unix seconds, else the time it was read.
    """
    while True:
        start = cursor
        try:
            rows, cursor = _tape(c, cursor)
        except TRANSIENT as e:
            if c._clock() >= end:
                raise QuoteExpired("the wait ended before the round picked a quote", details={"rfq_id": rfq_id}) from None
            _backoff(c, e, poll, "close round tape", end)
            continue
        for r in rows:
            if not _of(r, rfq_id):
                continue
            mine = quote_id in (str(r["data"].get("quote_id", "")).lower(), str(r["data"].get("leg_id", "")).lower())
            if r.get("type") == "rfq.awarded":
                if mine:
                    seq, ts = r.get("seq"), r.get("ts")
                    at = ts / 1000 if type(ts) is int and ts > 0 else c._clock()
                    return dict(r["data"], _at=min(at, c._clock())), (
                        seq if isinstance(seq, int) and not isinstance(seq, bool) and seq > start else start)
                raise QuoteExpired("the round awarded another quote", details={"rfq_id": rfq_id})
            if r.get("type") in ("rfq.expired", "rfq.cancelled") or (
                    r.get("type") in ("quote.declined", "quote.dropped") and mine):
                raise QuoteExpired("the round ended without this quote", details={"rfq_id": rfq_id})
        if c._clock() >= end:
            raise QuoteExpired("the wait ended before the round picked a quote", details={"rfq_id": rfq_id})
        c._sleep(poll)


def _decline(c: "Client", rfq_id: str, quote_id: str, digest: Any) -> None:
    body = {"quote_id": quote_id}
    if _word(digest) is not None:
        body["digest"] = _word(digest)
    try:
        c._gw.request("POST", f"/rfqs/{rfq_id}/close/decline", body=body)
    except CrxError as e:
        log.info("close decline on rfq %s: %s", rfq_id, e.code)


def _taken(c: "Client", rfq_id: str, cursor: int, elective: bool, end: float, poll: float,
           done: str = "open") -> tuple[str, str | None]:
    """Wait for the close to land. Return ``done``, ``refused``, or ``pending`` when ``end`` passes, and the
    trade id the tape names.

    ``done`` is ``open`` for the seat that takes the side over and ``closed`` for the counterparty. A close
    reads ``done`` once the tape shows it, or once its status is ``closed`` and final.
    """
    while True:
        try:
            rows, cursor = _tape(c, cursor)
        except TRANSIENT as e:
            if c._clock() >= end:
                return "pending", None
            _backoff(c, e, poll, "close round tape", end)
            continue
        for r in rows:
            moved = r.get("type") == "trade.novated" and done == "open" and _of(r, rfq_id, "close_rfq_id")
            ended = (r.get("type") == "trade.closed" and done == "closed" and _of(r, rfq_id)
                     and r["data"].get("ended") is True)
            if moved or ended:
                return done, _word(r["data"].get("trade_id"))
            if r.get("type") == "trade.closed_out" and _of(r, rfq_id, "close_rfq_id"):
                return "refused", None
        view = _soft(c, poll, f"/rfqs/{rfq_id}", end)
        if view is not None and view.get("status") in ENDED:
            return "refused", None
        if elective and view is not None and view.get("status") == "armed":
            a = _soft(c, poll, f"/rfqs/{rfq_id}/close/arm-tx", end) or {}
            if a.get("status") == "refused":
                return "refused", None
            if a.get("status") == "closed" and a.get("final") is True:
                return done, None
        if c._clock() >= end:
            return "pending", None
        c._sleep(poll)


def take_over(c: "Client", rfq: Any, rate: Any, wait: float, poll: float = 0.5,
              client_quote_id: str | None = None) -> TakeOver:
    r = rfq if isinstance(rfq, Rfq) else read(c, rfq)
    d = r.raw
    if r.kind != "close":
        raise BadRequest("take_over() takes a close RFQ: rfqs(kind=\"close\") yields them")
    if d.get("chain") not in (None, c.chain_key):
        raise BadRequest(f"the RFQ is on {clean(d.get('chain'), 40)}; this client signs for {c.chain_key}")
    if r.own:
        raise BadRequest("this is your own RFQ; a seat does not take over its own trade")
    trade_id = _word(d.get("position_id"))
    side = d.get("side")
    if isinstance(side, bool) or side not in (1, -1):
        raise BadAnswer("the close RFQ names no side")
    try:
        if not e7.pair_text_ok(r.pair) or r.notional is None or not isinstance(d.get("expiry"), int):
            raise ValueError
        pair, notional_e6, settlement_ms = r.pair, e7.scaled6(r.notional), d["expiry"]
    except (ValueError, ArithmeticError):
        raise BadAnswer("the close RFQ's pair, notional or settlement cannot be read") from None
    rate_d = _rate(rate)
    rate_e6 = e7.scaled6(rate_d)
    if client_quote_id is not None and not (isinstance(client_quote_id, str) and 1 <= len(client_quote_id) <= 128):
        raise BadRequest("client_quote_id is 1 to 128 printable characters")
    seat = c.address
    c._chain_info()
    end = c._clock() + wait
    if isinstance(r.seq, int) and r.seq > 0:
        cursor = r.seq
    else:
        cursor = _head(c, end)
    cid = client_quote_id or "take-" + uuid.uuid4().hex
    quote_id = close_quote_id(r.rfq_id, seat, rate_e6, cid)
    quote_by = min(end, r.closes_at.timestamp()) if r.closes_at is not None else end
    _post_quote(c, r.rfq_id, f"/rfqs/{r.rfq_id}/quotes", {"rate": _plain(rate_d), "client_quote_id": cid}, quote_id,
                quote_by)
    award, cursor = _await_award(c, r.rfq_id, quote_id, cursor, end, poll)
    kind = award.get("digest_kind")
    sign_by = min(end, award["_at"] + (FORCED_SIGN if kind == FAILOVER else ELECTIVE_SIGN))
    t = _template(c, r.rfq_id, sign_by)
    terms = dict(seat=seat, rfq_id=r.rfq_id, rate_e6=rate_e6, pair=pair, side=side, notional_e6=notional_e6,
                 settlement_ms=settlement_ms, now=c._clock())
    try:
        if kind == ACCEPTANCE:
            w = check_allocation(t, role="incoming", **terms)
            msg = acceptance_message(w, t["incoming_c"], t["commitment"])
            sig = sign_form(c, t, "AllocationAcceptance", msg)
        elif kind == FAILOVER:
            w = check_failover(t, **terms)
            sig = sign_form(c, t, "FailoverConsent", failover_message(w, t["incoming_c"], t["commitment"]))
        else:
            raise _refuse(f"the award names an unknown form ({clean(kind, 40)})")
    except RefusedToSign:
        _decline(c, r.rfq_id, quote_id, t.get("digest") if isinstance(t, dict) else None)
        raise
    if _post_sign(c, r.rfq_id, sig, "signing", sign_by) or _moved_on(c, r.rfq_id, end):
        status, moved = _taken(c, r.rfq_id, cursor, kind == ACCEPTANCE, end, max(poll, 2.0))
    else:
        status, moved = "unsigned", None
    return TakeOver(
        rfq_id=r.rfq_id, quote_id=quote_id, trade_id=moved or trade_id, pair=pair, side=side_word(side) or "",
        notional=r.notional, rate=rate_d, status=status, raw=t,
    )


# ── the counterparty ──────────────────────────────────────────────────────────


def _answer_by(row: dict) -> int | None:
    """Return the last ms an answer to a close request is taken: ``answer_by``, else the frame time plus
    the round's quote window."""
    d = row.get("data") if isinstance(row.get("data"), dict) else {}
    for v in (d.get("answer_by"), row.get("ts") + ROUND_WINDOW * 1000 if type(row.get("ts")) is int else None):
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    return None


def requests(c: "Client", after: int | None, wait: float | None, poll: float, stop: Any) -> Iterator[Rfq]:
    """Yield each live request to close a trade of this seat once, oldest first, until ``wait`` ends or ``stop`` is set."""
    cursor = int(after or 0)
    end = None if wait is None else c._clock() + max(float(wait), 0.0)
    seen: set[str] = set()
    while True:
        try:
            rows, cursor = _tape(c, cursor)
        except TRANSIENT as e:
            _backoff(c, e, poll, "close_requests", end)
            rows = []
        for row in rows:
            d = row.get("data")
            if row.get("type") != "trade.close_requested" or not isinstance(d, dict):
                continue
            ends = _answer_by(row)
            round_end = row["ts"] + ROUND_WINDOW * 1000 if type(row.get("ts")) is int else None
            r = rfq_of({**d, "kind": "close", "closes_at": ends, "round_ends_at": round_end}, row.get("seq"))
            live = ends is not None and ends > c._clock() * 1000
            if r is None or r.rfq_id in seen or not live or _word(d.get("trade_id")) is None:
                continue
            seen.add(r.rfq_id)
            yield r
        if (end is not None and c._clock() >= end) or (stop is not None and stop.is_set()):
            return
        c._sleep(poll)


def answer(c: "Client", request: Any, rate: Any, wait: float, poll: float = 0.5,
           client_quote_id: str | None = None) -> Close:
    if not isinstance(request, Rfq) or _word(request.raw.get("trade_id")) is None:
        raise BadRequest("answer_close() takes a request from close_requests()")
    d = request.raw
    tid, rfq_id = _word(d["trade_id"]), request.rfq_id
    pair, own, notional_e6, settlement_ms, entry = _own_trade(_position(c, tid))
    if own not in (1, -1) or request.pair != pair or request.notional is None or \
            e7.scaled6(request.notional) != notional_e6 or d.get("expiry") != settlement_ms:
        raise BadAnswer("the close request does not match the trade's pair, notional or settlement")
    rate_d = _rate(rate)
    rate_e6 = e7.scaled6(rate_d)
    if client_quote_id is not None and not (isinstance(client_quote_id, str) and 1 <= len(client_quote_id) <= 128):
        raise BadRequest("client_quote_id is 1 to 128 printable characters")
    seat = c.address
    c._chain_info()
    end = c._clock() + wait
    cid = client_quote_id or "answer-" + uuid.uuid4().hex
    quote_id = close_quote_id(rfq_id, seat, rate_e6, cid)
    answer_by = min(end, request.closes_at.timestamp()) if request.closes_at is not None else end
    _post_quote(c, rfq_id, f"/rfqs/{rfq_id}/close/answer", {"rate": _plain(rate_d), "client_quote_id": cid},
                quote_id, answer_by)
    cursor = request.seq if isinstance(request.seq, int) and request.seq > 0 else 0
    ends = d.get("round_ends_at")
    award_end = end if not isinstance(ends, int) else min(end, ends / 1000 + PICK_SLACK)
    award, cursor = _await_award(c, rfq_id, quote_id, cursor, award_end, poll)
    sign_by = min(end, award["_at"] + ELECTIVE_SIGN)
    t = _template(c, rfq_id, sign_by)
    try:
        if not isinstance(t, dict) or _word(t.get("trade_id")) != tid:
            raise _refuse("the template is for another trade")
        if award.get("digest_kind") != CONSENT:
            raise _refuse(f"the award names another form ({clean(award.get('digest_kind'), 40)})")
        w = check_allocation(t, role="remaining", seat=seat, rfq_id=rfq_id, rate_e6=rate_e6, pair=pair, side=-own,
                             notional_e6=notional_e6, settlement_ms=settlement_ms, now=c._clock(), entry=entry)
        sig = sign_form(c, t, "AllocationConsent", consent_message(w, t["commitment"], t["item"]["salt"]))
    except RefusedToSign:
        _decline(c, rfq_id, quote_id, t.get("digest") if isinstance(t, dict) else None)
        raise
    if not _post_sign(c, rfq_id, sig, "signing", sign_by) and not _moved_on(c, rfq_id, end):
        return Close(trade_id=tid, rfq_id=rfq_id, to=seat, rate=rate_d, status="unsigned", item=None, tx=None, raw=t,
                     counterparty=True)
    status, _ = _taken(c, rfq_id, cursor, True, end, max(poll, 2.0), done="closed")
    try:
        a = _soft(c, poll, f"/rfqs/{rfq_id}/close/arm-tx", end) or {}
    except CrxError:
        a = {}
    return Close(trade_id=tid, rfq_id=rfq_id, to=seat, rate=rate_d, status=status, item=_word(a.get("item")),
                 tx=_word(a.get("tx")), raw=t, final=status == "closed", counterparty=True)
