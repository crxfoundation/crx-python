"""Guardian calls: read the manual prices that wait for their write, and cancel one with a reason.

Each manual price waits 2 hours from its proposal. From its ready time any caller can write it. Until then a
guardian wallet can cancel it. The cancel is one transaction from the guardian wallet; the reason goes to CRX
signed by the same wallet. ``Guardian(test=True)`` works on the Test venue; the default works on Live (chain 1).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

import requests
from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_account.messages import encode_defunct
from eth_utils import keccak, to_checksum_address

from . import _env
from ._chain import Rpc, RpcError, TxLog, send_tx
from ._guardian_config import CONFIG, PAIRS, ZERO
from ._keys import load_account
from .errors import BadAnswer, BadRequest, ConfigError, CrxError, MarkUnavailable, NotGuardian, TxFailed, clean

log = logging.getLogger("crx")

LEG_LIMIT = 1 << 40  # a leg's expiry second sits below it
TRADE_HOLD_TAG = 1 << 62  # 2^62 | E: the hold of a trade. A cancel there releases the hold.
TRADE_PRICE_TAG = 1 << 63  # 2^63 | E: the price of one trade
FINAL_FIX_MARK = (1 << 64) - 1  # the final price of a delisted pair
LISTED_FIX_MARK = (1 << 64) - 2  # the listed flag of a pair: no proposal
MARK_SLOT_A = (1 << 64) - 3  # the two steward mark slots
MARK_SLOT_B = (1 << 64) - 4
DELAY_S = 2 * 3600  # the wait from a proposal to its ready time
MARK_OPEN_S = 24 * 3600  # a steward mark stays open this long after its ready time
DEFAULT_PORTAL = "https://safe.crxfx.com"
DEFAULT_STATE_DIR = "~/.crx-quickstart"  # the Client's folder: one record of sent transactions per wallet
REASON_MIN, REASON_MAX = 10, 280
REASON_TRIES = 3  # posts of one reason when the portal does not answer or answers 5xx
REASON_PAUSE_S = 2.0
REFERENCE_TIMEOUT_S = 5.0  # the portal and Hermes reads of a reference price
CONFIRMS = {"live": 12, "test": 1}  # blocks under the head after which a log read is kept for the next call
CHUNK = {"live": 10_000, "test": 2_000}  # blocks per eth_getLogs call; halves on a refusal
MIN_CHUNK = 10
MAX_OFF_BP = 5_000
TEST_EXPO = -6

KINDS = {"fixing": "Fixing", "final_price": "Final price", "steward_mark": "Steward mark",
         "trade_price": "Trade price"}


def _topic(sig: str) -> str:
    return "0x" + keccak(text=sig).hex()


def _sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


PROPOSED = _topic("ManualPriceProposed(address,bytes32,uint64,int64,int32,int64[6],uint64,uint8)")
CANCELLED = _topic("ManualPriceCancelled(address,bytes32,uint64)")
WRITTEN = _topic("ManualPriceSet(address,bytes32,uint64,int64,int32,int64[6],uint8)")
FINAL_WRITTEN = _topic("FinalPriceSet(address,bytes32,bytes32,uint64,int64,int32,int64[6])")
CANCEL = _sel("cancelManualPrice(bytes32,uint64)")
PENDING = _sel("pendingManualPrice(bytes32,uint64)")
LATEST = _sel("latestPriceOf(bytes32)")
EXEC = _sel("execTransactionWithRole(address,uint256,bytes,uint8,bytes32,bool)")
PROPOSE_TEST = _sel("proposeTestPrice(bytes32,uint64,int64,int32)")

# Refusals of the guardian role contract, by selector.
ROLE_REFUSALS: dict[bytes, tuple[type[CrxError], str, str | None]] = {
    _sel("NotAuthorized(address)"): (NotGuardian, "This wallet is not a guardian.", None),
    _sel("NoMembership()"): (NotGuardian, "This wallet is not a guardian.", None),
    _sel("ConditionViolation(uint8,bytes32)"): (BadRequest, "The guardian role does not cover this price.",
                                                 "not_cancellable"),
    _sel("ModuleTransactionFailed()"): (TxFailed, "The price is no longer waiting.", None),
}


def kind_of(expiry: int) -> str | None:
    """Return the kind of a manual price from its ``expiry``, or None for a value no guardian can cancel."""
    if 0 <= expiry < LEG_LIMIT:
        return "fixing"
    if expiry == FINAL_FIX_MARK:
        return "final_price"
    if expiry in (MARK_SLOT_A, MARK_SLOT_B):
        return "steward_mark"
    if TRADE_PRICE_TAG <= expiry < TRADE_PRICE_TAG + LEG_LIMIT:
        return "trade_price"
    return None


def check_cancellable(expiry: Any) -> int:
    """Return ``expiry`` as an int when a guardian can cancel the price it names. Else raise ``BadRequest``."""
    if isinstance(expiry, bool) or not isinstance(expiry, int) or not 0 <= expiry < 1 << 64:
        raise BadRequest("The expiry is not a uint64; nothing was sent.", code="not_cancellable")
    if kind_of(expiry) is not None:
        return expiry
    if TRADE_HOLD_TAG <= expiry < TRADE_PRICE_TAG:
        words = "A trade hold release is not a price cancel"
    elif expiry == LISTED_FIX_MARK:
        words = "The listed mark of a pair is not a price"
    else:
        words = f"Expiry {expiry} names no price a guardian can cancel"
    raise BadRequest(f"{words}; nothing was sent.", code="not_cancellable", details={"expiry": expiry})


def _word(value: str, what: str) -> bytes:
    try:
        b = bytes.fromhex(value[2:] if value[:2] in ("0x", "0X") else value)
    except (TypeError, ValueError):
        b = b""
    if len(b) != 32:
        raise BadRequest(f"{what} is not 32 bytes of hex")
    return b


def cancel_call(core: str, role_key: str, feed_id: str, expiry: int) -> str:
    """Return the calldata of the guardian's cancel: ``execTransactionWithRole`` of the role contract with
    ``cancelManualPrice(feed_id, expiry)`` on ``core``, value 0, a plain call and revert on failure."""
    inner = CANCEL + encode(["bytes32", "uint64"], [_word(feed_id, "feed_id"), expiry])
    outer = encode(["address", "uint256", "bytes", "uint8", "bytes32", "bool"],
                   [to_checksum_address(core), 0, inner, 0, _word(role_key, "roleKey"), True])
    return "0x" + (EXEC + outer).hex()


def reason_message(mode: str, tx: str, feed_id: str, expiry: int, reason: str) -> str:
    """Return the text the guardian wallet signs (EIP-191) for the reason of one cancel."""
    return "\n".join(["CRX-GUARDIAN-REASON", f"Mode: {mode}", f"Tx: {tx.lower()}", f"Feed: {feed_id.lower()}",
                      f"Expiry: {int(expiry)}", f"Reason: {reason}"])


def _reason(reason: Any) -> str:
    text = reason.strip() if isinstance(reason, str) else ""
    if not REASON_MIN <= len(text) <= REASON_MAX or not text.isprintable():
        raise BadRequest(f"The reason needs {REASON_MIN} to {REASON_MAX} characters on one line; nothing was sent.")
    return text


def _scaled(value: int, expo: int) -> Decimal:
    return Decimal(value).scaleb(expo)


def _utc(seconds: int | None) -> datetime | None:
    try:
        return datetime.fromtimestamp(seconds, timezone.utc) if seconds else None
    except (OverflowError, OSError, ValueError):
        return None


def _bp(price: Decimal, ref: Decimal | None) -> Decimal | None:
    if ref is None or ref <= 0:
        return None
    return ((price - ref) / ref * 10_000).quantize(Decimal("0.1"))


@dataclass(frozen=True)
class Proposal:
    """One manual price and the wait before its write.

    Attributes:
        pair: The pair, such as ``"USD/BRL"``. None for a trade price, or for a feed outside this SDK's list.
        kind: ``"fixing"``, ``"final_price"``, ``"steward_mark"`` or ``"trade_price"``.
        feed_id: The first key of the price, 0x hex: the spot feed, the pair id of a final price, or the trade's
            terms id.
        expiry: The second key: the leg's expiry second, or the reserved value of the kind.
        price: The proposed price.
        reference: The spot this SDK compares the price with, or None. ``reference_source`` names it: ``"pyth"``
            for Pyth spot, ``"crx"`` for the latest CRX print on the chain. ``reference_at`` is its time, UTC.
        diff_bp: (price - reference) / reference x 10,000, in basis points to 0.1. None without a reference.
        proposed_at: UTC. ``ready_at``: UTC; from then on any caller can write the price.
        seconds_left: Seconds from the read to ``ready_at``; 0 once ready.
        proposer: The address that proposed the price. ``tx``: the proposal's transaction.
        mode: ``"live"`` or ``"test"``.
    """

    pair: str | None
    kind: str
    feed_id: str
    expiry: int
    price: Decimal
    expo: int
    reference: Decimal | None
    reference_source: str | None
    reference_at: datetime | None
    diff_bp: Decimal | None
    proposed_at: datetime | None
    ready_at: datetime | None
    seconds_left: int
    proposer: str
    tx: str
    mode: str

    @property
    def kind_text(self) -> str:
        """The kind as the portal shows it: Fixing, Final price, Steward mark or Trade price."""
        return KINDS.get(self.kind, self.kind)

    @property
    def ready(self) -> bool:
        """True once the wait has run: any caller can write the price."""
        return self.seconds_left == 0


@dataclass(frozen=True)
class Outcome:
    """What became of one proposal.

    Attributes:
        proposal: The proposal, with no reference price.
        outcome: ``"cancelled"``, ``"written"``, ``"replaced"`` (a later proposal on the same key took its place)
            or ``"pending"`` (no cancel and no write yet).
        by: The address that cancelled or wrote it, or None.
        outcome_tx: The transaction of the cancel or the write, or None.
        outcome_at: The time of the cancel or the write, UTC, or None.
    """

    proposal: Proposal
    outcome: str
    by: str | None
    outcome_tx: str | None
    outcome_at: datetime | None


@dataclass(frozen=True)
class Cancel:
    """The result of ``Guardian.cancel``.

    Attributes:
        tx: The cancel transaction. It is mined: the price is cancelled.
        reason_recorded: True when CRX holds the reason. A refusal of the reason leaves the cancel in force.
        reason_error: The portal's error code when the reason is not recorded, else None. ``already_recorded``
            comes with ``reason_recorded`` True.
        proposal: The proposal the cancel removed.
    """

    tx: str
    reason_recorded: bool
    reason_error: str | None
    proposal: Proposal

    def __str__(self) -> str:
        line = f"Cancelled. Transaction {self.tx}."
        if not self.reason_recorded:
            line += f" The reason is not recorded ({self.reason_error}); the cancel stands."
        return line


@dataclass
class _Item:
    """One proposal read from the logs, with the event that ended it."""

    feed: str
    expiry: int
    proposer: str
    price: int
    expo: int
    ready: int
    tx: str
    outcome: str | None = None
    by: str | None = None
    outcome_tx: str | None = None
    outcome_block: int | None = None


class Guardian:
    """A guardian wallet on CRX Live, or on the Test venue with ``test=True``.

    Args:
        key: The guardian wallet's private key, hex or 32 bytes. None: reads only. Never read from the environment.
        key_file: A file that holds the key, owner-only (as for ``Client``), in place of ``key``.
        test: True for the Test venue. Its addresses are pinned in this SDK; its RPC is a public Avalanche Fuji
            node unless ``rpc_url`` names another.
        rpc_url: The chain RPC. Live: required, or ``CRX_RPC``. The RPC must serve the mode's chain.
        portal_url: The guardian portal, default ``https://safe.crxfx.com``, or ``CRX_SAFE_URL``.
        hermes_url: A Pyth Hermes endpoint for Pyth spot, or ``CRX_HERMES_URL``. Without it, the reference price
            comes from the portal, else from the latest CRX print on the chain.
        state_dir: The folder of the local record of sent transactions, or ``CRX_STATE_DIR``.
    """

    def __init__(
        self,
        key: str | bytes | None = None,
        *,
        key_file: str | Path | None = None,
        test: bool = False,
        rpc_url: str | None = None,
        portal_url: str | None = None,
        hermes_url: str | None = None,
        state_dir: str | Path | None = None,
        session: requests.Session | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._account = None
        self.mode = "test" if test is True else "live"
        try:
            env = _env.load()
            cfg = dict(CONFIG[self.mode])
            words = "Test venue" if test is True else "Live"
            for name in ("core", "lens") + (("roles",) if cfg.get("roles") is not None else ()):
                if str(cfg.get(name) or ZERO).lower() == ZERO:
                    raise ConfigError(f"This SDK version holds no {words} address; update the SDK.")
            env_rpc = None if test is True else env.get("CRX_RPC")
            if env_rpc and ("<" in env_rpc or ">" in env_rpc):
                env_rpc = None
            rpc = rpc_url or env_rpc or cfg.get("rpc")
            if not rpc:
                raise ConfigError("Live needs rpc_url= or CRX_RPC.")
            portal = (portal_url or env.get("CRX_SAFE_URL") or DEFAULT_PORTAL).rstrip("/")
            u = urlsplit(portal)
            if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")):
                raise ConfigError("portal_url must be an https URL")
            self._account = load_account(key, key_file, lambda _name: None)
        finally:
            key = None
        self._cfg = cfg
        self.chain_id = int(cfg["chainId"])
        self._core = to_checksum_address(cfg["core"])
        self._lens = to_checksum_address(cfg["lens"])
        self._print_lens = to_checksum_address(cfg["printLens"]) if cfg.get("printLens") else None
        self._roles = to_checksum_address(cfg["roles"]) if cfg.get("roles") else None
        self._role_key = cfg["roleKey"]
        self._start = int(cfg.get("startBlock") or 0)
        self._portal = portal
        self._hermes = (hermes_url or env.get("CRX_HERMES_URL") or "").rstrip("/") or None
        self._session = session or requests.Session()
        self._timeout = float(timeout)
        self._rpc = Rpc(rpc, self._session, self._timeout)
        state = state_dir or env.get("CRX_STATE_DIR")
        self._txlog = TxLog(Path(state or DEFAULT_STATE_DIR).expanduser() / "txs.json", own_dir=not state)
        self._clock: Callable[[], float] = time.time
        self._sleep: Callable[[float], None] = time.sleep
        self._chain_checked = False
        self._chunk = CHUNK[self.mode]
        self._kept: list[dict] = []  # parsed logs up to self._scanned
        self._scanned = self._start - 1
        self._block_times: dict[int, int] = {}

    def __repr__(self) -> str:
        return f"crx.Guardian(mode={self.mode!r}, address={self.address!r})"

    def __getstate__(self) -> Any:
        raise TypeError("crx.Guardian can hold a key and cannot be pickled or copied")

    @property
    def address(self) -> str | None:
        """The guardian wallet's address in lower case, or None without a key."""
        return self._account.address.lower() if self._account is not None else None

    # Reads

    def proposals(self) -> list[Proposal]:
        """Return each manual price that waits for its write, oldest ready time first.

        The SDK reads the proposals from the chain logs and keeps each one the chain still holds with the same
        ready time. A steward mark past its 24 hours of validity is left out.
        """
        now = int(self._clock())
        keep = []
        for it in self._items():
            if it.outcome is not None:
                continue
            ready = self._pending(it.feed, it.expiry)
            if ready is None or ready != it.ready:
                continue
            if kind_of(it.expiry) == "steward_mark" and now > it.ready + MARK_OPEN_S:
                continue
            keep.append(it)
        refs = self._references(keep)
        out = []
        for it in keep:
            row = self._pair_row(it.feed, it.expiry)
            out.append(self._proposal(it, refs.get(row["pair"]) if row else None, now))
        return sorted(out, key=lambda p: (p.ready_at or datetime.min.replace(tzinfo=timezone.utc), p.tx))

    def history(self, days: int = 30, pair: str | None = None) -> list[Outcome]:
        """Return each proposal of the last ``days`` days with its outcome, newest first. ``pair`` filters."""
        if isinstance(days, bool) or not isinstance(days, (int, float)) or days <= 0:
            raise BadRequest("days must be above zero")
        row = self._row_named(pair) if pair is not None else None
        now = int(self._clock())
        out = []
        for it in self._items():
            if it.ready - DELAY_S < now - days * 86_400:
                continue
            p = self._proposal(it, None, now)
            if row is not None and p.pair != row["pair"]:
                continue
            at = self._block_time(it.outcome_block) if it.outcome_block is not None else None
            out.append(Outcome(p, it.outcome or "pending", it.by, it.outcome_tx, _utc(at)))
        return sorted(out, key=lambda o: (o.proposal.proposed_at or datetime.min.replace(tzinfo=timezone.utc)),
                      reverse=True)

    def watch(self, every: float = 30.0) -> Iterator[Proposal]:
        """Yield each new proposal once, as ``proposals()`` finds it, reading every ``every`` seconds. No end."""
        if isinstance(every, bool) or not isinstance(every, (int, float)) or every < 1:
            raise BadRequest("every is 1 second or more")
        seen: set[tuple[str, int, str]] = set()
        while True:
            for p in self.proposals():
                k = (p.feed_id, p.expiry, p.tx)
                if k not in seen:
                    seen.add(k)
                    yield p
            self._sleep(float(every))

    # Writes

    def cancel(self, proposal: Proposal, reason: str) -> Cancel:
        """Cancel ``proposal`` and send ``reason`` to CRX, signed by the guardian wallet.

        The call refuses a trade hold release and any key the guardian role does not cover before it reads the
        chain. It sends one transaction, waits for its receipt and checks the cancel record in it. Then it signs
        the reason and posts it to the portal. A refused reason does not undo the cancel: ``reason_recorded`` is
        False and ``reason_error`` holds the portal's code.

        Raises:
            BadRequest: ``not_cancellable`` for a key the role does not cover; a reason outside 10 to 280
                characters on one line. Nothing was sent.
            NotGuardian: The wallet holds no guardian role in this mode, or Live takes no cancel yet.
            TxFailed: The price is no longer waiting, or the transaction failed.
            SendUnknown: The transaction was sent and has no receipt yet. Read ``proposals()`` before a new cancel.
        """
        expiry = check_cancellable(getattr(proposal, "expiry", None))
        feed = "0x" + _word(str(getattr(proposal, "feed_id", "")), "feed_id").hex()
        if getattr(proposal, "mode", self.mode) != self.mode:
            raise BadRequest(f"The proposal is a {proposal.mode} price; this guardian works in {self.mode}.")
        text = _reason(reason)
        account = self._need_key()
        if self._roles is None:
            raise NotGuardian("Live cancel is not open for this wallet.")
        self._check_chain()
        data = cancel_call(self._core, self._role_key, feed, expiry)
        self._simulate(account.address, self._roles, data)
        tx = send_tx(self._rpc, account, self.chain_id, "cancel", self._roles, data, sleep=self._sleep,
                     log=self._txlog)
        receipt = self._rpc("eth_getTransactionReceipt", tx)
        if not self._has_cancel(receipt, feed, expiry):
            raise TxFailed(f"Transaction {tx} holds no cancel of this price.", details={"step": "cancel", "tx": tx})
        recorded, error = self._post_reason(account, tx, feed, expiry, text)
        return Cancel(tx=tx, reason_recorded=recorded, reason_error=error, proposal=proposal)

    def post_test_price(self, pair: str, off_bp: Any = 0, kind: str = "fixing") -> Proposal:
        """Test only: propose a price on the Test venue at the reference spot x (1 + off_bp / 10,000).

        ``kind`` is ``"fixing"`` (expiry: the current second), ``"final_price"`` or ``"steward_mark"``. The spot is
        Pyth when ``hermes_url`` is set, else the latest CRX print. The venue takes at most 5 open proposals from
        one wallet. Returns the new proposal.
        """
        if self.mode != "test":
            raise BadRequest("post_test_price runs in Test mode only; nothing was sent.", code="test_only")
        row = self._row_named(pair)
        try:
            off = Decimal(str(off_bp))
        except (InvalidOperation, ValueError):
            off = Decimal("NaN")
        if isinstance(off_bp, bool) or not off.is_finite() or abs(off) > MAX_OFF_BP:
            raise BadRequest(f"off_bp is a number from -{MAX_OFF_BP} to {MAX_OFF_BP}")
        if kind not in ("fixing", "final_price", "steward_mark"):
            raise BadRequest("kind is fixing, final_price or steward_mark")
        account = self._need_key()
        self._check_chain()
        ref = self._spot(row)
        if ref is None:
            raise MarkUnavailable(f"No reference price is available for {row['pair']}; nothing was sent.")
        spot, source, at = ref
        price = int((spot * (1 + off / 10_000)).scaleb(-TEST_EXPO).to_integral_value())
        if not 0 < price < 1 << 63:
            raise BadRequest("The test price is out of range; nothing was sent.")
        now = int(self._clock())
        if kind == "fixing":
            feed, expiry = row["feedId"], now
        elif kind == "final_price":
            feed, expiry = row["pairId"], FINAL_FIX_MARK
        else:
            feed = row["feedId"]
            expiry = MARK_SLOT_A if self._pending(feed, MARK_SLOT_A) is None else MARK_SLOT_B
        data = "0x" + (PROPOSE_TEST + encode(["bytes32", "uint64", "int64", "int32"],
                                             [_word(feed, "feed_id"), expiry, price, TEST_EXPO])).hex()
        tx = send_tx(self._rpc, account, self.chain_id, "test price", self._core, data, sleep=self._sleep,
                     log=self._txlog)
        receipt = self._rpc("eth_getTransactionReceipt", tx)
        for raw in _logs_of(receipt):
            e = self._event(raw)
            if e and e["type"] == "proposed" and e["feed"] == feed.lower() and e["expiry"] == expiry:
                it = _Item(e["feed"], e["expiry"], e["who"], e["price"], e["expo"], e["ready"], e["tx"])
                return self._proposal(it, (spot, source, at), int(self._clock()))
        raise TxFailed(f"Transaction {tx} holds no test proposal.", details={"step": "test price", "tx": tx})

    # Chain reads

    def _need_key(self) -> Any:
        if self._account is None:
            raise ConfigError("This call needs the guardian key: pass key= or key_file=.")
        return self._account

    def _check_chain(self) -> None:
        if self._chain_checked:
            return
        got = self._rpc.int("eth_chainId")
        if got != self.chain_id:
            raise ConfigError(f"The RPC serves chain {got}; {self.mode.capitalize()} needs chain {self.chain_id}.")
        self._chain_checked = True

    def _call(self, to: str, data: bytes) -> bytes:
        out = self._rpc("eth_call", {"to": to, "data": "0x" + data.hex()}, "latest")
        try:
            return bytes.fromhex(out[2:]) if isinstance(out, str) and out[:2] == "0x" else b"\x00"
        except ValueError:
            return b"\x00"

    def _pending(self, feed: str, expiry: int) -> int | None:
        """Return the ready time of the open proposal at (feed, expiry), or None when the chain holds none."""
        raw = self._call(self._lens, PENDING + encode(["bytes32", "uint64"], [_word(feed, "feed_id"), expiry]))
        try:
            ready = decode(["address", "uint64", "uint8", "int64", "int32", "int64[6]"], raw)[1]
        except (DecodingError, OverflowError, ValueError):
            raise BadAnswer("The RPC sent a pendingManualPrice answer the SDK cannot read.") from None
        return ready or None

    def _latest_print(self, feed: str) -> tuple[Decimal, str, datetime | None] | None:
        if self._print_lens is None:
            return None
        try:
            raw = self._call(self._print_lens, LATEST + _word(feed, "feed_id"))
            (price, expo, bound), at = decode(["(int64,int32,bool)", "uint64"], raw)
        except (CrxError, RpcError, DecodingError, OverflowError, ValueError):
            return None
        return (_scaled(price, expo), "crx", _utc(at)) if bound and price > 0 else None

    def _simulate(self, sender: str, to: str, data: str) -> None:
        """Run the cancel as a call. A refusal of the role contract raises its own error; nothing is sent."""
        try:
            self._rpc("eth_call", {"from": sender, "to": to, "data": data}, "latest")
        except RpcError as e:
            hit = ROLE_REFUSALS.get(_revert_selector(e.error))
            if hit is None:
                return  # send_tx runs the same call and names the revert
            cls, words, code = hit
            raise cls(f"{words} Nothing was sent.", **({"code": code} if code else {})) from None

    def _has_cancel(self, receipt: Any, feed: str, expiry: int) -> bool:
        for raw in _logs_of(receipt):
            e = self._event(raw)
            if e and e["type"] == "cancelled" and e["feed"] == feed.lower() and e["expiry"] == expiry:
                return True
        return False

    def _event(self, raw: Any) -> dict | None:
        """Parse one core log; None for a log of another contract or event, or one the SDK cannot read."""
        try:
            if str(raw.get("address", "")).lower() != self._core.lower() or raw.get("removed"):
                return None
            t = [str(x).lower() for x in raw["topics"]]
            if len(t) != 4:
                return None
            data = bytes.fromhex(str(raw.get("data") or "0x")[2:])
            e = {"tx": str(raw["transactionHash"]).lower(), "block": int(raw["blockNumber"], 16),
                 "index": int(raw["logIndex"], 16), "who": "0x" + t[1][-40:], "feed": t[2]}
            if t[0] == FINAL_WRITTEN:
                e.update(type="written", feed=t[2], expiry=FINAL_FIX_MARK)
                return e
            e["expiry"] = int(t[3], 16)
            if e["expiry"] >= 1 << 64:
                return None
            if t[0] == PROPOSED:
                price, expo, _fwd, ready, _verdict = decode(["int64", "int32", "int64[6]", "uint64", "uint8"], data)
                e.update(type="proposed", price=price, expo=expo, ready=ready)
            elif t[0] == CANCELLED:
                e["type"] = "cancelled"
            elif t[0] == WRITTEN:
                e["type"] = "written"
            else:
                return None
            return e
        except (AttributeError, KeyError, TypeError, ValueError, DecodingError, OverflowError):
            return None

    def _scan(self, lo: int, hi: int) -> list[dict]:
        out: list[dict] = []
        at = lo
        while at <= hi:
            end = min(hi, at + self._chunk - 1)
            try:
                got = self._rpc("eth_getLogs", {"address": self._core, "fromBlock": hex(at), "toBlock": hex(end),
                                                "topics": [[PROPOSED, CANCELLED, WRITTEN, FINAL_WRITTEN]]})
            except RpcError as e:
                if self._chunk <= MIN_CHUNK:
                    raise BadAnswer(f"The RPC refused the log read: {clean(e.error, 160)}") from None
                self._chunk = max(MIN_CHUNK, self._chunk // 2)
                continue
            if not isinstance(got, list):
                raise BadAnswer("The RPC sent an eth_getLogs answer that is not a list.")
            out.extend(e for e in map(self._event, got) if e is not None)
            at = end + 1
        return out

    def _logs(self) -> list[dict]:
        """Return the core's price logs from the start block, in chain order. Logs deeper than the mode's
        confirmations are kept for the next call; the newer ones are read again each call."""
        self._check_chain()
        head = self._rpc.int("eth_blockNumber")
        safe = max(head - CONFIRMS[self.mode], self._start - 1)
        if safe > self._scanned:
            self._kept += self._scan(self._scanned + 1, safe)
            self._scanned = safe
        tail = self._scan(max(safe, self._scanned) + 1, head) if head > max(safe, self._scanned) else []
        return sorted(self._kept + tail, key=lambda e: (e["block"], e["index"]))

    def _items(self) -> list[_Item]:
        """Return each proposal in the logs, with the cancel, write or later proposal that ended it."""
        items: list[_Item] = []
        open_: dict[tuple[str, int], _Item] = {}
        for e in self._logs():
            k = (e["feed"], e["expiry"])
            if e["type"] == "proposed":
                if k in open_:
                    open_[k].outcome = "replaced"
                it = _Item(e["feed"], e["expiry"], e["who"], e["price"], e["expo"], e["ready"], e["tx"])
                open_[k] = it
                items.append(it)
                continue
            it = open_.pop(k, None)
            if it is not None:
                it.outcome, it.by, it.outcome_tx, it.outcome_block = e["type"], e["who"], e["tx"], e["block"]
        return items

    def _block_time(self, block: int) -> int | None:
        if block not in self._block_times:
            try:
                b = self._rpc("eth_getBlockByNumber", hex(block), False)
                self._block_times[block] = int(b["timestamp"], 16)
            except (CrxError, RpcError, KeyError, TypeError, ValueError):
                return None
        return self._block_times[block]

    # Pairs and reference prices

    def _pair_row(self, feed: str, expiry: int) -> dict | None:
        kind = kind_of(expiry)
        if kind == "trade_price" or kind is None:
            return None
        key = "pairId" if kind == "final_price" else "feedId"
        return next((r for r in PAIRS if r[key].lower() == feed.lower()), None)

    def _row_named(self, pair: Any) -> dict:
        name = str(pair).strip().upper() if isinstance(pair, str) else ""
        row = next((r for r in PAIRS if name in (r["pair"].upper(), r["code"].upper())), None)
        if row is None:
            raise BadRequest(f"Unknown pair {clean(pair, 20)!r}; pairs: {', '.join(r['pair'] for r in PAIRS)}.")
        return row

    def _proposal(self, it: _Item, ref: tuple[Decimal, str, datetime | None] | None, now: int) -> Proposal:
        row = self._pair_row(it.feed, it.expiry)
        price = _scaled(it.price, it.expo)
        value, source, at = ref if ref is not None else (None, None, None)
        return Proposal(
            pair=row["pair"] if row else None, kind=kind_of(it.expiry) or "unknown", feed_id=it.feed,
            expiry=it.expiry, price=price, expo=it.expo, reference=value, reference_source=source, reference_at=at,
            diff_bp=_bp(price, value), proposed_at=_utc(it.ready - DELAY_S), ready_at=_utc(it.ready),
            seconds_left=max(0, it.ready - now), proposer=it.proposer, tx=it.tx, mode=self.mode,
        )

    def _references(self, items: list[_Item]) -> dict[str, tuple[Decimal, str, datetime | None]]:
        """Return the reference spot of each pair the items name: Hermes when set, the portal, the CRX print."""
        rows = {r["pair"]: r for r in (self._pair_row(i.feed, i.expiry) for i in items) if r is not None}
        refs: dict[str, tuple[Decimal, str, datetime | None]] = {}
        if not rows:
            return refs
        if self._hermes:
            refs.update(self._hermes_spots(list(rows.values())))
        if len(refs) < len(rows):
            for name, ref in self._portal_spots().items():
                if name in rows and name not in refs:
                    refs[name] = ref
        for name, row in rows.items():
            if name not in refs:
                ref = self._latest_print(row["feedId"])
                if ref is not None:
                    refs[name] = ref
        return refs

    def _spot(self, row: dict) -> tuple[Decimal, str, datetime | None] | None:
        ref = self._hermes_spots([row]).get(row["pair"]) if self._hermes else None
        return ref or self._latest_print(row["feedId"])

    def _get_json(self, url: str, params: Any) -> Any:
        try:
            r = self._session.get(url, params=params, timeout=REFERENCE_TIMEOUT_S, allow_redirects=False)
            return r.json() if r.status_code == 200 else None
        except (requests.RequestException, ValueError):
            return None

    def _hermes_spots(self, rows: list[dict]) -> dict[str, tuple[Decimal, str, datetime | None]]:
        ids = {r["pythId"].lower().removeprefix("0x"): r["pair"] for r in rows if r.get("pythId")}
        if not ids:
            return {}
        body = self._get_json(f"{self._hermes}/v2/updates/price/latest",
                              [("ids[]", "0x" + i) for i in ids] + [("parsed", "true")])
        out = {}
        for f in (body.get("parsed") if isinstance(body, dict) else None) or []:
            try:
                name = ids.get(str(f["id"]).lower().removeprefix("0x"))
                p = f["price"]
                value = _scaled(int(p["price"]), int(p["expo"]))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
            if name and value > 0:
                out[name] = (value, "pyth", _utc(p.get("publish_time") if isinstance(p.get("publish_time"), int)
                                                 else None))
        return out

    def _portal_spots(self) -> dict[str, tuple[Decimal, str, datetime | None]]:
        body = self._get_json(f"{self._portal}/api/v1/proposals", {"mode": self.mode})
        out = {}
        for row in body if isinstance(body, list) else []:
            try:
                ref = row["reference"]
                value = Decimal(str(ref["pyth"]))
                at = ref.get("pythAt")
            except (KeyError, TypeError, InvalidOperation, ValueError, AttributeError):
                continue
            if isinstance(row.get("pair"), str) and value.is_finite() and value > 0:
                when = _utc(int(at)) if isinstance(at, (int, float)) and not isinstance(at, bool) else None
                out.setdefault(row["pair"], (value, "pyth", when))
        return out

    # Reason

    def _post_reason(self, account: Any, tx: str, feed: str, expiry: int, text: str) -> tuple[bool, str | None]:
        msg = reason_message(self.mode, tx, feed, expiry, text)
        sig = "0x" + bytes(account.sign_message(encode_defunct(text=msg)).signature).hex()
        body = {"mode": self.mode, "tx": tx.lower(), "feedId": feed.lower(), "expiry": str(expiry), "reason": text,
                "signer": account.address.lower(), "sig": sig}
        error = "network"
        for attempt in range(REASON_TRIES):
            if attempt:
                self._sleep(REASON_PAUSE_S)
            try:
                r = self._session.post(f"{self._portal}/api/v1/reasons", json=body, timeout=self._timeout,
                                       allow_redirects=False)
            except requests.RequestException:
                error = "network"
                continue
            if 200 <= r.status_code < 300:
                return True, None
            try:
                answer = r.json()
            except ValueError:
                answer = None
            code = answer.get("code") or answer.get("error") if isinstance(answer, dict) else None
            error = clean(code, 40) if isinstance(code, str) and code else f"http_{r.status_code}"
            if error == "already_recorded":
                return True, error
            if r.status_code < 500:
                break
        log.warning("cancel %s: the reason is not recorded (%s)", tx, error)
        return False, error


def _logs_of(receipt: Any) -> list:
    logs = receipt.get("logs") if isinstance(receipt, dict) else None
    return logs if isinstance(logs, list) else []


def _revert_selector(error: Any) -> bytes:
    data = error.get("data") if isinstance(error, dict) else None
    data = data.get("data") if isinstance(data, dict) else data
    try:
        return bytes.fromhex(data[2:10]) if isinstance(data, str) and len(data) >= 10 else b""
    except ValueError:
        return b""
