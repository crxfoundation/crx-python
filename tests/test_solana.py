"""The Solana network: the 3-field domain, deposit tx check, keypair, seat key, bind and deposit."""

from __future__ import annotations

import base64
import copy
import time
from datetime import datetime, timedelta, timezone

import pytest
from eth_utils import keccak

import crx
from crx import _eip712 as e7
from crx import _solana as sol

from .conftest import BASE, RPC, Clock, FakeSession

PID = sol.b58encode(bytes(range(1, 33)))  # a test program id
GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SEED = bytes([7] * 32)
AUTH = "GmaDrppBC7P5ARKV8g3djiwP89vz1jLK23V2GBjuAEGB"  # the Ed25519 key of SEED
AUTH_ATA = "7woc3ajaGMMXczFYjxon4aQoHH3j126fMUR9c58eHRsK"


def test_alias_and_domain_golden():
    pid = bytes(range(1, 33))
    a = keccak(b"solana:devnet" + pid)[12:]
    assert a.hex() == "b714a3f28ba86b050978109e25b149f776744c4b"
    sep = e7.domain_separator(None, "0x" + a.hex())
    assert sep.hex() == "a054991fc4b74d022fb9f42b13f036c66e25242540363a054b837957e542b195"
    for chain_id in (0, 1, 101, 43113, 43114):
        assert e7.domain_separator(chain_id, "0x" + a.hex()) != sep


def test_bindseat_typehash_and_solana_only():
    assert e7.h0x(e7.typehash("BindSeat")) == "0x30a34aadacd65e2873496806075473e5afcb7ceddcfcd4476ccfb6a9f2ba9858"
    assert "BindSeat" not in e7.structs()
    msg = {"seat": "0x" + "11" * 20, "authority": "0x" + "22" * 32, "payout": "0x" + "33" * 32, "nonce": "0",
           "deadline": "9"}
    td = e7.typed_data("BindSeat", None, "0x" + "44" * 20, msg)
    assert [f["name"] for f in td["types"]["EIP712Domain"]] == ["name", "version", "verifyingContract"]
    assert "chainId" not in td["domain"]
    sep = e7.domain_separator(None, "0x" + "44" * 20)
    assert e7.typed_digest(td) == keccak(b"\x19\x01" + sep + e7.struct_hash("BindSeat", msg))
    evm = e7.typed_data("BindSeat", 1, "0x" + "44" * 20, msg)
    with pytest.raises(ValueError):
        e7.typed_digest(evm)  # a bind never signs under the 4-field domain


def test_b58_round_trip_and_refusals():
    for b in (b"", b"\0", b"\0\0\x01", bytes(range(32)), bytes(64)):
        assert sol.b58decode(sol.b58encode(b)) == b if b else True
    with pytest.raises(ValueError):
        sol.b58decode("0OIl")
    with pytest.raises(ValueError):
        sol.key("1" * 31)


def test_program_addresses():
    assert sol.pda(PID, b"core") == "DXQMQxjuFeiqrc3zu7NX3W3p77UBTPe22BFFffsaHuwX"
    assert sol.pda(PID, b"seats") == "FPqRwwTB8VMaxmD1H35YzCCojAZCPfHe6kjV4MfxMaHv"
    assert sol.pda(PID, b"ckpt", b"\x00") == "CaEBeagUFTnHpRQsDRaS77kTvyyo5nGGDxvDm8XrXYQB"
    assert sol.pda(PID, b"ckpt", b"\x02") == "7Z6tZqBYhtzScFxuD3J3EWkhnfT2jzTQDFJ6Mhg5cznZ"
    assert sol.pda(PID, b"vault") == "BhzpNCyapi7n6wTJCi4u4NFN3G3pSWxFDZ1GWGtVgAUs"
    assert sol.ata(AUTH, MINT) == AUTH_ATA
    assert sol.on_curve(sol.key(AUTH))
    assert not sol.on_curve(sol.key(sol.pda(PID, b"core")))


PINNED = crx.NETWORKS["solana"]["program_id"]
# The pinned program's alias and domain separator, derived outside the SDK.
PINNED_VECTOR = {"alias": "0x4f24816e47e7f12ef046fd7bb1f429cb3f232dd2", "separator": "0xf48bf500c50be0ac78c260d6ef1b08228d4bec66bc0df860bfb7733af3ba70ac"}
# The pinned program's core and vault PDAs, derived outside the SDK.
PINNED_PDAS = {"core": "8ZdqL3tMWtPF8HLCEgT9GY5NvkEpMknaARKv4CY3usvT",
               "vault": "H5f3cXD4BmGmqSux43VY2TCnPFuSDzpHbfEHS7Csm4Sm"}


@pytest.mark.skipif(PINNED is None, reason="the solana row pins no program yet")
def test_this_sdk_pins_the_record_program():
    """The pinned program is a canonical base58 key whose alias, separator and PDAs match the derivation."""
    net = crx.NETWORKS["solana"]
    sol.need_pin(net)
    assert sol.b58encode(sol.key(PINNED)) == PINNED
    alias = sol.alias(net["cluster_tag"], PINNED)
    assert alias == PINNED_VECTOR["alias"]
    assert e7.h0x(e7.domain_separator(None, alias)) == PINNED_VECTOR["separator"]
    assert sol.pda(PINNED, b"core") == PINNED_PDAS["core"] and sol.pda(PINNED, b"vault") == PINNED_PDAS["vault"]


