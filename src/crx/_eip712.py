"""EIP-712 digests the seat signs, rebuilt locally from the fields."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

DOMAIN_TYPEHASH = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
LEG_TYPEHASH = keccak(
    text=(
        "Leg(address seat,bytes32 legId,bytes32 joinRef,bytes32 pair,"
        "uint8 instrumentId,int8 side,uint256 notional,uint64 rate,uint16 imBps,"
        "int16 premiumBps,uint40 expiry,uint64 nonce,uint64 quoteExpiry)"
    )
)
SIDE_TYPEHASH = keccak(
    text="Side(bytes32 pairC,bytes32 ownLegId,uint64 quoteExpiry,uint64 ownNonce,bytes32 ownSalt,bytes32 wrapsHash)"
)
WITHDRAW_TYPEHASH = keccak(
    text="WithdrawIntent(address account,uint256 amount,address recipient,uint64 nonce,uint64 deadline)"
)
ARM_WORDS = [
    "address", "bytes32", "bytes32", "uint8", "int8", "uint256",
    "uint64", "uint16", "int16", "uint64", "uint64", "uint64",
]
WITHDRAW_ITEM = ["address", "uint256", "address", "uint64", "uint64"]


def hx(value: str) -> bytes:
    """0x-hex to bytes."""
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("not 0x-hex")
    return bytes.fromhex(value[2:])


def h0x(value: bytes) -> str:
    return "0x" + bytes(value).hex()


def pair_id(pair: str) -> bytes:
    """keccak256 of the slash form, e.g. ``USD/BRL``."""
    return keccak(text=pair)


def scaled6(value: Any) -> int:
    """A decimal string at 6 decimals, as an integer. Refuses finer precision."""
    d = Decimal(str(value)).scaleb(6)
    if d != d.to_integral_value():
        raise ValueError("more than 6 decimals")
    return int(d)


def domain_separator(chain_id: int, core: str) -> bytes:
    return keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [DOMAIN_TYPEHASH, keccak(text="CRX"), keccak(text="rulebook-1.0"), int(chain_id), to_checksum_address(core)],
        )
    )


def leg_struct_hash(a: dict) -> bytes:
    return keccak(
        encode(
            ["bytes32", "address", "bytes32", "bytes32", "bytes32", "uint8", "int8",
             "uint256", "uint64", "uint16", "int16", "uint40", "uint64", "uint64"],
            [
                LEG_TYPEHASH, to_checksum_address(a["seat"]), hx(a["leg_id"]), hx(a["join_ref"]), hx(a["pair_id"]),
                int(a["instrument_id"]), int(a["side"]), scaled6(a["notional"]), scaled6(a["rate"]),
                int(a["im_bps"]), int(a["premium_bps"]), int(a["expiry"]) // 1000, int(a["nonce"]),
                int(a["quote_expiry"]) // 1000,
            ],
        )
    )


def leg_digest(separator: bytes, a: dict) -> bytes:
    return keccak(b"\x19\x01" + separator + leg_struct_hash(a))


def arm_words(a: dict, nonce: Any, quote_expiry: Any) -> list:
    """The twelve words a half commits to: 6-dp notional and rate, unix-second times."""
    return [
        to_checksum_address(a["seat"]), hx(a["leg_id"]), hx(a["pair_id"]), int(a["instrument_id"]), int(a["side"]),
        scaled6(a["notional"]), scaled6(a["rate"]), int(a["im_bps"]), int(a["premium_bps"]),
        int(a["expiry"]) // 1000, int(nonce), int(quote_expiry),
    ]


def half_commitment(words: list, salt: str) -> bytes:
    return keccak(encode(ARM_WORDS, words) + hx(salt))


def pair_commitment(c_taker: bytes, c_maker: bytes) -> bytes:
    return keccak(b"\x03" + c_taker + c_maker)


def side_digest(separator: bytes, t: dict) -> bytes:
    struct = keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint64", "uint64", "bytes32", "bytes32"],
            [SIDE_TYPEHASH, hx(t["pair_c"]), hx(t["own_leg_id"]), int(t["quote_expiry"]), int(t["own_nonce"]),
             hx(t["own_salt"]), hx(t["wraps_hash"])],
        )
    )
    return keccak(b"\x19\x01" + separator + struct)


def withdraw_fields(w: dict) -> list:
    return [
        to_checksum_address(w["account"]), int(w["amount"]), to_checksum_address(w["recipient"]),
        int(w["nonce"]), int(w["deadline"]),
    ]


def withdraw_digest(separator: bytes, w: dict) -> bytes:
    struct = keccak(encode(["bytes32"] + WITHDRAW_ITEM, [WITHDRAW_TYPEHASH, *withdraw_fields(w)]))
    return keccak(b"\x19\x01" + separator + struct)


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def calldata(signature: str, types: list, args: list) -> str:
    return h0x(selector(signature) + encode(types, args))
