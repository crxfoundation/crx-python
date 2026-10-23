"""Solana support: base58, program addresses, deposit checks, signing, RPC sends, seat binds and tenor checks."""

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
DEPOSIT_FOR_IX = 11  # crx_core deposit_for, served to a removed seat that deposits to itself
CH_ARM, CH_MARKET = 0, 2  # checkpoint chains a deposit writes
MAX_PRIORITY_LAMPORTS = 100_000  # priority fee cap: compute unit limit x price / 1e6
MAX_COMPUTE_UNITS = 400_000


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(b) - len(b.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(s: str, size: int | None = None) -> bytes:
    """Decode base58, and check the length when ``size`` is given."""
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
    """Decode a 32-byte Solana key from base58."""
    return b58decode(s, 32)


_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def on_curve(b: bytes) -> bool:
    """Return True when 32 bytes decompress to an Ed25519 point (RFC 8032, section 5.1.3)."""
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
    """Return the wallet's associated token account for the mint (classic SPL token program)."""
    return b58encode(find_program_address([key(wallet), key(TOKEN_PROGRAM), key(mint)], key(ATA_PROGRAM))[0])


def alias(cluster_tag: str, program_id: str) -> str:
    """Return the program alias: the low 20 bytes of keccak256(clusterTag ‖ programId), as 0x hex."""
    return "0x" + keccak(key(cluster_tag) + key(program_id))[12:].hex()


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
    """Parse a legacy transaction into signatures, header, keys, blockhash and instructions."""
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
    """Return the (pubkey, is_signer, is_writable) accounts of crx_core.deposit, in order."""
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


def deposit_for_data(amount_raw: int, row: int, seat20: bytes) -> bytes:
    """Encode crx_core.deposit_for with the seat as both payer and account."""
    return (bytes([DEPOSIT_FOR_IX]) + int(amount_raw).to_bytes(8, "little") + int(row).to_bytes(4, "little") + seat20
            + int(row).to_bytes(4, "little") + seat20)


def check_deposit(raw: bytes, *, program_id: str, authority: str, source: str, amount_raw: int, row: int | None,
                  seat20: bytes, max_priority_lamports: int = MAX_PRIORITY_LAMPORTS) -> dict:
    """Return the parse of a served deposit transaction. Raise RefusedToSign for any other transaction.

    It may hold only a compute limit, a compute price under ``max_priority_lamports``, and one crx_core.deposit
    (or deposit_for of the seat to itself) with exactly these accounts and bytes. The authority is the only
    signer and pays the fee. With ``row`` None, the transaction's row is used, and the program refuses a row
    that does not hold the seat."""
    try:
        t = parse_tx(raw)
    except (ValueError, IndexError):
        raise RefusedToSign("the gateway served a deposit tx this SDK cannot read; nothing signed") from None
    # Header (1, 0, 3): one writable signer (the authority, per the metas check) and three read-only programs.
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
    # Writability is per message, and the authority pays the fee, so its meta reads writable.
    metas = deposit_metas(program_id, authority, source)
    want = [(k, sg, wr or k == authority) for k, sg, wr in metas]
    if got != want:
        raise RefusedToSign("the deposit tx names other accounts; nothing signed")
    # Each key once and no other: 1 signer, 6 writable accounts, 3 read-only programs.
    keys = t["keys"]
    if len(keys) != len(set(keys)) or set(keys) != {k for k, _, _ in metas} | {COMPUTE_BUDGET, program_id}:
        raise RefusedToSign("the deposit tx holds a key twice or a key it does not use; nothing signed")
    data = dep["data"]
    if row is None and len(data) in (33, 57):
        row = int.from_bytes(data[9:13], "little")
    if row is None or data not in (deposit_data(amount_raw, row, seat20), deposit_for_data(amount_raw, row, seat20)):
        raise RefusedToSign("the deposit tx carries other bytes; nothing signed")
    return t


class Keypair:
    """Hold a Solana Ed25519 keypair and sign with it.

    Takes a 64-byte secret (seed then public key), a 32-byte seed, a list of those bytes, or the path of a
    ``solana-keygen`` JSON file with mode 600. Needs ``crx-python[solana]``. No repr, error or traceback
    frame of this class holds the secret."""

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
            # The string can be the secret itself, so no frame below this one survives a refusal.
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
        return f"Keypair({getattr(self, 'pubkey', None)})"

    def __getstate__(self) -> Any:
        raise TypeError("a Keypair cannot be pickled or copied")

    def sign(self, message: bytes) -> bytes:
        return self._k.sign(message)


def _is_b58_secret(s: str) -> bool:
    """Return True when ``s`` decodes as a 32- or 64-byte base58 secret."""
    n = None
    try:
        n = len(b58decode(s.strip()))
    except ValueError:
        pass
    return n in (32, 64)


def _list_bytes(v: list) -> bytes | None:
    """Return the list as bytes, or None unless every item is an int from 0 to 255. Never raises."""
    if all(type(x) is int and 0 <= x < 256 for x in v):
        return bytes(v)
    return None


def _read_keypair_file(path: Any) -> bytes:
    """Read the secret from a mode-600 ``solana-keygen`` file. Parse one flat array only, and no error carries
    the file's bytes."""
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

# The trading key text and the default gateway URL use this host. Another host derives another seat key.
HOST = "portal.crxfx.com"


def trading_key_text(wallet: str, host: str = HOST) -> bytes:
    """Return the message a wallet signs to derive its CRX trading key. The site signs the same text."""
    return (f"CRX trading key for {wallet} on {host} (Solana mainnet). "
            "Signing this creates your CRX trading key. It moves no funds and is not a login. "
            f"Sign it only on https://{host}.").encode()


def seat_secret(kp: Keypair, host: str = HOST) -> bytes:
    """Derive the secp256k1 seat key: keccak256 of the wallet's signature of ``trading_key_text``.

    The text is signed twice and must give the same signature. A counter byte is appended when the hash
    falls outside 1..n-1."""
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
    """Return the wire transaction with its one signature slot filled."""
    return bytes([1]) + kp.sign(parsed["message"]) + parsed["message"]


# Program errors for a full intake (RING_FULL, RING_USER_FULL): the transaction fails whole and no token moves.
RING_CODES = (660, 661)
RING_LINE = "no USDC left the wallet; the chain's intake is full until the hourly check; deposit again after it"
_CUSTOM_HEX = re.compile(r"custom program error:\s*0x([0-9a-fA-F]{1,8})\b")


def program_code(err: Any) -> int | None:
    """Return the custom program error code in an RPC error or transaction status, or None.

    Reads ``{"InstructionError": [i, {"Custom": n}]}`` first, then ``custom program error: 0x...`` in any
    text. Walks at most 2000 values."""
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
    """Build the TxFailed for a refused deposit. A full intake reads ``RING_LINE`` with reason ``intake_full``.
    Any other refusal reads as the node sent it, cut to ``limit`` characters."""
    code = program_code(err)
    if code in RING_CODES:
        return TxFailed(f"{lead}: {RING_LINE}", details={"tx": sig, "program_error": code, "reason": "intake_full"})
    return TxFailed(f"{lead}: {clean(err, limit)}", details={"tx": sig})


SEND_UNKNOWN_LINE = "deposit outcome unknown: check transaction {sig} on the chain before a new deposit"
EXPIRED_LINE = "deposit not sent: the transaction expired before it reached the chain; no USDC left the wallet"
STALE_LINE = "deposit not sent: the transaction expired before it was sent; no USDC left the wallet"
FOUND, ABSENT, UNREAD = "found", "absent", "unread"
# sendTransaction errors that prove the node did not take the transaction: failed preflight, bad signature,
# bad params. After any other error, status reads decide.
SURE_REFUSALS = (-32002, -32003, -32602)


def sig_status(rpc: Any, sig: str) -> tuple[str, dict | None]:
    """Read the status of ``sig``: (FOUND, row), (ABSENT, None), or (UNREAD, None) when the answer is unclear."""
    try:
        row = rpc("getSignatureStatuses", [sig], {"searchTransactionHistory": True})["value"][0]
    except Exception:
        return UNREAD, None
    if row is None:
        return ABSENT, None
    return (FOUND, row) if isinstance(row, dict) else (UNREAD, None)


def tx_blockhash(raw: bytes) -> str | None:
    """Return the recent blockhash of a wire transaction, or None when it does not parse."""
    try:
        return parse_tx(raw)["blockhash"]
    except (ValueError, IndexError, KeyError):
        return None


def blockhash_valid(rpc: Any, blockhash: str) -> tuple[bool | None, int | None]:
    """Read ``isBlockhashValid`` at confirmed commitment: (valid, slot), or (None, None) when unclear."""
    try:
        v = rpc("isBlockhashValid", blockhash, {"commitment": "confirmed"})
        ok, slot = v["value"], v["context"]["slot"]
    except Exception:
        return None, None
    if type(ok) is not bool or type(slot) is not int:
        return None, None
    return ok, slot


def expired_on_both(rpcs: tuple, sig: str, blockhash: str, known: int) -> tuple[str, dict | None]:
    """Ask each RPC whether ``sig`` landed before its blockhash expired.

    Return (FOUND, row) when an RPC holds ``sig``. Return (ABSENT, None) only when every RPC reads the
    blockhash expired at a slot above ``known`` and then holds no status for ``sig``. Else (UNREAD, None)."""
    verdict: tuple[str, dict | None] = (ABSENT, None)
    for r in rpcs:
        ok, slot = blockhash_valid(r, blockhash)
        if ok is not False or slot is None or slot <= known:
            verdict = (UNREAD, None)
            continue
        kind, row = sig_status(r, sig)
        if kind == FOUND:
            return kind, row
        if kind == UNREAD:
            verdict = (UNREAD, None)
    return verdict


def send_refused(err: Any) -> bool:
    """Return True when a sendTransaction error proves the node refused the transaction."""
    code = err.get("code") if isinstance(err, dict) else None
    return type(code) is int and code in SURE_REFUSALS


def send_and_confirm(rpc: Any, raw: bytes, *, check_rpc: Any = None, wait_s: float = 120,
                     sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> str:
    """Send a transaction once and poll until it is confirmed. Return the base58 signature.

    The transaction is sent only while its blockhash reads valid. Any answer to the send other than a sure
    refusal can leave it on the chain, so status reads decide the outcome. Raise TxFailed when the blockhash
    expires before the send, the send is refused, the program fails the transaction, or ``rpc`` and
    ``check_rpc`` both read the blockhash expired with no status for it. Raise SendUnknown, with the signature
    in ``details['tx']``, on any other end."""
    sig = b58encode(raw[1:65])
    blockhash = tx_blockhash(raw)
    known = None  # highest confirmed slot at which an RPC read the blockhash valid
    if blockhash is not None:
        ok, slot = blockhash_valid(rpc, blockhash)
        if ok is False:
            raise TxFailed(STALE_LINE, details={"tx": sig, "reason": "expired"})
        if ok:
            known = slot
    try:
        rpc("sendTransaction", base64.b64encode(raw).decode(),
            {"encoding": "base64", "preflightCommitment": "confirmed"})
    except RpcError as e:
        if send_refused(e.error):
            raise deposit_failed("deposit not sent", e.error, sig, 160) from None
    except Exception:
        pass  # no clear answer: the node may hold the transaction, so poll by signature
    rpcs = (rpc,) if check_rpc is None else (rpc, check_rpc)
    deadline = clock() + wait_s
    while True:
        kind, st = sig_status(rpc, sig)
        if kind != FOUND and blockhash is not None:
            ok, slot = blockhash_valid(rpc, blockhash)
            if ok:
                known = slot if known is None else max(known, slot)
            elif ok is False and known is not None and check_rpc is not None:
                kind, st = expired_on_both(rpcs, sig, blockhash, known)
                if kind == ABSENT:
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
    """Raise ConfigError when the network pins no genesis hash, program id or cluster tag."""
    if not (net.get("genesis_hash") and net.get("program_id") and net.get("cluster_tag")):
        raise ConfigError(UNPINNED)


def check_cluster(rpc: Any, net: dict, served: dict) -> dict:
    """Check the served Solana chain against the pinned one before the first signature, and return the chain
    row to sign with. Its ``core`` is the program alias, the domain's verifyingContract."""
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


STOPPED_LINE = "CRX removed this seat's access: no new trade; deposit() and withdraw() still work"


class SeatStopped(NotWhitelisted):
    """Raised when CRX has removed this seat's access.

    No trade opens and nothing is sent to the chain. ``bind``, ``deposit`` to the seat itself, ``balance``,
    ``positions`` and ``withdraw`` still work.
    """

    code = "seat_stopped"


def seat_stopped(status: int, body: Any) -> SeatStopped | None:
    """Return SeatStopped for a 403 ``not_whitelisted`` answer with outcome ``account_removed``, else None."""
    if (status == 403 and isinstance(body, dict) and gateway_code(body) == "not_whitelisted"
            and body.get("outcome") == "account_removed"):
        return SeatStopped(STOPPED_LINE, status=403, gateway_code="not_whitelisted",
                           details={"outcome": "account_removed"})
    return None


# Seconds bind() polls GET /bind. After an RPC fault the gateway reads the bind's end 2 to 3 minutes later.
BIND_WAIT = 240.0
BIND_POLL = 2.0  # seconds between reads
BIND_KEYS = ("authority", "payout_wallet", "payout_ata")
OTHER_PAYOUT_LINE = "this seat is bound to another payout wallet: the bind is permanent; contact CRX"
OTHER_AUTHORITY_LINE = "this seat is bound already, with another authority"
BIND_LIVE_LINE = "a bind of this seat is in progress: no other bind is taken before it ends; bind() reads its end"
BIND_FAILED_LINE = "the bind did not complete: this seat is not bound"
BIND_UNAVAILABLE_LINE = "service temporarily unavailable: the bind is not filed"


class SeatBoundOtherPayout(CrxError):
    """Raised when the seat is already bound to another payout wallet or payout account.

    A bind fixes the seat's payout permanently. ``details['bound']`` holds the keys on the chain, and
    ``details['filed']`` the keys this bind named.
    """

    code = "seat_bound_other_payout"


class BindInProgress(CrxError):
    """Raised when an earlier bind of this seat has not finished.

    The seat is not bound yet, and the bind has not failed. Call ``bind()`` again with the same keys to wait
    for it without a new signature. ``details['filed']`` holds that bind's keys, when known.
    """

    code = "bind_in_progress"


class BindFailed(CrxError):
    """Raised when a bind ended without binding the seat.

    ``details['error']`` holds the gateway's code, when it sent one.
    """

    code = "bind_failed"


def bind_keys(authority: str, payout_wallet: str, mint: str) -> dict:
    """Return the base58 keys a bind names: authority, payout wallet and its token account."""
    return {"authority": authority, "payout_wallet": payout_wallet, "payout_ata": ata(payout_wallet, mint)}


def _key_or_none(v: Any) -> bytes | None:
    try:
        return key(v)
    except ValueError:
        return None


def shown_keys(keys: Any) -> dict:
    """Return the bind keys of ``keys`` that are strings, cleaned for display."""
    return {k: clean(keys[k], 64) for k in BIND_KEYS if isinstance(keys, dict) and isinstance(keys.get(k), str)}


def same_keys(held: Any, filed: dict) -> bool:
    """Return True when ``held`` names the same three keys as ``filed``, compared as bytes."""
    return isinstance(held, dict) and all(_key_or_none(held.get(k)) == key(filed[k]) for k in BIND_KEYS)


def bind_end(held: Any, filed: dict) -> dict | None:
    """Judge a ``GET /bind`` answer against the bind ``filed``.

    Return None until the answer reads ``bound`` True. The status word decides nothing. Return ``held`` when
    its three keys match ``filed`` as 32-byte values. Raise SeatBoundOtherPayout for another payout, CrxError
    ``seat_already_bound`` for another authority, and BadAnswer for a key that is not 32 bytes of base58."""
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
    """Poll ``GET /bind`` until the bind ``filed`` ends, for at most ``wait`` seconds, and return the bound
    answer. A network or server error counts as no end yet. Raise BindFailed when the bind fails, and
    BindInProgress when it has not ended within ``wait``."""
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
    """Map a ``POST /bind`` refusal to SeatBoundOtherPayout, ServiceUnavailable or BindInProgress, else None."""
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
    """Post a bind and return the 200 or 202 answer, or None on 409 ``seat_already_bound``."""
    r = gw.raw_request("POST", "/bind", body=body)
    if r.status_code in (200, 202):
        return gw.parse(r, ok=(200, 202))
    served = gw.body_of(r)
    if (r.status_code, gateway_code(served)) == (409, "seat_already_bound"):
        return None
    raise bind_refused(r.status_code, served, filed) or gw.error_of(r, gw.host)


def _secs(v: Any) -> int | None:
    """Return a served limit in whole seconds, or None when it is not an int of 0 or more."""
    return v if type(v) is int and v >= 0 else None


def _utc(ms: int) -> str:
    """Format unix ms as ``YYYY-MM-DD HH:MM:SS UTC``, or as the number when it is out of range."""
    try:
        return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return str(ms)


def tenor_refusal(now_ms: int, tenor_secs: int, min_secs: Any, max_secs: Any, **kw: Any) -> BadRequest | None:
    """Return a BadRequest when ``tenor_secs`` after ``now_ms`` falls outside the tenor band, else None.

    A limit not given is not checked. ``details`` holds ``min_secs`` with ``earliest_expiry`` (unix ms), or
    ``max_secs`` with ``latest_expiry``."""
    lo, hi = _secs(min_secs), _secs(max_secs)
    if lo is not None and tenor_secs < lo:
        at = now_ms + lo * 1000  # the message rounds up to a whole second that meets the minimum
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
    """Raise BadRequest when ``expiry_ms`` is outside the pair's tenor band. ``now`` is unix seconds."""
    now_ms = int(now * 1000)
    err = tenor_refusal(now_ms, (expiry_ms - now_ms) // 1000, min_secs, max_secs)
    if err is not None:
        raise err


def tenor_refused(e: CrxError, sent: Any) -> BadRequest | None:
    """Rebuild a gateway 400 tenor refusal with the SDK's message, else None. The gateway's clock is the sent
    ``expiry`` less the served ``tenor_secs``."""
    d = e.details
    expiry, tenor = sent.get("expiry") if isinstance(sent, dict) else None, d.get("tenor_secs")
    if e.status != 400 or d.get("limit") != "tenor" or type(expiry) is not int or type(tenor) is not int:
        return None
    return tenor_refusal(expiry - tenor * 1000, tenor, d.get("min_secs"), d.get("max_secs"), status=400,
                         gateway_code=e.gateway_code)


class SeatGateway(Gateway):
    """Gateway that raises SeatStopped for a removed seat, and the SDK's tenor error for an RFQ out of band."""

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
    """Binder that raises SeatStopped for a removed seat's accept, even after a signature. The gateway refuses
    that accept before it reserves anything, so no trade can open."""

    _stopped: SeatStopped | None = None

    def post_accept(self, rfq_id: str, body: dict) -> Any:
        try:
            return super().post_accept(rfq_id, body)
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