def test_need_pin_refuses_a_row_without_a_program():
    net = dict(crx.NETWORKS["solana"], program_id=PID)
    sol.need_pin(net)
    for k in ("genesis_hash", "program_id", "cluster_tag"):
        for empty in (None, ""):
            with pytest.raises(crx.ConfigError) as ei:
                sol.need_pin(dict(net, **{k: empty}))
            assert str(ei.value) == sol.UNPINNED
        with pytest.raises(crx.ConfigError):
            sol.need_pin({x: v for x, v in net.items() if x != k})
    assert sol.UNPINNED == "this SDK has no Solana program pinned yet; nothing signed"


def _sv(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def build_tx(payer: str, ixs: list, blockhash: str = "11111111111111111111111111111111") -> bytes:
    """Build a legacy tx from ``ixs`` of (program, [(pubkey, signer, writable)], data).

    Keys go payer first, then signed writable, signed read-only, unsigned writable, unsigned read-only.
    """
    metas: dict = {payer: [True, True]}
    for prog, accs, _ in ixs:
        for k, s, w in accs:
            m = metas.setdefault(k, [False, False])
            m[0] |= s
            m[1] |= w
        metas.setdefault(prog, [False, False])
    order = sorted(metas, key=lambda k: (k != payer, not metas[k][0], not metas[k][1], sol.key(k)))
    n_req = sum(1 for k in order if metas[k][0])
    ro_signed = sum(1 for k in order if metas[k][0] and not metas[k][1])
    ro_unsigned = sum(1 for k in order if not metas[k][0] and not metas[k][1])
    msg = bytes([n_req, ro_signed, ro_unsigned]) + _sv(len(order)) + b"".join(sol.key(k) for k in order)
    msg += sol.key(blockhash) + _sv(len(ixs))
    for prog, accs, data in ixs:
        msg += bytes([order.index(prog)]) + _sv(len(accs)) + bytes(order.index(k) for k, _, _ in accs)
        msg += _sv(len(data)) + data
    return _sv(n_req) + b"\0" * 64 * n_req + msg


def deposit_ixs(seat20: bytes, amount_raw: int = 100_000_000, row: int = 3, units: int = 60_000, micro: int = 1000,
                source: str = AUTH_ATA):
    return [
        (sol.COMPUTE_BUDGET, [], bytes([2]) + units.to_bytes(4, "little")),
        (sol.COMPUTE_BUDGET, [], bytes([3]) + micro.to_bytes(8, "little")),
        (PID, sol.deposit_metas(PID, AUTH, source), sol.deposit_data(amount_raw, row, seat20)),
    ]


def check(raw, seat20, **kw):
    args = dict(program_id=PID, authority=AUTH, source=AUTH_ATA, amount_raw=100_000_000, row=3, seat20=seat20)
    args.update(kw)
    return sol.check_deposit(raw, **args)


def test_deposit_check_accepts_the_exact_tx():
    seat = b"\x11" * 20
    t = check(build_tx(AUTH, deposit_ixs(seat)), seat)
    assert len(t["instructions"]) == 3
    assert check(build_tx(AUTH, deposit_ixs(seat)), seat, row=None)


def deposit_for_ixs(payer20: bytes, account20: bytes, amount_raw: int = 100_000_000, payer_row: int = 3,
                    account_row: int = 3, tag: int = sol.DEPOSIT_FOR_IX):
    """Return deposit_ixs with crx_core.deposit_for in place of deposit, on the same accounts."""
    data = (bytes([tag]) + amount_raw.to_bytes(8, "little") + payer_row.to_bytes(4, "little") + payer20
            + account_row.to_bytes(4, "little") + account20)
    ixs = deposit_ixs(payer20)
    p, a, _ = ixs[2]
    ixs[2] = (p, a, data)
    return ixs


def test_deposit_check_takes_a_deposit_for_of_the_seat_to_itself():
    """A seat CRX removed gets crx_core.deposit_for (tag 0x0b, 57 bytes) with itself as payer and account."""
    seat = b"\x11" * 20
    raw = build_tx(AUTH, deposit_for_ixs(seat, seat))
    t = check(raw, seat)
    assert t["instructions"][2]["data"] == sol.deposit_for_data(100_000_000, 3, seat)
    assert len(t["instructions"][2]["data"]) == 57 and t["instructions"][2]["data"][0] == 0x0B
    assert check(raw, seat, row=None)
    with pytest.raises(crx.RefusedToSign):
        check(build_tx(AUTH, deposit_for_ixs(seat, b"\x12" * 20)), seat)


def test_deposit_check_refuses_each_mutation():
    seat = b"\x11" * 20
    for mut in ("amount", "source", "fee_payer", "program", "signed", "cut", "trailing"):
        ixs = deposit_ixs(seat)
        payer = AUTH
        if mut == "amount":
            ixs = deposit_ixs(seat, amount_raw=100_000_001)
        elif mut == "source":
            other = sol.ata(AUTH, "h3G6C6NE59b7NFAsHZt6sN5NjKVzmbCqjp3xvPphdq2")
            ixs = deposit_ixs(seat, source=other)
        elif mut == "fee_payer":
            payer = sol.pda(PID, b"reserve")
        elif mut == "program":
            p, a, d = ixs[2]
            ixs[2] = (sol.b58encode(bytes(range(2, 34))), a, d)
        raw = build_tx(payer, ixs)
        if mut == "signed":
            raw = raw[:1] + b"\x01" * 64 + raw[65:]
        if mut == "cut":
            raw = raw[:68]
        if mut == "trailing":
            raw = raw + b"\0"
        try:
            check(raw, seat)
        except crx.RefusedToSign:
            continue
        pytest.fail(f"{mut}: not refused")


def _with_key(raw, k, *, dup_at=None):
    """Add key ``k`` to the deposit tx's unsigned writable block, and point deposit account ``dup_at`` at it."""
    t = sol.parse_tx(raw)
    keys = list(t["keys"])
    hdr = list(t["message"][:3])
    pos = len(keys) - hdr[2]
    keys.insert(pos, k)
    ixs = []
    for ix in t["instructions"]:
        p = keys.index(ix["program_id"])
        accs = [keys.index(m["pubkey"]) for m in ix["accounts"]]
        ixs.append([p, accs, bytes(ix["data"])])
    if dup_at is not None:
        ixs[2][1][dup_at] = pos
    msg = bytes(hdr) + _sv(len(keys)) + b"".join(sol.key(x) for x in keys) + sol.key(t["blockhash"]) + _sv(len(ixs))
    for p, accs, data in ixs:
        msg += bytes([p]) + _sv(len(accs)) + bytes(accs) + _sv(len(data)) + data
    return _sv(1) + b"\0" * 64 + msg


def test_deposit_check_refuses_a_duplicate_or_unused_key():
    seat = b"\x11" * 20
    raw = build_tx(AUTH, deposit_ixs(seat))
    with pytest.raises(crx.RefusedToSign):
        check(_with_key(raw, sol.pda(PID, b"vault"), dup_at=6), seat)
    with pytest.raises(crx.RefusedToSign):
        check(_with_key(raw, sol.b58encode(bytes([0xEE]) * 32)), seat)


def test_deposit_check_refuses_zero_units_and_rounds_the_fee_up():
    seat = b"\x11" * 20
    with pytest.raises(crx.RefusedToSign):
        check(build_tx(AUTH, deposit_ixs(seat, units=0, micro=2**64 - 1)), seat)
    assert check(build_tx(AUTH, deposit_ixs(seat, units=400_000, micro=250_000)), seat)  # 100 000 lamports
    with pytest.raises(crx.RefusedToSign):
        check(build_tx(AUTH, deposit_ixs(seat, units=400_000, micro=250_002)), seat)  # 100 000.8, the runtime 100 001


def sol_health(pid=PID, **over):
    alias = sol.alias(GENESIS, pid)
    row = {
        "key": "solana", "family": "solana", "cluster": "mainnet-beta", "genesis_hash": GENESIS, "program_id": pid,
        "core": sol.pda(pid, b"core"), "vault": sol.pda(pid, b"vault"), "verifying_contract": alias,
        "domain": e7.h0x(e7.domain_separator(None, alias)), "domain_fields": ["name", "version", "verifyingContract"],
        "domain_verified": True, "base_token": MINT, "base_decimals": 6, "enabled": True,
    }
    row.update(over)
    return {"status": "ok", "chains": [row]}


@pytest.fixture
def solnet(monkeypatch):
    net = dict(crx.NETWORKS["solana"], program_id=PID)
    monkeypatch.setitem(crx.NETWORKS, "solana-t", net)
    for k in ("CRX_ALLOW_MAINNET", "CRX_BASE", "CRX_RPC"):
        monkeypatch.delenv(k, raising=False)
    return net


@pytest.fixture
def solsession(session):
    session.routes[("GET", "/health")] = sol_health()
    session.rpc.update({
        "getGenesisHash": GENESIS,
        "getAccountInfo": lambda p: {"value": {"executable": True, "owner": sol.LOADER_V3}} if p[0] == PID
        else {"value": None},
    })
    return session


def sclient(session, tmp_path, account, **kw):
    args = dict(key=account.key.hex(), network="solana-t", base_url=BASE, rpc_url=RPC,
                state_dir=tmp_path / "state", session=session, allow_mainnet=True)
    args.update(kw)
    c = crx.Client(**args)
    clk = Clock(1_760_000_000)
    c._clock, c._sleep = clk, clk.sleep
    return c


CHAIN_REFUSALS = [  # step, /health row edit, RPC answer edit
    ("family", {"family": "evm"}, {}),
    ("genesis", {"genesis_hash": "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"}, {}),
    ("rpc_genesis", {}, {"getGenesisHash": "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"}),
    ("not_exec", {}, {"getAccountInfo": {"value": {"executable": False, "owner": sol.LOADER_V3}}}),
    ("alias", {"verifying_contract": "0x" + "ab" * 20}, {}),
    ("core", {"core": sol.pda(PID, b"seats")}, {}),
]


def test_chain_check_refuses_each_step(solnet, solsession, tmp_path, account):
    rpc = dict(solsession.rpc)
    for step, row, answers in CHAIN_REFUSALS:
        solsession.routes[("GET", "/health")] = sol_health(**row)
        solsession.rpc.update(rpc, **answers)
        c = sclient(solsession, tmp_path / step, account)
        try:
            c._chain_ready()
        except crx.CrxError:
            continue
        pytest.fail(f"{step}: not refused")


SITE_SEAT ="0xef9a243b0c45e3e8caa096cb4b06f88f9bc5a46c"  # the site's seat for the wallet of seed 0x07 * 32
HOST_TODAY = "portal.crxfx.com"  # this file's own pin of sol.HOST
OTHER_HOST = "solana.crxfx.com"  # not the SDK's host: its key text and its seat are the ones the SDK must not make
# Per host: the key text of wallet AUTH, byte for byte, and the seat of that wallet (seed 0x07 * 32). Both
# rows come from one derivation run outside the SDK.
HOST_VECTORS = {
    HOST_TODAY: (
        b"CRX trading key for GmaDrppBC7P5ARKV8g3djiwP89vz1jLK23V2GBjuAEGB on portal.crxfx.com (Solana mainnet). "
        b"Signing this creates your CRX trading key. It moves no funds and is not a login. "
        b"Sign it only on https://portal.crxfx.com.",
        "4952dfa6d7a60bca32008f5a8f5bd86ae513e2d97ce9410e7a51c6cd281f71d4",
        "0xef9a243b0c45e3e8caa096cb4b06f88f9bc5a46c"),
    OTHER_HOST: (
        b"CRX trading key for GmaDrppBC7P5ARKV8g3djiwP89vz1jLK23V2GBjuAEGB on solana.crxfx.com (Solana mainnet). "
        b"Signing this creates your CRX trading key. It moves no funds and is not a login. "
        b"Sign it only on https://solana.crxfx.com.",
        "6eefbc4941247b2d8a9bc14bbe37204a77f278fb92d084bafa70e8d4b5cfd2f6",
        "0x14c07a6ad10bcbb4ebc57874577e2cdad2ba9f26"),
}


def test_seat_from_wallet_golden():
    pytest.importorskip("cryptography")
    kp = sol.Keypair(SEED)
    assert kp.pubkey == AUTH
    from eth_account import Account
    seat = Account.from_key(sol.seat_secret(kp)).address.lower()
    assert seat == SITE_SEAT == HOST_VECTORS[HOST_TODAY][2]
    assert seat != HOST_VECTORS[OTHER_HOST][2]
    text, digest, _ = HOST_VECTORS[HOST_TODAY]
    assert sol.trading_key_text(AUTH) == text
    assert len(text) == 225 and keccak(text).hex() == digest


def test_seat_refuses_a_wallet_that_signs_two_ways():
    pytest.importorskip("cryptography")

    class Flaky(sol.Keypair):
        n = 0

        def sign(self, m):
            self.n += 1
            return bytes([self.n]) * 64
    with pytest.raises(crx.ConfigError):
        sol.seat_secret(Flaky(SEED))


def test_seat_retries_an_out_of_range_scalar(monkeypatch):
    pytest.importorskip("cryptography")
    real = sol.keccak
    calls = []

    def k(b):
        calls.append(b)
        return b"\0" * 32 if len(calls) == 1 else real(b)
    monkeypatch.setattr(sol, "keccak", k)
    d = sol.seat_secret(sol.Keypair(SEED))
    assert len(calls) == 2 and calls[1][-1:] == b"\x01" and d == real(calls[1])


SECRET64 = SEED + sol.key(AUTH)


def _secret_strings():
    return [repr(SEED)[2:40], SEED.hex(), SECRET64.hex(), sol.b58encode(SECRET64), "7, 7, 7, 7, 7, 7, 7",
            sol.b58encode(SEED)]


def _no_secret(exc, extra=()):
    """Return True when the error and its SDK frames hold no form of the wallet secret or ``extra``.

    The test's own frames hold the secret it passed in, so they are left out.
    """
    import traceback
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename == __file__:
        tb = tb.tb_next
    assert tb is not None, "the error did not come from the SDK"
    text = "".join(traceback.format_list(traceback.StackSummary.extract(traceback.walk_tb(tb), capture_locals=True)))
    for e in (exc, exc.__context__, exc.__cause__):
        if e is not None:
            text += repr(getattr(e, "doc", "")) + repr(e.args) + str(e)
    return [x for x in list(_secret_strings()) + list(extra) if x in text] == []


INIT_REFUSALS = [  # Client keywords besides keypair=, error text
    (dict(network="solana-t", allow_mainnet=False), "mainnet is off"),  # raised before the wallet is read
    (dict(network="solana-t", rpc_url=None), "no default RPC"),
    (dict(network="testnet"), "keypair= is for network='solana'"),
    (dict(network="nowhere"), "unknown network"),
    (dict(network="solana-t", key="0x" + "11" * 32), "one only"),
    (dict(network="solana-t", signer=object(), key="0x" + "11" * 32), "one only"),
    (dict(network="solana-t", core="0x" + "11" * 20), "core= is for Ethereum networks"),
] + [(dict(network="solana-t", keepalive=bad), "keepalive is None, or 1 to 10 s") for bad in (0.5, 11, True, "5")]


def test_client_init_drops_the_wallet_secret_on_every_refusal(solnet, solsession, tmp_path):
    pytest.importorskip("cryptography")
    common = dict(base_url=BASE, rpc_url=RPC, session=solsession, state_dir=tmp_path / "s", allow_mainnet=True)
    for kw, text in INIT_REFUSALS:
        for secret in (SECRET64, list(SECRET64), bytearray(SECRET64)):
            with pytest.raises(crx.ConfigError) as ei:
                crx.Client(keypair=secret, **dict(common, **kw))
            assert text in str(ei.value) and _no_secret(ei.value), kw


def test_keypair_errors_carry_no_secret():
    pytest.importorskip("cryptography")
    for secret in (SEED + b"\x07", SECRET64[:63], bytes(SEED[:31]) + b"\x08" + sol.key(AUTH),
                   list(SEED) + [256], list(SEED)[:31] + [True]):
        with pytest.raises(crx.ConfigError) as ei:
            sol.Keypair(secret)
        assert _no_secret(ei.value) and ei.value.__context__ is None
    for secret in (sol.b58encode(SECRET64), sol.b58encode(SEED)):
        with pytest.raises(crx.ConfigError) as ei:
            sol.Keypair(secret)
        assert "base58" in str(ei.value) and _no_secret(ei.value)


def test_keypair_file_errors_carry_no_secret(tmp_path):
    pytest.importorskip("cryptography")
    import json
    import os
    p = tmp_path / "id.json"
    p.write_text(json.dumps(list(SECRET64))[:-1] + ",]")
    os.chmod(p, 0o600)
    with pytest.raises(crx.ConfigError) as ei:
        sol.Keypair(str(p))
    assert _no_secret(ei.value) and ei.value.__context__ is None and ei.value.__cause__ is None
    # The file reader itself leaves no context and no secret in its frames.
    with pytest.raises(crx.ConfigError) as ei:
        sol._read_keypair_file(p)
    assert _no_secret(ei.value, ["7,7,7,7,7,7,7"]) and ei.value.__context__ is None


def test_keypair_file_that_is_not_a_flat_array_is_refused_unparsed(tmp_path, monkeypatch):
    import json
    import os
    import types
    parsed = []

    def loads(text):
        parsed.append(len(text))
        raise ValueError("not JSON")
    p = tmp_path / "id.json"
    p.write_text("[]")
    os.chmod(p, 0o600)
    with monkeypatch.context() as m:
        m.setattr(sol, "json", types.SimpleNamespace(loads=loads))
        for body in ("[" * 100_000 + "7" + "]" * 100_000, "[[7]]", "[7, [7]]", '[{"k": 7}]', '{"k": [7]}', "7"):
            p.write_text(body)
            with pytest.raises(crx.ConfigError) as ei:
                sol._read_keypair_file(p)
            assert parsed == [] and "JSON array" in str(ei.value)
            assert _no_secret(ei.value) and ei.value.__context__ is None
    # A flat array is parsed, on one line or on many.
    for text in (json.dumps(list(SECRET64)), json.dumps(list(SECRET64), indent=2) + "\n"):
        p.write_text(text)
        assert sol._read_keypair_file(p) == SECRET64


def test_keypair_file_set_owner_only(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    import json
    import os
    import crx._keys as keys_mod
    p = tmp_path / "id.json"
    p.write_text(json.dumps(list(SECRET64)))
    os.chmod(p, 0o644)
    if os.name == "posix":
        me = os.getuid()
        monkeypatch.setattr(keys_mod.os, "getuid", lambda: me + 1)
        with pytest.raises(crx.ConfigError) as ei:
            sol.Keypair(str(p))
        assert "chmod 600" in str(ei.value) and p.stat().st_mode & 0o777 == 0o644
        monkeypatch.undo()
    assert sol.Keypair(str(p)).pubkey == AUTH and sol.Keypair(p).pubkey == AUTH
    if os.name == "posix":
        assert p.stat().st_mode & 0o777 == 0o600


def bind_routes(session, client, nonce=0, deadline=None, pid=PID, **over):
    deadline = deadline or int(client._clock()) + 3600
    alias = sol.alias(GENESIS, pid)
    msg = {"seat": client.address, "authority": e7.h0x(sol.key(AUTH)), "payout": e7.h0x(sol.key(AUTH)),
           "nonce": str(nonce), "deadline": str(deadline)}
    td = e7.typed_data("BindSeat", None, alias, msg)
    body = {"bind": {"seat": client.address, "authority": AUTH, "payout": AUTH, "nonce": str(nonce),
                     "deadline": deadline},
            "payout_ata": AUTH_ATA, "row": 3, "digest": e7.h0x(e7.typed_digest(td)), "typed_data": td,
            "program_id": pid, "verifying_contract": alias}
    for k, v in over.items():
        body[k] = v
    posted = []
    # The seat reads unbound until its signed bind is posted, then bound.
    held = {"bound": False, "status": "unbound", "authority": None, "payout_wallet": None, "payout_ata": None}

    def post(req):
        posted.append(req["body"])
        if "sig" in req["body"]:
            held.update(bound=True, status="bound", authority=AUTH, payout_wallet=AUTH, payout_ata=AUTH_ATA)
            return (202, {"status": "pending", "tx": None})
        return body
    session.routes[("POST", "/bind")] = post
    session.routes[("GET", "/bind")] = lambda req: dict(held)
    return posted, td


def test_client_from_keypair(solnet, solsession, tmp_path):
    pytest.importorskip("cryptography")
    from crx.signer import recover
    c = crx.Client(network="solana-t", keypair=list(SEED), base_url=BASE, rpc_url=RPC, session=solsession,
                   state_dir=tmp_path / "s", allow_mainnet=True)
    assert c.address == SITE_SEAT
    clk = Clock(1_760_000_000)
    c._clock, c._sleep = clk, clk.sleep
    posted, td = bind_routes(solsession, c)
    assert c.bind()["status"] == "bound"
    assert posted[0]["authority"] == AUTH and posted[0]["payout_wallet"] == AUTH
    assert recover(e7.typed_digest(td), bytes.fromhex(posted[-1]["sig"][2:])) == c.address
    with pytest.raises(crx.ConfigError):
        crx.Client(network="solana-t", keypair=list(SEED), key="0x" + "11" * 32, base_url=BASE, rpc_url=RPC,
                   session=solsession, allow_mainnet=True)


def test_bind_refuses_a_served_change(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    now = int(c._clock())
    bind = {"seat": c.address, "authority": AUTH, "payout": AUTH, "deadline": now + 3600}
    rows = [
        ("ata", {"payout_ata": sol.pda(PID, b"vault")}),
        ("digest", {"digest": "0x" + "00" * 32}),
        ("deadline_far", {"deadline": now + 25 * 3600}),
        ("nonce_past_u64", {"bind": dict(bind, nonce=str(2**64))}),
        ("nonce_negative", {"bind": dict(bind, nonce="-1")}),
    ]
    for bad, over in rows:
        posted, _ = bind_routes(solsession, c, **over)
        with pytest.raises(crx.RefusedToSign):
            c.bind(AUTH)
        assert all("sig" not in p for p in posted), bad


BOUND = {"bound": True, "status": "bound", "authority": AUTH, "payout_wallet": AUTH, "payout_ata": AUTH_ATA,
         "tx": "txsig", "slot": 9, "bind_nonce": "1"}
OTHER = sol.b58encode(bytes([0xEE]) * 32)  # another wallet
OTHER_ATA = sol.ata(OTHER, MINT)


def no_post(req):
    raise AssertionError("a bind was posted")


def calls_of(session):
    return [(x["method"], x["path"]) for x in session.calls]


def test_bind_state_reads_the_bind(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    solsession.routes[("GET", "/bind")] = dict(BOUND)
    solsession.routes[("POST", "/bind")] = no_post
    assert c.bind_state() == BOUND
    assert calls_of(solsession) == [("GET", "/bind")] and solsession.rpc_calls == []
    solsession.routes[("GET", "/bind")] = {"bound": False, "status": "unbound", "authority": None,
                                           "payout_wallet": None, "payout_ata": None}
    assert c.bind_state()["status"] == "unbound"
    solsession.routes[("GET", "/bind")] = (200, "not an object")
    with pytest.raises(crx.BadAnswer):
        c.bind_state()
    viewer = crx.Client(key=account.key.hex(), network="solana-t", base_url=BASE, rpc_url=RPC, session=solsession,
                        state_dir=tmp_path / "v", allow_mainnet=True, account="0x" + "22" * 20)
    with pytest.raises(crx.ConfigError):
        viewer.bind_state()
    assert calls_of(solsession) == [("GET", "/bind")] * 3


FILED = {"authority": AUTH, "payout_wallet": AUTH, "payout_ata": AUTH_ATA}


def test_bind_of_a_seat_read_bound_lets_the_three_keys_decide(solnet, solsession, tmp_path, account, monkeypatch):
    """The only request is the one read: no BindSeat is signed or posted."""
    c = sclient(solsession, tmp_path, account)
    solsession.routes[("POST", "/bind")] = no_post
    signed = []
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(a) or "0x")
    solsession.routes[("GET", "/bind")] = dict(BOUND)
    assert c.bind(AUTH) == BOUND
    assert c.bind(AUTH) == BOUND  # a repeat reads again and stays quiet
    solsession.routes[("GET", "/bind")] = dict(BOUND, authority=OTHER)
    with pytest.raises(crx.CrxError) as ei:
        c.bind(AUTH)
    e = ei.value
    assert type(e) is crx.CrxError and e.code == "seat_already_bound" and e.status is None
    assert str(e) == "this seat is bound already, with another authority"
    assert e.details == {"bound": dict(FILED, authority=OTHER), "filed": FILED}
    solsession.routes[("GET", "/bind")] = dict(BOUND, authority=None)
    with pytest.raises(crx.BadAnswer):
        c.bind(AUTH)
    assert signed == [] and calls_of(solsession) == [("GET", "/bind")] * 4 and solsession.rpc_calls == []


PENDING = {"bound": False, "status": "pending", "authority": AUTH, "payout_wallet": AUTH, "payout_ata": AUTH_ATA,
           "tx": None, "slot": None, "bind_nonce": "0"}
UNBOUND = {"bound": False, "status": "unbound", "authority": None, "payout_wallet": None, "payout_ata": None}
OTHER_KEYS = {"authority": AUTH, "payout_wallet": OTHER, "payout_ata": OTHER_ATA}
BIND_LIVE_TEXT = "a bind of this seat is in progress: no other bind is taken before it ends; bind() reads its end"
STILL_TEXT = "the bind is still in progress after 240 s: it has not failed; bind() reads its end"
DOOR_BOUND = (409, {"error": "seat 0x11 is bound already", "code": "seat_already_bound",
                    "outcome": "not_permitted", "message": "This account already has a linked wallet."})


def test_bind_waits_on_its_own_bind_in_progress_and_signs_nothing(solnet, solsession, tmp_path, account, monkeypatch):
    """A bind already in progress with the same three keys ends this call too."""
    c = sclient(solsession, tmp_path, account)
    solsession.routes[("GET", "/bind")] = [dict(PENDING), dict(PENDING), dict(BOUND)]
    solsession.routes[("POST", "/bind")] = no_post
    signed = []
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(a) or "0x")
    start = c._clock()
    assert c.bind(AUTH) == BOUND
    assert signed == [] and calls_of(solsession) == [("GET", "/bind")] * 3 and solsession.rpc_calls == []
    assert c._clock() - start == sol.BIND_POLL


def test_a_bind_in_progress_with_other_keys_raises_before_a_signature(solnet, solsession, tmp_path, account,
                                                                      monkeypatch):
    c = sclient(solsession, tmp_path, account)
    solsession.routes[("GET", "/bind")] = dict(PENDING, **OTHER_KEYS)
    solsession.routes[("POST", "/bind")] = no_post
    signed = []
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(a) or "0x")
    with pytest.raises(crx.BindInProgress) as ei:
        c.bind(AUTH)
    e = ei.value
    assert str(e) == BIND_LIVE_TEXT and e.details == {"filed": OTHER_KEYS} and e.status is None
    assert signed == [] and calls_of(solsession) == [("GET", "/bind")] and solsession.rpc_calls == []


def bind_then(session, c, reads, signed_post=None, unsigned_post=None):
    """File a bind, then serve ``reads`` in order on GET /bind, repeating the last.

    ``signed_post`` and ``unsigned_post`` replace the answer to that post. Return the posted bodies
    and the clock of each read after the filing.
    """
    posted, _ = bind_routes(session, c)
    route = session.routes[("POST", "/bind")]
    after, filed = [], []

    def post(req):
        over = signed_post if "sig" in req["body"] else unsigned_post
        if over is None:
            return route(req)
        posted.append(req["body"])
        filed.append(1)
        return over

    def get(req):
        if not filed and not any("sig" in b for b in posted):
            return dict(UNBOUND)
        after.append(c._clock())
        r = reads[min(len(after), len(reads)) - 1]
        return r(req) if callable(r) else copy.deepcopy(r)
    session.routes[("POST", "/bind")] = post
    session.routes[("GET", "/bind")] = get
    return posted, after


def test_a_door_that_reads_the_seat_bound_lets_the_three_keys_decide(solnet, solsession, tmp_path, account,
                                                                    monkeypatch):
    """A 409 seat_already_bound names no key, so the bind reads GET /bind and compares the three keys."""
    signed = []
    real = crx.client.sign_typed
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(1) or real(*a, **k))
    c = sclient(solsession, tmp_path, account)
    posted, after = bind_then(solsession, c, [UNBOUND, BOUND], unsigned_post=DOOR_BOUND)
    assert c.bind(AUTH) == BOUND and len(after) == 2
    assert len(signed) == 0 and len(posted) == 1
    c = sclient(solsession, tmp_path / "2", account)
    bind_then(solsession, c, [dict(BOUND, **OTHER_KEYS)], unsigned_post=DOOR_BOUND)
    with pytest.raises(crx.SeatBoundOtherPayout) as ei:
        c.bind(AUTH)
    assert ei.value.details == {"bound": OTHER_KEYS, "filed": FILED}
    c = sclient(solsession, tmp_path / "3", account)
    bind_then(solsession, c, [UNBOUND], unsigned_post=DOOR_BOUND)
    with pytest.raises(crx.BindInProgress) as ei:
        c.bind(AUTH)
    assert str(ei.value) == STILL_TEXT


