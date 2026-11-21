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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_account import Account
from eth_account.messages import SignableMessage, encode_defunct
from eth_account.typed_transactions import TypedTransaction
from eth_utils import is_address, keccak, to_checksum_address
from hexbytes import HexBytes

from . import _env
from ._chain import Rpc, RpcError, TxLog, send_tx
from ._guardian_config import CONFIG, PAIRS, ZERO
from ._keys import load_account
from .errors import (BadAnswer, BadRequest, ConfigError, CrxError, MarkUnavailable, NotGuardian, RefusedToSign,
                     SendUnknown, TxFailed, clean)

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
REASON_PAUSES_S = (5.0, 15.0)  # pauses between the 3 posts of one reason
REASON_LAG_CODES = ("not_a_cancel", "not_seen_yet")  # the portal's chain read has not reached the cancel yet
REASON_V1_CODES = ("bad_signature", "bad_proof")  # answers to a v2 reason text after which the v1 text goes
NEW_YORK = "America/New_York"
READ_PAUSES_S = (1.0, 3.0)  # pauses between the 3 tries of a chain read
RECEIPT_READS = 5  # reads of a mined transaction's receipt
REFERENCE_TIMEOUT_S = 5.0  # the portal and Hermes reads of a reference price
REFERENCE_KEEP_S = 15.0  # a pair's reference serves each call of the next 15 seconds
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
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
    """Return the v1 text the guardian wallet signs (EIP-191) for the reason of one cancel."""
    return "\n".join(["CRX-GUARDIAN-REASON", f"Mode: {mode}", f"Tx: {tx.lower()}", f"Feed: {feed_id.lower()}",
                      f"Expiry: {int(expiry)}", f"Reason: {reason}"])


def reason_message_v2(mode: str, tx: str, feed_id: str, expiry: int, reason: str, domain: str,
                      issued_at: str) -> str:
    """Return the v2 text the guardian wallet signs (EIP-191) for the reason of one cancel.

    Over the v1 text it names the price (``Pair: USD/BRL``, or ``Trade: 0x525e...a4fd`` for a trade price), its
    fixing or end time in New York time (``Fixes: 2026-11-21 14:00 ET``, ``Ends:`` for a trade price), the portal
    host (``Domain``) and the signing time (``Issued at``, ISO 8601 UTC). A final price or a steward mark has no
    time line; a feed outside this SDK's pairs has no ``Pair`` line.

    Raises:
        ValueError: The fixing or end time cannot be shown in New York time.
    """
    feed, expiry = feed_id.lower(), int(expiry)
    kind = kind_of(expiry)
    lines = ["CRX-GUARDIAN-REASON", f"Mode: {mode}"]
    if kind == "trade_price":
        lines.append(f"Trade: {feed[:6]}...{feed[-4:]}")
    else:
        row = next((r for r in PAIRS if feed in (r["feedId"].lower(), r["pairId"].lower())), None)
        if row is not None:
            lines.append(f"Pair: {row['pair']}")
    if kind == "fixing":
        lines.append(f"Fixes: {_new_york(expiry)} ET")
    elif kind == "trade_price":
        lines.append(f"Ends: {_new_york(expiry - TRADE_PRICE_TAG)} ET")
    lines += [f"Tx: {tx.lower()}", f"Feed: {feed}", f"Expiry: {expiry}", f"Reason: {reason}", f"Domain: {domain}",
              f"Issued at: {issued_at}"]
    return "\n".join(lines)


def _new_york(seconds: int) -> str:
    """``seconds`` as New York time, ``YYYY-MM-DD HH:MM``."""
    try:
        return datetime.fromtimestamp(seconds, ZoneInfo(NEW_YORK)).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError, ZoneInfoNotFoundError):
        raise ValueError(f"{seconds} has no New York time on this system") from None


