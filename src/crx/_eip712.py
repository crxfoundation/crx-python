"""EIP-712 digests and typed data the seat signs, rebuilt locally from the fields (SPEC v5)."""

from __future__ import annotations

import re
from decimal import Context, Decimal
from typing import Any

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

# Switches (SPEC v5 §9). On: the v5 format. Off: the member goes back in its v4 place.
# drop_quote_expiry: no `quoteExpiry` in Trade, Quote and the Leg words.
DROP_QUOTE_EXPIRY = True
# drop_withdraw_recipient: no `recipient` in WithdrawIntent.
DROP_WITHDRAW_RECIPIENT = True
# derive_maker_nonce: the Quote nonce is the u64, big endian, of leg_id bytes 16..24; the body sends no nonce.
DERIVE_MAKER_NONCE = True

DOMAIN_TYPEHASH = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
DOMAIN_FIELDS = (
    ("name", "string"), ("version", "string"), ("chainId", "uint256"), ("verifyingContract", "address"),
)
# The Solana domain: 3 fields, no chainId. verifyingContract is the program alias (see _solana.alias).
# A chain id of None selects it in every function below.
SOLANA_DOMAIN_TYPEHASH = keccak(text="EIP712Domain(string name,string version,address verifyingContract)")
SOLANA_DOMAIN_FIELDS = (("name", "string"), ("version", "string"), ("verifyingContract", "address"))
MAX_MATURITY = 253402300799  # 9999-12-31T23:59:59Z
SUMMARY_MAX_LEN = 162
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
TAKER_REF_TAG = b"CRX/takerRef/v1"
_EXACT = Context(prec=100)  # wide enough for a u256 word: no rounding
WITHDRAW_KIND = 5

TERMS_FIELDS = (
    ("takerSide", "bytes32"), ("makerSide", "bytes32"), ("notional", "uint256"), ("premiumBps", "int16"),
    ("nonce", "uint64"), ("pair", "bytes32"), ("side", "int8"), ("settlement", "uint40"), ("rate", "uint64"),
)
ALLOCATION_CONSENT_FIELDS = (
    ("oldId", "bytes32"), ("exitingSide", "bytes32"), ("remainingSide", "bytes32"), ("incomingSide", "bytes32"),
    ("nonce", "uint64"), ("deadline", "uint64"), ("commitment", "bytes32"), ("salt", "bytes32"),
)
FAILOVER_CONSENT_FIELDS = (
    ("oldId", "bytes32"), ("closedOutSide", "bytes32"), ("remainingSide", "bytes32"), ("incomingSide", "bytes32"),
    ("incomingC", "bytes32"), ("nonce", "uint64"), ("openNonce", "uint64"), ("deadline", "uint64"),
    ("commitment", "bytes32"),
)
# The seat's bind on Solana: authority and payout are 32-byte Ed25519 keys (payout = the wallet, not its ATA).
BIND_SEAT_FIELDS = (
    ("seat", "address"), ("authority", "bytes32"), ("payout", "bytes32"), ("nonce", "uint64"), ("deadline", "uint64"),
)
ALLOCATION_ACCEPTANCE_FIELDS = (
    ("oldId", "bytes32"), ("incomingSide", "bytes32"), ("incomingC", "bytes32"), ("nonce", "uint64"),
    ("deadline", "uint64"), ("commitment", "bytes32"),
)
# Hidden words (SPEC v5 §4): committed as keccak256(abi.encode(words) ‖ salt), never signed as a struct.
ALLOCATION_ITEM_FIELDS = (
    ("oldId", "bytes32"), ("exitingSide", "bytes32"), ("remainingSide", "bytes32"), ("incoming", "address"),
    ("incomingSide", "bytes32"), ("closeRate", "uint256"), ("spread", "uint256"), ("nonce", "uint64"),
    ("deadline", "uint64"), ("pairId", "bytes32"), ("side", "int8"), ("notional", "uint256"),
    ("settlement", "uint40"),
)
CLOSEOUT_ITEM_FIELDS = (
    ("oldId", "bytes32"), ("closedOutSide", "bytes32"), ("remainingSide", "bytes32"), ("incoming", "address"),
    ("incomingSide", "bytes32"), ("feedId", "bytes32"), ("closeTime", "uint64"), ("spread", "uint256"),
    ("nonce", "uint64"), ("deadline", "uint64"), ("openNonce", "uint64"), ("subsidyMaxUsd", "uint256"),
)