def deposit_routes(session, client, ixs=None, amount_raw=100_000_000):
    seat20 = bytes.fromhex(client.address[2:])
    raw = build_tx(AUTH, ixs or deposit_ixs(seat20))
    session.routes[("POST", "/deposit")] = {
        "transactions": [{"family": "solana", "encoding": "base64", "version": "legacy",
                          "tx": base64.b64encode(raw).decode(), "fee_payer": AUTH, "signers": [AUTH],
                          "recent_blockhash": "11111111111111111111111111111111", "last_valid_block_height": 500}],
        "amount_raw": str(amount_raw), "amount_ledger": str(amount_raw), "source": AUTH_ATA,
        "vault": sol.pda(PID, b"vault")}
    session.routes[("GET", "/bind")] = {"bound": True, "status": "bound", "authority": AUTH, "payout_wallet": AUTH,
                                        "payout_ata": AUTH_ATA}
    return raw


def test_deposit_unsigned_and_signed(solnet, solsession, tmp_path, account):
    pytest.importorskip("cryptography")
    c = sclient(solsession, tmp_path, account)
    clk = Clock(1_760_000_000)
    c._clock, c._sleep = clk, clk.sleep
    raw = deposit_routes(solsession, c)
    d = c.deposit("100", unsigned=True, authority=AUTH)
    assert d.status == "unsigned" and base64.b64decode(d.txs[0]) == raw
    sent = []

    def send(p):
        sent.append(base64.b64decode(p[0]))
        return sol.b58encode(sent[-1][1:65])
    solsession.rpc.update({
        "sendTransaction": send,
        "getSignatureStatuses": lambda p: {"value": [{"confirmationStatus": "confirmed", "err": None}]},
        "getBlockHeight": 10,
    })
    sig_box = {}

    def balance(req):
        return {"account": c.address, "deposit": {"last": {"tx": sig_box.get("sig"), "status": "credited"}}}
    solsession.routes[("GET", "/balance")] = balance
    orig = sol.send_and_confirm

    def spy(*a, **k):
        s = orig(*a, **k)
        sig_box["sig"] = s
        return s
    sol.send_and_confirm, saved = spy, sol.send_and_confirm
    try:
        d = c.deposit("100", keypair=list(SEED))
    finally:
        sol.send_and_confirm = saved
    assert d.status == "credited" and d.txs == [sig_box["sig"]]
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    tx = sent[0]
    Ed25519PublicKey.from_public_bytes(sol.key(AUTH)).verify(tx[1:65], tx[65:])


