"""Solana pieces the SDK needs, in plain Python: base58, program addresses, the program alias, the
legacy transaction wire format, the deposit check, Ed25519 signing (``crx-python[solana]``), the
JSON-RPC calls of the chain check and the deposit send, the error of a seat CRX stopped, the end of a
seat's bind, and the tenor band check of an RFQ.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from eth_utils import keccak

from ._bind import UNAVAILABLE, Binder
from ._chain import RpcError
from ._http import Gateway
from .errors import (
    BadAnswer, BadRequest, ConfigError, CrxError, NetworkError, NotWhitelisted, RefusedToSign, SendUnknown,
    ServerError, ServiceUnavailable, TradeUnknown, TxFailed, clean, gateway_code,
)

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(B58)}

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
LOADER_V3 = "BPFLoaderUpgradeab1e11111111111111111111111"

DEPOSIT_IX = 10  # crx_core instruction tag
CH_ARM, CH_MARKET = 0, 2  # checkpoint chains the deposit touches
# A deposit's priority fee ceiling, lamports: compute unit limit x price / 1e6.
MAX_PRIORITY_LAMPORTS = 100_000
MAX_COMPUTE_UNITS = 400_000


# ---------- base58 ----------

def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(b) - len(b.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(s: str, size: int | None = None) -> bytes:
    """Base58 to bytes. ``size``: the exact length the value must have."""
    if not isinstance(s, str) or not s or any(c not in _B58_INDEX for c in s):
        raise ValueError("not base58")
    n = 0
    for c in s:
        n = n * 58 + _B58_INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    out = b"\0" * (len(s) - len(s.lstrip("1"))) + body
    if size is not None and len(out) != size:
        raise ValueError(f"not {size} bytes of base58")
    return out


def key(s: Any) -> bytes:
    """A 32-byte Solana key from base58."""
    return b58decode(s, 32)


# ---------- Ed25519 curve check (program addresses must be off the curve) ----------

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def on_curve(b: bytes) -> bool:
    """True when 32 bytes decompress to an Ed25519 point (RFC 8032 §5.1.3)."""
    if len(b) != 32:
        return False
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    sign = b[31] >> 7
    if y >= _P:
        return False
    y2 = y * y % _P
    u, v = (y2 - 1) % _P, (_D * y2 + 1) % _P
    x = (u * pow(v, 3, _P) * pow(u * pow(v, 7, _P), (_P - 5) // 8, _P)) % _P
    vx2 = v * x * x % _P
    if vx2 != u % _P:
        if vx2 != (-u) % _P:
            return False
        x = x * _SQRT_M1 % _P
    if x == 0 and sign == 1:
        return False
    return True


def create_program_address(seeds: list[bytes], program: bytes) -> bytes | None:
    h = hashlib.sha256(b"".join(seeds) + program + b"ProgramDerivedAddress").digest()
    return None if on_curve(h) else h


def find_program_address(seeds: list[bytes], program: bytes) -> tuple[bytes, int]:
    for bump in range(255, -1, -1):
        a = create_program_address(seeds + [bytes([bump])], program)
        if a is not None:
            return a, bump
    raise ValueError("no program address for these seeds")


def pda(program_id: str, *seeds: bytes) -> str:
    return b58encode(find_program_address(list(seeds), key(program_id))[0])


def ata(wallet: str, mint: str) -> str:
    """The wallet's associated token account of the mint (classic SPL)."""
    return b58encode(find_program_address([key(wallet), key(TOKEN_PROGRAM), key(mint)], key(ATA_PROGRAM))[0])


def alias(cluster_tag: str, program_id: str) -> str:
    """The program alias: the low 20 bytes of keccak256(clusterTag ‖ programId), 0x-hex."""
    return "0x" + keccak(key(cluster_tag) + key(program_id))[12:].hex()


# ---------- the legacy transaction ----------

