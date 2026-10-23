"""Sign the typed data and login messages the SDK builds.

A custodian signer is any object with:

- ``address``: the seat wallet, 0x hex.
- ``sign_typed_data(typed_data)``: return an ``eth_signTypedData_v4`` signature of the object.
- ``sign_message(message)``: return an EIP-191 (``personal_sign``) signature of the ``message`` bytes.

A signer that signs only a hash (KMS, raw MPC) has ``sign_hash(digest)`` in place of
``sign_typed_data``. The SDK checks the digest against its own typed data first.

Each method returns 65 bytes ``r ‖ s ‖ v``, as bytes or 0x hex. The SDK refuses a signature
that does not recover to ``address``.
"""

from __future__ import annotations

import copy
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount

from . import _eip712 as e7
from .errors import ConfigError, RefusedToSign


class LocalSigner:
    """Sign typed data and EIP-191 messages with a local private key."""

    def __init__(self, account: LocalAccount) -> None:
        self._account = account

    @property
    def address(self) -> str:
        return self._account.address.lower()

    def __repr__(self) -> str:
        return f"LocalSigner({self.address})"

    def __getstate__(self) -> Any:
        raise TypeError("a LocalSigner holds a key and cannot be pickled or copied")

    def sign_typed_data(self, typed_data: dict) -> bytes:
        return bytes(self._account.sign_typed_data(full_message=typed_data).signature)

    def sign_message(self, message: bytes) -> bytes:
        return bytes(self._account.sign_message(encode_defunct(primitive=bytes(message))).signature)


def as_signer(obj: Any) -> Any:
    """Wrap a LocalAccount, or check that ``obj`` has the signer methods."""
    if obj is None or isinstance(obj, LocalAccount):
        return None if obj is None else LocalSigner(obj)
    try:
        e7.address(getattr(obj, "address", None))
    except ValueError:
        raise ConfigError("the signer has no 0x address") from None
    typed = callable(getattr(obj, "sign_typed_data", None)) or callable(getattr(obj, "sign_hash", None))
    if not (typed and callable(getattr(obj, "sign_message", None))):
        raise ConfigError("a signer needs sign_message, and sign_typed_data or sign_hash")
    return obj


def recover(digest: bytes, sig: bytes) -> str:
    """Return the lower-case address that signed ``digest``."""
    return Account._recover_hash(digest, signature=sig).lower()


def sign_hash_only(signer: Any, td: dict, digest: bytes) -> Any:
    """Sign ``digest`` with ``signer.sign_hash`` after checking it is the digest of ``td``.

    Raises:
        RefusedToSign: ``digest`` is not the digest of ``td``.
    """
    if e7.typed_digest(td) != digest:
        raise RefusedToSign("refused to sign: the digest is not this typed data's")
    return signer.sign_hash(digest)


def sign_typed(signer: Any, td: dict, digest: bytes, seat: str) -> str:
    """Sign ``td`` and return 0x hex. Raise RefusedToSign unless the signature recovers to ``seat``."""
    try:
        if callable(getattr(signer, "sign_typed_data", None)):
            raw = signer.sign_typed_data(copy.deepcopy(td))
        else:
            raw = sign_hash_only(signer, td, digest)
        sig = e7.normalize_sig(raw)
        who = recover(digest, sig)
    except RefusedToSign:
        raise
    except Exception as e:  # noqa: BLE001 - a custodian's failure: nothing was sent
        why = type(e).__name__
    else:
        why = None
    if why is not None:
        raise RefusedToSign(f"refused to sign: the signer gave no usable signature ({why})")
    if who != seat.lower():
        raise RefusedToSign("refused to sign: the signature does not recover to the seat")
    return e7.h0x(sig)


def sign_login(signer: Any, message: str) -> str:
    """Return the EIP-191 signature of a login message as 0x hex."""
    try:
        sig = e7.normalize_sig(signer.sign_message(message.encode()))
    except Exception as e:  # noqa: BLE001 - a custodian's failure: nothing was sent
        why = type(e).__name__
    else:
        return e7.h0x(sig)
    raise RefusedToSign(f"the signer did not sign the gateway login ({why}); nothing sent")


__all__ = ["LocalSigner", "sign_hash_only"]
