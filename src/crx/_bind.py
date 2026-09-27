"""The taker's bind: accept a quote, sign the Side, send the arm.

Every value the gateway serves is rebuilt locally before a signature exists.
A mismatch raises RefusedToSign and nothing is signed or sent.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak, to_checksum_address

from . import _eip712 as e7
from ._chain import Rpc, RpcError, revert_name
from ._http import Gateway
from .errors import (
    BadAnswer, CrxError, NetworkError, OwnRoundOpen, QuoteExpired, RefusedToSign,
    TradeUnknown, clean, from_gateway,
)

ARM_APPENDED = "0x" + keccak(text="ArmAppended(uint64,uint8,bytes32,bytes32,bytes)").hex()
KIND_OPEN_PAIR = "0x" + encode(["uint8"], [9]).hex()
ARM_OPEN_PAIR = e7.selector("armOpenPair(uint8,bytes)")
SIDE_WINDOW = 630  # s: the core takes a Side quote_expiry at most 600 s past its block time, plus 30 s of clock slack
NEW_QUOTE = "no bind; request a new quote"
UNCONFIRMED = "an arm for this pair landed but is not confirmed; do not trade again; read positions() after the next fold"


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S UTC")


def status_code(r: Any) -> tuple[int, str]:
    if r is None:
        return (0, "")
    body = Gateway.body_of(r)
    code = clean(body.get("code") or "", 64) if isinstance(body, dict) else ""
    return (r.status_code, code)


def obj(r: Any) -> dict:
    body = Gateway.body_of(r) if r is not None else None
    return body if isinstance(body, dict) else {}


def refused(r: Any) -> CrxError:
    """A bind refusal: a closed round asks for a new quote."""
    k = status_code(r)
    if k[0] == 410 or k in ((409, "rejected"), (409, "round_closed"), (409, "conflict")):
        return QuoteExpired(NEW_QUOTE, status=k[0], gateway_code=k[1] or None)
    return from_gateway(r.status_code, Gateway.body_of(r), r.text[:300] if r.text else "")


class Binder:
    def __getstate__(self) -> Any:
        raise TypeError("a Binder holds a key and cannot be pickled or copied")

    def __init__(
        self,
        gateway: Gateway,
        rpc: Rpc,
        account: Any,
        chain: dict,
        separator: bytes,
        state_dir: Path,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.gw = gateway
        self.rpc = rpc
        self.account = account
        self.seat = account.address.lower()
        self.chain = chain
        self.sep = separator
        self.state_path = Path(state_dir).expanduser() / f"side-nonce-{self.seat}"
        self.sleep = sleep
        self.now = clock

    # ---------- the taker's own half ----------

    def leg(self, quote: dict, leg_id: str, nonce: int, quote_expiry: int, im_bps: int) -> dict:
        """The joined fields ride on the quote row; leg_id, nonce and quote_expiry are this seat's own."""
        return {
            "seat": self.seat, "leg_id": leg_id, "join_ref": quote["join_ref"],
            "pair_id": quote["pair_id"], "instrument_id": quote.get("instrument_id", 1),
            "side": quote["side"], "notional": quote["notional"], "rate": quote["rate"],
            "im_bps": im_bps, "premium_bps": quote["premium_bps"], "expiry": quote["expiry"],
            "nonce": str(nonce), "quote_expiry": quote_expiry,
        }

    @staticmethod
    def check_terms(a: dict, want: dict) -> None:
        """Sign only the terms this request asked for."""
        try:
            got = {
                "pair_id": e7.hx(a["pair_id"]), "instrument_id": a["instrument_id"], "side": a["side"],
                "notional": Decimal(str(a["notional"])), "expiry": int(a["expiry"]) // 1000,
                "im_bps": a["im_bps"], "premium_bps": a["premium_bps"],
            }
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise RefusedToSign("refused to sign: the quote's terms cannot be read") from None
        bad = [k for k in want if got[k] != want[k]]
        if bad:
            raise RefusedToSign(f"refused to sign: the quote's {', '.join(bad)} is not this request's")

    def sign_leg(self, a: dict) -> str:
        try:
            digest = e7.leg_digest(self.sep, a)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise RefusedToSign("refused to sign: the quote's leg cannot be encoded") from None
        return "0x" + bytes(self.account.unsafe_sign_hash(digest).signature).hex()

    # ---------- the Side round ----------

    def last_signed(self) -> int:
        """The last own_nonce this seat signed from this machine; 0 before the first."""
        try:
            return int(self.state_path.read_text())
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            raise RefusedToSign("refused to sign: the Side nonce file is unreadable") from None

    def keep_signed(self, nonce: int) -> None:
        """Written before the signature exists: a temporary file, an fsync, a rename."""
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(tmp, "w") as f:
                f.write(str(nonce))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        except OSError:
            raise RefusedToSign("refused to sign: the Side nonce file is not writable") from None

    def check_side(self, t: dict, arm: dict) -> bytes:
        """Rebuild this seat's half with the gateway's nonce and the Side quote expiry."""
        try:
            if t["own_leg_id"].lower() != arm["leg_id"].lower():
                raise RefusedToSign("refused to sign: own_leg_id is not this leg")
            c_taker = e7.half_commitment(e7.arm_words(arm, t["own_nonce"], t["quote_expiry"]), t["own_salt"])
            if e7.h0x(c_taker) != t["c_taker"].lower():
                raise RefusedToSign("refused to sign: c_taker is not this leg")
            if e7.h0x(e7.pair_commitment(c_taker, e7.hx(t["c_maker"]))) != t["pair_c"].lower():
                raise RefusedToSign("refused to sign: pair_c is not keccak(0x03, c_taker, c_maker)")
            own_nonce, qe = int(t["own_nonce"]), int(t["quote_expiry"])
            now = self.now()
            if own_nonce > now * 1000 + 86_400_000:
                raise RefusedToSign("refused to sign: own_nonce is more than one day ahead")
            if own_nonce <= self.last_signed():
                raise RefusedToSign("refused to sign: own_nonce is not above the last one this seat signed")
            if qe <= now:
                raise QuoteExpired(f"the Side window has passed; {NEW_QUOTE}")
            if qe > now + SIDE_WINDOW:
                raise RefusedToSign(f"refused to sign: the Side quote_expiry is more than {SIDE_WINDOW} s ahead")
            digest = e7.side_digest(self.sep, t)
            if t.get("digest") and e7.h0x(digest) != str(t["digest"]).lower():
                raise RefusedToSign("refused to sign: the served digest is not this Side")
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            raise RefusedToSign("refused to sign: the Side template cannot be read") from None
        self.keep_signed(own_nonce)
        return digest

    # ---------- the arm tx ----------

    def carries(self, data: bytes, t: dict, sig: str) -> bytes | None:
        """armOpenPair(9, env) for this pair, whose taker half is this seat's own Side signature
        (r, s; v 27 or 28) and whose maker half is not. Returns those 130 bytes, or None."""
        try:
            kind, env = decode(["uint8", "bytes"], data[4:])
            pair_c, public, wraps_hash, wraps, sigs = decode(["bytes32", "bytes", "bytes32", "bytes[]", "bytes"], env)
            public = decode(["bytes32", "bytes32", "uint64"], public)
            (sigs,) = decode(["bytes"], sigs)
            own = e7.hx(sig)
            ok = (
                (data[:4], kind, pair_c, tuple(public), wraps_hash, keccak(encode(["bytes[]"], [list(wraps)])), len(sigs))
                == (ARM_OPEN_PAIR, 9, e7.hx(t["pair_c"]), (e7.hx(t["c_taker"]), e7.hx(t["c_maker"]), int(t["quote_expiry"])),
                    e7.hx(t["wraps_hash"]), e7.hx(t["wraps_hash"]), 130)
                and sigs[:64] == own[:64] and sigs[64] in (27, 28) and sigs[65:129] != own[:64]
            )
        except (DecodingError, OverflowError, KeyError, TypeError, ValueError):
            return None
        return sigs if ok else None

    def check_arm(self, tx: dict, t: dict, sig: str) -> dict:
        """Returns the tx to send, rebuilt from the checked bytes."""
        data, gas = b"", 0
        try:
            data, gas = e7.hx(tx["data"]), int(tx["gas"])
            try:
                kind, env = decode(["uint8", "bytes"], data[4:])
            except (DecodingError, OverflowError):
                kind, env = None, b""
            same = (tx["to"].lower(), int(tx["chain_id"]), data[:4], kind, env[:32]) == (
                self.chain["core"].lower(), int(self.chain["chain_id"]), ARM_OPEN_PAIR, 9, e7.hx(t["pair_c"]))
            same = same and 0 < gas <= 30_000_000
        except (KeyError, TypeError, ValueError, AttributeError):
            same = False
        if not same:
            raise RefusedToSign("refused to send: the arm tx is not this pair's armOpenPair")
        if not self.carries(data, t, sig):
            raise RefusedToSign("refused to send: the arm tx does not carry this seat's Side")
        return {"to": self.chain["core"], "data": e7.h0x(data), "gas": gas}

    def item_hashes(self, t: dict, sigs: bytes) -> set:
        """The core's itemHash for this arm, at each second the core takes it."""
        qe = int(t["quote_expiry"])
        return {
            keccak(encode(["uint8", "uint64", "bytes32", "bytes32", "uint64", "bytes32"],
                          [9, at, e7.hx(t["c_taker"]), e7.hx(t["c_maker"]), qe, e7.hx(t["wraps_hash"])]) + sigs)
            for at in range(qe - 600, qe)
        }

    def armed(self, h: Any, t: dict, sig: str, mine: set) -> dict | None:
        """The receipt of the gateway's `sent` tx, when it is mined with status 1 and is this seat's
        own arm, an armOpenPair to the core that carries this Side, or a wrapper whose input nests one
        that the core logged."""
        try:
            receipt = self.rpc("eth_getTransactionReceipt", h) if isinstance(h, str) else None
            if not receipt or receipt.get("status") != "0x1":
                return None
            if h.lower() in mine:
                return receipt
            tx = self.rpc("eth_getTransactionByHash", h) or {}
            core = self.chain["core"].lower()
            data = e7.hx(tx.get("input") or "0x")
            if (tx.get("to") or "").lower() == core and self.carries(data, t, sig):
                return receipt
            logged = {
                e7.hx(g["data"])[:32] for g in receipt.get("logs") or []
                if (g.get("address") or "").lower() == core
                and [x.lower() for x in g.get("topics") or []][0:3:2] == [ARM_APPENDED, KIND_OPEN_PAIR]
            }
            calls = (self.carries(data[i:], t, sig) for i in range(len(data) - 3) if data[i:i + 4] == ARM_OPEN_PAIR)
            return receipt if any(s and logged & self.item_hashes(t, s) for s in calls) else None
        except (RpcError, NetworkError, BadAnswer, AttributeError, KeyError, TypeError, ValueError):
            return None

    def pair_of(self, log: dict) -> bytes | None:
        # ArmAppended data = abi.encode(itemHash, head, summary); summary = abi.encode(cTaker, cMaker).
        try:
            return keccak(b"\x03" + decode(["bytes32", "bytes32", "bytes"], e7.hx(log["data"]))[2])
        except (DecodingError, KeyError, TypeError, ValueError):
            return None

    def logged(self, t: dict, since: int) -> tuple[list, bool] | None:
        """(the core's arm logs for this pair_c since the Side was signed, whether the latest block is
        30 s past the Side quote_expiry). None when the RPC does not answer."""
        try:
            head = self.rpc("eth_getBlockByNumber", "latest", False)
            logs = self.rpc("eth_getLogs", {
                "address": self.chain["core"], "fromBlock": hex(since), "toBlock": head["number"],
                "topics": [ARM_APPENDED, None, KIND_OPEN_PAIR]})
            arms = [g.get("transactionHash") for g in logs if self.pair_of(g) == e7.hx(t["pair_c"])]
            return arms, int(head["timestamp"], 16) >= int(t["quote_expiry"]) + 30
        except (RpcError, NetworkError, BadAnswer, AttributeError, KeyError, TypeError, ValueError):
            return None

    def send_arm(self, tx: dict) -> tuple[str | None, str | None]:
        """Simulate first: a revert sends nothing. Returns (tx hash, None) or (None, why)."""
        try:
            self.rpc("eth_call", {"from": self.seat, "to": tx["to"], "data": tx["data"]}, "latest")
        except RpcError as e:
            return None, f"armOpenPair would revert {revert_name(e.error)}"
        except (NetworkError, BadAnswer):
            return None, "armOpenPair not sent: the RPC did not answer"
        try:
            signed = self.account.sign_transaction({
                "chainId": int(self.chain["chain_id"]), "nonce": self.rpc.int("eth_getTransactionCount", self.seat, "pending"),
                "to": to_checksum_address(tx["to"]), "data": tx["data"], "value": 0, "gas": int(tx["gas"]),
                "gasPrice": self.rpc.int("eth_gasPrice")})
        except (RpcError, NetworkError, BadAnswer):
            return None, "armOpenPair not sent: the RPC did not answer"
        try:
            h = self.rpc("eth_sendRawTransaction", "0x" + bytes(signed.raw_transaction).hex())
        except RpcError as e:
            return None, f"armOpenPair not sent: {clean(e.error, 160)}"
        except (NetworkError, BadAnswer):
            h = "0x" + bytes(signed.hash).hex()
        return h, None

    def ask(self, method: str, path: str, body: Any = None) -> Any:
        """One gateway read; None when it does not answer."""
        try:
            return self.gw.raw_request(method, path, body=body)
        except NetworkError:
            return None

    def rpc_or_none(self, method: str, *params: Any) -> Any:
        try:
            return self.rpc(method, *params)
        except (RpcError, NetworkError, BadAnswer):
            return None

    def bind(self, rfq_id: str, arm: dict, t: dict, maker_by: float, log: Callable[[str], None]) -> str:
        """Sign this seat's Side, poll /arm-tx, and send the arm once it is ready.
        Returns the arm tx hash. Raises QuoteExpired (no bind) or TradeUnknown (maybe bound).
        Before the Side signature exists, a refusal is RefusedToSign. After it, the maker can
        still land the arm, so any other failure is TradeUnknown."""
        since = self.rpc.int("eth_blockNumber")  # no arm carries this signature before this block
        sig = "0x" + bytes(self.account.unsafe_sign_hash(self.check_side(t, arm)).signature).hex()
        why = None
        try:
            return self._after_sig(rfq_id, t, sig, since, maker_by, log)
        except (QuoteExpired, TradeUnknown):
            raise
        except CrxError as e:
            why = f"{e.code}: {e}"
        except Exception as e:  # noqa: BLE001 - the Side is signed: no failure may read as "nothing happened"
            why = type(e).__name__
        raise TradeUnknown(
            f"stopped after the Side was signed ({clean(why, 200)}); the maker may still land the arm; "
            "do not trade again; read positions() after the next fold", details={"rfq_id": rfq_id})

    def _after_sig(self, rfq_id: str, t: dict, sig: str, since: int, maker_by: float, log: Callable[[str], None]) -> str:
        qe, told, mine = int(t["quote_expiry"]), set(), set()
        r = self.ask("POST", f"/rfqs/{rfq_id}/side", {"sig": sig})  # 5xx or no answer: it may have landed
        if r is not None and r.status_code != 200 and r.status_code < 500:
            raise refused(r)
        log(f"side signed, nonce {clean(t['own_nonce'])}; the maker signs by {utc(maker_by)}")

        def say(key: Any, line: str) -> None:
            if key not in told:
                told.add(key)
                log(line)

        own, landed, sends, errs, seen = None, False, 0, 0, False  # seen: an arm for this pair is on chain
        while True:
            if own:
                receipt = self.rpc_or_none("eth_getTransactionReceipt", own)
                if receipt and receipt.get("status") == "0x1":
                    own, landed, seen = None, True, True
                elif receipt:
                    say(own, f"armOpenPair tx {own} reverted")
                    own = None
            r = self.ask("GET", f"/rfqs/{rfq_id}/arm-tx")
            k = status_code(r)
            a = obj(r) if k[0] == 200 else {}
            live = a.get("status") in ("sent", "ready") or k in (
                (409, "conflict"), (409, "rejected"), (422, "insufficient_collateral"))
            shut = k[0] == 410 or k == (409, "round_closed")
            errs = 0 if live or shut else min(errs + 1, 4)
            if a.get("status") == "sent":
                h = a.get("sent")
                if self.armed(h, t, sig, mine):
                    return str(h)
                say("sent", f"/arm-tx says sent: tx {clean(h)} is not this pair's mined armOpenPair")
            elif a.get("status") == "ready":
                if own is None and not landed and sends < 3:
                    checked = self.check_arm(a.get("tx") or {}, t, sig)
                    own, why = self.send_arm(checked)
                    if own:
                        sends, mine = sends + 1, mine | {str(own).lower()}
                        log(f"armOpenPair sent {own}")
                    if why:
                        say(why, f"{why}; nothing sent")
                        seen = seen or why.endswith("DuplicateArm")
            elif k in ((422, "insufficient_collateral"), (409, "rejected")):
                say(k, f"{k[0]} {k[1]}; the round stays open until {utc(qe)}")
            elif not shut and k != (409, "conflict"):
                say(k, f"{k[0] or 'no answer'} {k[1]}; polling")
            end = self.now() > qe + 90
            if not live or end:  # the gateway closed the round or cannot say: the chain's word
                word = self.logged(t, since)
                seen = seen or bool(word and word[0])
                if word and word[1] and not seen and not live:
                    raise QuoteExpired(NEW_QUOTE)
                if seen and (shut or end):
                    raise TradeUnknown(UNCONFIRMED, details={"rfq_id": rfq_id})
            if end:
                raise TradeUnknown(
                    f"no verdict from the RPC by {utc(qe + 90)}; do not trade again; read positions() after the next fold",
                    details={"rfq_id": rfq_id})
            self.sleep(2 if own else min(3 << errs, 30))

    def accept(self, rfq_id: str, expires_at_ms: Any, body: dict) -> tuple[dict, float]:
        """409 rejected can be a busy maker: retry until the quote expires (120 s at most).
        The reject code is opaque; when the retries end, the round is over."""
        until = min(self.now() + 120, (expires_at_ms if isinstance(expires_at_ms, int) else 10**13) / 1000)
        while True:
            r = self.gw.raw_request("POST", f"/rfqs/{rfq_id}/accept", body=body)
            k = status_code(r)
            if k == (409, "own_round_open"):
                d = obj(r).get("details") or {}
                raise OwnRoundOpen(
                    "your previous round is still open; no new bind before it ends",
                    status=409, gateway_code="own_round_open", details=d if isinstance(d, dict) else {})
            if k == (409, "rejected") and self.now() + 3 < until:
                self.sleep(3)
                continue
            if r.status_code == 200:
                out = obj(r)
                if not out:
                    raise BadAnswer("the gateway sent an accept answer that is not an object")
                return out, self.now()
            if k == (409, "rejected"):
                d = obj(r).get("details")
                raise QuoteExpired(f"the maker refused the accept; {NEW_QUOTE}", status=409, gateway_code="rejected",
                                   details=d if isinstance(d, dict) else {})
            raise refused(r)