def _shortvec(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    for k in range(3):
        if i >= len(b):
            raise ValueError("cut short")
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            if k and c == 0:
                raise ValueError("a length is not minimal")
            return n, i
        shift += 7
    raise ValueError("a length is too long")


def parse_tx(raw: bytes) -> dict:
    """A legacy transaction: signature slots, header, keys, blockhash, instructions with account metas."""
    nsig, i = _shortvec(raw, 0)
    sigs = [raw[i + 64 * k:i + 64 * (k + 1)] for k in range(nsig)]
    i += 64 * nsig
    if i > len(raw):
        raise ValueError("cut short")
    msg = raw[i:]
    if len(msg) < 3 or msg[0] & 0x80:
        raise ValueError("not a legacy message")
    n_req, n_ro_signed, n_ro_unsigned = msg[0], msg[1], msg[2]
    j = 3
    nkeys, j = _shortvec(msg, j)
    keys = [b58encode(msg[j + 32 * k:j + 32 * (k + 1)]) for k in range(nkeys)]
    j += 32 * nkeys
    blockhash = b58encode(msg[j:j + 32])
    j += 32
    nix, j = _shortvec(msg, j)
    ixs = []
    for _ in range(nix):
        prog = msg[j]
        j += 1
        nacc, j = _shortvec(msg, j)
        accs = list(msg[j:j + nacc])
        j += nacc
        nd, j = _shortvec(msg, j)
        data = msg[j:j + nd]
        if len(data) != nd:
            raise ValueError("cut short")
        j += nd
        ixs.append((prog, accs, data))
    if j != len(msg) or nsig != n_req or len(msg) < 3 + 32 * nkeys:
        raise ValueError("not one whole transaction")
    if n_ro_signed > n_req or n_ro_unsigned > nkeys - n_req:
        raise ValueError("a bad header")

    def meta(k: int) -> dict:
        signer = k < n_req
        writable = k < n_req - n_ro_signed if signer else k < nkeys - n_ro_unsigned
        return {"pubkey": keys[k], "is_signer": signer, "is_writable": writable}

    out = []
    for prog, accs, data in ixs:
        if prog >= nkeys or any(a >= nkeys for a in accs):
            raise ValueError("an index past the keys")
        out.append({"program_id": keys[prog], "accounts": [meta(a) for a in accs], "data": data})
    return {"signatures": sigs, "message": msg, "keys": keys, "num_signers": n_req,
            "blockhash": blockhash, "instructions": out}


def deposit_metas(program_id: str, authority: str, source: str) -> list[tuple[str, bool, bool]]:
    """(pubkey, is_signer, is_writable) of crx_core.deposit, in order."""
    return [
        (pda(program_id, b"core"), False, True),
        (pda(program_id, b"seats"), False, True),
        (pda(program_id, b"ckpt", bytes([CH_ARM])), False, True),
        (pda(program_id, b"ckpt", bytes([CH_MARKET])), False, True),
        (authority, True, False),
        (source, False, True),
        (pda(program_id, b"vault"), False, True),
        (TOKEN_PROGRAM, False, False),
    ]


def deposit_data(amount_raw: int, row: int, seat20: bytes) -> bytes:
    return bytes([DEPOSIT_IX]) + int(amount_raw).to_bytes(8, "little") + int(row).to_bytes(4, "little") + seat20


def check_deposit(raw: bytes, *, program_id: str, authority: str, source: str, amount_raw: int, row: int | None,
                  seat20: bytes, max_priority_lamports: int = MAX_PRIORITY_LAMPORTS) -> dict:
    """Refuses (RefusedToSign) any tx but: compute limit (1..MAX_COMPUTE_UNITS), compute price, crx_core.deposit
    of exactly these accounts and bytes, no other key, one signer = the authority as fee payer, empty signature
    slot. Returns the parse.

    ``row`` None takes the seat row the tx names; the program refuses a row that does not hold the seat."""
    try:
        t = parse_tx(raw)
    except (ValueError, IndexError):
        raise RefusedToSign("the gateway served a deposit tx this SDK cannot read; nothing signed") from None
    # Header (1, 0, 3): one signer, writable (the metas check below makes it the authority: the fee payer), and
    # the three programs read-only.
    if t["message"][:3] != bytes([1, 0, 3]):
        raise RefusedToSign("the deposit tx has a signer, fee payer or writable program other than the "
                            "deposit's; nothing signed")
    if any(s != b"\0" * 64 for s in t["signatures"]):
        raise RefusedToSign("the deposit tx is already signed; nothing signed")
    ixs = t["instructions"]
    if [ix["program_id"] for ix in ixs] != [COMPUTE_BUDGET, COMPUTE_BUDGET, program_id]:
        raise RefusedToSign("the deposit tx holds other instructions; nothing signed")
    lim, price = ixs[0], ixs[1]
    if lim["accounts"] or len(lim["data"]) != 5 or lim["data"][0] != 2:
        raise RefusedToSign("the deposit tx has a bad compute limit; nothing signed")
    if price["accounts"] or len(price["data"]) != 9 or price["data"][0] != 3:
        raise RefusedToSign("the deposit tx has a bad compute price; nothing signed")
    units = int.from_bytes(lim["data"][1:], "little")
    micro = int.from_bytes(price["data"][1:], "little")
    if units == 0:
        raise RefusedToSign("the deposit tx has a compute limit of 0; nothing signed")
    # The runtime rounds the priority fee up: ceil(units x price / 1e6).
    if units > MAX_COMPUTE_UNITS or -(-units * micro // 1_000_000) > max_priority_lamports:
        raise RefusedToSign("the deposit tx asks for a priority fee above the ceiling; nothing signed")
    dep = ixs[2]
    got = [(m["pubkey"], m["is_signer"], m["is_writable"]) for m in dep["accounts"]]
    # Writability is per message: the authority pays the fee, so it is writable in the message.
    metas = deposit_metas(program_id, authority, source)
    want = [(k, sg, wr or k == authority) for k, sg, wr in metas]
    if got != want:
        raise RefusedToSign("the deposit tx names other accounts; nothing signed")
    # The message holds exactly these keys, each once: 1 signer, 6 writable accounts, 3 read-only programs.
    keys = t["keys"]
    if len(keys) != len(set(keys)) or set(keys) != {k for k, _, _ in metas} | {COMPUTE_BUDGET, program_id}:
        raise RefusedToSign("the deposit tx holds a key twice or a key it does not use; nothing signed")
    if row is None and len(dep["data"]) == 33:
        row = int.from_bytes(dep["data"][9:13], "little")
    if row is None or dep["data"] != deposit_data(amount_raw, row, seat20):
        raise RefusedToSign("the deposit tx carries other bytes; nothing signed")
    return t


# ---------- Ed25519 keypair ----------

class Keypair:
    """A Solana keypair: a 64-byte secret (seed ‖ public key), a 32-byte seed, a list of those bytes, or the
    path of a ``solana-keygen`` JSON file (mode 600).

    Needs ``crx-python[solana]`` (the ``cryptography`` package). The seed never appears in a repr, and no
    error or traceback frame of this class holds the secret.
    """

    def __init__(self, secret: Any) -> None:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        except ImportError:
            secret = None
            raise ConfigError("Solana signing needs the extra: pip install 'crx-python[solana]'") from None
        if isinstance(secret, str) and _is_b58_secret(secret):
            secret = None
            raise ConfigError("keypair= takes a keypair file path, a list or bytes; not a base58 secret")
        if isinstance(secret, (str, os.PathLike)):
            # A string can be the secret itself (hex, JSON text): no frame under this one survives the refusal.
            path, secret = secret, None
            err = None
            try:
                secret = _read_keypair_file(path)
            except Exception as e:  # noqa: BLE001 - the original error and its frames can hold the string
                err = str(e) if isinstance(e, ConfigError) else "the keypair file cannot be read"
            path = None
            if secret is None:
                raise ConfigError(err or "the keypair file cannot be read")
        if isinstance(secret, list):
            secret = _list_bytes(secret)
        if not isinstance(secret, (bytes, bytearray)) or len(secret) not in (32, 64):
            secret = None
            raise ConfigError("a Solana keypair is 32 or 64 bytes")
        self._k = Ed25519PrivateKey.from_private_bytes(bytes(secret[:32]))
        pub = self._k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        same = len(secret) == 32 or bytes(secret[32:]) == pub
        secret = None
        if not same:
            self._k = None
            raise ConfigError("the keypair's public half does not match its secret")
        self.pubkey = b58encode(pub)

    def __repr__(self) -> str:
        # The public key only. A keypair whose key was refused has none and prints None.
        return f"Keypair({getattr(self, 'pubkey', None)})"

    def __getstate__(self) -> Any:
        raise TypeError("a Keypair cannot be pickled or copied")

    def sign(self, message: bytes) -> bytes:
        return self._k.sign(message)


def _is_b58_secret(s: str) -> bool:
    """True when ``s`` reads as a base58 32- or 64-byte secret (no path holds only base58 characters at that
    length)."""
    n = None
    try:
        n = len(b58decode(s.strip()))
    except ValueError:
        pass
    return n in (32, 64)


def _list_bytes(v: list) -> bytes | None:
    """The bytes of a list of ints 0..255; None for any other list. No error carries the values."""
    if all(type(x) is int and 0 <= x < 256 for x in v):
        return bytes(v)
    return None


def _read_keypair_file(path: Any) -> bytes:
    """The secret in a ``solana-keygen`` JSON file. Read as the EVM key file is read: the file must be mode
    600, and no error carries the file's bytes. The file is one flat array: a text with more than one ``[``,
    or with a ``{``, is refused and not parsed."""
    from pathlib import Path

    from ._keys import _read_key_file
    text = _read_key_file(Path(os.fspath(path)).expanduser())
    flat = text.count("[") == 1 and "{" not in text
    v = None
    if flat:
        try:
            v = json.loads(text)
        except ValueError:
            pass
    text = None
    out = _list_bytes(v) if isinstance(v, list) else None
    v = None
    if out is None:
        # Raised outside the except block: no context carries the file's bytes.
        raise ConfigError("the keypair file is not a solana-keygen JSON array of bytes")
    return out


SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

# The host of the Solana client surface: the one place the SDK names it. The trading key text and the
# network's default gateway URL are built from it. Another host gives another text, another seat key and
# another seat address for the same wallet.
HOST = "portal.crxfx.com"


def trading_key_text(wallet: str, host: str = HOST) -> bytes:
    """The one message a wallet signs to make its CRX trading (seat) key. Same text as the site."""
    return (f"CRX trading key for {wallet} on {host} (Solana mainnet). "
            "Signing this creates your CRX trading key. It moves no funds and is not a login. "
            f"Sign it only on https://{host}.").encode()


def seat_secret(kp: Keypair, host: str = HOST) -> bytes:
    """The secp256k1 seat key of a wallet: keccak256 of its Ed25519 signature of ``trading_key_text``, with a
    counter byte appended on the rare value outside 1..n-1. Signed twice: two different signatures refuse."""
    text = trading_key_text(kp.pubkey, host)
    sig = kp.sign(text)
    if kp.sign(text) != sig:
        sig = None
        raise ConfigError("the wallet signs the trading key text two ways; it cannot make a stable seat key")
    i = 0
    while True:
        d = keccak(sig if i == 0 else sig + bytes([i]))
        if 0 < int.from_bytes(d, "big") < SECP256K1_N:
            return d
        i += 1


def signed_tx(parsed: dict, kp: Keypair) -> bytes:
    """The wire tx with its one signature slot filled."""
    return bytes([1]) + kp.sign(parsed["message"]) + parsed["message"]


# ---------- RPC ----------

# crx_core refuses a new item while its intake has no room for it: 660 RING_FULL, 661 RING_USER_FULL. The tx
# fails whole and no token moves. The hourly check takes items out of the intake.
RING_CODES = (660, 661)
RING_LINE = "no USDC left the wallet; the chain's intake is full until the hourly check; deposit again after it"
_CUSTOM_HEX = re.compile(r"custom program error:\s*0x([0-9a-fA-F]{1,8})\b")


def program_code(err: Any) -> int | None:
    """The custom program error code that an RPC error or a tx status ``err`` names; None when it names none.

    A node spells it two ways: ``{"InstructionError": [2, {"Custom": 660}]}`` (a tx status ``err``, and
    ``data.err`` of a refused send), and ``custom program error: 0x294`` in a message or a log line. The
    first form is read first. At most 2000 values of the answer are read."""
    texts: list[str] = []
    stack, seen = [err], 0
    while stack and seen < 2000:
        v = stack.pop()
        seen += 1
        if isinstance(v, dict):
            ie = v.get("InstructionError")
            if isinstance(ie, list) and len(ie) == 2 and isinstance(ie[1], dict):
                c = ie[1].get("Custom")
                if type(c) is int and 0 <= c < 2**32:
                    return c
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, str):
            texts.append(v)
    for t in texts:
        m = _CUSTOM_HEX.search(t)
        if m:
            return int(m.group(1), 16)
    return None


def deposit_failed(lead: str, err: Any, sig: str, limit: int) -> TxFailed:
    """The error of a deposit that the node or the program refused; ``lead`` says how far the tx got. A refusal
    on a full intake (``RING_CODES``) reads ``RING_LINE``, with ``details['reason']`` ``intake_full`` and the
    code in ``details['program_error']``. Any other reads as the node sent it, cut to ``limit`` characters."""
    code = program_code(err)
    if code in RING_CODES:
        return TxFailed(f"{lead}: {RING_LINE}", details={"tx": sig, "program_error": code, "reason": "intake_full"})
    return TxFailed(f"{lead}: {clean(err, limit)}", details={"tx": sig})


SEND_UNKNOWN_LINE = "deposit outcome unknown: check transaction {sig} on the chain before a new deposit"
EXPIRED_LINE = "deposit not sent: the transaction expired before it reached the chain; no USDC left the wallet"
FOUND, ABSENT, UNREAD = "found", "absent", "unread"


def sig_status(rpc: Any, sig: str) -> tuple[str, dict | None]:
    """The status of ``sig`` on one RPC: (FOUND, its row), (ABSENT, None) when the RPC answers that it holds
    none, (UNREAD, None) when the read has no clear answer."""
    try:
        row = rpc("getSignatureStatuses", [sig], {"searchTransactionHistory": True})["value"][0]
    except Exception:
        return UNREAD, None
    if row is None:
        return ABSENT, None
    return (FOUND, row) if isinstance(row, dict) else (UNREAD, None)


def past(rpc: Any, last_valid: int) -> bool:
    """True when the RPC reads its confirmed block height above ``last_valid``: the tx can no longer land."""
    try:
        return int(rpc("getBlockHeight", {"commitment": "confirmed"})) > last_valid
    except Exception:
        return False


def expired_on_both(rpcs: tuple, sig: str, last_valid: int) -> tuple[str, dict | None]:
    """The verdict of two RPCs after the blockhash expired. (FOUND, row) when one holds ``sig``; (ABSENT, None)
    only when each RPC reads its height past ``last_valid`` and then holds no status; else (UNREAD, None)."""
    verdict: tuple[str, dict | None] = (ABSENT, None)
    for r in rpcs:
        if not past(r, last_valid):
            verdict = (UNREAD, None)
            continue
        kind, row = sig_status(r, sig)
        if kind == FOUND:
            return kind, row
        if kind == UNREAD:
            verdict = (UNREAD, None)
    return verdict


def send_and_confirm(rpc: Any, raw: bytes, *, last_valid: int | None, check_rpc: Any = None, wait_s: float = 120,
                     sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> str:
    """sendTransaction once, then poll getSignatureStatuses until confirmed. Returns the base58 signature.

    The tx is sent once only. A send refused by the node (an RPC error object) raises ``TxFailed``: not sent.
    A send with no clear answer (none, not JSON, HTTP 5xx) can be on the chain: the status reads decide.
    ``TxFailed`` "not sent" after the send only when ``rpc`` and ``check_rpc`` each read their block height
    past ``last_valid`` and hold no status for the signature. Any other end without a confirmed status
    raises ``SendUnknown`` with the signature in ``details['tx']``."""
    sig = b58encode(raw[1:65])
    try:
        rpc("sendTransaction", base64.b64encode(raw).decode(),
            {"encoding": "base64", "preflightCommitment": "confirmed"})
    except RpcError as e:  # the node refused it: preflight or the send
        raise deposit_failed("deposit not sent", e.error, sig, 160) from None
    except Exception:
        pass  # no clear answer: the node may hold it; its own signature is the handle
    rpcs = (rpc,) if check_rpc is None else (rpc, check_rpc)
    deadline = clock() + wait_s
    while True:
        kind, st = sig_status(rpc, sig)
        if kind != FOUND and last_valid is not None and past(rpc, last_valid):
            kind, st = expired_on_both(rpcs, sig, last_valid)
            if kind == ABSENT and check_rpc is not None:
                raise TxFailed(EXPIRED_LINE, details={"tx": sig, "reason": "expired"})
        if kind == FOUND and st is not None:
            if st.get("err"):
                raise deposit_failed(f"deposit tx {sig} failed", st["err"], sig, 120)
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig
        if clock() > deadline:
            raise SendUnknown(SEND_UNKNOWN_LINE.format(sig=sig), details={"tx": sig})
        sleep(2)


UNPINNED = "this SDK has no Solana program pinned yet; nothing signed"


def need_pin(net: dict) -> None:
    """Refuse a network row that pins no genesis hash, no program id or no cluster tag. Called before a wallet
    is read and before the chain check."""
    if not (net.get("genesis_hash") and net.get("program_id") and net.get("cluster_tag")):
        raise ConfigError(UNPINNED)


def check_cluster(rpc: Any, net: dict, served: dict) -> dict:
    """The chain check before the first signature (fail closed). Returns the chain row the SDK signs with:
    ``chain_id`` None, ``core`` = the alias (the domain's verifyingContract), ``core_pda``, ``program_id``."""
    need_pin(net)
    pinned_gen, pinned_pid, tag = net["genesis_hash"], net["program_id"], net["cluster_tag"]
    try:
        gen, pid = str(served["genesis_hash"]), str(served["program_id"])
        core_pda, vault, va = str(served["core"]), str(served["vault"]), str(served["verifying_contract"]).lower()
        domain = str(served["domain"]).lower()
    except (KeyError, TypeError):
        raise BadAnswer("/health sent a Solana chain this SDK cannot read") from None
    if served.get("family") != "solana":
        raise ConfigError("/health names a chain that is not Solana")
    if gen != pinned_gen or pid != pinned_pid:
        raise RefusedToSign("/health serves another cluster or program than this SDK pins; nothing signed")
    if rpc("getGenesisHash") != pinned_gen:
        raise ConfigError("the RPC is not on the pinned Solana cluster")
    acct = (rpc("getAccountInfo", pinned_pid, {"encoding": "base64"}) or {}).get("value")
    if not acct or acct.get("executable") is not True or acct.get("owner") != LOADER_V3:
        raise ConfigError("the program /health names is not a deployed program; nothing sent")
    from . import _eip712 as e7
    want_alias = alias(tag, pinned_pid)
    if va != want_alias or domain != e7.h0x(e7.domain_separator(None, want_alias)):
        raise RefusedToSign("domain moved: /health does not match the program; nothing signed")
    if core_pda != pda(pinned_pid, b"core") or vault != pda(pinned_pid, b"vault"):
        raise RefusedToSign("/health names a core or vault that is not the program's; nothing signed")
    return dict(served, chain_id=None, core=want_alias, core_pda=core_pda, program_id=pinned_pid)


# ---------- a seat CRX stopped ----------

STOPPED_LINE = "CRX removed this seat's access: no new trade and no deposit; withdraw() still works"


class SeatStopped(NotWhitelisted):
    """CRX removed this seat's access. ``deposit``, ``bind``, ``ask``, ``quote`` and ``trade`` raise it: nothing
    is sent to the chain and no trade opens. ``balance``, ``positions`` and ``withdraw`` still work. A stopped
    seat that holds collateral can still ``bind``, so that it can withdraw."""

    code = "seat_stopped"


def seat_stopped(status: int, body: Any) -> SeatStopped | None:
    """The error for the gateway's answer to a stopped seat: 403 ``not_whitelisted`` with the outcome
    ``account_removed``. None for any other answer. The message is the SDK's own line."""
    if (status == 403 and isinstance(body, dict) and gateway_code(body) == "not_whitelisted"
            and body.get("outcome") == "account_removed"):
        return SeatStopped(STOPPED_LINE, status=403, gateway_code="not_whitelisted",
                           details={"outcome": "account_removed"})
    return None


# ---------- a seat's bind: it ends on the three keys the chain holds ----------

# s that bind() reads GET /bind for the end of a bind. A bind lands in seconds. After an RPC fault the gateway
# reads its end 2 to 3 minutes later.
BIND_WAIT = 240.0
BIND_POLL = 2.0  # s between two reads
BIND_KEYS = ("authority", "payout_wallet", "payout_ata")
OTHER_PAYOUT_LINE = "this seat is bound to another payout wallet: the bind is permanent; contact CRX"
OTHER_AUTHORITY_LINE = "this seat is bound already, with another authority"
BIND_LIVE_LINE = "a bind of this seat is in progress: no other bind is taken before it ends; bind() reads its end"
BIND_FAILED_LINE = "the bind did not complete: this seat is not bound"
BIND_UNAVAILABLE_LINE = "service temporarily unavailable: the bind is not filed"


class SeatBoundOtherPayout(CrxError):
    """The chain holds this seat bound to another payout wallet or payout account than the bind names. A bind
    fixes the seat's payout for good. ``details['bound']``: the authority, the payout wallet and the payout
    account the chain holds. ``details['filed']``: the three this bind named."""

    code = "seat_bound_other_payout"


class BindInProgress(CrxError):
    """A bind of this seat has no end yet: the seat is not bound, and the bind has not failed. ``bind()`` reads
    its end: with the keys of that bind it waits on it and signs nothing. The gateway takes no other bind of
    the seat before it. ``details['filed']``: the three keys of that bind, when the SDK knows them."""

    code = "bind_in_progress"


class BindFailed(CrxError):
    """The bind ended and the seat is not bound. ``details['error']`` is the gateway's code, when served."""

    code = "bind_failed"


def bind_keys(authority: str, payout_wallet: str, mint: str) -> dict:
    """The three keys a bind names, base58: the authority, the payout wallet and that wallet's token account."""
    return {"authority": authority, "payout_wallet": payout_wallet, "payout_ata": ata(payout_wallet, mint)}


def _key_or_none(v: Any) -> bytes | None:
    try:
        return key(v)
    except ValueError:
        return None


def shown_keys(keys: Any) -> dict:
    """The three keys of ``keys`` that are text, each one printable line."""
    return {k: clean(keys[k], 64) for k in BIND_KEYS if isinstance(keys, dict) and isinstance(keys.get(k), str)}


def same_keys(held: Any, filed: dict) -> bool:
    """``held`` names the three keys of ``filed``, each the same 32 bytes."""
    return isinstance(held, dict) and all(_key_or_none(held.get(k)) == key(filed[k]) for k in BIND_KEYS)


def bind_end(held: Any, filed: dict) -> dict | None:
    """What a ``GET /bind`` answer says of the bind ``filed`` (``bind_keys``).

    None while the answer does not read the seat bound: ``bound`` is not True. The status word decides nothing.
    A bound answer is judged on its three keys, each compared with the filed one as 32 bytes:

    - the three are the filed ones: returns ``held``;
    - another payout wallet or payout account: raises ``SeatBoundOtherPayout``;
    - the filed payout and another authority: raises ``CrxError`` (``seat_already_bound``);
    - a key that is not 32 bytes of base58: raises ``BadAnswer``.

    Both refusals carry ``details['bound']`` (what the chain holds) and ``details['filed']``."""
    if not isinstance(held, dict) or held.get("bound") is not True:
        return None
    got = {k: _key_or_none(held.get(k)) for k in BIND_KEYS}
    if None in got.values():
        raise BadAnswer("/bind reads the seat bound and names a key this SDK cannot read")
    other = [k for k in BIND_KEYS if got[k] != key(filed[k])]
    if not other:
        return held
    details = {"bound": shown_keys(held), "filed": dict(filed)}
    if other == ["authority"]:
        raise CrxError(OTHER_AUTHORITY_LINE, code="seat_already_bound", details=details)
    raise SeatBoundOtherPayout(OTHER_PAYOUT_LINE, details=details)


def await_bind(read: Callable[[], Any], filed: dict, clock: Callable[[], float], sleep: Callable[[float], None],
               wait: float = BIND_WAIT) -> dict:
    """Read ``GET /bind`` (``read``) until the bind ``filed`` ends, for ``wait`` s at most.

    Returns the answer that reads the seat bound with the three filed keys (``bind_end``). A bind that failed
    raises ``BindFailed``. An answer in progress is read again ``BIND_POLL`` s later; so is a read the gateway
    did not answer. Past ``wait`` the bind has not failed: ``BindInProgress``."""
    until = clock() + wait
    while True:
        try:
            held = read()
        except (NetworkError, ServerError):
            held = None
        end = bind_end(held, filed)
        if end is not None:
            return end
        if isinstance(held, dict) and held.get("status") == "failed":
            code = held.get("error")
            details = {"filed": dict(filed)}
            if isinstance(code, str) and code:
                details["error"] = clean(code, 64)
            raise BindFailed(BIND_FAILED_LINE, details=details)
        if clock() >= until:
            raise BindInProgress(
                f"the bind is still in progress after {wait:g} s: it has not failed; bind() reads its end",
                details={"filed": dict(filed)})
        sleep(BIND_POLL)


def bind_refused(status: int, body: Any, filed: dict) -> CrxError | None:
    """The error for a ``POST /bind`` refusal that this SDK names itself. None for any other answer.

    - 409 ``seat_bound_other_payout``: ``SeatBoundOtherPayout``; ``details['bound']`` is the served one.
    - The service sends nothing now: ``ServiceUnavailable``. The gateway spells it ``service_unavailable``,
      ``relay_unavailable``, or 409 ``conflict`` with the outcome ``unavailable``.
    - Another bind of the seat is in progress (409 ``conflict``, outcome ``not_permitted``): ``BindInProgress``."""
    if not isinstance(body, dict):
        return None
    code, outcome = gateway_code(body), body.get("outcome")
    kw = {"status": status, "gateway_code": code}
    if (status, code) == (409, "seat_bound_other_payout"):
        served = body.get("details") if isinstance(body.get("details"), dict) else {}
        return SeatBoundOtherPayout(OTHER_PAYOUT_LINE, details={"bound": shown_keys(served.get("bound")),
                                                                "filed": dict(filed)}, **kw)
    if code in UNAVAILABLE or ((status, code) == (409, "conflict") and outcome == "unavailable"):
        return ServiceUnavailable(BIND_UNAVAILABLE_LINE, **kw)
    if (status, code) == (409, "conflict") and outcome == "not_permitted":
        return BindInProgress(BIND_LIVE_LINE, **kw)
    return None


def post_bind(gw: Gateway, body: dict, filed: dict) -> dict | None:
    """``POST /bind``: the 200 or 202 answer. None when the gateway reads the seat bound already (409
    ``seat_already_bound``): the three keys of ``GET /bind`` then decide. Any other refusal raises the error
    of ``bind_refused``, else the gateway's own."""
    r = gw.raw_request("POST", "/bind", body=body)
    if r.status_code in (200, 202):
        return gw.parse(r, ok=(200, 202))
    served = gw.body_of(r)
    if (r.status_code, gateway_code(served)) == (409, "seat_already_bound"):
        return None
    raise bind_refused(r.status_code, served, filed) or gw.error_of(r, gw.host)


# ---------- the tenor band of a pair ----------

def _secs(v: Any) -> int | None:
    """A served limit in whole seconds; None for anything that is not an integer of 0 or more."""
    return v if type(v) is int and v >= 0 else None


def _utc(ms: int) -> str:
    """Unix ms as ``YYYY-MM-DD HH:MM:SS UTC``, the part of a second cut; the number itself when it is no date."""
    try:
        return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return str(ms)


def tenor_refusal(now_ms: int, tenor_secs: int, min_secs: Any, max_secs: Any, **kw: Any) -> BadRequest | None:
    """The error for a settlement ``tenor_secs`` whole seconds after ``now_ms`` (unix ms) that is outside the
    tenor band. None for one inside it, and for a limit not given.

    The shortest trade settles at ``now_ms`` plus ``min_secs``, the longest at ``now_ms`` plus ``max_secs``.
    ``details``: ``limit`` (``tenor``), ``tenor_secs``, and ``min_secs`` with ``earliest_expiry``, or
    ``max_secs`` with ``latest_expiry`` (unix ms)."""
    lo, hi = _secs(min_secs), _secs(max_secs)
    if lo is not None and tenor_secs < lo:
        at = now_ms + lo * 1000  # the message names the next whole second: an instant not under the minimum
        return BadRequest(
            f"the settlement is too soon: the shortest trade now settles at {_utc(at + 999)} ({lo} s from now)",
            details={"limit": "tenor", "tenor_secs": tenor_secs, "min_secs": lo, "earliest_expiry": at}, **kw)
    if hi is not None and tenor_secs > hi:
        at = now_ms + hi * 1000
        return BadRequest(
            f"the settlement is too far: the longest trade now settles at {_utc(at)} ({hi} s from now)",
            details={"limit": "tenor", "tenor_secs": tenor_secs, "max_secs": hi, "latest_expiry": at}, **kw)
    return None


def check_tenor(min_secs: Any, max_secs: Any, expiry_ms: int, now: float) -> None:
    """Raise for a settlement outside the band the pair's /markets row serves. ``now`` is unix s. A limit the
    row does not serve is not checked: the gateway decides."""
    now_ms = int(now * 1000)
    err = tenor_refusal(now_ms, (expiry_ms - now_ms) // 1000, min_secs, max_secs)
    if err is not None:
        raise err


def tenor_refused(e: CrxError, sent: Any) -> BadRequest | None:
    """The error for the gateway's refusal of an RFQ outside the tenor band: 400 with ``details['limit']``
    ``tenor``. Built from the answer's limit and its ``tenor_secs``: the gateway's clock is the ``expiry`` of
    the body sent less those seconds. None for any other refusal."""
    d = e.details
    expiry, tenor = sent.get("expiry") if isinstance(sent, dict) else None, d.get("tenor_secs")
    if e.status != 400 or d.get("limit") != "tenor" or type(expiry) is not int or type(tenor) is not int:
        return None
    return tenor_refusal(expiry - tenor * 1000, tenor, d.get("min_secs"), d.get("max_secs"), status=400,
                         gateway_code=e.gateway_code)


class SeatGateway(Gateway):
    """The gateway of a Solana client. Its answer to a stopped seat raises ``SeatStopped``, on every call. Its
    refusal of an RFQ outside the tenor band raises the error of ``tenor_refusal``."""

    def _send(self, method: str, path: str, raw: bytes, query: dict | None, timeout: float | None, auth: dict) -> Any:
        r = super()._send(method, path, raw, query, timeout, auth)
        stopped = seat_stopped(r.status_code, self.body_of(r)) if r.status_code == 403 else None
        if stopped is not None:
            raise stopped
        return r

    def request(self, method: str, path: str, **kw: Any) -> dict:
        try:
            return super().request(method, path, **kw)
        except BadRequest as e:
            err = tenor_refused(e, kw.get("body"))
            if err is None:
                raise
        raise err


class SeatBinder(Binder):
    """The binder of a Solana seat. The gateway refuses a stopped seat's accept before it reserves anything,
    so that refusal raises ``SeatStopped`` after a signature too, where any other failure raises
    ``TradeUnknown``."""

    _stopped: SeatStopped | None = None

    def post_accept(self, rfq_id: str, body: dict, until: float) -> Any:
        try:
            return super().post_accept(rfq_id, body, until)
        except SeatStopped as e:
            self._stopped = e
            raise

    def accept_trade(self, rfq_id: str, quote_id: str, ask: dict, t: dict | None, closes_at_ms: Any) -> tuple:
        try:
            return super().accept_trade(rfq_id, quote_id, ask, t, closes_at_ms)
        except TradeUnknown:
            if self._stopped is None:
                raise
        raise self._stopped