# ---------- the member lists, under the switches ----------

def leg_fields() -> list[tuple[str, str]]:
    """The hidden words of a Leg half (the C half), in order: 9 in v5."""
    f = [("seat", "address"), ("legId", "bytes32"), ("pair", "bytes32"), ("side", "int8"), ("notional", "uint256"),
         ("rate", "uint64"), ("premiumBps", "int16"), ("expiry", "uint40"), ("nonce", "uint64")]
    return f if DROP_QUOTE_EXPIRY else f + [("quoteExpiry", "uint64")]


def trade_fields() -> list[tuple[str, str]]:
    """The ``Trade`` members, in order: 11 in v5."""
    head = [("summary", "string"), ("pair", "string"), ("side", "string"), ("notionalE6", "uint256"),
            ("rateE6", "uint64"), ("premiumBps", "int16"), ("maturity", "uint40"), ("pairC", "bytes32"),
            ("ownLegId", "bytes32")]
    return head + ([] if DROP_QUOTE_EXPIRY else [("quoteExpiry", "uint64")]) + [("ownNonce", "uint64"),
                                                                              ("ownSalt", "bytes32")]


def quote_fields() -> list[tuple[str, str]]:
    """The ``Quote`` members: the Leg words, ``salt``, ``takerRef``."""
    return leg_fields() + [("salt", "bytes32"), ("takerRef", "bytes32")]


def withdraw_fields_of() -> list[tuple[str, str]]:
    """The ``WithdrawIntent`` members: 4 in v5."""
    f = [("account", "address"), ("amount", "uint256"), ("recipient", "address"), ("nonce", "uint64"),
         ("deadline", "uint64")]
    return [m for m in f if not (DROP_WITHDRAW_RECIPIENT and m[0] == "recipient")]


def structs() -> dict[str, list[tuple[str, str]]]:
    """Every struct this module hashes, by primary type, under the current switches."""
    return {
        "Trade": trade_fields(), "Quote": quote_fields(), "WithdrawIntent": withdraw_fields_of(),
        "Terms": list(TERMS_FIELDS), "AllocationConsent": list(ALLOCATION_CONSENT_FIELDS),
        "FailoverConsent": list(FAILOVER_CONSENT_FIELDS), "AllocationAcceptance": list(ALLOCATION_ACCEPTANCE_FIELDS),
    }


def solana_structs() -> dict[str, list[tuple[str, str]]]:
    """Structs signed only under the 3-field Solana domain."""
    return {"BindSeat": list(BIND_SEAT_FIELDS)}


def type_string(primary: str) -> str:
    """The EIP-712 type string of a known struct, e.g. ``Trade(string summary,...)``."""
    return primary + "(" + ",".join(f"{t} {n}" for n, t in fields_of(primary)) + ")"


def typehash(primary: str) -> bytes:
    return keccak(text=type_string(primary))


def fields_of(primary: str) -> list[tuple[str, str]]:
    """(name, type) of each member of a known struct, in order. KeyError for any other name."""
    s = structs()
    return s[primary] if primary in s else solana_structs()[primary]


# ---------- small helpers ----------

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
    """A decimal string at 6 decimals, as an exact integer. Refuses finer precision."""
    d = Decimal(str(value)).scaleb(6, _EXACT)
    if d != d.to_integral_value():
        raise ValueError("more than 6 decimals")
    return int(d)


def domain_fields(chain_id: int | None) -> tuple:
    """The domain members: 4 with a chain id, the 3 Solana members with None."""
    return SOLANA_DOMAIN_FIELDS if chain_id is None else DOMAIN_FIELDS


