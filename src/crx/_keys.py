"""Load the seat key. The key never leaves the eth-account object."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Callable

from eth_account import Account
from eth_account.signers.local import LocalAccount

from .errors import ConfigError

ENV_KEY = "CRX_WALLET_PK"
ENV_KEY_FILE = "CRX_WALLET_PK_FILE"


def load_account(key: str | bytes | None = None, key_file: str | os.PathLike | None = None,
                 env: Callable[[str], str | None] | None = None) -> LocalAccount | None:
    """Return the seat account, or None when no key is set.

    Order: ``key``, ``key_file``, ``CRX_WALLET_PK``, then ``CRX_WALLET_PK_FILE``. ``env`` reads the two
    settings (default: the environment).
    """
    getenv = env if env is not None else os.environ.get
    if key is not None and key_file is not None:
        key = None
        raise ConfigError("set key or key_file, not both")
    if key is None and key_file is None:
        key = getenv(ENV_KEY) or None
        if key is None:
            key_file = getenv(ENV_KEY_FILE) or None
    if key is None and key_file is not None:
        key = _read_key_file(Path(key_file).expanduser())
    if key is None:
        return None
    account = None
    if isinstance(key, str):
        key = key.strip()
        body = key[2:] if key[:2] in ("0x", "0X") else key
        if len(body) != 64:
            key = body = None
            raise ConfigError("the key is not a valid private key: it needs 64 hex characters")
        body = None
    elif not isinstance(key, bytes) or len(key) != 32:
        key = None
        raise ConfigError("the key is not a valid private key: it needs 32 bytes")
    try:
        account = Account.from_key(key)
    except Exception:  # noqa: BLE001 - the original exception can quote the key; it is dropped
        pass
    key = None
    if account is None:
        raise ConfigError("the key is not a valid private key")
    return account


OPEN_TO_OTHERS = "the key file is open to other users; run chmod 600 on it"


def owner_only(fd: int, st: os.stat_result, path: os.PathLike | str) -> bool:
    """Make the open file ``fd`` (``st`` is its fstat) owner-only. True when it is owner-only now.

    POSIX only. A regular file that group or other users can open is set to 0600 when the current user
    owns it, and one line goes to stderr. False when another user owns it, when it is no regular file, or
    when its mode does not change.
    """
    if os.name != "posix" or not st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return True
    if st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode):
        return False
    done = False
    try:
        os.fchmod(fd, 0o600)
        done = not os.fstat(fd).st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    except OSError:
        pass
    if done:
        print(f"set {os.fspath(path)} to owner-only (0600)", file=sys.stderr)
    return done


def _read_key_file(path: Path) -> str:
    """The key file's text. The mode check and the read use one open file: the read comes after the check."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        pass
    if fd is None:
        st = None
        try:
            st = path.stat()
        except OSError:
            pass
        if st is not None and os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError(OPEN_TO_OTHERS)
        raise ConfigError("the key file cannot be read")
    private = None
    text = None
    try:
        private = owner_only(fd, os.fstat(fd), path)
        if private:
            with os.fdopen(fd, "rb", closefd=False) as f:
                text = f.read().decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        pass
    finally:
        os.close(fd)
    if private is False:
        raise ConfigError(OPEN_TO_OTHERS)
    if text is None:
        # Raised outside the except block: no context carries the file's bytes.
        raise ConfigError("the key file cannot be read")
    return text