def _issued_at(now: float) -> str:
    """``now`` as ISO 8601 UTC to the millisecond: ``2026-11-21T06:00:00.000Z``."""
    at = datetime.fromtimestamp(now, timezone.utc)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def _sign_text(account: Any, text: str) -> str:
    return "0x" + bytes(account.sign_message(encode_defunct(text=text)).signature).hex()


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
            ``Guardian.record_reason`` sends a reason that is not recorded again.
        reason_error: The portal's error code when the reason is not recorded, else None. ``already_recorded``
            comes with ``reason_recorded`` True. ``no_receipt``: the RPC did not serve the receipt again after the
            cancel was mined, and the reason was not sent. ``not_signed``: the ``account=`` signer did not sign the
            reason, and the reason was not sent.
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
        account: A custody or hardware signer in place of a key: an object with ``address``,
            ``sign_transaction(dict)`` and ``sign_message(SignableMessage)``, as an eth_account ``LocalAccount``.
            The SDK checks each signature it returns before it sends. Set one of ``key``, ``key_file`` and
            ``account``.
        test: True for the Test venue, False (default) for Live. Any other value raises ``ConfigError``. The Test
            venue's addresses are pinned in this SDK; its RPC is a public node of the test network unless
            ``rpc_url`` names another.
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
        account: Any = None,
        test: bool = False,
        rpc_url: str | None = None,
        portal_url: str | None = None,
        hermes_url: str | None = None,
        state_dir: str | Path | None = None,
        session: requests.Session | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._account = None
        try:
            if not isinstance(test, bool):
                raise ConfigError("test must be True or False.")
            self.mode = "test" if test else "live"
            env = _env.load()
            cfg = dict(CONFIG[self.mode])
            words = "Test venue" if test else "Live"
            for name in ("core", "lens") + (("roles",) if cfg.get("roles") is not None else ()):
                if str(cfg.get(name) or ZERO).lower() == ZERO:
                    raise ConfigError(f"This SDK version holds no {words} address; update the SDK.")
            env_rpc = None if test else env.get("CRX_RPC")
            if env_rpc and ("<" in env_rpc or ">" in env_rpc):
                env_rpc = None
            rpc = rpc_url or env_rpc or cfg.get("rpc")
            if not rpc:
                raise ConfigError("Live needs rpc_url= or CRX_RPC.")
            portal = (portal_url or env.get("CRX_SAFE_URL") or DEFAULT_PORTAL).rstrip("/")
            u = urlsplit(portal)
            if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")):
                raise ConfigError("portal_url must be an https URL")
            if account is None:
                self._account = load_account(key, key_file, lambda _name: None)
            elif key is not None or key_file is not None:
                raise ConfigError("Set one of key, key_file and account.")
            else:
                self._account = _Signer(account)
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
        self._domain = (u.hostname or "").lower()
        self._hermes = (hermes_url or env.get("CRX_HERMES_URL") or "").rstrip("/") or None
        self._session = session or requests.Session()
        self._timeout = float(timeout)
        self._rpc = _ReadRpc(rpc, self._session, self._timeout, lambda s: self._sleep(s))
        state = state_dir or env.get("CRX_STATE_DIR")
        self._txlog = TxLog(Path(state or DEFAULT_STATE_DIR).expanduser() / "txs.json", own_dir=not state)
        self._clock: Callable[[], float] = time.time
        self._sleep: Callable[[float], None] = time.sleep
        self._chain_checked = False
        self._chunk = CHUNK[self.mode]
        self._kept: list[dict] = []  # parsed logs up to self._scanned
        self._scanned = self._start - 1
        self._block_times: dict[int, int] = {}
        self._kept_refs: dict[str, tuple[float, tuple[Decimal, str, datetime | None]]] = {}

    def __repr__(self) -> str:
        return f"crx.Guardian(mode={self.mode!r}, address={self.address!r})"

    def __getstate__(self) -> Any:
        raise TypeError("crx.Guardian can hold a key and cannot be pickled or copied")

    @property
    def address(self) -> str | None:
        """The guardian wallet's address in lower case, or None without a key or an account."""
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
        refs = self._references([r for r in (self._pair_row(i.feed, i.expiry) for i in keep) if r is not None])
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
        chain. It sends one transaction, waits for its receipt and checks the cancel record in it. Then it sends
        the reason with ``record_reason``. A reason that is not recorded does not undo the cancel:
        ``reason_recorded`` is False and ``reason_error`` holds the code.

        Raises:
            BadRequest: ``not_cancellable`` for a key the role does not cover; a reason outside 10 to 280
                characters on one line. Nothing was sent.
            NotGuardian: The wallet holds no guardian role in this mode, or Live takes no cancel yet.
            TxFailed: The price is no longer waiting, or the transaction failed.
            SendUnknown: The transaction was sent and has no receipt yet. Read ``proposals()`` before a new cancel.
            RefusedToSign: The ``account=`` signer did not sign the cancel, or signed another transaction or from
                another address. Nothing was sent.
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
        receipt = self._receipt(tx)
        if receipt is None:
            log.warning("cancel %s: the receipt is not readable; the reason is not sent", tx)
            return Cancel(tx=tx, reason_recorded=False, reason_error="no_receipt", proposal=proposal)
        if not self._has_cancel(receipt, feed, expiry):
            raise TxFailed(f"Transaction {tx} holds no cancel of this price.", details={"step": "cancel", "tx": tx})
        try:
            recorded, error = self.record_reason(tx, feed, expiry, text)
        except RefusedToSign:
            log.warning("cancel %s: the account did not sign the reason; the reason is not sent", tx)
            recorded, error = False, "not_signed"
        return Cancel(tx=tx, reason_recorded=recorded, reason_error=error, proposal=proposal)

    def record_reason(self, tx: str, feed_id: str, expiry: int, reason: str) -> tuple[bool, str | None]:
        """Send the reason of the cancel ``tx`` of (``feed_id``, ``expiry``) to CRX, signed by the guardian wallet.

        ``cancel()`` calls it once the cancel is mined. A reason that is not recorded can be sent again with the
        same arguments. The portal checks the cancel on the chain. A post that gets no answer, a 429 or 5xx, a
        ``not_a_cancel`` or ``not_seen_yet`` (the portal has not read the cancel yet), or a 2xx with no
        ``recorded`` answer goes again: 3 posts over about 20 seconds.

        The wallet signs the v2 text (``reason_message_v2``): it names the pair, the fixing time, the portal host and
        the signing time. A portal that answers ``bad_signature`` or ``bad_proof`` to it gets the v1 text
        (``reason_message``) once, at once.

        Returns:
            ``(recorded, error)``. ``recorded`` is True when CRX holds the reason. ``error`` is the portal's code,
            else None; ``already_recorded`` comes with True.

        Raises:
            BadRequest: ``tx`` or ``feed_id`` is not 32 bytes of hex, ``expiry`` names no price a guardian can
                cancel, or the reason is outside 10 to 280 characters on one line. Nothing was sent.
            ConfigError: The Guardian holds no key.
            RefusedToSign: The ``account=`` signer did not sign the reason, or its signature does not recover to
                its address. Nothing was sent.
        """
        expiry = check_cancellable(expiry)
        feed = "0x" + _word(str(feed_id), "feed_id").hex()
        tx = "0x" + _word(str(tx), "tx").hex()
        text = _reason(reason)
        account = self._need_key()
        v1 = {"mode": self.mode, "tx": tx, "feedId": feed, "expiry": str(expiry), "reason": text,
              "signer": account.address.lower()}
        v1_text = reason_message(self.mode, tx, feed, expiry, text)
        issued = _issued_at(self._clock())
        try:
            msg = reason_message_v2(self.mode, tx, feed, expiry, text, self._domain, issued)
        except ValueError:
            body, v2 = {**v1, "sig": _sign_text(account, v1_text)}, False
        else:
            body, v2 = {**v1, "sig": _sign_text(account, msg), "issuedAt": issued, "domain": self._domain}, True
        pauses = iter(REASON_PAUSES_S)
        while True:
            try:
                r = self._session.post(f"{self._portal}/api/v1/reasons", json=body, timeout=self._timeout,
                                       allow_redirects=False)
            except requests.RequestException:
                error, again = "network", True
            else:
                error, again = _reason_answer(r)
                if error is None or error == "already_recorded":
                    return True, error
                if v2 and error in REASON_V1_CODES:
                    log.info("cancel %s: the portal took no v2 reason text (%s); the v1 text goes", tx, error)
                    body, v2 = {**v1, "sig": _sign_text(account, v1_text)}, False
                    continue
            pause = next(pauses, None)
            if not again or pause is None:
                break
            self._sleep(pause)
        log.warning("cancel %s: the reason is not recorded (%s)", tx, error)
        return False, error

    def post_test_price(self, pair: str, off_bp: Any = 0, kind: str = "fixing") -> Proposal:
        """Test only: propose a price on the Test venue at the reference spot x (1 + off_bp / 10,000).

        ``kind`` is ``"fixing"`` (expiry: the current second), ``"final_price"`` or ``"steward_mark"``. The spot is
        the pair's reference, as ``proposals()`` reads it. The venue takes at most 5 open proposals from one wallet.
        Returns the new proposal, with the reference and ``diff_bp`` that ``proposals()`` gives it.
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
        ref = self._references([row]).get(row["pair"])
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
        receipt = self._receipt(tx)
        if receipt is None:
            raise SendUnknown(f"Transaction {tx} is mined and its receipt is not readable. Read proposals().",
                              details={"step": "test price", "tx": tx})
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

    def _receipt(self, tx: str) -> dict | None:
        """Return the receipt of a mined transaction, or None. A node behind the one that served the receipt can
        answer null; the read goes again, ``RECEIPT_READS`` reads in all."""
        for n in range(RECEIPT_READS):
            if n:
                self._sleep(self._rpc.poll_s)
            try:
                receipt = self._rpc("eth_getTransactionReceipt", tx)
            except (CrxError, RpcError):
                receipt = None
            if isinstance(receipt, dict):
                return receipt
        return None

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

    def _references(self, rows: list[dict]) -> dict[str, tuple[Decimal, str, datetime | None]]:
        """Return the reference of each pair in ``rows``: Pyth from Hermes when set, else the portal's reference,
        else the latest CRX print. A pair's reference serves each call of the next ``REFERENCE_KEEP_S`` seconds,
        so ``post_test_price`` and ``proposals`` compare with the same price."""
        now = self._clock()
        refs: dict[str, tuple[Decimal, str, datetime | None]] = {}
        need: dict[str, dict] = {}
        for row in rows:
            hit = self._kept_refs.get(row["pair"])
            if hit is not None and 0 <= now - hit[0] < REFERENCE_KEEP_S:
                refs[row["pair"]] = hit[1]
            else:
                need[row["pair"]] = row
        if not need:
            return refs
        got: dict[str, tuple[Decimal, str, datetime | None]] = {}
        if self._hermes:
            got.update(self._hermes_spots(list(need.values())))
        rest = [row for name, row in need.items() if name not in got]
        if rest:
            got.update(self._portal_refs(rest))
        for name, row in need.items():
            if name not in got:
                ref = self._latest_print(row["feedId"])
                if ref is not None:
                    got[name] = ref
        for name, ref in got.items():
            self._kept_refs[name] = (now, ref)
        return {**refs, **got}

    def _get_json(self, url: str, params: Any) -> Any:
        """Return the JSON of a 200 answer, else None."""
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

    def _portal_refs(self, rows: list[dict]) -> dict[str, tuple[Decimal, str, datetime | None]]:
        """Return the portal's reference of each pair: ``/api/v1/reference``, one read per pair. When a read gets
        no answer (a portal without that route), the pairs left come from the portal's ``/api/v1/proposals``."""
        out = {}
        answered = True
        for row in rows:
            body = self._get_json(f"{self._portal}/api/v1/reference", {"mode": self.mode, "pair": row["pair"]})
            if not isinstance(body, dict):
                answered = False
                continue
            ref = _portal_ref(body, body.get("base", _NO_BASE)) if body.get("pair") == row["pair"] else None
            if ref is not None:
                out[row["pair"]] = ref
        if not answered:
            names = {row["pair"] for row in rows}
            for name, ref in self._portal_spots().items():
                if name in names:
                    out.setdefault(name, ref)
        return out

    def _portal_spots(self) -> dict[str, tuple[Decimal, str, datetime | None]]:
        body = self._get_json(f"{self._portal}/api/v1/proposals", {"mode": self.mode})
        out = {}
        for row in body if isinstance(body, list) else []:
            try:
                ref = _portal_ref(row["reference"], row.get("diffBase", _NO_BASE))
            except (KeyError, TypeError, AttributeError):
                continue
            if isinstance(row.get("pair"), str) and ref is not None:
                out.setdefault(row["pair"], ref)
        return out