def domain_separator(chain_id: int | None, core: str) -> bytes:
    """The domain separator of a chain id and core; with ``chain_id`` None, the 3-field Solana domain of the
    program alias ``core``."""
    if chain_id is None:
        return keccak(encode(
            ["bytes32", "bytes32", "bytes32", "address"],
            [SOLANA_DOMAIN_TYPEHASH, keccak(text="CRX"), keccak(text="rulebook-1.0"), to_checksum_address(core)],
        ))
    return keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [DOMAIN_TYPEHASH, keccak(text="CRX"), keccak(text="rulebook-1.0"), int(chain_id), to_checksum_address(core)],
        )
    )


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def calldata(signature: str, types: list, args: list) -> str:
    return h0x(selector(signature) + encode(types, args))


# ---------- leg ids, the Quote and the hidden words ----------

def leg_id_for(random24: bytes, quote_end: int) -> str:
    """A leg id: 24 random bytes, then the quote end (unix s) as 8 bytes big endian."""
    if len(random24) != 24:
        raise ValueError("the leg id takes 24 random bytes")
    return h0x(random24 + int(quote_end).to_bytes(8, "big"))


def leg_id_tail(leg_id: str) -> int:
    """The quote end (unix s) a leg id ends with: bytes 24 to 31, big endian."""
    b = hx(leg_id)
    if len(b) != 32:
        raise ValueError("a leg id is 32 bytes")
    return int.from_bytes(b[24:], "big")


def maker_nonce(leg_id: str) -> int:
    """The Quote nonce a leg id gives: the u64, big endian, of bytes 16 to 23."""
    b = hx(leg_id)
    if len(b) != 32:
        raise ValueError("a leg id is 32 bytes")
    return int.from_bytes(b[16:24], "big")


def taker_ref(taker_seat: str, taker_leg_id: str) -> bytes:
    """keccak256("CRX/takerRef/v1" ‖ taker seat ‖ taker leg id), packed: the RFQ's ``taker_ref``."""
    return keccak(TAKER_REF_TAG + hx(address(taker_seat)) + hx(taker_leg_id))


def leg_words(half: dict) -> list:
    """The Leg words of a half, in ``leg_fields`` order: 6-decimal notional and rate, unix-second expiry.

    ``half``: ``seat``, ``leg_id``, ``pair_id``, ``side``, ``notional`` and ``rate`` (decimal strings),
    ``premium_bps``, ``expiry`` (unix ms), ``nonce``; ``quote_expiry`` (unix s) with drop_quote_expiry off.
    """
    words = [
        to_checksum_address(half["seat"]), hx(half["leg_id"]), hx(half["pair_id"]), int(half["side"]),
        scaled6(half["notional"]), scaled6(half["rate"]), int(half["premium_bps"]), int(half["expiry"]) // 1000,
        int(half["nonce"]),
    ]
    return words if DROP_QUOTE_EXPIRY else words + [int(half["quote_expiry"])]


def half_commitment(words: list, salt: str) -> bytes:
    """A Leg half's commitment: keccak256(abi.encode(Leg words) ‖ salt)."""
    return keccak(encode([t for _, t in leg_fields()], words) + hx(salt))


def pair_commitment(c_taker: bytes, c_maker: bytes) -> bytes:
    return keccak(b"\x03" + c_taker + c_maker)


def hidden_commitment(fields: Any, message: dict, salt: str) -> bytes:
    """keccak256(abi.encode(words) ‖ salt) of hidden words in their JSON form (an AllocationItem, a CloseoutItem)."""
    kinds, words = [], []
    for name, kind in fields:
        v = word_of(kind, message[name])
        kinds.append(kind)
        words.append(to_checksum_address(v) if kind == "address" else v)
    return keccak(encode(kinds, words) + hx(salt))


