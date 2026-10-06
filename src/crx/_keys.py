"""Key loading. The key never leaves the eth-account object."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from eth_account import Account
from eth_account.signers.local import LocalAccount

from .errors import ConfigError

ENV_KEY = "CRX_WALLET_PK"
ENV_KEY_FILE = "CRX_WALLET_PK_FILE"


def load_account(key: str | bytes | None = None, key_file: str | os.PathLike | None = None) -> LocalAccount | None:
    """Return the seat account, or None when no key is set.

    Order: ``key``, then ``key_file``, then ``CRX_WALLET_PK``, then ``CRX_WALLET_PK_FILE``.
    """
    if key is not None and key_file is not None:
        key = None
        raise ConfigError("set key or key_file, not both")
    if key is None and key_file is None:
        key = os.environ.get(ENV_KEY) or None
        if key is None:
            key_file = os.environ.get(ENV_KEY_FILE) or None
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


def _read_key_file(path: Path) -> str:
    st = None
    try:
        st = path.stat()
    except OSError:
        pass
    if st is None:
        raise ConfigError("the key file cannot be read")
    if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError("the key file is open to other users; run chmod 600 on it")
    text = None
    try:
        text = path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        pass
    if text is None:
        # Raised outside the except block: no context carries the file's bytes.
        raise ConfigError("the key file cannot be read")
    return text