_NO_BASE = object()  # a portal answer that names no base: Pyth is the reference


def _tidy(d: Decimal) -> Decimal:
    """``d`` with no trailing zeros after the point: a JSON number to 15 digits reads 5.98059, not 5.980589999999999."""
    if not d.is_finite():
        return d
    return d.quantize(Decimal(1)) if d == d.to_integral_value() else d.normalize()


def _portal_ref(ref: Any, base: Any) -> tuple[Decimal, str, datetime | None] | None:
    """Return the price the portal compares a proposal with: the price its ``base`` names (``"pyth"`` or
    ``"crx"``), or Pyth when the answer names no base. None for a base of null or a price that is not above 0."""
    source = base if base in ("pyth", "crx") else "pyth" if base is _NO_BASE else None
    if source is None or not isinstance(ref, dict):
        return None
    value, at = ref.get(source), ref.get(source + "At")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        price = _tidy(Decimal(format(value, ".15g"))) if isinstance(value, float) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or price <= 0:
        return None
    when = _utc(int(at)) if isinstance(at, (int, float)) and not isinstance(at, bool) else None
    return price, source, when


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


def _reason_answer(r: requests.Response) -> tuple[str | None, bool]:
    """Read the portal's answer to a reason post: the code (None when recorded) and whether to post again."""
    try:
        body = r.json()
    except ValueError:
        body = None
    if 200 <= r.status_code < 300:
        if isinstance(body, dict) and body.get("recorded") is True:
            return None, False
        return "bad_answer", True
    err = body.get("error") if isinstance(body, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    if not (isinstance(code, str) and code) and isinstance(body, dict):
        code = body.get("code") or err
    error = clean(code, 40) if isinstance(code, str) and code else f"http_{r.status_code}"
    return error, r.status_code >= 500 or r.status_code == 429 or error in REASON_LAG_CODES


def _rate_limited(error: Any) -> bool:
    """True for a JSON-RPC error that refuses the call for the caller's rate."""
    if not isinstance(error, dict):
        return False
    words = str(error.get("message", "")).lower()
    return error.get("code") == 429 or "too many requests" in words or "rate limit" in words


class _ReadRpc(Rpc):
    """The Guardian's RPC. A read that the RPC refuses for the caller's rate, or answers with a 5xx or with no
    JSON, goes again after each pause of ``READ_PAUSES_S``. A send goes once."""

    def __init__(self, url: str, session: requests.Session, timeout: float, sleep: Callable[[float], None]) -> None:
        super().__init__(url, session, timeout)
        self._pause = sleep

    def __call__(self, method: str, *params: Any) -> Any:
        pauses = iter(() if method == "eth_sendRawTransaction" else READ_PAUSES_S)
        while True:
            try:
                return super().__call__(method, *params)
            except BadAnswer:
                pause = next(pauses, None)
                if pause is None:
                    raise
            except RpcError as e:
                pause = next(pauses, None) if _rate_limited(e.error) else None
                if pause is None:
                    raise
            self._pause(pause)


@dataclass(frozen=True)
class _SignedTx:
    raw_transaction: bytes
    hash: bytes


@dataclass(frozen=True)
class _SignedText:
    signature: bytes


class _Signer:
    """An ``account=`` signer in the shape of an eth_account ``LocalAccount``. Each signature it returns is checked
    before use: a transaction must be the one asked and recover to ``address``; a text signature must recover to
    ``address``. A signer that raises, or returns another signature, raises ``RefusedToSign``."""

    def __init__(self, account: Any) -> None:
        address = getattr(account, "address", None)
        signs = all(callable(getattr(account, name, None)) for name in ("sign_transaction", "sign_message"))
        if not (isinstance(address, str) and is_address(address) and signs):
            raise ConfigError("account needs an address, sign_transaction and sign_message.")
        self._inner = account
        self.address = to_checksum_address(address)

    def __repr__(self) -> str:
        return f"account({self.address})"

    def sign_transaction(self, tx: dict) -> _SignedTx:
        try:
            raw = _raw_of(self._inner.sign_transaction(dict(tx)))
        except Exception:  # noqa: BLE001 - the signer's own error; nothing is sent
            raw = None
        if raw is None:
            raise RefusedToSign("The account did not sign the transaction; nothing was sent.")
        if not _same_tx(raw, tx, self.address):
            raise RefusedToSign("The account signed another transaction or from another address; nothing was sent.")
        return _SignedTx(raw_transaction=raw, hash=keccak(raw))

    def sign_message(self, message: SignableMessage) -> _SignedText:
        try:
            out = self._inner.sign_message(message)
            sig = _low_s(_bytes_of(getattr(out, "signature", out)))
        except Exception:  # noqa: BLE001 - the signer's own error; nothing is sent
            sig = None
        if sig is None:
            raise RefusedToSign("The account did not sign the reason; nothing was sent.")
        try:
            ok = Account.recover_message(message, signature=sig) == self.address
        except Exception:  # noqa: BLE001 - a signature that does not recover
            ok = False
        if not ok:
            raise RefusedToSign("The account's signature does not recover to its address; nothing was sent.")
        return _SignedText(signature=sig)


def _bytes_of(value: Any) -> bytes | None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return bytes(HexBytes(value))
        except ValueError:
            return None
    return None


def _raw_of(signed: Any) -> bytes | None:
    """The raw bytes of a signed transaction: ``raw_transaction`` (eth_account 0.13), ``rawTransaction`` (older),
    or the bytes themselves."""
    for name in ("raw_transaction", "rawTransaction"):
        raw = getattr(signed, name, None)
        if raw is not None:
            return _bytes_of(raw)
    return _bytes_of(signed)


def _low_s(sig: bytes | None) -> bytes | None:
    """A 65-byte signature with ``v`` 27 or 28 and low ``s``, or None."""
    if sig is None or len(sig) != 65:
        return None
    r, s, v = sig[:32], int.from_bytes(sig[32:64], "big"), sig[64]
    v = v + 27 if v in (0, 1) else v
    if v not in (27, 28) or not 0 < s < SECP256K1_N:
        return None
    if s > SECP256K1_N // 2:
        s, v = SECP256K1_N - s, 55 - v
    return r + s.to_bytes(32, "big") + bytes([v])


def _same_tx(raw: bytes, want: dict, address: str) -> bool:
    """True when ``raw`` is ``want`` signed by ``address``."""
    try:
        if Account.recover_transaction(raw) != address:
            return False
        if raw[:1] and raw[0] < 0x80:
            got = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        else:
            import rlp  # eth_account's own encoder

            nonce, price, gas, to, value, data, v, _r, _s = rlp.decode(raw)
            v = int.from_bytes(v, "big")
            got = {"nonce": int.from_bytes(nonce, "big"), "gasPrice": int.from_bytes(price, "big"),
                   "gas": int.from_bytes(gas, "big"), "to": to, "value": int.from_bytes(value, "big"), "data": data,
                   "chainId": (v - 35) // 2 if v >= 35 else None}
    except Exception:  # noqa: BLE001 - bytes that are no signed transaction
        return False
    if bytes(got.get("to") or b"").hex() != str(want["to"]).lower().removeprefix("0x"):
        return False
    if bytes(got.get("data") or b"") != bytes(HexBytes(want.get("data") or "0x")):
        return False
    names = ("chainId", "nonce", "value", "gas", "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas")
    return all(got.get(n) == want.get(n) for n in names if n in want) and got.get("chainId") == want.get("chainId")