def quote_struct_hash(words: list, salt: str, taker_ref_: str) -> bytes:
    """The ``Quote`` struct hash: the typehash, the Leg words, the salt, the RFQ's taker_ref."""
    return keccak(
        encode(
            ["bytes32", *[t for _, t in leg_fields()], "bytes32", "bytes32"],
            [typehash("Quote"), *words, hx(salt), hx(taker_ref_)],
        )
    )


def quote_digest(separator: bytes, words: list, salt: str, taker_ref_: str) -> bytes:
    return keccak(b"\x19\x01" + separator + quote_struct_hash(words, salt, taker_ref_))


def quote_message(words: list, salt: str, taker_ref_: str) -> dict:
    """The ``Quote`` message of Leg words, JSON form."""
    msg = {}
    for (name, kind), v in zip(leg_fields(), words):
        msg[name] = v.lower() if kind == "address" else h0x(v) if kind == "bytes32" else str(v)
    msg["salt"] = h0x(hx(salt))
    msg["takerRef"] = h0x(hx(taker_ref_))
    return msg


def terms_id(message: dict) -> bytes:
    """The Terms id (unsigned): the ``Terms`` struct hash of its JSON message."""
    return struct_hash("Terms", message)


# ---------- the withdraw intent ----------

def withdraw_fields(w: dict) -> list:
    """The intent's words: account, amount, nonce, deadline (and the recipient with its switch off)."""
    vals = {
        "account": to_checksum_address(w["account"]), "amount": int(w["amount"]), "nonce": int(w["nonce"]),
        "deadline": int(w["deadline"]),
    }
    if not DROP_WITHDRAW_RECIPIENT:
        vals["recipient"] = to_checksum_address(w["recipient"])
    return [vals[n] for n, _ in withdraw_fields_of()]


def withdraw_digest(separator: bytes, w: dict) -> bytes:
    kinds = [t for _, t in withdraw_fields_of()]
    struct = keccak(encode(["bytes32", *kinds], [typehash("WithdrawIntent"), *withdraw_fields(w)]))
    return keccak(b"\x19\x01" + separator + struct)


def withdraw_item(w: dict) -> bytes:
    """The chain item id of a withdraw intent: keccak256(uint256(5) ‖ abi.encode(the intent's words))."""
    kinds = [t for _, t in withdraw_fields_of()]
    return keccak(encode(["uint256", *kinds], [WITHDRAW_KIND, *withdraw_fields(w)]))


def withdraw_message(w: dict) -> dict:
    """The ``WithdrawIntent`` message, JSON form."""
    msg = {"account": address(w["account"]), "amount": str(int(w["amount"]))}
    if not DROP_WITHDRAW_RECIPIENT:
        msg["recipient"] = address(w["recipient"])
    msg.update(nonce=str(int(w["nonce"])), deadline=str(int(w["deadline"])))
    return msg


# ---------- exact amounts ----------

_DECIMAL = re.compile(r"([0-9]+)(?:\.([0-9]+))?")
_INT = re.compile(r"-?[0-9]+")
_HEX = re.compile(r"0x[0-9a-fA-F]*")


def e6(value: Any, bits: int = 128) -> int:
    """An exact decimal amount as an integer at 6 decimals.

    Takes a decimal string, an int or a finite Decimal. Refuses a float, a sign, more
    than 6 decimals, and a result at or above ``2**bits``.
    """
    if isinstance(value, float):
        raise ValueError("an amount is a decimal string, not a float")
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("the amount is not finite")
        value = format(value, "f")
    m = _DECIMAL.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        raise ValueError("the amount is not an unsigned decimal")
    frac = m[2] or ""
    if len(frac) > 6:
        raise ValueError("the amount has more than 6 decimals")
    v = int(m[1]) * 10**6 + int(frac.ljust(6, "0"))
    if v >= 1 << bits:
        raise ValueError(f"the amount is at or above 2^{bits}")
    return v


# ---------- the Trade summary (integers only) ----------

def pair_text_ok(pair: Any) -> bool:
    """True for exactly ``AAA/BBB``: three ASCII capitals, ``/``, three ASCII capitals."""
    return (isinstance(pair, str) and len(pair) == 7 and pair[3] == "/"
            and all("A" <= ch <= "Z" for ch in pair[:3] + pair[4:]))