WALLET_REFUSALS = [  # deposit() keywords, error
    (dict(keypair=list(bytes([8] * 32))), crx.ConfigError),
    (dict(unsigned=True), crx.ConfigError),
    (dict(unsigned=True, authority="not-a-key"), crx.BadRequest),
    (dict(keypair=list(SEED), authority=AUTH), crx.ConfigError),
]


def test_deposit_refuses_a_wrong_or_missing_wallet_and_unbound(solnet, solsession, tmp_path, account):
    pytest.importorskip("cryptography")
    c = sclient(solsession, tmp_path, account)
    deposit_routes(solsession, c)
    for kw, err in WALLET_REFUSALS:
        with pytest.raises(err):
            c.deposit("100", **kw)
        assert ("POST", "/deposit") not in calls_of(solsession), kw
    solsession.routes[("GET", "/bind")] = {"bound": False, "status": "unbound"}
    with pytest.raises(crx.RefusedToSign):
        c.deposit("100", unsigned=True, authority=AUTH)
    assert ("POST", "/deposit") not in calls_of(solsession)


def test_deposit_refuses_a_served_amount_change(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    deposit_routes(solsession, c, amount_raw=100_000_001)
    with pytest.raises(crx.RefusedToSign):
        c.deposit("100", unsigned=True, authority=AUTH)


def test_a_blockhash_not_valid_before_the_send_sends_nothing():
    from crx._chain import Rpc
    session, check_session = FakeSession(), FakeSession()
    blockhash = sol.b58encode(bytes([9]) * 32)
    session.rpc.update({"sendTransaction": {"__http__": (502, "<html><body>502 Bad Gateway</body></html>")},
                        "isBlockhashValid": {"context": {"slot": 260}, "value": False}})
    raw = bytes([1]) + bytes([5]) * 64 + build_tx(AUTH, [], blockhash=blockhash)[65:]
    clk = Clock(0)
    with pytest.raises(crx.TxFailed) as ei:
        sol.send_and_confirm(Rpc(RPC, session), raw, check_rpc=Rpc(RPC, check_session), sleep=clk.sleep, clock=clk)
    e = ei.value
    assert type(e) is crx.TxFailed and e.details == {"tx": sol.b58encode(bytes([5]) * 64), "reason": "expired"}
    assert str(e) == "deposit not sent: the transaction expired before it was sent; no USDC left the wallet"
    assert session.rpc_calls == [("isBlockhashValid", [blockhash, {"commitment": "confirmed"}])]
    assert check_session.rpc_calls == []


TENOR_ROW = {"pair": "USD/MXN", "notional": {"min": "1000", "max": "10000000"}, "session": {"open": True},
             "tenor": {"min_secs": 7800, "max_secs": 7_776_000}, "max_premium_bps": 200}


def test_solana_sends_no_rfq_outside_the_served_band(solnet, solsession, tmp_path, account):
    solsession.routes[("GET", "/markets")] = {"markets": [TENOR_ROW]}
    solsession.routes[("POST", "/rfqs")] = {"rfq_id": "0x" + "aa" * 32, "leg_id": "0x" + "bb" * 32,
                                            "quote_expiry_max": 1_900_000_000_000}
    c = sclient(solsession, tmp_path, account)
    under = (timedelta(hours=1), int(time.time() * 1000) + 3_600_000, datetime.now(timezone.utc) + timedelta(hours=2),
             timedelta(0), int(time.time()) + 30 * 86_400)
    for expiry in under:
        before = time.time() * 1000
        with pytest.raises(crx.BadRequest) as ei:
            c.ask("USD/MXN", "buy", 25_000, expiry=expiry)
        e = ei.value
        assert type(e) is crx.BadRequest and e.status is None and e.gateway_code is None
        assert sorted(e.details) == ["earliest_expiry", "limit", "min_secs", "tenor_secs"]
        assert e.details["limit"] == "tenor" and e.details["min_secs"] == 7800
        assert before + 7_800_000 - 1 <= e.details["earliest_expiry"] <= time.time() * 1000 + 7_800_000
        assert str(e).startswith("the settlement is too soon: the shortest trade now settles at 20")
        assert str(e).endswith(" UTC (7800 s from now)")
    for expiry in (timedelta(days=91), timedelta(days=400)):
        before = time.time() * 1000
        with pytest.raises(crx.BadRequest) as ei:
            c.ask("USD/MXN", "buy", 25_000, expiry=expiry)
        e = ei.value
        assert sorted(e.details) == ["latest_expiry", "limit", "max_secs", "tenor_secs"]
        assert e.details["limit"] == "tenor" and e.details["max_secs"] == 7_776_000
        assert before + 7_776_000_000 - 1 <= e.details["latest_expiry"] <= time.time() * 1000 + 7_776_000_000
        assert str(e).endswith(" UTC (7776000 s from now)")
    assert [x for x in solsession.calls if (x["method"], x["path"]) == ("POST", "/rfqs")] == []


STOPPED_TRADE = (403, {"error": "not whitelisted: account 0x11 was revoked on solana; contact CRX",
                       "code": "not_whitelisted", "outcome": "account_removed",
                       "message": "Access removed: no new trades. You can still deposit and withdraw."})


def test_a_stopped_seat_ask_and_quote_raise_seat_stopped(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    solsession.routes[("GET", "/markets")] = {"markets": [dict(TENOR_ROW, tenor={"min_secs": 600,
                                                                                 "max_secs": 7_776_000})]}
    solsession.routes[("POST", "/rfqs")] = STOPPED_TRADE
    for call in (c.ask, c.quote):
        with pytest.raises(crx.SeatStopped) as ei:
            call("USD/MXN", "buy", 25_000)
        e = ei.value
        assert type(e) is crx.SeatStopped and isinstance(e, crx.NotWhitelisted) and "SeatStopped" in crx.__all__
        assert e.code == "seat_stopped" and e.status == 403 and e.gateway_code == "not_whitelisted"
        assert str(e) == "CRX removed this seat's access: no new trade; deposit() and withdraw() still work"
        assert e.details == {"outcome": "account_removed"}
    assert len([x for x in calls_of(solsession) if x == ("POST", "/rfqs")]) == 2
