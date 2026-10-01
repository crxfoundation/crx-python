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
QUOTE_TYPE = (
    "Quote(address seat,bytes32 legId,bytes32 pair,uint8 instrumentId,int8 side,uint256 notional,uint64 rate,"
    "uint16 imBps,int16 premiumBps,uint40 expiry,uint64 nonce,uint64 quoteExpiry,bytes32 salt,bytes32 wrapsHash,"
    "bytes32 takerRef)"
)
QUOTE_TYPEHASH = keccak(text=QUOTE_TYPE)
EMPTY_WRAPS_HASH = bytes.fromhex("569e75fc77c1a856f6daaf9e69d8a9566ca34aa47f9133711ce065a571af0cfd")  # no wraps
WITHDRAW_TYPEHASH = keccak(
    text="WithdrawIntent(address account,uint256 amount,address recipient,uint64 nonce,uint64 deadline)"
)
ARM_WORDS = [
    "address", "bytes32", "bytes32", "uint8", "int8", "uint256",
    "uint64", "uint16", "int16", "uint64", "uint64", "uint64",
]
WITHDRAW_ITEM = ["address", "uint256", "address", "uint64", "uint64"]
WITHDRAW_KIND = 5


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


def leg_id_for(random24: bytes, quote_expiry: int) -> str:
    """A binding quote's leg id: 24 random bytes, then ``quote_expiry`` (unix s) as 8 bytes big-endian."""
    if len(random24) != 24:
        raise ValueError("the leg id takes 24 random bytes")
    return h0x(random24 + int(quote_expiry).to_bytes(8, "big"))


def leg_id_tail(leg_id: str) -> int:
    """The ``quote_expiry`` (unix s) a binding quote's leg id ends with."""
    return int.from_bytes(hx(leg_id)[-8:], "big")


def quote_struct_hash(words: list, salt: str, taker_ref: str) -> bytes:
    """The ``Quote`` struct hash: the twelve arm words, the salt, the empty wrap set, the RFQ's taker_ref."""
    return keccak(
        encode(
            ["bytes32", *ARM_WORDS, "bytes32", "bytes32", "bytes32"],
            [QUOTE_TYPEHASH, *words, hx(salt), EMPTY_WRAPS_HASH, hx(taker_ref)],
        )
    )


def quote_digest(separator: bytes, words: list, salt: str, taker_ref: str) -> bytes:
    return keccak(b"\x19\x01" + separator + quote_struct_hash(words, salt, taker_ref))


def withdraw_fields(w: dict) -> list:
    return [
        to_checksum_address(w["account"]), int(w["amount"]), to_checksum_address(w["recipient"]),
        int(w["nonce"]), int(w["deadline"]),
    ]


def withdraw_digest(separator: bytes, w: dict) -> bytes:
    struct = keccak(encode(["bytes32"] + WITHDRAW_ITEM, [WITHDRAW_TYPEHASH, *withdraw_fields(w)]))
    return keccak(b"\x19\x01" + separator + struct)


def withdraw_item(w: dict) -> bytes:
    """The chain item id of a withdraw intent: keccak256 of kind 5 and the five fields, ABI-encoded."""
    return keccak(encode(["uint256"] + WITHDRAW_ITEM, [WITHDRAW_KIND, *withdraw_fields(w)]))


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def calldata(signature: str, types: list, args: list) -> str:
    return h0x(selector(signature) + encode(types, args))