def _amount_text(v: int, group: bool) -> str:
    whole, frac = divmod(v, 10**6)
    s = str(whole)
    if group:
        head = len(s) % 3 or 3
        s = " ".join([s[:head]] + [s[i:i + 3] for i in range(head, len(s), 3)])
    return s + ("." + f"{frac:06d}".rstrip("0") if frac else "")


def _pct_text(bps: int) -> str:
    a = abs(bps)
    return f"{a // 100}.{a % 100:02d}"


def _date_text(ts: int) -> str:
    """Unix seconds as ``YYYY-MM-DDTHH:MM:SSZ``: the civil date by integer arithmetic (Hinnant)."""
    days, secs = divmod(ts, 86_400)
    z = days + 719_468
    era = z // 146_097
    doe = z - era * 146_097
    yoe = (doe - doe // 1460 + doe // 36_524 - doe // 146_096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    year = yoe + era * 400 + (1 if month <= 2 else 0)
    return f"{year:04d}-{month:02d}-{day:02d}T{secs // 3600:02d}:{secs // 60 % 60:02d}:{secs % 60:02d}Z"


def _int_in(value: Any, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{what} is out of range")
    return value


def side_text(side: Any) -> str:
    """+1 is ``buy``, -1 is ``sell``: the taker's side of the first currency."""
    if side == 1 and not isinstance(side, bool):
        return "buy"
    if side == -1 and not isinstance(side, bool):
        return "sell"
    raise ValueError("the side is not buy or sell")


def trade_summary(pair: str, side: int, notional_e6: int, rate_e6: int, premium_bps: int, maturity: int) -> str:
    """The ``summary`` line of a ``Trade``. Raises ValueError on a value the guest refuses or cannot format."""
    word = side_text(side)
    _int_in(maturity, 0, MAX_MATURITY, "the maturity")
    if not pair_text_ok(pair):
        raise ValueError("the pair is not AAA/BBB")
    _int_in(notional_e6, 0, (1 << 128) - 1, "the notional")
    _int_in(rate_e6, 0, (1 << 64) - 1, "the rate")
    _int_in(premium_bps, -(1 << 15), (1 << 15) - 1, "the premium")
    base, quote = pair[:3], pair[4:]
    up = ("none" if premium_bps == 0 else
          f"{'taker' if premium_bps > 0 else 'maker'} pays {_pct_text(premium_bps)} %")
    return (f"{word} {_amount_text(notional_e6, True)} {base} vs {quote} at {_amount_text(rate_e6, False)} {quote} "
            f"per {base}, matures {_date_text(maturity)}, upfront {up}")


def v1_side(side: Any, pair: Any) -> str:
    """A ``/v1`` side as the ``Trade`` side. ``buy_usd`` and ``sell_usd`` name USD: valid only when USD is the
    pair's first currency."""
    if not pair_text_ok(pair) or pair[:3] != "USD":
        raise ValueError("a /v1 side names USD: the pair's first currency must be USD")
    if side == "buy_usd":
        return "buy"
    if side == "sell_usd":
        return "sell"
    raise ValueError("a /v1 side is buy_usd or sell_usd")


# ---------- typed data ----------

def domain_json(chain_id: int | None, core: str) -> dict:
    """The CRX domain of one chain and core, in its JSON form. No ``chainId`` with ``chain_id`` None."""
    if chain_id is None:
        return {"name": "CRX", "version": "rulebook-1.0", "verifyingContract": address(core)}
    return {"name": "CRX", "version": "rulebook-1.0", "chainId": int(chain_id), "verifyingContract": address(core)}


def typed_data(primary: str, chain_id: int | None, core: str, message: dict) -> dict:
    """The ``eth_signTypedData_v4`` object of one struct: ``types`` with ``EIP712Domain``, the domain of
    ``chain_id`` and ``core``, and ``message`` in its JSON form."""
    return {
        "types": {
            "EIP712Domain": [{"name": n, "type": t} for n, t in domain_fields(chain_id)],
            primary: [{"name": n, "type": t} for n, t in fields_of(primary)],
        },
        "primaryType": primary,
        "domain": domain_json(chain_id, core),
        "message": message,
    }


def address(value: Any) -> str:
    """A 20-byte address, lower case."""
    if not isinstance(value, str) or len(value) != 42 or not _HEX.fullmatch(value):
        raise ValueError("not a 20-byte address")
    return value.lower()


def word_of(kind: str, value: Any) -> Any:
    """One member's value, parsed: an int, 32 bytes, a lower-case address or a string. Raises ValueError."""
    if kind == "string":
        if not isinstance(value, str):
            raise ValueError("not a string")
        return value
    if kind == "address":
        return address(value)
    if kind == "bytes32":
        if not isinstance(value, str) or len(value) != 66 or not _HEX.fullmatch(value):
            raise ValueError("not 32 bytes of hex")
        return bytes.fromhex(value[2:])
    m = re.fullmatch(r"(u?)int([0-9]+)", kind)
    if m is None:
        raise ValueError(f"no rule for {kind}")
    if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and _INT.fullmatch(value))):
        raise ValueError("not an integer")
    v, bits = int(value), int(m[2])
    lo, hi = (0, (1 << bits) - 1) if m[1] else (-(1 << (bits - 1)), (1 << (bits - 1)) - 1)
    if not lo <= v <= hi:
        raise ValueError(f"out of {kind} range")
    return v


