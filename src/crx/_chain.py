"""A small JSON-RPC client and a checked transaction sender.

The RPC URL is kept out of reprs and errors. urllib3 DEBUG logs still show request paths.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from time import time as wall
from typing import Any, Callable

import requests
from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak, to_checksum_address

from .errors import BadAnswer, NetworkError, TxFailed, clean

try:
    import fcntl
except ImportError:  # Windows: the tx record is written with no lock
    fcntl = None  # type: ignore[assignment]

REVERTS = {
    keccak(text=n + "()")[:4]: n
    for n in (
        "NotWhitelisted DepositsClosed ZeroAmount DepositBelowMinimum NotAdmin SeatNotSeeded AmountOverflow "
        "NotFunded DuplicateArm DeadlinePassed DeadlineTooFar BadSignature StopDepositsUndrained "
        "HardPaused BadTerms BadArmKind EnvelopeTooLong"
    ).split()
}


class RpcError(Exception):
    """The node answered with an error object."""

    def __init__(self, error: Any) -> None:
        super().__init__(clean(error))
        self.error = error


ERROR_STRING = bytes.fromhex("08c379a0")  # Error(string)
PANIC = bytes.fromhex("4e487b71")  # Panic(uint256)


def revert_name(error: Any) -> str:
    """The revert reason: an ``Error(string)`` text, ``panic 0x..``, a CRX error name, else the selector hex."""
    if not isinstance(error, dict):
        return clean(error, 120)
    data = error.get("data")
    data = data.get("data") if isinstance(data, dict) else data
    if isinstance(data, str) and len(data) >= 10:
        try:
            sel = bytes.fromhex(data[2:10])
        except ValueError:
            return clean(data[:10])
        try:
            body = bytes.fromhex(data[10:])
            if sel == ERROR_STRING:
                text = clean(decode(["string"], body)[0], 160).strip()
                if text:
                    return text
            elif sel == PANIC:
                return f"panic 0x{decode(['uint256'], body)[0]:02x}"
        except (DecodingError, ValueError, OverflowError):
            pass
        return REVERTS.get(sel, clean(data[:10]))
    return clean(error.get("message", "a revert"), 120)


class Rpc:
    def __init__(self, url: str, session: requests.Session, timeout: float = 20) -> None:
        self._url = url
        self._session = session
        self._timeout = timeout

    def __repr__(self) -> str:
        return "Rpc(<url hidden>)"

    def __getstate__(self) -> object:
        raise TypeError("the RPC URL can hold a key; an Rpc cannot be pickled or copied")

    def __call__(self, method: str, *params: Any) -> Any:
        err: Exception | None = None
        r = body = None
        try:
            r = self._session.post(
                self._url, timeout=self._timeout,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)},
            )
        except requests.RequestException as e:
            err = NetworkError(f"the RPC did not answer ({type(e).__name__})")
        if err is None:
            try:
                body = r.json()
            except ValueError:  # requests' JSONDecodeError is a ValueError
                err = BadAnswer("the RPC sent an answer that is not JSON")
        if err is not None:
            # Raised outside the except block: no context carries the URL.
            raise err
        if not isinstance(body, dict):
            raise BadAnswer("the RPC sent an answer that is not an object")
        if "error" in body:
            raise RpcError(body["error"])
        if "result" not in body:
            raise BadAnswer("the RPC answer has no result")
        return body["result"]

    def int(self, method: str, *params: Any) -> int:
        v = self(method, *params)
        try:
            return int(v, 16)
        except (TypeError, ValueError):
            raise BadAnswer(f"the RPC sent a {method} answer that is not a number") from None


TIP_FLOOR = 10**8  # wei (0.1 gwei): the least priority fee a type-2 tx offers
WAIT_S = 300  # s that send_tx waits for a receipt
STUCK_S = 60  # s that send_tx waits for a pending tx it did not send to mine
KEEP = 100  # rows that the tx record keeps


def bump(x: int) -> int:
    """1.125 x, rounded up: over the +10% a node asks of each fee field of a replacement."""
    return -(-x * 9 // 8)


def fees(rpc: Rpc, old: dict[str, int] | None = None) -> dict[str, int]:
    """The fee fields of a tx. A chain with a base fee gets type 2: the tip is
    ``eth_maxPriorityFeePerGas``, at least ``TIP_FLOOR``, and ``maxFeePerGas`` is twice the latest
    base fee plus the tip. A chain with no base fee gets a legacy ``gasPrice``: 1.25 x ``eth_gasPrice``.

    ``old`` is the tip and fee cap of this SDK's own pending tx that the new tx replaces at its
    nonce (a legacy ``gasPrice`` is both). Then the tip is also at least 1.125 x the old tip, and
    the fee cap at least 1.125 x the old cap."""
    block = rpc("eth_getBlockByNumber", "latest", False)
    base = block.get("baseFeePerGas") if isinstance(block, dict) else None
    if base is None:
        price = rpc.int("eth_gasPrice") * 5 // 4
        return {"gasPrice": max(price, bump(old["tip"]), bump(old["cap"])) if old else price}
    try:
        base = int(base, 16)
    except (TypeError, ValueError):
        raise BadAnswer("the RPC sent a baseFeePerGas that is not a number") from None
    if base < 0:
        raise BadAnswer("the RPC sent a baseFeePerGas below 0")
    try:
        tip = rpc.int("eth_maxPriorityFeePerGas")
    except RpcError:
        tip = 0
    tip = max(tip, TIP_FLOOR)
    if not old:
        return {"type": 2, "maxFeePerGas": 2 * base + tip, "maxPriorityFeePerGas": tip}
    tip = max(tip, bump(old["tip"]))
    return {"type": 2, "maxFeePerGas": max(2 * base + tip, bump(old["cap"])), "maxPriorityFeePerGas": tip}


class TxLog:
    """The txs this SDK sent, in a local JSON file of mode 600: chain id, sender, nonce, hash, to,
    data, fee fields and time. It holds no key material. ``own_dir``: the directory is the SDK's
    own, and is set to mode 700."""

    def __init__(self, path: str | os.PathLike, *, own_dir: bool = False) -> None:
        self.path = Path(path).expanduser()
        self.own_dir = own_dir

    def rows(self) -> list[dict]:
        try:
            rows = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def at(self, chain_id: int, sender: str, nonce: int) -> list[dict]:
        """The rows of ``sender`` at ``nonce`` on ``chain_id``, oldest first."""
        return [r for r in self.rows() if r.get("chain_id") == chain_id and r.get("nonce") == nonce
                and str(r.get("from", "")).lower() == sender.lower()]

    def add(self, row: dict) -> None:
        """Appends ``row`` under an exclusive lock on the sidecar ``<file>.lock``. Drops the sender's
        rows on the chain below its nonce, then all but the newest ``KEEP`` rows. Sets the directory
        to mode 700 when it is the SDK's own or this call made it, and this user owns it. A file
        that cannot be written is left as it is."""
        def done(r: dict) -> bool:
            return (r.get("chain_id") == row["chain_id"] and str(r.get("from", "")).lower() == row["from"].lower()
                    and isinstance(r.get("nonce"), int) and r["nonce"] < row["nonce"])

        folder = self.path.parent
        try:
            made = not folder.is_dir()
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = os.open(folder / f"{self.path.name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return
        try:
            st = folder.stat()
            if ((self.own_dir or made) and hasattr(os, "getuid") and st.st_uid == os.getuid()
                    and st.st_mode & 0o777 != 0o700):
                os.chmod(folder, 0o700)
        except OSError:
            pass
        tmp = folder / f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            rows = [r for r in self.rows() if not done(r)] + [row]
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                if hasattr(os, "fchmod"):
                    os.fchmod(f.fileno(), 0o600)
                json.dump(rows[-KEEP:], f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        finally:
            os.close(lock)  # ends the lock


def underpriced(error: Any) -> bool:
    """True when the node refused a tx as an underpriced replacement of a pending tx at its nonce."""
    m = str(error.get("message") if isinstance(error, dict) else error).lower()
    return "replace" in m and "underpriced" in m


def is_pending(rpc: Rpc, sender: str, nonce: int, tx: Any) -> bool:
    """True when the node holds ``tx`` from ``sender`` at ``nonce``, not yet mined."""
    if not isinstance(tx, str):
        return False
    try:
        t = rpc("eth_getTransactionByHash", tx)
        return (isinstance(t, dict) and t.get("blockNumber") is None
                and str(t["from"]).lower() == sender.lower() and int(t["nonce"], 16) == nonce)
    except (RpcError, NetworkError, BadAnswer, KeyError, TypeError, ValueError):
        return False


def own(
    rpc: Rpc, log: TxLog | None, chain_id: int, sender: str, nonce: int, to: str, data: str,
) -> tuple[dict[str, int], int, list[str]] | None:
    """For this SDK's own tx pending at ``nonce`` with the same ``to`` and ``data``: (its recorded
    ``tip`` and ``cap``, the unix s it was sent, the hashes of every recorded tx at the nonce with
    that ``to`` and ``data``). Else None."""
    rows = [r for r in (log.at(chain_id, sender, nonce) if log is not None else [])
            if str(r.get("to", "")).lower() == to.lower() and str(r.get("data", "")).lower() == data.lower()]
    hashes = [r["hash"] for r in rows if isinstance(r.get("hash"), str)]
    for r in reversed(rows):
        try:
            if "maxFeePerGas" in r:
                old = {"tip": int(r["maxPriorityFeePerGas"]), "cap": int(r["maxFeePerGas"])}
            else:
                old = {"tip": int(r["gasPrice"]), "cap": int(r["gasPrice"])}
            sent = int(r["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if min(old.values()) >= 0 and is_pending(rpc, sender, nonce, r.get("hash")):
            return old, sent, hashes
    return None


def slot(
    rpc: Rpc, log: TxLog | None, chain_id: int, sender: str, to: str, data: str, what: str,
    sleep: Callable[[float], None], held: int | None = None,
) -> tuple[int, dict[str, int] | None, list[str], bool]:
    """(nonce, old, replaced, waited): the nonce of the next tx; the recorded fees of the SDK's own
    tx it replaces there, and the hashes of the recorded txs it replaces.

    A pending tx holds the next nonce when the pending count is above the latest count, or when a
    node refused a send at ``held`` as an underpriced replacement. Each pending tx gets
    ``STUCK_S`` to mine. When the tx record shows this SDK sent it with the same ``to`` and
    ``data``, and the node holds it, it is then replaced: the nonce is the latest count. Any other
    raises ``TxFailed`` and nothing is sent. With no pending tx, the nonce is the pending count,
    ``old`` is None and ``replaced`` is empty. ``waited`` is True when the send waited.
    """
    deadline = None
    while True:
        latest = rpc.int("eth_getTransactionCount", sender, "latest")
        pending = rpc.int("eth_getTransactionCount", sender, "pending")
        if pending <= latest and latest != held:
            return pending, None, [], deadline is not None
        hit = own(rpc, log, chain_id, sender, latest, to, data)
        if hit is not None and wall() - hit[1] >= STUCK_S:
            return latest, hit[0], hit[2], deadline is not None
        if deadline is None:
            deadline = time.monotonic() + STUCK_S
        elif time.monotonic() >= deadline:
            if hit is not None:
                return latest, hit[0], hit[2], True
            raise TxFailed(f"{what} not sent: a transaction from this wallet is pending at nonce {latest}; "
                           "it must confirm before this step can be sent.", details={"step": what, "nonce": latest})
        sleep(2)


def send_tx(
    rpc: Rpc,
    account: Any,
    chain_id: int,
    what: str,
    to: str,
    data: str,
    *,
    gas: int | None = None,
    wait_s: float = WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    log: TxLog | None = None,
) -> str:
    """Simulate, sign, send, and wait for status 1. Returns the tx hash.

    ``slot`` names the nonce: a pending tx at it is replaced only when ``log`` shows this SDK sent
    it with the same ``to`` and ``data``. A node that refuses a send as an underpriced replacement
    gets one more send at that nonce on the same terms. Each tx the node takes, or may hold, goes
    into ``log``. A replacement waits for its own receipt and for those of the txs it replaces:
    the first receipt ends the step, and its hash is the result.
    """
    sender = account.address
    chain_id = int(chain_id)
    call = {"from": sender, "to": to, "data": data}

    def check() -> int:
        try:
            rpc("eth_call", call, "latest")
            return gas if gas is not None else rpc.int("eth_estimateGas", call) * 12 // 10
        except RpcError as e:
            raise TxFailed(f"{what} would revert: {revert_name(e.error)}; nothing sent", details={"step": what}) from None

    limit = check()
    held = None
    for attempt in (1, 2):
        nonce, old, replaced, waited = slot(rpc, log, chain_id, sender, to, data, what, sleep, held)
        if waited:
            limit = check()  # the chain moved while the send waited
        fee = fees(rpc, old)
        signed = account.sign_transaction(
            {
                "chainId": chain_id,
                "nonce": nonce,
                "to": to_checksum_address(to),
                "data": data,
                "value": 0,
                "gas": int(limit),
                **fee,
            }
        )
        raw = "0x" + bytes(signed.raw_transaction).hex()
        try:
            tx = rpc("eth_sendRawTransaction", raw)
        except RpcError as e:
            if not underpriced(e.error):
                raise TxFailed(f"{what} not sent: {clean(e.error, 160)}", details={"step": what}) from None
            if old is not None:
                raise TxFailed(f"{what} not sent: the replacement at nonce {nonce} was refused by the node.",
                               details={"step": what, "nonce": nonce}) from None
            if attempt == 2:
                raise TxFailed(f"{what} not sent: a transaction from this wallet is pending at nonce {nonce}; "
                               "it must confirm before this step can be sent.",
                               details={"step": what, "nonce": nonce}) from None
            held = nonce
            continue
        except NetworkError:
            # The node may hold it. Its own hash is the handle.
            tx = "0x" + bytes(signed.hash).hex()
        if log is not None:
            log.add({"chain_id": chain_id, "from": sender, "nonce": nonce, "hash": "0x" + bytes(signed.hash).hex(),
                     "to": to_checksum_address(to), "data": data, **fee, "time": int(wall())})
        break
    watch = [tx] + [h for h in replaced if h.lower() != str(tx).lower()]
    deadline = time.monotonic() + wait_s
    while True:
        for h in watch:
            try:
                receipt = rpc("eth_getTransactionReceipt", h)
            except (RpcError, NetworkError):
                receipt = None
            if receipt:
                if receipt.get("status") != "0x1":
                    raise TxFailed(f"{what} tx {h} reverted", details={"step": what, "tx": h})
                return h
        if time.monotonic() > deadline:
            raise TxFailed(f"{what} tx {tx} has no receipt after {int(wait_s)} s", details={"step": what, "tx": tx})
        sleep(2)