def struct_hash(primary: str, message: dict) -> bytes:
    """``hashStruct`` of a flat struct: strings hashed, every other member as its ABI word."""
    fields = fields_of(primary)
    if not isinstance(message, dict) or set(message) != {n for n, _ in fields}:
        raise ValueError(f"the {primary} message does not have its members")
    kinds, words = ["bytes32"], [typehash(primary)]
    for name, kind in fields:
        v = word_of(kind, message[name])
        if kind == "string":
            kinds.append("bytes32")
            words.append(keccak(v.encode("utf-8")))
        else:
            kinds.append(kind)
            words.append(to_checksum_address(v) if kind == "address" else v)
    return keccak(encode(kinds, words))


def typed_digest(td: dict) -> bytes:
    """The EIP-712 digest of a typed-data object this module knows. Raises ValueError on any other shape."""
    primary = td.get("primaryType") if isinstance(td, dict) else None
    known = primary in structs() or primary in solana_structs()
    if not known or set(td) != {"types", "primaryType", "domain", "message"}:
        raise ValueError("not a typed-data object of a known struct")
    d = td["domain"]
    if isinstance(d, dict) and set(d) == {n for n, _ in SOLANA_DOMAIN_FIELDS}:
        sep = keccak(encode(
            ["bytes32", "bytes32", "bytes32", "address"],
            [SOLANA_DOMAIN_TYPEHASH, keccak(text=word_of("string", d["name"])),
             keccak(text=word_of("string", d["version"])),
             to_checksum_address(word_of("address", d["verifyingContract"]))],
        ))
        if td.get("types") != typed_data(primary, None, "0x" + "00" * 20, {})["types"]:
            raise ValueError("the types are not this struct's")
        return keccak(b"\x19\x01" + sep + struct_hash(primary, td["message"]))
    if not isinstance(d, dict) or set(d) != {n for n, _ in DOMAIN_FIELDS} or primary not in structs():
        raise ValueError("the domain does not have its members")
    sep = keccak(encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [DOMAIN_TYPEHASH, keccak(text=word_of("string", d["name"])), keccak(text=word_of("string", d["version"])),
         word_of("uint256", d["chainId"]), to_checksum_address(word_of("address", d["verifyingContract"]))],
    ))
    if td.get("types") != typed_data(primary, 1, "0x" + "00" * 20, {})["types"]:
        raise ValueError("the types are not this struct's")
    return keccak(b"\x19\x01" + sep + struct_hash(primary, td["message"]))


def typed_mismatch(served: Any, own: dict) -> str | None:
    """The first member where ``served`` differs from ``own``, by parsed value; None when they agree.

    Compares ``primaryType``, ``types``, each domain member and each message member. A member the
    served object lacks, adds or cannot parse is a difference.
    """
    if not isinstance(served, dict) or set(served) != set(own):
        return "typed_data"
    if served["primaryType"] != own["primaryType"]:
        return "primaryType"
    if served["types"] != own["types"]:
        return "types"
    dom = SOLANA_DOMAIN_FIELDS if "chainId" not in own["domain"] else DOMAIN_FIELDS
    parts = (("domain", dom), ("message", fields_of(own["primaryType"])))
    for part, fields in parts:
        s, o = served[part], own[part]
        if not isinstance(s, dict) or set(s) != set(o):
            return part
        for name, kind in fields:
            try:
                same = word_of(kind, s[name]) == word_of(kind, o[name])
            except ValueError:
                same = False
            if not same:
                return f"{part}.{name}"
    return None


def trade_message(
    pair: str, side: int, notional_e6: int, rate_e6: int, premium_bps: int, maturity: int,
    pair_c: str, own_leg_id: str, own_nonce: Any, own_salt: str, quote_expiry: Any = None,
) -> dict:
    """The ``Trade`` message, JSON form: integers as decimal strings, ``bytes32`` as lower-case hex.

    The summary is formatted from the integers. ``quote_expiry`` is a member only with
    drop_quote_expiry off. Raises ValueError on a value the guest refuses.
    """
    summary = trade_summary(pair, side, notional_e6, rate_e6, premium_bps, maturity)
    vals = {
        "summary": summary, "pair": pair, "side": side_text(side), "notionalE6": str(notional_e6),
        "rateE6": str(rate_e6), "premiumBps": str(premium_bps), "maturity": str(maturity), "pairC": pair_c,
        "ownLegId": own_leg_id, "quoteExpiry": quote_expiry, "ownNonce": own_nonce, "ownSalt": own_salt,
    }
    msg = {}
    for name, kind in fields_of("Trade"):
        v = vals[name]
        if name in ("pairC", "ownLegId", "quoteExpiry", "ownNonce", "ownSalt"):
            v = word_of(kind, v)
            v = h0x(v) if kind == "bytes32" else str(v)
        msg[name] = v
    return msg


def trade_struct_hash(msg: dict) -> bytes:
    return struct_hash("Trade", msg)


def trade_digest(separator: bytes, msg: dict) -> bytes:
    return keccak(b"\x19\x01" + separator + trade_struct_hash(msg))


# ---------- signatures ----------

def normalize_sig(sig: Any) -> bytes:
    """A 65-byte ``r ‖ s ‖ v`` signature with low ``s`` and ``v`` 27 or 28.

    ``v`` 0 or 1 becomes 27 or 28. A high ``s`` becomes ``n - s`` with ``v`` flipped. Raises ValueError
    on another length, ``v`` outside {0, 1, 27, 28}, or ``r`` or ``s`` outside 1 to n - 1.
    """
    if isinstance(sig, str):
        b = hx(sig)
    elif isinstance(sig, (bytes, bytearray, memoryview)):
        b = bytes(sig)
    else:
        raise ValueError("a signature is bytes or 0x-hex")
    if len(b) != 65:
        raise ValueError("a signature is 65 bytes")
    r, s, v = int.from_bytes(b[:32], "big"), int.from_bytes(b[32:64], "big"), b[64]
    if v in (0, 1):
        v += 27
    if v not in (27, 28):
        raise ValueError("the signature's v is not 0, 1, 27 or 28")
    if not (0 < r < SECP256K1_N and 0 < s < SECP256K1_N):
        raise ValueError("the signature's r or s is out of range")
    if s > SECP256K1_N // 2:
        s, v = SECP256K1_N - s, 55 - v
    return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v])
