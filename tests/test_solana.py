"""The Solana network: 3-field domain, chain check, bind, the deposit tx check, withdraw. No network."""

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

from .conftest import BASE, RPC, Clock

PID = sol.b58encode(bytes(range(1, 33)))  # a test program id
GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SEED = bytes([7] * 32)
AUTH = "GmaDrppBC7P5ARKV8g3djiwP89vz1jLK23V2GBjuAEGB"  # the Ed25519 key of SEED
AUTH_ATA = "7woc3ajaGMMXczFYjxon4aQoHH3j126fMUR9c58eHRsK"


# ---------- goldens (crx-solana crates/codec/tests/eip712_goldens.rs) ----------

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


def test_ethereum_domain_unchanged():
    core = "0x" + "c0" * 20
    want = keccak(e7.encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [e7.DOMAIN_TYPEHASH, keccak(text="CRX"), keccak(text="rulebook-1.0"), 1, e7.to_checksum_address(core)]))
    assert e7.domain_separator(1, core) == want
    assert set(e7.domain_json(1, core)) == {"name", "version", "chainId", "verifyingContract"}


# ---------- base58, PDAs, curve (vectors cross-checked with solders) ----------

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


def test_on_curve():
    assert sol.on_curve(sol.key(AUTH))
    assert not sol.on_curve(sol.key(sol.pda(PID, b"core")))


# ---------- a legacy tx builder (test side) ----------

def _sv(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def build_tx(payer: str, ixs: list, blockhash: str = "11111111111111111111111111111111") -> bytes:
    """ixs: (program, [(pubkey, signer, writable)], data). Legacy order: signer-writable, signer-ro,
    unsigned-writable, unsigned-ro; the payer first."""
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


SEAT = None  # set per test from the client address


def deposit_ixs(seat20: bytes, amount_raw: int = 100_000_000, row: int = 3, units: int = 60_000, micro: int = 1000,
                source: str = AUTH_ATA):
    return [
        (sol.COMPUTE_BUDGET, [], bytes([2]) + units.to_bytes(4, "little")),
        (sol.COMPUTE_BUDGET, [], bytes([3]) + micro.to_bytes(8, "little")),
        (PID, sol.deposit_metas(PID, AUTH, source), sol.deposit_data(amount_raw, row, seat20)),
    ]


def test_builder_matches_solders():
    solders = pytest.importorskip("solders")  # noqa: F841
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction
    ixs = deposit_ixs(b"\x11" * 20)
    sixs = [Instruction(Pubkey.from_string(p), d, [AccountMeta(Pubkey.from_string(k), s, w) for k, s, w in a])
            for p, a, d in ixs]
    m = Message.new_with_blockhash(sixs, Pubkey.from_string(AUTH), Hash.default())
    want = bytes(Transaction.new_unsigned(m))
    assert build_tx(AUTH, ixs) == want


def check(raw, seat20, **kw):
    args = dict(program_id=PID, authority=AUTH, source=AUTH_ATA, amount_raw=100_000_000, row=3, seat20=seat20)
    args.update(kw)
    return sol.check_deposit(raw, **args)


def test_deposit_check_accepts_the_exact_tx():
    seat = b"\x11" * 20
    t = check(build_tx(AUTH, deposit_ixs(seat)), seat)
    assert len(t["instructions"]) == 3
    assert check(build_tx(AUTH, deposit_ixs(seat)), seat, row=None)


@pytest.mark.parametrize("mut", [
    "amount", "row", "seat", "source", "extra_ix", "extra_budget_ix", "drop_budget", "fee_payer", "price_high",
    "units_high", "account_ro", "program", "signed", "vault", "swap_accounts", "trailing",
])
def test_deposit_check_refuses_each_mutation(mut):
    seat = b"\x11" * 20
    ixs = deposit_ixs(seat)
    payer = AUTH
    raw = None
    if mut == "amount":
        ixs = deposit_ixs(seat, amount_raw=100_000_001)
    elif mut == "row":
        ixs = deposit_ixs(seat, row=4)
    elif mut == "seat":
        ixs = deposit_ixs(b"\x12" * 20)
    elif mut == "source":
        other = sol.ata(AUTH, "h3G6C6NE59b7NFAsHZt6sN5NjKVzmbCqjp3xvPphdq2")
        ixs = deposit_ixs(seat, source=other)
    elif mut == "extra_ix":
        ixs = ixs + [(sol.SYSTEM_PROGRAM, [(AUTH, True, True), (AUTH_ATA, False, True)], b"\x02" + b"\0" * 11)]
    elif mut == "extra_budget_ix":
        ixs = ixs + [ixs[1]]  # no new key: only the instruction list check refuses it
    elif mut == "drop_budget":
        ixs = ixs[1:]
    elif mut == "fee_payer":
        payer = sol.pda(PID, b"reserve")
    elif mut == "price_high":
        ixs = deposit_ixs(seat, units=200_000, micro=10_000_000)
    elif mut == "units_high":
        ixs = deposit_ixs(seat, units=1_400_000, micro=0)
    elif mut == "account_ro":
        p, a, d = ixs[2]
        a = list(a)
        a[1] = (a[1][0], False, False)
        ixs[2] = (p, a, d)
    elif mut == "program":
        p, a, d = ixs[2]
        ixs[2] = (sol.b58encode(bytes(range(2, 34))), a, d)
    elif mut == "vault":
        p, a, d = ixs[2]
        a = list(a)
        a[6] = (sol.pda(PID, b"reserve"), False, True)
        ixs[2] = (p, a, d)
    elif mut == "swap_accounts":
        p, a, d = ixs[2]
        a = list(a)
        a[0], a[1] = a[1], a[0]  # the same keys and flags in another order: only the metas check refuses it
        ixs[2] = (p, a, d)
    raw = build_tx(payer, ixs)
    if mut == "signed":
        raw = raw[:1] + b"\x01" * 64 + raw[65:]
    if mut == "trailing":
        raw = raw + b"\0"
    with pytest.raises(crx.RefusedToSign):
        check(raw, seat)


# ---------- the client on Solana ----------

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


# ---------- the program pin: NETWORKS["solana"]["program_id"], both states ----------

PINNED = crx.NETWORKS["solana"]["program_id"]
# The alias and the domain separator of the pinned program on the cluster tag, from the birth record (an
# independent run). Both stay empty while the row pins no program.
PINNED_VECTOR = {"alias": "", "separator": ""}
while_unpinned = pytest.mark.skipif(PINNED is not None, reason="the solana row pins a program")
while_pinned = pytest.mark.skipif(PINNED is None, reason="the solana row pins no program yet")


def test_solana_row_is_listed_and_off(session, account, monkeypatch):
    net = crx.NETWORKS["solana"]
    assert net["family"] == "solana" and net["rpc_url"] is None
    assert net["base_url"] == "https://portal.crxfx.com/api"
    assert net["genesis_hash"] == net["cluster_tag"] == GENESIS and net["mint"] == MINT
    assert PINNED is None or isinstance(PINNED, str)
    for k in ("CRX_ALLOW_MAINNET", "CRX_BASE", "CRX_RPC"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(crx.ConfigError):
        crx.Client(key=account.key.hex(), network="solana", base_url=BASE, rpc_url=RPC, session=session)


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


@pytest.fixture
def unpinned_net(monkeypatch):
    """A Solana row with no program: the state of the real row before the launch."""
    net = dict(crx.NETWORKS["solana"], program_id=None)
    monkeypatch.setitem(crx.NETWORKS, "solana-u", net)
    for k in ("CRX_ALLOW_MAINNET", "CRX_BASE", "CRX_RPC"):
        monkeypatch.delenv(k, raising=False)
    return net


def test_an_unpinned_network_reads_no_wallet_and_signs_nothing(unpinned_net, session, tmp_path, account, monkeypatch):
    """No program pinned: a client is not made from a wallet; bind, deposit, withdraw and the trade binder
    refuse with one line. No wallet is read, no request carries a signature, no RPC is called."""
    read = []

    def no_read(secret):
        read.append(1)
        raise AssertionError("the wallet was read")
    monkeypatch.setattr(sol, "Keypair", no_read)
    session.routes[("GET", "/health")] = sol_health()
    common = dict(network="solana-u", base_url=BASE, rpc_url=RPC, session=session, allow_mainnet=True,
                  state_dir=tmp_path / "s")
    with pytest.raises(crx.ConfigError) as ei:
        crx.Client(keypair=list(SEED), **common)
    assert str(ei.value) == sol.UNPINNED and _no_secret(ei.value)
    c = crx.Client(key=account.key.hex(), **common)
    calls = {
        "bind": lambda: c.bind(AUTH),
        "deposit with a keypair": lambda: c.deposit("100", keypair=list(SEED)),
        "unsigned deposit": lambda: c.deposit("100", unsigned=True, authority=AUTH),
        "withdraw": lambda: c.withdraw("100"),
        "trade binder": lambda: c._binder(),
        "chain check": lambda: c._chain_ready(),
    }
    for name, call in calls.items():
        with pytest.raises(crx.ConfigError) as ei:
            call()
        assert str(ei.value) == sol.UNPINNED, name
        assert _no_secret(ei.value), name
    assert read == [] and session.rpc_calls == []
    assert session.calls and set(session.paths()) == {"/health"}
    assert all(not str(h).lower().startswith("x-crx-") for req in session.calls for h in req["headers"])
    # Reads that need no key stay open.
    assert c.health()["status"] == "ok" and c.next_check() is not None


@while_unpinned
def test_this_sdk_pins_no_program_yet(session, tmp_path, account, monkeypatch):
    assert PINNED_VECTOR == {"alias": "", "separator": ""}
    for k in ("CRX_ALLOW_MAINNET", "CRX_BASE", "CRX_RPC"):
        monkeypatch.delenv(k, raising=False)
    c = crx.Client(key=account.key.hex(), network="solana", base_url=BASE, rpc_url=RPC, session=session,
                   allow_mainnet=True, state_dir=tmp_path / "s")
    session.routes[("GET", "/health")] = sol_health()
    with pytest.raises(crx.ConfigError) as ei:
        c._chain_ready()  # no program pinned: refuses before any signature
    assert str(ei.value) == sol.UNPINNED
    with pytest.raises(crx.ConfigError) as ei:
        crx.Client(keypair=list(SEED), network="solana", base_url=BASE, rpc_url=RPC, session=session,
                   allow_mainnet=True, state_dir=tmp_path / "s")
    assert str(ei.value) == sol.UNPINNED


@while_pinned
def test_this_sdk_pins_the_record_program():
    """The pinned program is one canonical 32-byte base58 key; its alias and separator are the record's."""
    net = crx.NETWORKS["solana"]
    sol.need_pin(net)
    assert sol.b58encode(sol.key(PINNED)) == PINNED
    alias = sol.alias(net["cluster_tag"], PINNED)
    assert alias == PINNED_VECTOR["alias"]
    assert e7.h0x(e7.domain_separator(None, alias)) == PINNED_VECTOR["separator"]


@pytest.mark.parametrize("now, at", [("08:00:00", "08:05:00"), ("08:05:00", "09:05:00"), ("08:35:00", "09:05:00")])
def test_solana_next_check_is_05_past_the_hour(session, tmp_path, account, monkeypatch, now, at):
    from datetime import datetime
    for k in ("CRX_ALLOW_MAINNET", "CRX_BASE", "CRX_RPC"):
        monkeypatch.delenv(k, raising=False)
    c = crx.Client(key=account.key.hex(), network="solana", base_url=BASE, rpc_url=RPC, session=session,
                   allow_mainnet=True, state_dir=tmp_path / "s")
    c._clock = lambda: datetime.fromisoformat(f"2026-10-01T{now}+00:00").timestamp()
    assert c.next_check() == datetime.fromisoformat(f"2026-10-01T{at}+00:00")
    assert session.calls == []


def test_chain_check_passes(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    row = c._chain_ready()
    assert row["chain_id"] is None and row["core"] == sol.alias(GENESIS, PID)
    assert c._sep == e7.domain_separator(None, row["core"])
    assert solsession.rpc_methods() == ["getGenesisHash", "getAccountInfo"]


@pytest.mark.parametrize("step", ["family", "genesis", "program", "rpc_genesis", "not_exec", "owner", "alias",
                                  "domain", "core", "vault"])
def test_chain_check_refuses_each_step(solnet, solsession, tmp_path, account, step):
    other = sol.b58encode(bytes(range(2, 34)))
    if step == "family":
        solsession.routes[("GET", "/health")] = sol_health(family="evm")
    elif step == "genesis":
        solsession.routes[("GET", "/health")] = sol_health(genesis_hash="EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG")
    elif step == "program":
        solsession.routes[("GET", "/health")] = sol_health(pid=other)
    elif step == "rpc_genesis":
        solsession.rpc["getGenesisHash"] = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
    elif step == "not_exec":
        solsession.rpc["getAccountInfo"] = {"value": {"executable": False, "owner": sol.LOADER_V3}}
    elif step == "owner":
        solsession.rpc["getAccountInfo"] = {"value": {"executable": True, "owner": sol.SYSTEM_PROGRAM}}
    elif step == "alias":
        solsession.routes[("GET", "/health")] = sol_health(verifying_contract="0x" + "ab" * 20)
    elif step == "domain":
        solsession.routes[("GET", "/health")] = sol_health(domain="0x" + "cd" * 32)
    elif step == "core":
        solsession.routes[("GET", "/health")] = sol_health(core=sol.pda(PID, b"seats"))
    elif step == "vault":
        solsession.routes[("GET", "/health")] = sol_health(vault=sol.pda(PID, b"seats"))
    c = sclient(solsession, tmp_path, account)
    with pytest.raises(crx.CrxError):
        c._chain_ready()


def bind_routes(session, client, nonce=0, deadline=None, **over):
    deadline = deadline or int(client._clock()) + 3600
    alias = sol.alias(GENESIS, PID)
    msg = {"seat": client.address, "authority": e7.h0x(sol.key(AUTH)), "payout": e7.h0x(sol.key(AUTH)),
           "nonce": str(nonce), "deadline": str(deadline)}
    td = e7.typed_data("BindSeat", None, alias, msg)
    body = {"bind": {"seat": client.address, "authority": AUTH, "payout": AUTH, "nonce": str(nonce),
                     "deadline": deadline},
            "payout_ata": AUTH_ATA, "row": 3, "digest": e7.h0x(e7.typed_digest(td)), "typed_data": td,
            "program_id": PID, "verifying_contract": alias}
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


def test_bind_signs_the_sdk_own_typed_data(solnet, solsession, tmp_path, account):
    from crx.signer import recover
    c = sclient(solsession, tmp_path, account)
    posted, td = bind_routes(solsession, c)
    out = c.bind(AUTH)
    assert out["status"] == "bound"
    sig = posted[-1]["sig"]
    assert recover(e7.typed_digest(td), bytes.fromhex(sig[2:])) == c.address


@pytest.mark.parametrize("bad", ["ata", "digest", "typed", "deadline_far", "deadline_past", "seat"])
def test_bind_refuses_a_served_change(solnet, solsession, tmp_path, account, bad):
    c = sclient(solsession, tmp_path, account)
    now = int(c._clock())
    over = {}
    if bad == "ata":
        over["payout_ata"] = sol.pda(PID, b"vault")
    elif bad == "digest":
        over["digest"] = "0x" + "00" * 32
    posted, td = bind_routes(solsession, c, **over)
    if bad == "typed":
        t2 = copy.deepcopy(td)
        t2["domain"]["verifyingContract"] = "0x" + "ab" * 20
        posted, _ = bind_routes(solsession, c, typed_data=t2)
    elif bad == "deadline_far":
        posted, _ = bind_routes(solsession, c, deadline=now + 25 * 3600)
    elif bad == "deadline_past":
        posted, _ = bind_routes(solsession, c, deadline=now - 1)
    elif bad == "seat":
        posted, td = bind_routes(solsession, c)
        orig = solsession.routes[("POST", "/bind")]

        def swap(req):
            r = orig(req)
            if isinstance(r, dict):
                r = copy.deepcopy(r)
                r["bind"]["seat"] = "0x" + "99" * 20
            return r
        solsession.routes[("POST", "/bind")] = swap
    with pytest.raises(crx.RefusedToSign):
        c.bind(AUTH)
    assert all("sig" not in p for p in posted)


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


def test_deposit_refuses_a_wrong_keypair_and_unbound(solnet, solsession, tmp_path, account):
    pytest.importorskip("cryptography")
    c = sclient(solsession, tmp_path, account)
    deposit_routes(solsession, c)
    with pytest.raises(crx.ConfigError):
        c.deposit("100", keypair=list(bytes([8] * 32)))
    solsession.routes[("GET", "/bind")] = {"bound": False, "status": "unbound"}
    with pytest.raises(crx.RefusedToSign):
        c.deposit("100", unsigned=True, authority=AUTH)
    assert ("POST", "/deposit") not in [(x["method"], x["path"]) for x in solsession.calls]


def test_deposit_refuses_a_served_amount_change(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    deposit_routes(solsession, c, amount_raw=100_000_001)
    with pytest.raises(crx.RefusedToSign):
        c.deposit("100", unsigned=True, authority=AUTH)


def test_withdraw_needs_a_bind_and_signs_under_the_3field_domain(solnet, solsession, tmp_path, account):
    from crx.signer import recover
    c = sclient(solsession, tmp_path, account)
    solsession.routes[("GET", "/balance")] = {"account": c.address, "withdraw": {"nonce": "4", "live": True}}
    seen = {}

    def wd(req):
        b = req["body"]
        w = {"account": c.address, "amount": e7.scaled6("10"), "recipient": c.address, "nonce": int(b["nonce"]),
             "deadline": b["deadline"]}
        seen["w"], seen["sig"] = w, b["sig"]
        return (202, {"item": e7.h0x(e7.withdraw_item(w))})
    solsession.routes[("POST", "/withdraw")] = wd
    solsession.routes[("GET", "/bind")] = {"bound": False, "status": "unbound"}
    with pytest.raises(crx.RefusedToSign):
        c.withdraw("10")
    assert "w" not in seen
    solsession.routes[("GET", "/bind")] = {"bound": True, "status": "bound", "authority": AUTH,
                                           "payout_ata": AUTH_ATA}
    c.withdraw("10")
    sep = e7.domain_separator(None, sol.alias(GENESIS, PID))
    assert recover(e7.withdraw_digest(sep, seen["w"]), bytes.fromhex(seen["sig"][2:])) == c.address


def test_bind_is_solana_only(make_client):
    c = make_client()
    with pytest.raises(crx.ConfigError):
        c.bind(AUTH)


# ---------- the seat key derived from the wallet (same text and vector as the site) ----------

SITE_SEAT = "0xef9a243b0c45e3e8caa096cb4b06f88f9bc5a46c"  # the site's seat for the wallet of seed 0x07 * 32


def test_seat_from_wallet_golden():
    pytest.importorskip("cryptography")
    kp = sol.Keypair(SEED)
    assert kp.pubkey == AUTH
    from eth_account import Account
    seat = Account.from_key(sol.seat_secret(kp)).address.lower()
    assert seat == SITE_SEAT == HOST_VECTORS[HOST_TODAY][2]
    assert seat != HOST_VECTORS[OTHER_HOST][2]
    assert sol.trading_key_text(AUTH).startswith(b"CRX trading key for " + AUTH.encode() + b" on portal.crxfx.com")


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


def test_client_from_keypair(solnet, solsession, tmp_path):
    pytest.importorskip("cryptography")
    c = crx.Client(network="solana-t", keypair=list(SEED), base_url=BASE, rpc_url=RPC, session=solsession,
                   state_dir=tmp_path / "s", allow_mainnet=True)
    assert c.address == SITE_SEAT
    clk = Clock(1_760_000_000)
    c._clock, c._sleep = clk, clk.sleep
    posted, _ = bind_routes(solsession, c)
    assert c.bind()["status"] == "bound"
    assert posted[0]["authority"] == AUTH and posted[0]["payout_wallet"] == AUTH
    with pytest.raises(crx.ConfigError):
        crx.Client(network="solana-t", keypair=list(SEED), key="0x" + "11" * 32, base_url=BASE, rpc_url=RPC,
                   session=solsession, allow_mainnet=True)


def test_keypair_is_solana_only(session, tmp_path):
    with pytest.raises(crx.ConfigError, match="keypair= is for network='solana'"):
        crx.Client(keypair=list(SEED), base_url=BASE, rpc_url=RPC, session=session, state_dir=tmp_path / "s")


def test_deposit_check_refuses_an_extra_unused_signer():
    seat = b"\x11" * 20
    raw = build_tx(AUTH, deposit_ixs(seat))
    # Header: one more required signature; an extra key in the signer block that no instruction uses.
    t = sol.parse_tx(raw)
    msg = t["message"]
    extra = sol.key(sol.pda(PID, b"reserve"))
    n_keys = len(t["keys"])
    body = msg[3 + 1 + 32 * n_keys:]
    # Re-index: the new key sits at index 1, so every index >= 1 shifts by one.
    keys = [sol.key(t["keys"][0]), extra] + [sol.key(k) for k in t["keys"][1:]]
    ix_bytes = bytearray(body[32:])
    out = bytearray()
    i = 0
    nix = ix_bytes[i]; i += 1
    out.append(nix)
    for _ in range(nix):
        prog = ix_bytes[i]; i += 1
        out.append(prog + 1 if prog >= 1 else prog)
        na = ix_bytes[i]; i += 1
        out.append(na)
        for _ in range(na):
            a = ix_bytes[i]; i += 1
            out.append(a + 1 if a >= 1 else a)
        nd = ix_bytes[i]; i += 1
        out.append(nd)
        out += ix_bytes[i:i + nd]; i += nd
    new_msg = bytes([2, msg[1], msg[2], n_keys + 1]) + b"".join(keys) + body[:32] + bytes(out)
    raw2 = bytes([2]) + b"\0" * 128 + new_msg
    with pytest.raises(crx.RefusedToSign):
        check(raw2, seat)


def test_deposit_credited_without_a_tx_id(solnet, solsession, tmp_path, account):
    pytest.importorskip("cryptography")
    c = sclient(solsession, tmp_path, account)
    deposit_routes(solsession, c)
    solsession.rpc.update({
        "sendTransaction": lambda p: "x",
        "getSignatureStatuses": lambda p: {"value": [{"confirmationStatus": "confirmed", "err": None}]},
        "getBlockHeight": 10,
    })
    seen = {"n": 0}

    def balance(req):
        seen["n"] += 1
        old = {"tx": None, "amount": "100", "status": "credited", "at": 1}
        new = {"tx": None, "amount": "100", "status": "credited", "at": 2}
        return {"account": c.address, "deposit": {"last": old if seen["n"] == 1 else new}}
    solsession.routes[("GET", "/balance")] = balance
    d = c.deposit("100", keypair=list(SEED))
    assert d.status == "credited"


def test_deposit_does_not_take_an_old_record_without_a_tx_id(solnet, solsession, tmp_path, account):
    pytest.importorskip("cryptography")
    c = sclient(solsession, tmp_path, account)
    deposit_routes(solsession, c)
    solsession.rpc.update({
        "sendTransaction": lambda p: "x",
        "getSignatureStatuses": lambda p: {"value": [{"confirmationStatus": "confirmed", "err": None}]},
        "getBlockHeight": 10,
    })
    old = {"tx": None, "amount": "100", "status": "credited", "at": 1}
    solsession.routes[("GET", "/balance")] = {"account": c.address, "deposit": {"last": old}}
    d = c.deposit("100", keypair=list(SEED))
    assert d.status == "pending"


# ---------- the wallet secret leaves no trace in errors ----------

SECRET64 = SEED + sol.key(AUTH)


def _secret_strings():
    return [repr(SEED)[2:40], SEED.hex(), SECRET64.hex(), sol.b58encode(SECRET64), "7, 7, 7, 7, 7, 7, 7",
            sol.b58encode(SEED)]


def _no_secret(exc, extra=()):
    """The exception, its chain and the locals of every SDK frame it passed hold no form of the wallet
    secret (nor any string in ``extra``). The test's own frame (which holds the secret it passed in) is left out."""
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


def test_client_init_drops_the_wallet_secret_on_every_refusal(solnet, solsession, session, tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.delenv("CRX_ALLOW_MAINNET", raising=False)
    common = dict(base_url=BASE, rpc_url=RPC, session=solsession, state_dir=tmp_path / "s")
    cases = [
        dict(network="solana-t", allow_mainnet=False),  # mainnet is off: raised before the wallet is read
        dict(network="testnet"),  # keypair= on an Ethereum network
        dict(network="nowhere"),
        dict(network="solana-t", key="0x" + "11" * 32, allow_mainnet=True),
        dict(network="solana-t", signer=object(), key="0x" + "11" * 32, allow_mainnet=True),
    ]
    for kw in cases:
        for secret in (SECRET64, list(SECRET64), bytearray(SECRET64)):
            with pytest.raises(crx.ConfigError) as ei:
                crx.Client(keypair=secret, **common, **kw)
            assert _no_secret(ei.value), kw


def test_hostile_deposit_answer_leaves_no_secret_in_frames(solnet, solsession, tmp_path, account):
    pytest.importorskip("cryptography")
    c = sclient(solsession, tmp_path, account)
    me = bytes.fromhex(c.address[2:])
    deposit_routes(solsession, c, ixs=deposit_ixs(me) + [(sol.SYSTEM_PROGRAM, [(AUTH, True, True)], b"\x02")])
    for secret in (SECRET64, list(SECRET64)):
        with pytest.raises(crx.RefusedToSign) as ei:
            c.deposit("100", keypair=secret)
        assert _no_secret(ei.value)
    with pytest.raises(crx.ConfigError) as ei:
        c.deposit("100", keypair=bytes(SEED[:31]) + b"\x08" + sol.key(AUTH))  # public half does not match
    assert _no_secret(ei.value)


def test_keypair_errors_carry_no_secret():
    pytest.importorskip("cryptography")
    for secret in (SEED + b"\x07", SECRET64[:63], bytes(SEED[:31]) + b"\x08" + sol.key(AUTH),
                   list(SEED) + [256], list(SEED)[:31] + [True]):
        with pytest.raises(crx.ConfigError) as ei:
            sol.Keypair(secret)
        assert _no_secret(ei.value) and ei.value.__context__ is None


def test_keypair_refuses_a_base58_secret_string():
    pytest.importorskip("cryptography")
    for secret in (sol.b58encode(SECRET64), sol.b58encode(SEED)):
        with pytest.raises(crx.ConfigError) as ei:
            sol.Keypair(secret)
        assert "base58" in str(ei.value) and _no_secret(ei.value)


@pytest.mark.parametrize("content", ["base58", "trailing_comma", "byte_256", "nested", "object"])
def test_keypair_file_errors_carry_no_secret(tmp_path, content):
    pytest.importorskip("cryptography")
    import json
    import os
    p = tmp_path / "id.json"
    body = {"base58": sol.b58encode(SECRET64), "trailing_comma": json.dumps(list(SECRET64))[:-1] + ",]",
            "byte_256": json.dumps(list(SECRET64)[:-1] + [256]), "nested": "[" * 100_000 + "7" + "]" * 100_000,
            "object": json.dumps({"secret": list(SECRET64)})}[content]
    p.write_text(body)
    os.chmod(p, 0o600)
    with pytest.raises(crx.ConfigError) as ei:
        sol.Keypair(str(p))
    assert _no_secret(ei.value) and ei.value.__context__ is None and ei.value.__cause__ is None
    # The file reader itself, under the Keypair's own wrapper: no context, no secret in its frames.
    with pytest.raises(crx.ConfigError) as ei:
        sol._read_keypair_file(p)
    assert _no_secret(ei.value, ["7,7,7,7,7,7,7"]) and ei.value.__context__ is None


def test_keypair_repr_never_raises_and_holds_no_secret():
    pytest.importorskip("cryptography")
    whole = sol.Keypair(SECRET64)
    assert repr(whole) == str(whole) == f"Keypair({AUTH})"
    assert [x for x in _secret_strings() if x in repr(whole)] == []
    # A keypair whose key was refused, and one whose __init__ never ran: each prints None.
    with pytest.raises(crx.ConfigError) as ei:
        sol.Keypair(bytes(SEED[:31]) + b"\x08" + sol.key(AUTH))  # public half does not match
    tb = ei.value.__traceback__
    while tb.tb_next is not None:
        tb = tb.tb_next
    for half in (tb.tb_frame.f_locals["self"], sol.Keypair.__new__(sol.Keypair)):
        assert repr(half) == str(half) == "Keypair(None)"
    assert repr(crx.Client.__new__(crx.Client)) == "crx.Client(network=None, address=None)"


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


def test_keypair_file_must_be_private(tmp_path):
    pytest.importorskip("cryptography")
    import json
    import os
    p = tmp_path / "id.json"
    p.write_text(json.dumps(list(SECRET64)))
    os.chmod(p, 0o644)
    with pytest.raises(crx.ConfigError) as ei:
        sol.Keypair(str(p))
    assert "chmod 600" in str(ei.value)
    os.chmod(p, 0o600)
    assert sol.Keypair(str(p)).pubkey == AUTH and sol.Keypair(p).pubkey == AUTH


def test_seat_refusal_drops_the_signature():
    pytest.importorskip("cryptography")
    import traceback

    class Flaky(sol.Keypair):
        n = 0

        def sign(self, m):
            self.n += 1
            return bytes([0xAB]) * 64 if self.n == 1 else bytes([0xCD]) * 64
    with pytest.raises(crx.ConfigError) as ei:
        sol.seat_secret(Flaky(SEED))
    text = "".join(traceback.TracebackException.from_exception(ei.value, capture_locals=True).format())
    assert "abababab" not in text.replace("\\x", "").lower() and "\\xab\\xab" not in text


# ---------- the deposit tx holds exactly its keys ----------

def _with_key(raw, k, *, dup_at=None):
    """The deposit tx with key ``k`` added to the unsigned writable block; ``dup_at`` points that deposit
    account index at it."""
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


def test_deposit_check_refuses_a_writable_program():
    seat = b"\x11" * 20
    raw = build_tx(AUTH, deposit_ixs(seat))
    n = raw[0]
    i = 1 + 64 * n
    bad = raw[:i + 2] + bytes([raw[i + 2] - 1]) + raw[i + 3:]  # one fewer read-only unsigned key
    with pytest.raises(crx.RefusedToSign):
        check(bad, seat)


def test_deposit_check_refuses_zero_units():
    seat = b"\x11" * 20
    with pytest.raises(crx.RefusedToSign):
        check(build_tx(AUTH, deposit_ixs(seat, units=0, micro=2**64 - 1)), seat)


def test_deposit_check_rounds_the_fee_up():
    seat = b"\x11" * 20
    assert check(build_tx(AUTH, deposit_ixs(seat, units=400_000, micro=250_000)), seat)  # 100 000 lamports
    with pytest.raises(crx.RefusedToSign):
        check(build_tx(AUTH, deposit_ixs(seat, units=400_000, micro=250_002)), seat)  # 100 000.8, the runtime 100 001


# ---------- the unsigned deposit names its wallet; bind refuses a nonce past u64 ----------

def test_unsigned_deposit_refuses_a_served_authority_other_than_the_wallet(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    evil = sol.b58encode(bytes([0xEE]) * 32)
    deposit_routes(solsession, c)
    solsession.routes[("GET", "/bind")] = {"bound": True, "status": "bound", "authority": evil,
                                           "payout_wallet": evil, "payout_ata": sol.ata(evil, MINT)}
    with pytest.raises(crx.ConfigError):
        c.deposit("100", unsigned=True, authority=AUTH)
    assert ("POST", "/deposit") not in [(x["method"], x["path"]) for x in solsession.calls]


def test_unsigned_deposit_needs_the_wallet(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    deposit_routes(solsession, c)
    with pytest.raises(crx.ConfigError):
        c.deposit("100", unsigned=True)
    with pytest.raises(crx.BadRequest):
        c.deposit("100", unsigned=True, authority="not-a-key")
    with pytest.raises(crx.ConfigError):
        c.deposit("100", keypair=list(SEED), authority=AUTH)
    assert ("POST", "/deposit") not in [(x["method"], x["path"]) for x in solsession.calls]


def test_unsigned_deposit_takes_the_client_keypair(solnet, solsession, tmp_path):
    pytest.importorskip("cryptography")
    c = crx.Client(network="solana-t", keypair=list(SEED), base_url=BASE, rpc_url=RPC, session=solsession,
                   state_dir=tmp_path / "s", allow_mainnet=True)
    raw = deposit_routes(solsession, c)
    d = c.deposit("100", unsigned=True)
    assert d.status == "unsigned" and base64.b64decode(d.txs[0]) == raw


def test_solana_deposit_arguments_refuse_on_ethereum(make_client, session):
    c = make_client()
    for kw in (dict(unsigned=True), dict(keypair=list(SEED)), dict(authority=AUTH)):
        with pytest.raises(crx.ConfigError):
            c.deposit("100", **kw)
    assert session.calls == [] and session.rpc_calls == []


def test_bind_refuses_a_nonce_past_u64(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    now = int(c._clock())
    for nonce in (2**64, -1):
        posted, _ = bind_routes(solsession, c, bind={"seat": c.address, "authority": AUTH, "payout": AUTH,
                                                     "nonce": str(nonce), "deadline": now + 3600})
        with pytest.raises(crx.RefusedToSign):
            c.bind(AUTH)
        assert all("sig" not in p for p in posted)


def _string_secrets():
    """The wallet secret as text, in forms that are not a file path."""
    import json
    b58 = sol.b58encode(SECRET64)
    return {
        "hex128": "0x" + SECRET64.hex(), "hex128_bare": SECRET64.hex(), "hex64": "0x" + SEED.hex(),
        "json_text": json.dumps(list(SECRET64)), "json_compact": json.dumps(list(SECRET64), separators=(",", ":")),
        "b58_quoted": '"' + b58 + '"', "b58_cut": b58[:-1], "b58_seed_cut": sol.b58encode(SEED)[:-1],
    }


@pytest.mark.parametrize("form", sorted(_string_secrets()))
def test_a_string_secret_leaves_no_frame(solnet, solsession, tmp_path, account, form):
    pytest.importorskip("cryptography")
    secret = _string_secrets()[form]
    marks = [secret.strip('"')[:40], "7,7,7,7,7,7,7"]
    with pytest.raises(crx.ConfigError) as ei:
        sol.Keypair(secret)
    assert _no_secret(ei.value, marks) and ei.value.__context__ is None
    with pytest.raises(crx.ConfigError) as ei:
        crx.Client(network="solana-t", keypair=secret, base_url=BASE, rpc_url=RPC, session=solsession,
                   state_dir=tmp_path / "s", allow_mainnet=True)
    assert _no_secret(ei.value, marks)
    c = sclient(solsession, tmp_path, account)
    deposit_routes(solsession, c)
    with pytest.raises(crx.ConfigError) as ei:
        c.deposit("100", keypair=secret)
    assert _no_secret(ei.value, marks)


def test_keypair_odd_paths_are_config_errors(tmp_path):
    pytest.importorskip("cryptography")

    class BytesPath:
        def __fspath__(self):
            return b"/no/such/id.json"
    for p in (str(tmp_path / "id\0.json"), BytesPath(), tmp_path / "missing.json"):
        with pytest.raises(crx.ConfigError) as ei:
            sol.Keypair(p)
        assert ei.value.__context__ is None


def test_ethereum_deposit_refusal_drops_the_wallet_secret(make_client):
    c = make_client()
    for secret in (SECRET64, list(SECRET64)):
        with pytest.raises(crx.ConfigError) as ei:
            c.deposit("100", keypair=secret)
        assert _no_secret(ei.value)


def test_client_init_never_holds_the_seat_key(solnet, solsession, tmp_path):
    pytest.importorskip("cryptography")
    seat = sol.seat_secret(sol.Keypair(SEED))
    marks = [seat.hex(), repr(seat)[2:30]]
    for kw in (dict(base_url="https://[::1"), dict(base_url=BASE, timeout="10"), dict(base_url=123)):
        with pytest.raises(Exception) as ei:
            crx.Client(network="solana-t", keypair=list(SEED), rpc_url=RPC, session=solsession,
                       state_dir=tmp_path / "s", allow_mainnet=True, **kw)
        assert not isinstance(ei.value, crx.ConfigError) and _no_secret(ei.value, marks), kw


def test_an_off_solana_network_reads_no_wallet(solnet, solsession, tmp_path, monkeypatch):
    """The allow_mainnet and RPC refusals come before the keypair is read or the seat key derived."""
    monkeypatch.delenv("CRX_ALLOW_MAINNET", raising=False)
    monkeypatch.delenv("CRX_RPC", raising=False)
    read = []

    def no_read(secret):
        read.append(1)
        raise AssertionError("the wallet was read")
    monkeypatch.setattr(sol, "Keypair", no_read)
    for kw, msg in ((dict(allow_mainnet=False, rpc_url=RPC), "mainnet is off"),
                    (dict(allow_mainnet=True), "no default RPC")):
        with pytest.raises(crx.ConfigError) as ei:
            crx.Client(network="solana-t", keypair=list(SEED), base_url=BASE, session=solsession,
                       state_dir=tmp_path / "s", **kw)
        assert msg in str(ei.value)
    assert read == []


# ---------- the host: one value; the key text and the default gateway URL are built from it ----------

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


def test_the_host_is_named_once_in_the_code():
    """One constant names the host. The default gateway URL is built from it. The README and the changelog
    name the same host and no other."""
    from pathlib import Path
    root = Path(__file__).parent.parent
    assert sol.HOST == HOST_TODAY
    assert crx.NETWORKS["solana"]["base_url"] == "https://" + HOST_TODAY + "/api"
    hits = [(f.name, line.strip()) for f in sorted((root / "src" / "crx").glob("*.py"))
            for line in f.read_text().splitlines() if any(h in line for h in HOST_VECTORS)]
    assert hits == [("_solana.py", 'HOST = "' + HOST_TODAY + '"')]
    for doc in ("README.md", "CHANGELOG.md"):
        text = (root / doc).read_text()
        assert HOST_TODAY in text, doc
        assert [h for h in HOST_VECTORS if h != HOST_TODAY and h in text] == [], doc


def test_the_key_text_is_the_pinned_bytes():
    """The text built from the host constant equals the pinned literal, byte for byte; each host has its own."""
    text, digest, _ = HOST_VECTORS[HOST_TODAY]
    assert sol.trading_key_text(AUTH) == text
    assert len(text) == 225 and keccak(text).hex() == digest
    for host, (want, digest, _) in HOST_VECTORS.items():
        got = sol.trading_key_text(AUTH, host)
        assert got == want and keccak(got).hex() == digest, host
    assert len({text for text, _, _ in HOST_VECTORS.values()}) == len(HOST_VECTORS)


def test_the_key_text_names_the_host_twice_and_no_other_host():
    """The key text names the SDK's host twice: once after the wallet and once as the https origin it ends on.
    It names no other host."""
    text = sol.trading_key_text(AUTH)
    host = HOST_TODAY.encode()
    assert text.count(host) == 2 and text.count(b"crxfx.com") == 2
    assert text.count(b" on " + host + b" (Solana mainnet). ") == 1
    assert text.endswith(b" Sign it only on https://" + host + b".")
    assert OTHER_HOST.encode() not in text
    assert text != HOST_VECTORS[OTHER_HOST][0]


def test_each_host_gives_its_own_seat():
    """The same wallet has one seat per host: the seat of today's host is the site's, and no two hosts share one."""
    pytest.importorskip("cryptography")
    from eth_account import Account
    kp = sol.Keypair(SEED)
    seats = {host: Account.from_key(sol.seat_secret(kp, host)).address.lower() for host in HOST_VECTORS}
    assert seats == {host: seat for host, (_, _, seat) in HOST_VECTORS.items()}
    assert len(set(seats.values())) == len(seats)
    assert Account.from_key(sol.seat_secret(kp)).address.lower() == seats[HOST_TODAY] == SITE_SEAT
    assert SITE_SEAT != seats[OTHER_HOST]


def test_a_client_with_no_gateway_named_talks_to_the_host(solnet, session, account, tmp_path):
    """With no base_url= and no CRX_BASE the gateway is the host's own URL."""
    c = crx.Client(key=account.key.hex(), network="solana-t", rpc_url=RPC, session=session,
                   state_dir=tmp_path / "s", allow_mainnet=True)
    assert c._gw.base_url == "https://" + HOST_TODAY + "/api"
    assert c._gw.host == HOST_TODAY


def test_another_gateway_url_does_not_move_the_seat(solnet, solsession, tmp_path, monkeypatch):
    """base_url= and CRX_BASE name the gateway only: the key text and the seat stay those of the SDK's host."""
    pytest.importorskip("cryptography")
    other = "https://" + OTHER_HOST + "/api"
    for by_env in (False, True):
        if by_env:
            monkeypatch.setenv("CRX_BASE", other)
        c = crx.Client(network="solana-t", keypair=list(SEED), rpc_url=RPC, session=solsession,
                       state_dir=tmp_path / "s", allow_mainnet=True, **({} if by_env else {"base_url": other}))
        assert c._gw.base_url == other
        assert c.address == SITE_SEAT and c.address != HOST_VECTORS[OTHER_HOST][2]


# ---------- a deposit the program refuses on a full intake (codes 660 and 661) ----------

RING_TEXT = "no USDC left the wallet; the chain's intake is full until the hourly check; deposit again after it"
WIRE = bytes([1]) + bytes([5]) * 64 + b"message"  # a wire tx: one signature, then the message
WIRE_SIG = sol.b58encode(bytes([5]) * 64)


def node_refusal(code, shape):
    """A node's error object for custom program error ``code``, in one of the two spellings a node gives."""
    if shape == "text":
        return {"code": -32002, "message": "Transaction simulation failed: Error processing Instruction 2: "
                                           f"custom program error: {code:#x}"}
    return {"code": -32002, "message": "Transaction simulation failed",
            "data": {"err": {"InstructionError": [2, {"Custom": code}]}, "logs": [f"Program {PID} failed"],
                     "unitsConsumed": 9}}


@pytest.mark.parametrize("code", [660, 661])
@pytest.mark.parametrize("shape", ["text", "json"])
def test_program_code_reads_both_spellings(code, shape):
    assert sol.program_code(node_refusal(code, shape)) == code
    assert sol.program_code({"InstructionError": [2, {"Custom": code}]}) == code
    assert sol.program_code(f"Program {PID} failed: custom program error: {code:#x}") == code
    assert f"{code:#x}" in ("0x294", "0x295")


def test_program_code_names_none_for_any_other_answer():
    for err in (None, "", "Blockhash not found", [], {"code": -32002, "message": "x", "data": {"err": "AccountNotFound"}},
                {"InstructionError": [2, "InvalidAccountData"]}, {"InstructionError": [2, {"Custom": True}]},
                {"InstructionError": [2, {"Custom": "660"}]}, {"InstructionError": [2, {"Custom": -1}]},
                {"Custom": 660}, "custom program error: 660", ValueError("custom program error: 0x294")):
        assert sol.program_code(err) is None, err
    deep = {"InstructionError": [2, {"Custom": 660}]}
    for _ in range(3000):
        deep = [deep]
    assert sol.program_code(deep) is None  # past the read bound: no code, no recursion


@pytest.mark.parametrize("code", [660, 661])
@pytest.mark.parametrize("shape", ["text", "json"])
def test_a_deposit_refused_at_the_send_on_a_full_intake_reads_one_line(code, shape):
    from crx._chain import RpcError
    calls = []

    def rpc(method, *params):
        calls.append(method)
        raise RpcError(node_refusal(code, shape))
    with pytest.raises(crx.TxFailed) as ei:
        sol.send_and_confirm(rpc, WIRE, last_valid=None, sleep=lambda s: None)
    e = ei.value
    assert str(e) == "deposit not sent: " + RING_TEXT and e.code == "tx_failed"
    assert e.details == {"tx": WIRE_SIG, "program_error": code, "reason": "intake_full"}
    assert calls == ["sendTransaction"]


@pytest.mark.parametrize("code", [660, 661])
def test_a_landed_deposit_that_failed_on_a_full_intake_reads_one_line(code):
    err = {"InstructionError": [2, {"Custom": code}]}

    def rpc(method, *params):
        if method == "sendTransaction":
            return WIRE_SIG
        assert method == "getSignatureStatuses"
        return {"value": [{"slot": 7, "confirmationStatus": "confirmed", "err": err, "status": {"Err": err}}]}
    with pytest.raises(crx.TxFailed) as ei:
        sol.send_and_confirm(rpc, WIRE, last_valid=500, sleep=lambda s: None)
    e = ei.value
    assert str(e) == f"deposit tx {WIRE_SIG} failed: " + RING_TEXT
    assert e.details == {"tx": WIRE_SIG, "program_error": code, "reason": "intake_full"}


def test_any_other_program_error_reads_as_the_node_sent_it():
    from crx._chain import RpcError
    for shape in ("text", "json"):
        refusal = node_refusal(1, shape)

        def rpc(method, *params):
            raise RpcError(refusal)
        with pytest.raises(crx.TxFailed) as ei:
            sol.send_and_confirm(rpc, WIRE, last_valid=None, sleep=lambda s: None)
        assert str(ei.value) == "deposit not sent: " + crx.errors.clean(refusal, 160)
        assert ei.value.details == {"tx": WIRE_SIG} and RING_TEXT not in str(ei.value)
    err = {"InstructionError": [2, {"Custom": 6}]}

    def landed(method, *params):
        return WIRE_SIG if method == "sendTransaction" else {"value": [{"confirmationStatus": "confirmed", "err": err}]}
    with pytest.raises(crx.TxFailed) as ei:
        sol.send_and_confirm(landed, WIRE, last_valid=None, sleep=lambda s: None)
    assert str(ei.value) == f"deposit tx {WIRE_SIG} failed: " + crx.errors.clean(err, 120)
    assert ei.value.details == {"tx": WIRE_SIG}


def test_client_deposit_on_a_full_intake_raises_the_plain_line(solnet, solsession, tmp_path, account):
    pytest.importorskip("cryptography")
    c = sclient(solsession, tmp_path, account)
    deposit_routes(solsession, c)
    solsession.routes[("GET", "/balance")] = {"account": c.address, "deposit": {"last": None}}
    for shape in ("text", "json"):
        solsession.rpc["sendTransaction"] = {"__error__": node_refusal(660, shape)}
        with pytest.raises(crx.TxFailed) as ei:
            c.deposit("100", keypair=list(SEED))
        assert str(ei.value) == "deposit not sent: " + RING_TEXT
        assert ei.value.details["reason"] == "intake_full" and ei.value.details["program_error"] == 660
        assert _no_secret(ei.value)
    assert "getSignatureStatuses" not in solsession.rpc_methods()


# ---------- the bind read; a bind of a seat that is bound ----------

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


def test_bind_state_is_solana_only(make_client, session):
    c = make_client()
    with pytest.raises(crx.ConfigError):
        c.bind_state()
    assert session.calls == [] and session.rpc_calls == []


@pytest.mark.parametrize("args", [(AUTH,), (AUTH, AUTH)])
def test_bind_of_a_seat_bound_to_the_same_keys_returns_the_bind(solnet, solsession, tmp_path, account, monkeypatch,
                                                                args):
    """No BindSeat is signed, nothing is posted, and no request goes out but the one read."""
    c = sclient(solsession, tmp_path, account)
    solsession.routes[("GET", "/bind")] = dict(BOUND)
    solsession.routes[("POST", "/bind")] = no_post
    signed = []
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(a) or "0x")
    assert c.bind(*args) == BOUND
    assert signed == [] and calls_of(solsession) == [("GET", "/bind")] and solsession.rpc_calls == []
    assert c.bind(*args) == BOUND  # a repeat reads again and stays quiet
    assert signed == [] and calls_of(solsession) == [("GET", "/bind")] * 2


def test_bind_of_a_bound_seat_takes_the_client_keypair(solnet, solsession, tmp_path):
    pytest.importorskip("cryptography")
    c = crx.Client(network="solana-t", keypair=list(SEED), base_url=BASE, rpc_url=RPC, session=solsession,
                   state_dir=tmp_path / "s", allow_mainnet=True)
    solsession.routes[("GET", "/bind")] = dict(BOUND)
    solsession.routes[("POST", "/bind")] = no_post
    assert c.bind() == BOUND and calls_of(solsession) == [("GET", "/bind")]


FILED = {"authority": AUTH, "payout_wallet": AUTH, "payout_ata": AUTH_ATA}
OTHER_PAYOUT_TEXT = "this seat is bound to another payout wallet: the bind is permanent; contact CRX"


def chain_keys(held):
    return {k: held[k] for k in ("authority", "payout_wallet", "payout_ata")}


@pytest.mark.parametrize("held, err, code, text", [
    (dict(BOUND, authority=OTHER), crx.CrxError, "seat_already_bound",
     "this seat is bound already, with another authority"),
    (dict(BOUND, payout_wallet=OTHER, payout_ata=OTHER_ATA), crx.SeatBoundOtherPayout, "seat_bound_other_payout",
     OTHER_PAYOUT_TEXT),
    (dict(BOUND, payout_ata=OTHER_ATA), crx.SeatBoundOtherPayout, "seat_bound_other_payout", OTHER_PAYOUT_TEXT),
    (dict(BOUND, authority=OTHER, payout_wallet=OTHER, payout_ata=OTHER_ATA), crx.SeatBoundOtherPayout,
     "seat_bound_other_payout", OTHER_PAYOUT_TEXT),
], ids=["authority", "payout", "account", "all"])
def test_bind_of_a_seat_bound_to_other_keys_raises_with_what_the_chain_holds(solnet, solsession, tmp_path, account,
                                                                            monkeypatch, held, err, code, text):
    """Another payout wallet or payout account is the payout error, whatever the authority. Nothing is signed
    or posted."""
    c = sclient(solsession, tmp_path, account)
    solsession.routes[("GET", "/bind")] = dict(held)
    solsession.routes[("POST", "/bind")] = no_post
    signed = []
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(a) or "0x")
    with pytest.raises(crx.CrxError) as ei:
        c.bind(AUTH)
    e = ei.value
    assert type(e) is err and e.code == code and e.status is None and str(e) == text
    assert e.details == {"bound": chain_keys(held), "filed": FILED}
    assert signed == [] and calls_of(solsession) == [("GET", "/bind")] and solsession.rpc_calls == []


def test_bind_on_an_unpinned_network_makes_no_request(unpinned_net, session, tmp_path, account):
    c = crx.Client(key=account.key.hex(), network="solana-u", base_url=BASE, rpc_url=RPC, session=session,
                   allow_mainnet=True, state_dir=tmp_path / "s")
    with pytest.raises(crx.ConfigError) as ei:
        c.bind(AUTH)
    assert str(ei.value) == sol.UNPINNED and session.calls == [] and session.rpc_calls == []


# ---------- a seat CRX stopped: one error on deposit, bind, ask, quote and trade; withdraw stays open ----------

STOPPED_TEXT = "CRX removed this seat's access: no new trade and no deposit; withdraw() still works"


def stopped_answer(line):
    """The gateway's answer to a stopped seat, with the line of the door that refused."""
    return (403, {"error": "not whitelisted: account 0x11 was revoked on solana; contact CRX",
                  "code": "not_whitelisted", "outcome": "account_removed", "message": line})


STOPPED_TRADE = stopped_answer("Access removed: no new trades. You can still deposit and withdraw.")
STOPPED_MONEY = stopped_answer("Access removed: no new trades and no deposits. You can still withdraw.")
SOL_MARKETS = {"markets": [{"pair": "USD/MXN", "notional": {"min": "1000", "max": "10000000"},
                            "session": {"open": True}, "tenor": {"min_secs": 600, "max_secs": 7776000},
                            "max_premium_bps": 200}]}
RFQ_ID = "0x" + "aa" * 32


def is_stopped(e):
    assert type(e) is crx.SeatStopped and isinstance(e, crx.NotWhitelisted) and "SeatStopped" in crx.__all__
    assert e.code == "seat_stopped" and e.status == 403 and e.gateway_code == "not_whitelisted"
    assert str(e) == STOPPED_TEXT and e.details == {"outcome": "account_removed"}
    return True


def test_seat_stopped_reads_the_code_and_the_outcome_only():
    body = STOPPED_MONEY[1]
    assert is_stopped(sol.seat_stopped(403, body)) and is_stopped(sol.seat_stopped(403, STOPPED_TRADE[1]))
    others = [
        (401, body), (409, body), (403, dict(body, outcome="account_not_active")), (403, dict(body, code="unauthorized")),
        (403, {k: v for k, v in body.items() if k != "outcome"}), (403, None), (403, "Access removed"), (403, [body]),
        (403, {"error": "not_whitelisted"}),
    ]
    for status, b in others:
        assert sol.seat_stopped(status, b) is None, (status, b)


def test_a_stopped_seat_deposit_and_bind_raise_seat_stopped(solnet, solsession, tmp_path, account, monkeypatch):
    c = sclient(solsession, tmp_path, account)
    assert type(c._gw) is sol.SeatGateway
    signed = []
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(a) or "0x")
    deposit_routes(solsession, c)
    solsession.routes[("POST", "/deposit")] = STOPPED_MONEY
    with pytest.raises(crx.SeatStopped) as ei:
        c.deposit("100", unsigned=True, authority=AUTH)
    assert is_stopped(ei.value)
    solsession.routes[("GET", "/bind")] = {"bound": False, "status": "unbound", "authority": None,
                                           "payout_wallet": None, "payout_ata": None}
    solsession.routes[("POST", "/bind")] = STOPPED_MONEY
    with pytest.raises(crx.SeatStopped) as ei:
        c.bind(AUTH)
    assert is_stopped(ei.value)
    assert signed == [] and "sendTransaction" not in solsession.rpc_methods()


def test_a_stopped_seat_ask_and_quote_raise_seat_stopped(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    solsession.routes[("GET", "/markets")] = SOL_MARKETS
    solsession.routes[("POST", "/rfqs")] = STOPPED_TRADE
    for call in (c.ask, c.quote):
        with pytest.raises(crx.SeatStopped) as ei:
            call("USD/MXN", "buy", 25_000)
        assert is_stopped(ei.value)
    assert len([x for x in calls_of(solsession) if x == ("POST", "/rfqs")]) == 2


@pytest.mark.parametrize("signed_first", [False, True])
def test_a_stopped_seat_trade_raises_seat_stopped(solnet, solsession, tmp_path, account, monkeypatch, signed_first):
    """The accept of a stopped seat reserves nothing: before a signature and after one, the error is
    SeatStopped, not TradeUnknown."""
    c = sclient(solsession, tmp_path, account)
    b = c._binder()
    assert type(b) is sol.SeatBinder
    sig = "0x" + "11" * 65
    monkeypatch.setattr(crx._bind.Binder, "sign_template", lambda self, t, ask: sig)
    solsession.routes[("POST", f"/rfqs/{RFQ_ID}/accept")] = STOPPED_TRADE
    leg = "0x" + "00" * 24 + (int(c._clock()) + 300).to_bytes(8, "big").hex()
    t = {"typed_data": {"message": {"ownNonce": "5", "ownLegId": leg}}} if signed_first else None
    with pytest.raises(crx.SeatStopped) as ei:
        b.accept_trade(RFQ_ID, "q1", {}, t, None)
    assert is_stopped(ei.value)
    posted = [x["body"] for x in solsession.calls if x["path"].endswith("/accept")]
    assert posted == [{"quote_id": "q1", "sig": sig} if signed_first else {"quote_id": "q1"}]


def test_any_other_failure_after_a_signature_is_still_trade_unknown(solnet, solsession, tmp_path, account, monkeypatch):
    c = sclient(solsession, tmp_path, account)
    b = c._binder()
    monkeypatch.setattr(crx._bind.Binder, "sign_template", lambda self, t, ask: "0x" + "11" * 65)
    solsession.routes[("POST", f"/rfqs/{RFQ_ID}/accept")] = (403, {"error": "not whitelisted: no row",
                                                                  "code": "not_whitelisted",
                                                                  "outcome": "account_not_active"})
    leg = "0x" + "00" * 24 + (int(c._clock()) + 300).to_bytes(8, "big").hex()
    with pytest.raises(crx.TradeUnknown) as ei:
        b.accept_trade(RFQ_ID, "q1", {}, {"typed_data": {"message": {"ownNonce": "5", "ownLegId": leg}}}, None)
    assert "may still open" in str(ei.value) and "not_whitelisted" in str(ei.value)


def test_a_stopped_seat_still_withdraws(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    solsession.routes[("GET", "/bind")] = dict(BOUND)
    solsession.routes[("GET", "/balance")] = {"account": c.address, "withdraw": {"nonce": "4", "live": True}}
    for door in (("POST", "/rfqs"), ("POST", "/deposit"), ("POST", "/bind")):
        solsession.routes[door] = STOPPED_MONEY

    def wd(req):
        b = req["body"]
        w = {"account": c.address, "amount": e7.scaled6("10"), "recipient": c.address, "nonce": int(b["nonce"]),
             "deadline": b["deadline"]}
        return (202, {"item": e7.h0x(e7.withdraw_item(w))})
    solsession.routes[("POST", "/withdraw")] = wd
    w = c.withdraw("10")
    assert w.nonce == 4 and w.amount == 10 and ("POST", "/withdraw") in calls_of(solsession)
    assert c.balance().withdraw_nonce == 4 and c.bind_state() == BOUND


def test_an_ethereum_client_reads_that_answer_as_before(make_client, session):
    c = make_client()
    assert type(c._gw) is crx._http.Gateway and type(c._binder()) is crx._bind.Binder
    session.routes[("GET", "/balance")] = STOPPED_TRADE
    with pytest.raises(crx.NotWhitelisted) as ei:
        c.balance()
    e = ei.value
    assert type(e) is crx.NotWhitelisted and e.code == "not_whitelisted" and e.status == 403
    assert str(e) == STOPPED_TRADE[1]["message"] and e.details == {"error": STOPPED_TRADE[1]["error"]}


# ---------- the README's Solana quickstart ----------

def test_readme_solana_quickstart_names_the_version_binds_by_the_keys_and_waits_for_collateral():
    """The sample installs this version. It calls bind(), which compares the three keys, and reads no bind
    flag or status word itself. It waits for credited collateral between the deposit and the first quote. The
    section says nothing about when a deposit is credited."""
    from pathlib import Path
    text = (Path(__file__).parent.parent / "README.md").read_text()
    section = text[text.index("## Solana"):text.index("## Errors")]
    assert f'"crx-python[solana] @ git+https://github.com/crxfoundation/crx-python@v{crx.__version__}"' in section
    assert crx.__version__ == "0.2.0"
    sample = section[section.index("```python"):]
    sample = sample[:sample.index("```", 3)]
    steps = ["c.bind()", "c.deposit(", "while not c.balance().free:", "c.quote(", "c.trade(", "c.withdraw("]
    at = [sample.index(step) for step in steps]
    assert at == sorted(at) and all(sample.count(step) == 1 for step in steps)
    for word in ("bind_state", '"bound"', ".bound", "status"):
        assert word not in sample, word
    for claim in ("next hourly check", "at once", "within", "minutes"):
        assert claim not in section, claim


# ---------- withdraw: the repost after the 409 "the latest settlement is not read yet" ----------

# The wire text of crx-api: its Conflict error displays as "conflict: {0}".
UNREAD = {"error": "conflict: the latest settlement is not read yet; retry shortly", "code": "conflict",
          "outcome": "unavailable", "message": "Service unavailable."}


def withdraw_routes(session, c, refusals):
    """A bound seat with withdraw nonce 4. POST /withdraw answers ``refusals`` 409s first (an int; None: on
    every post), then queues the signed item. Returns the posts: (the clock, the body)."""
    session.routes[("GET", "/bind")] = dict(BOUND)
    session.routes[("GET", "/balance")] = {"account": c.address, "withdraw": {"nonce": "4", "live": True}}
    posts = []

    def wd(req):
        b = req["body"]
        posts.append((c._clock(), b))
        if refusals is None or len(posts) <= refusals:
            return (409, UNREAD)
        w = {"account": c.address, "amount": e7.scaled6(b["amount"]), "recipient": c.address,
             "nonce": int(b["nonce"]), "deadline": b["deadline"]}
        return (202, {"item": e7.h0x(e7.withdraw_item(w))})
    session.routes[("POST", "/withdraw")] = wd
    return posts


def test_solana_withdraw_posts_the_same_signed_intent_again_after_the_refusal(solnet, solsession, tmp_path, account,
                                                                              monkeypatch):
    """One signature. The bound check and the nonce read run once, before the first post. Each repost follows
    a 409 and carries the same body: 15 s, 30 s, then 60 s apart."""
    c = sclient(solsession, tmp_path, account)
    posts = withdraw_routes(solsession, c, refusals=3)
    signed = []
    real = crx.client.sign_typed
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(1) or real(*a, **k))
    t0 = c._clock()
    out = c.withdraw("10")
    assert [round(t - t0) for t, _ in posts] == [0, 15, 45, 105]
    assert all(b == posts[0][1] for _, b in posts) and len(signed) == 1
    assert out.nonce == 4 and posts[0][1]["nonce"] == "4" and posts[0][1]["deadline"] == int(t0) + 22 * 3600
    calls = calls_of(solsession)
    first = calls.index(("POST", "/withdraw"))
    assert calls[:first].count(("GET", "/bind")) == 1 and calls[:first].count(("GET", "/balance")) == 1
    assert calls[first:first + 4] == [("POST", "/withdraw")] * 4 and ("GET", "/bind") not in calls[first:]


def test_solana_withdraw_stops_at_the_20_minute_bound(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    posts = withdraw_routes(solsession, c, refusals=None)
    t0 = c._clock()
    with pytest.raises(crx.CrxError) as ei:
        c.withdraw("10")
    assert str(ei.value) == ("withdraw not sent: the latest settlement is not yet confirmed; "
                             "the request was retried for 20 minutes.")
    assert (ei.value.code, ei.value.status) == ("conflict", 409)
    times = [round(t - t0) for t, _ in posts]
    assert times[:5] == [0, 15, 45, 105, 165] and times[-1] == 1200 and len(times) == 23
    assert all(b == posts[0][1] for _, b in posts)
    assert posts[0][1]["deadline"] > t0 + 1200  # the signed deadline outlasts the retries


@pytest.mark.parametrize("status, body", [
    (409, {"error": "CRX is paused; withdraw after it resumes", "code": "conflict"}),
    (409, {"error": "a withdraw is in progress", "code": "withdraw_in_progress"}),
    (409, {"error": "conflict: the latest settlement is not read yet; retry shortly", "code": "other"}),
    (409, {"error": "conflict: CRX is paused; the latest settlement is not read yet", "code": "conflict"}),
    (503, {"error": "conflict: the latest settlement is not read yet; retry shortly", "code": "conflict"}),
], ids=["paused", "in_progress", "other_code", "text_not_at_start", "other_status"])
def test_solana_withdraw_any_other_refusal_raises_at_once(solnet, solsession, tmp_path, account, status, body):
    c = sclient(solsession, tmp_path, account)
    withdraw_routes(solsession, c, refusals=0)
    posts = []
    solsession.routes[("POST", "/withdraw")] = lambda req: posts.append(c._clock()) or (status, body)
    t0 = c._clock()
    with pytest.raises(crx.CrxError) as ei:
        c.withdraw("10")
    assert str(ei.value) == body["error"] and len(posts) == 1 and c._clock() == t0


def test_solana_withdraw_does_not_post_again_when_the_gateway_gives_no_answer(solnet, solsession, tmp_path, account):
    import requests
    c = sclient(solsession, tmp_path, account)
    withdraw_routes(solsession, c, refusals=0)
    posts = []

    def down(req):
        posts.append(c._clock())
        raise requests.ConnectionError("down")
    solsession.routes[("POST", "/withdraw")] = down
    t0 = c._clock()
    with pytest.raises(crx.NetworkError):
        c.withdraw("10")
    assert len(posts) == 1 and c._clock() == t0


# ---------- the tenor band: on Solana no RFQ leaves outside the band the pair's /markets row serves ----------

T0 = 1_760_000_000  # unix s: 2025-10-09 08:53:20 UTC
BAND = {"min_secs": 7800, "max_secs": 7_776_000}
TOO_SOON = "the settlement is too soon: the shortest trade now settles at 2025-10-09 11:03:20 UTC (7800 s from now)"
TOO_FAR = "the settlement is too far: the longest trade now settles at 2026-01-07 08:53:20 UTC (7776000 s from now)"
RFQ_ANSWER = {"rfq_id": RFQ_ID, "leg_id": "0x" + "bb" * 32, "quote_expiry_max": 1_900_000_000_000}


def tenor_client(session, tmp_path, account, tenor=BAND, answer=RFQ_ANSWER):
    """A Solana client whose USD/MXN row serves ``tenor`` (None: the row has no ``tenor``). POST /rfqs gives
    ``answer``."""
    row = {k: v for k, v in SOL_MARKETS["markets"][0].items() if k != "tenor"}
    if tenor is not None:
        row["tenor"] = tenor
    session.routes[("GET", "/markets")] = {"markets": [row]}
    session.routes[("POST", "/rfqs")] = answer
    return sclient(session, tmp_path, account)


def rfqs_sent(session):
    return [x["body"] for x in session.calls if (x["method"], x["path"]) == ("POST", "/rfqs")]


def tenor_answer(**details):
    """The gateway's refusal of an RFQ outside its tenor band."""
    return (400, {"error": "bad request: tenor 7790s is below the minimum 7800s (expiry 1); see GET /markets",
                  "code": "bad_request", "outcome": "outside_limits",
                  "message": "Fixing date too soon. Earliest is 2025-10-09.", "details": dict(details, limit="tenor")})


def test_the_tenor_check_at_the_edges_of_the_band():
    """The minimum is taken and one ms under it is refused. The last ms of the maximum's second is taken and
    the next ms is refused."""
    ms = T0 * 1000
    assert sol.check_tenor(7800, 7_776_000, ms + 7_800_000, T0) is None
    assert sol.check_tenor(7800, 7_776_000, ms + 7_776_000_999, T0) is None
    with pytest.raises(crx.BadRequest) as ei:
        sol.check_tenor(7800, 7_776_000, ms + 7_799_999, T0)
    e = ei.value
    assert type(e) is crx.BadRequest and (e.code, e.status, e.gateway_code) == ("bad_request", None, None)
    assert e.details == {"limit": "tenor", "tenor_secs": 7799, "min_secs": 7800, "earliest_expiry": ms + 7_800_000}
    assert str(e) == TOO_SOON
    with pytest.raises(crx.BadRequest) as ei:
        sol.check_tenor(7800, 7_776_000, ms + 7_776_001_000, T0)
    e = ei.value
    assert e.details == {"limit": "tenor", "tenor_secs": 7_776_001, "max_secs": 7_776_000,
                         "latest_expiry": ms + 7_776_000_000}
    assert str(e) == TOO_FAR


@pytest.mark.parametrize("served", [600, 7800, 86_400])
def test_the_shortest_trade_is_the_clock_plus_the_served_minimum(served):
    """The instant and the seconds in the message follow the served minimum. A clock between two seconds: the
    instant in ``details`` is exact, the one in the message is the next whole second."""
    now = T0 + 0.25
    with pytest.raises(crx.BadRequest) as ei:
        sol.check_tenor(served, None, T0 * 1000 + 60_000, now)
    e = ei.value
    assert e.details == {"limit": "tenor", "tenor_secs": 59, "min_secs": served,
                         "earliest_expiry": T0 * 1000 + 250 + served * 1000}
    at = datetime.fromtimestamp(T0 + served + 1, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    assert str(e) == f"the settlement is too soon: the shortest trade now settles at {at} ({served} s from now)"
    assert sol.check_tenor(served, None, e.details["earliest_expiry"], now) is None


def test_a_past_instant_and_unix_seconds_read_as_too_soon():
    for expiry in ((T0 - 86_400) * 1000, T0 + 30 * 86_400, 0, -10 ** 30):
        with pytest.raises(crx.BadRequest) as ei:
            sol.check_tenor(7800, 7_776_000, expiry, T0)
        assert ei.value.details["earliest_expiry"] == (T0 + 7800) * 1000 and str(ei.value) == TOO_SOON
    with pytest.raises(crx.BadRequest) as ei:
        sol.check_tenor(7800, 7_776_000, 10 ** 30, T0)
    assert str(ei.value) == TOO_FAR


@pytest.mark.parametrize("lo,hi", [(None, None), ("7800", "7776000"), (True, False), (7800.0, 1.5), (-1, -1), ([], {})])
def test_a_limit_that_is_not_whole_seconds_is_not_checked(lo, hi):
    for expiry in (T0 * 1000 + 1000, (T0 + 400 * 86_400) * 1000, 0, 10 ** 30):
        assert sol.check_tenor(lo, hi, expiry, T0) is None
        assert sol.tenor_refusal(T0 * 1000, (expiry - T0 * 1000) // 1000, lo, hi) is None


@pytest.mark.parametrize("call", ["ask", "quote"])
def test_solana_sends_no_rfq_outside_the_served_band(solnet, solsession, tmp_path, account, call):
    c = tenor_client(solsession, tmp_path, account)
    under = (timedelta(hours=1), int(time.time() * 1000) + 3_600_000, datetime.now(timezone.utc) + timedelta(hours=2),
             timedelta(0), int(time.time()) + 30 * 86_400)
    for expiry in under:
        before = time.time() * 1000
        with pytest.raises(crx.BadRequest) as ei:
            getattr(c, call)("USD/MXN", "buy", 25_000, expiry=expiry)
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
            getattr(c, call)("USD/MXN", "buy", 25_000, expiry=expiry)
        e = ei.value
        assert sorted(e.details) == ["latest_expiry", "limit", "max_secs", "tenor_secs"]
        assert e.details["limit"] == "tenor" and e.details["max_secs"] == 7_776_000
        assert before + 7_776_000_000 - 1 <= e.details["latest_expiry"] <= time.time() * 1000 + 7_776_000_000
        assert str(e).endswith(" UTC (7776000 s from now)")
    assert rfqs_sent(solsession) == []


def test_solana_sends_an_rfq_inside_the_served_band(solnet, solsession, tmp_path, account):
    c = tenor_client(solsession, tmp_path, account)
    at = int(time.time() * 1000) + 3 * 3_600_000
    for expiry in (timedelta(hours=3), at, timedelta(days=89), None):
        a = c.ask("USD/MXN", "buy", 25_000, expiry=expiry)
        assert a.rfq_id == RFQ_ID
    sent = rfqs_sent(solsession)
    assert len(sent) == 4 and sent[1]["expiry"] == at and sent[1]["chain"] == "solana"


@pytest.mark.parametrize("tenor", [None, {}, "x", {"min_secs": None, "max_secs": None},
                                   {"min_secs": "7800", "max_secs": "7776000"}, {"min_secs": 7800.5, "max_secs": True},
                                   {"min_secs": -1, "max_secs": -1}],
                         ids=["no_tenor", "empty", "text", "nulls", "strings", "float_and_bool", "negative"])
def test_a_row_that_serves_no_band_is_not_checked(solnet, solsession, tmp_path, account, tenor):
    """No band on the row: the RFQ leaves, whatever its settlement. The gateway decides."""
    c = tenor_client(solsession, tmp_path, account, tenor=tenor)
    for expiry in (timedelta(seconds=30), timedelta(days=400)):
        assert c.ask("USD/MXN", "buy", 25_000, expiry=expiry).rfq_id == RFQ_ID
    assert len(rfqs_sent(solsession)) == 2


def test_half_a_band_checks_its_one_side(solnet, solsession, tmp_path, account):
    c = tenor_client(solsession, tmp_path, account, tenor={"min_secs": 7800})
    with pytest.raises(crx.BadRequest) as ei:
        c.ask("USD/MXN", "buy", 25_000, expiry=timedelta(hours=1))
    assert ei.value.details["min_secs"] == 7800
    c.ask("USD/MXN", "buy", 25_000, expiry=timedelta(days=400))
    c = tenor_client(solsession, tmp_path, account, tenor={"max_secs": 7_776_000})
    with pytest.raises(crx.BadRequest) as ei:
        c.ask("USD/MXN", "buy", 25_000, expiry=timedelta(days=91))
    assert ei.value.details["max_secs"] == 7_776_000
    c.ask("USD/MXN", "buy", 25_000, expiry=timedelta(seconds=30))
    assert len(rfqs_sent(solsession)) == 2


@pytest.mark.parametrize("call", ["ask", "quote"])
def test_the_gateways_tenor_refusal_reads_as_the_local_one(solnet, solsession, tmp_path, account, call):
    """The row serves no band, so the RFQ leaves. The gateway refuses it on its own band: the error is the one
    the local check makes, from the answer's figures. The gateway's clock is the expiry sent less the answer's
    ``tenor_secs``."""
    c = tenor_client(solsession, tmp_path, account, tenor=None, answer=tenor_answer(tenor_secs=7790, min_secs=7800))
    with pytest.raises(crx.BadRequest) as ei:
        getattr(c, call)("USD/MXN", "buy", 25_000, expiry=(T0 + 7790) * 1000 + 400)
    e = ei.value
    assert type(e) is crx.BadRequest and (e.code, e.status, e.gateway_code) == ("bad_request", 400, "bad_request")
    assert e.details == {"limit": "tenor", "tenor_secs": 7790, "min_secs": 7800,
                         "earliest_expiry": T0 * 1000 + 400 + 7_800_000}
    assert str(e) == TOO_SOON.replace("11:03:20", "11:03:21")
    local = sol.tenor_refusal(T0 * 1000 + 400, 7790, 7800, None)
    assert str(local) == str(e) and local.details == e.details
    solsession.routes[("POST", "/rfqs")] = tenor_answer(tenor_secs=7_776_009, max_secs=7_776_000)
    with pytest.raises(crx.BadRequest) as ei:
        getattr(c, call)("USD/MXN", "buy", 25_000, expiry=(T0 + 7_776_009) * 1000)
    e = ei.value
    assert (e.status, e.gateway_code, str(e)) == (400, "bad_request", TOO_FAR)
    assert e.details == {"limit": "tenor", "tenor_secs": 7_776_009, "max_secs": 7_776_000,
                         "latest_expiry": T0 * 1000 + 7_776_000_000}
    assert len(rfqs_sent(solsession)) == 2


@pytest.mark.parametrize("status,details", [
    (400, {"tenor_secs": 7790, "min_secs": 7800, "limit": "notional"}),
    (422, {"tenor_secs": 7790, "min_secs": 7800, "limit": "tenor"}),
    (400, {"limit": "tenor"}),
    (400, {"tenor_secs": "7790", "min_secs": 7800, "limit": "tenor"}),
    (400, {"tenor_secs": 7790, "min_secs": "7800", "limit": "tenor"}),
    (400, {"tenor_secs": 7800, "min_secs": 7800, "limit": "tenor"}),
    (400, {"tenor_secs": 7790, "limit": "tenor"}),
    (400, None),
], ids=["other_limit", "other_status", "no_figures", "text_tenor", "text_minimum", "inside_the_band", "no_limit_value",
        "no_details"])
def test_any_other_rfq_refusal_stays_the_gateways_own(solnet, solsession, tmp_path, account, status, details):
    body = {"error": "bad request: the gateway's own line", "code": "bad_request" if status == 400 else
            "unprocessable_entity"}
    if details is not None:
        body["details"] = details
    c = tenor_client(solsession, tmp_path, account, tenor=None, answer=(status, body))
    with pytest.raises(crx.BadRequest) as ei:
        c.ask("USD/MXN", "buy", 25_000, expiry=(T0 + 7790) * 1000)
    e = ei.value
    assert str(e) == body["error"] and e.status == status and e.details == (details or {})


def test_a_tenor_refusal_with_figures_of_any_size_raises_no_other_error(solnet, solsession, tmp_path, account):
    c = tenor_client(solsession, tmp_path, account, tenor=None,
                     answer=tenor_answer(tenor_secs=10 ** 30, max_secs=5))
    with pytest.raises(crx.BadRequest) as ei:
        c.ask("USD/MXN", "buy", 25_000, expiry=T0 * 1000)
    at = T0 * 1000 - 10 ** 33 + 5000
    assert ei.value.details["latest_expiry"] == at and str(at) in str(ei.value)


def test_an_ethereum_client_makes_no_tenor_check_and_keeps_the_gateways_error(make_client, session, markets):
    """The band is checked on Solana only. An Ethereum client sends the RFQ whatever the row serves, and a
    tenor refusal carries the gateway's own message."""
    m = copy.deepcopy(markets)
    for row in m["markets"]:
        row["tenor"] = dict(BAND)
    session.routes[("GET", "/markets")] = m
    answer = tenor_answer(tenor_secs=59, min_secs=7800)
    session.routes[("POST", "/rfqs")] = answer
    c = make_client()
    assert type(c._gw) is crx._http.Gateway and c.market("USD/MXN").min_tenor_s == 7800
    for call in (c.ask, c.quote):
        with pytest.raises(crx.BadRequest) as ei:
            call("USD/MXN", "buy", 25_000, expiry=timedelta(seconds=60))
        e = ei.value
        assert str(e) == answer[1]["message"] and e.status == 400
        assert e.details == dict(answer[1]["details"], error=answer[1]["error"])
    assert len(rfqs_sent(session)) == 2


# ---------- on the Solana path: the GET retry, the service refusal and the maker keepalive, as on Ethereum ----------

SERVICE_DOWN = {"code": "service_unavailable", "outcome": "unavailable", "error": "service temporarily unavailable",
                "message": "Service temporarily unavailable: trade not executed."}
# The code a gateway before service_unavailable sends.
RELAY_DOWN = dict(SERVICE_DOWN, code="relay_unavailable")
UPSTREAM_DOWN = (503, {"code": "upstream", "error": "down"})


class Pauses(list):
    def __call__(self, s):
        self.append(s)


def no_answer(req):
    import requests
    raise requests.ConnectionError("down")


@pytest.mark.parametrize("first", [no_answer, UPSTREAM_DOWN, (502, "<html>bad gateway</html>"), (503, RELAY_DOWN)],
                         ids=["network", "503", "502", "relay"])
def test_solana_get_is_sent_once_more_and_answers(solnet, solsession, tmp_path, account, first):
    c = sclient(solsession, tmp_path, account)
    c._sleep = pauses = Pauses()
    solsession.routes[("GET", "/bind")] = [first, dict(BOUND)]
    assert c.bind_state() == BOUND
    assert calls_of(solsession) == [("GET", "/bind")] * 2 and pauses == [crx._http.GET_RETRY_PAUSE]


def test_solana_get_raises_after_the_second_failure(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    c._sleep = pauses = Pauses()
    solsession.routes[("GET", "/bind")] = UPSTREAM_DOWN
    with pytest.raises(crx.ServerError):
        c.bind_state()
    assert calls_of(solsession) == [("GET", "/bind")] * 2 and pauses == [crx._http.GET_RETRY_PAUSE]


def test_a_stopped_seat_read_is_sent_once(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    c._sleep = pauses = Pauses()
    solsession.routes[("GET", "/bind")] = STOPPED_MONEY
    with pytest.raises(crx.SeatStopped) as ei:
        c.bind_state()
    assert is_stopped(ei.value) and calls_of(solsession) == [("GET", "/bind")] and pauses == []


@pytest.mark.parametrize("path", ["/rfqs", "/bind", "/deposit", "/withdraw"])
@pytest.mark.parametrize("fail, err", [(no_answer, crx.NetworkError), (UPSTREAM_DOWN, crx.ServerError),
                                       ((503, SERVICE_DOWN), crx.ServiceUnavailable),
                                       ((503, RELAY_DOWN), crx.ServiceUnavailable)],
                         ids=["network", "503", "service", "relay"])
def test_solana_post_is_sent_once(solnet, solsession, tmp_path, account, path, fail, err):
    c = sclient(solsession, tmp_path, account)
    assert type(c._gw) is sol.SeatGateway
    c._sleep = pauses = Pauses()
    solsession.routes[("POST", path)] = fail
    with pytest.raises(err) as ei:
        c._gw.request("POST", path, body={})
    assert type(ei.value) is err and calls_of(solsession) == [("POST", path)] and pauses == []


@pytest.mark.parametrize("down", [SERVICE_DOWN, RELAY_DOWN], ids=["service", "relay"])
@pytest.mark.parametrize("status", [503, 409])
@pytest.mark.parametrize("signed_first", [False, True])
def test_solana_accept_service_unavailable_raises_as_on_ethereum(solnet, solsession, tmp_path, account, monkeypatch,
                                                                 signed_first, status, down):
    """The service takes no trade: before a signature and after one the accept raises ServiceUnavailable, not
    TradeUnknown, in both spellings of the gateway. One post."""
    c = sclient(solsession, tmp_path, account)
    b = c._binder()
    assert type(b) is sol.SeatBinder
    sig = "0x" + "11" * 65
    monkeypatch.setattr(crx._bind.Binder, "sign_template", lambda self, t, ask: sig)
    solsession.routes[("POST", f"/rfqs/{RFQ_ID}/accept")] = (status, down)
    leg = "0x" + "00" * 24 + (int(c._clock()) + 300).to_bytes(8, "big").hex()
    t = {"typed_data": {"message": {"ownNonce": "5", "ownLegId": leg}}} if signed_first else None
    start = c._clock()
    with pytest.raises(crx.ServiceUnavailable) as ei:
        b.accept_trade(RFQ_ID, "q1", {}, t, None)
    e = ei.value
    assert type(e) is crx.ServiceUnavailable and isinstance(e, crx.ServerError)
    assert (e.code, e.status, e.gateway_code) == ("service_unavailable", status, down["code"])
    assert "not opened" in str(e) and c._clock() == start
    posted = [x["body"] for x in solsession.calls if x["path"].endswith("/accept")]
    assert posted == [{"quote_id": "q1", "sig": sig} if signed_first else {"quote_id": "q1"}]


@pytest.mark.parametrize("down", [SERVICE_DOWN, RELAY_DOWN], ids=["service", "relay"])
def test_solana_bind_and_withdraw_raise_service_unavailable_and_post_once(solnet, solsession, tmp_path, account,
                                                                          monkeypatch, down):
    c = sclient(solsession, tmp_path, account)
    signed = []
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(a) or "0x" + "22" * 65)
    bind_routes(solsession, c)
    solsession.routes[("POST", "/bind")] = (503, down)
    with pytest.raises(crx.ServiceUnavailable) as ei:
        c.bind(AUTH)
    assert (ei.value.code, ei.value.status, ei.value.gateway_code) == ("service_unavailable", 503, down["code"])
    assert signed == [] and calls_of(solsession).count(("POST", "/bind")) == 1
    withdraw_routes(solsession, c, refusals=0)
    solsession.routes[("POST", "/withdraw")] = (503, down)
    start = c._clock()
    with pytest.raises(crx.ServiceUnavailable):
        c.withdraw("10")
    assert len(signed) == 1 and calls_of(solsession).count(("POST", "/withdraw")) == 1 and c._clock() == start


def test_a_refused_keepalive_drops_the_wallet_secret(solnet, solsession, tmp_path):
    for bad in (0.5, 11, True, "5"):
        for secret in (SECRET64, list(SECRET64)):
            with pytest.raises(crx.ConfigError, match="keepalive is None, or 1 to 10 s") as ei:
                crx.Client(network="solana-t", keypair=secret, keepalive=bad, base_url=BASE, rpc_url=RPC,
                           session=solsession, state_dir=tmp_path / "s", allow_mainnet=True)
            assert _no_secret(ei.value), bad


def keepalive_reads(session, answer):
    """GET /trades answers ``answer``. Returns the keepalive's reads (``limit=1``), each as its headers."""
    reads = []

    def tape(req):
        if req["query"].get("limit") == ["1"]:
            reads.append(req["headers"])
        return answer
    session.routes[("GET", "/trades")] = tape
    return reads


def wait_for(reads, n, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and len(reads) < n:
        time.sleep(0.05)
    return len(reads) >= n


@pytest.mark.keepalive
def test_a_solana_maker_call_starts_the_keepalive(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account, keepalive=1.0)
    reads = keepalive_reads(solsession, {"trades": [], "seq": 0})
    try:
        c.rfqs(wait=0)
        assert c._live is not None and c._live.thread.daemon and c._live._gw is c._gw
        assert type(c._gw) is sol.SeatGateway and wait_for(reads, 1)
        assert reads[0]["x-crx-address"] == c.address and reads[0]["x-crx-sig"]
    finally:
        c.close()
    c._live.thread.join(3)
    assert not c._live.thread.is_alive()


@pytest.mark.keepalive
def test_a_stopped_seat_does_not_end_the_keepalive(solnet, solsession, tmp_path, account):
    """The maker call of a stopped seat raises SeatStopped. The keepalive's own read raises it inside the
    thread: the thread logs it and reads again one interval later."""
    c = sclient(solsession, tmp_path, account, keepalive=1.0)
    reads = keepalive_reads(solsession, STOPPED_MONEY)
    try:
        with pytest.raises(crx.SeatStopped):
            c.rfqs(wait=0)
        assert wait_for(reads, 2) and c._live.thread.is_alive()
    finally:
        c.close()
    c._live.thread.join(3)
    assert not c._live.thread.is_alive()


# ---------- a bind ends on the three keys the chain holds, never on the status word ----------

PENDING = {"bound": False, "status": "pending", "authority": AUTH, "payout_wallet": AUTH, "payout_ata": AUTH_ATA,
           "tx": None, "slot": None, "bind_nonce": "0"}
UNBOUND = {"bound": False, "status": "unbound", "authority": None, "payout_wallet": None, "payout_ata": None}
OTHER_KEYS = {"authority": AUTH, "payout_wallet": OTHER, "payout_ata": OTHER_ATA}
BIND_LIVE_TEXT = "a bind of this seat is in progress: no other bind is taken before it ends; bind() reads its end"
BIND_DOWN_TEXT = "service temporarily unavailable: the bind is not filed"
STILL_TEXT = "the bind is still in progress after 240 s: it has not failed; bind() reads its end"
IN_FLIGHT = (409, {"error": "conflict: another bind of this seat is in flight", "code": "conflict",
                   "outcome": "not_permitted",
                   "message": "A wallet connection for this account is already in progress."})
DOOR_CLOSED = (409, {"error": "conflict: this gateway holds no relay key; it sends nothing", "code": "conflict",
                     "outcome": "unavailable", "message": "CRX cannot answer now. Check your trades, then retry."})
DOOR_BOUND = (409, {"error": "seat 0x11 is bound already", "code": "seat_already_bound",
                    "outcome": "not_permitted", "message": "This account already has a linked wallet."})


def bind_then(session, c, reads, signed_post=None, unsigned_post=None):
    """A bind that files (``bind_routes``). After the signed post ``GET /bind`` answers ``reads`` in order, the
    last one again and again; before it, unbound. ``signed_post`` and ``unsigned_post``: the answer of that
    post, in place of the route's own. Returns the bodies posted and the clock of each read after the filing."""
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


def signed_posts(posted):
    return [b for b in posted if "sig" in b]


def test_bind_reads_until_the_answer_reads_bound_with_the_filed_keys(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    posted, after = bind_then(solsession, c, [PENDING, PENDING, BOUND])
    assert c.bind(AUTH) == BOUND
    assert len(signed_posts(posted)) == 1 and len(after) == 3
    assert after[-1] - after[0] == 2 * sol.BIND_POLL


@pytest.mark.parametrize("fault", [no_answer, UPSTREAM_DOWN], ids=["network", "503"])
def test_bind_reads_again_after_a_read_the_gateway_did_not_answer(solnet, solsession, tmp_path, account, fault):
    """A read with no answer is sent once more (the GET retry), then read again one pause later."""
    c = sclient(solsession, tmp_path, account)
    posted, after = bind_then(solsession, c, [PENDING, fault, fault, PENDING, BOUND])
    assert c.bind(AUTH) == BOUND
    assert len(signed_posts(posted)) == 1 and len(after) == 5
    assert after[-1] - after[0] == 3 * sol.BIND_POLL + crx._http.GET_RETRY_PAUSE


@pytest.mark.parametrize("row", [
    dict(BOUND, payout_wallet=OTHER, payout_ata=OTHER_ATA),
    dict(BOUND, payout_ata=OTHER_ATA),
    dict(BOUND, authority=OTHER, payout_wallet=OTHER, payout_ata=OTHER_ATA),
], ids=["payout", "account", "all"])
def test_a_row_bound_to_another_payout_is_never_a_bound_bind(solnet, solsession, tmp_path, account, row):
    """After the filing the gateway reads the seat bound, by the word and by the flag, to another payout: the
    bind raises with what the chain holds."""
    c = sclient(solsession, tmp_path, account)
    held = dict(row, tx=None, slot=None, error="seat_bound_other_payout", filed=dict(FILED))
    assert held["bound"] is True and held["status"] == "bound"
    posted, after = bind_then(solsession, c, [PENDING, held])
    with pytest.raises(crx.SeatBoundOtherPayout) as ei:
        c.bind(AUTH)
    e = ei.value
    assert type(e) is crx.SeatBoundOtherPayout and isinstance(e, crx.CrxError) and e.code == "seat_bound_other_payout"
    assert str(e) == OTHER_PAYOUT_TEXT and e.details == {"bound": chain_keys(row), "filed": FILED}
    assert len(signed_posts(posted)) == 1 and len(after) == 2


def test_a_row_bound_to_another_authority_is_never_a_bound_bind(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    row = dict(BOUND, authority=OTHER)
    bind_then(solsession, c, [row])
    with pytest.raises(crx.CrxError) as ei:
        c.bind(AUTH)
    e = ei.value
    assert type(e) is crx.CrxError and e.code == "seat_already_bound"
    assert str(e) == "this seat is bound already, with another authority"
    assert e.details == {"bound": chain_keys(row), "filed": FILED}


@pytest.mark.parametrize("answer", [
    dict(BOUND, bound=False), {k: v for k, v in BOUND.items() if k != "bound"}, dict(BOUND, bound="true"),
    dict(BOUND, bound=1), dict(PENDING, status="bound"), {"status": "bound"},
], ids=["flag_false", "no_flag", "flag_text", "flag_one", "pending_keys", "word_only"])
def test_the_status_word_alone_never_ends_a_bind(solnet, solsession, tmp_path, account, answer):
    """The word ``bound`` without the flag ends nothing: the bind reads on, then raises as still in progress."""
    assert answer["status"] == "bound" and sol.bind_end(answer, FILED) is None
    c = sclient(solsession, tmp_path, account)
    posted, after = bind_then(solsession, c, [answer])
    with pytest.raises(crx.BindInProgress):
        c.bind(AUTH)
    assert len(signed_posts(posted)) == 1 and after[-1] - after[0] == sol.BIND_WAIT


@pytest.mark.parametrize("answer", [
    dict(BOUND, authority=None), dict(BOUND, payout_wallet=""), dict(BOUND, payout_ata="0OIl"),
    dict(BOUND, payout_ata=AUTH_ATA[:20]), {"bound": True, "status": "bound"}, dict(BOUND, authority=7),
], ids=["none", "empty", "not_base58", "short", "no_keys", "number"])
def test_a_bound_answer_with_an_unreadable_key_is_a_bad_answer(solnet, solsession, tmp_path, account, answer):
    c = sclient(solsession, tmp_path, account)
    bind_then(solsession, c, [answer])
    with pytest.raises(crx.BadAnswer):
        c.bind(AUTH)
    solsession.routes[("GET", "/bind")] = dict(answer)
    solsession.routes[("POST", "/bind")] = no_post
    with pytest.raises(crx.BadAnswer):
        c.bind(AUTH)


def test_the_bind_keys_are_compared_as_bytes():
    assert sol.bind_keys(AUTH, AUTH, MINT) == FILED and sol.bind_end(dict(BOUND), FILED) == BOUND
    assert sol.same_keys(BOUND, FILED) and sol.same_keys(PENDING, FILED)
    for k in sol.BIND_KEYS:
        one_off = sol.b58encode(sol.key(FILED[k])[:-1] + bytes([sol.key(FILED[k])[-1] ^ 1]))
        assert not sol.same_keys(dict(BOUND, **{k: one_off}), FILED)
        assert not sol.same_keys(dict(BOUND, **{k: FILED[k] + " "}), FILED)
        assert not sol.same_keys({x: v for x, v in BOUND.items() if x != k}, FILED)
        with pytest.raises(crx.CrxError):
            sol.bind_end(dict(BOUND, **{k: one_off}), FILED)
    assert not sol.same_keys(None, FILED) and sol.bind_end(None, FILED) is None and sol.bind_end("bound", FILED) is None


def test_a_bind_still_in_progress_after_the_wait_is_not_a_failure(solnet, solsession, tmp_path, account):
    """The wait covers the 2 to 3 minutes a bind can stay in progress. Past it the error is BindInProgress, with
    what was filed; it is no BindFailed."""
    assert 180 <= sol.BIND_WAIT <= 600 and 0 < sol.BIND_POLL <= 5
    c = sclient(solsession, tmp_path, account)
    posted, after = bind_then(solsession, c, [PENDING])
    with pytest.raises(crx.CrxError) as ei:
        c.bind(AUTH)
    e = ei.value
    assert type(e) is crx.BindInProgress and not isinstance(e, crx.BindFailed) and e.code == "bind_in_progress"
    assert str(e) == STILL_TEXT and e.details == {"filed": FILED} and e.status is None
    assert after[-1] - after[0] == sol.BIND_WAIT and len(after) == sol.BIND_WAIT / sol.BIND_POLL + 1
    assert len(signed_posts(posted)) == 1


@pytest.mark.parametrize("error", ["unprocessable_entity", "conflict", None])
def test_a_bind_that_failed_raises_bind_failed(solnet, solsession, tmp_path, account, error):
    c = sclient(solsession, tmp_path, account)
    failed = dict(PENDING, status="failed")
    if error is not None:
        failed["error"] = error
    posted, after = bind_then(solsession, c, [PENDING, failed])
    with pytest.raises(crx.CrxError) as ei:
        c.bind(AUTH)
    e = ei.value
    assert type(e) is crx.BindFailed and e.code == "bind_failed"
    assert str(e) == "the bind did not complete: this seat is not bound"
    assert e.details == ({"filed": FILED, "error": error} if error else {"filed": FILED})
    assert len(after) == 2 and len(signed_posts(posted)) == 1


def test_bind_waits_on_its_own_bind_in_progress_and_signs_nothing(solnet, solsession, tmp_path, account, monkeypatch):
    """The gateway holds a bind of the seat in progress with these three keys: its end is this call's."""
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


@pytest.mark.parametrize("door, err, text", [
    (IN_FLIGHT, crx.BindInProgress, BIND_LIVE_TEXT),
    (DOOR_CLOSED, crx.ServiceUnavailable, BIND_DOWN_TEXT),
    ((503, SERVICE_DOWN), crx.ServiceUnavailable, BIND_DOWN_TEXT),
    ((409, dict(SERVICE_DOWN)), crx.ServiceUnavailable, BIND_DOWN_TEXT),
    ((503, RELAY_DOWN), crx.ServiceUnavailable, BIND_DOWN_TEXT),
    ((409, dict(RELAY_DOWN)), crx.ServiceUnavailable, BIND_DOWN_TEXT),
    ((409, {"error": "conflict: the gateway's own line", "code": "conflict"}), crx.CrxError,
     "conflict: the gateway's own line"),
    ((409, {"error": "conflict: the gateway's own line", "code": "conflict", "outcome": "rejected"}), crx.CrxError,
     "conflict: the gateway's own line"),
    ((400, dict(IN_FLIGHT[1])), crx.CrxError, IN_FLIGHT[1]["message"]),
], ids=["in_flight", "door_closed_409", "service_503", "service_409", "relay_503", "relay_409",
        "conflict_no_outcome", "conflict_other_outcome", "other_status"])
def test_the_bind_door_refusals(solnet, solsession, tmp_path, account, door, err, text):
    """A second bind while one is in progress is BindInProgress. A service that sends nothing is
    ServiceUnavailable, in each spelling of the gateway. Any other 409 stays the gateway's own. One post, no
    read after it, no pause."""
    c = sclient(solsession, tmp_path, account)
    posted, after = bind_then(solsession, c, [BOUND], signed_post=door)
    start = c._clock()
    with pytest.raises(crx.CrxError) as ei:
        c.bind(AUTH)
    e = ei.value
    assert type(e) is err and str(e) == text and e.status == door[0] and e.gateway_code == door[1]["code"]
    assert len(signed_posts(posted)) == 1 and after == [] and c._clock() == start


def test_the_door_refusal_of_another_payout_is_typed(solnet, solsession, tmp_path, account):
    c = sclient(solsession, tmp_path, account)
    door = (409, {"error": "the seat is bound to another payout account than this bind names",
                  "code": "seat_bound_other_payout", "outcome": "not_permitted",
                  "message": "This account is linked to another payout wallet.",
                  "details": {"bound": dict(OTHER_KEYS), "filed": dict(FILED)}})
    posted, after = bind_then(solsession, c, [BOUND], signed_post=door)
    with pytest.raises(crx.SeatBoundOtherPayout) as ei:
        c.bind(AUTH)
    e = ei.value
    assert (e.code, e.status, e.gateway_code) == ("seat_bound_other_payout", 409, "seat_bound_other_payout")
    assert str(e) == OTHER_PAYOUT_TEXT and e.details == {"bound": OTHER_KEYS, "filed": FILED} and after == []
    for bad in (None, "x", {"bound": "x"}, {"bound": {"authority": 7}}):
        body = dict(door[1], details=bad)
        got = sol.bind_refused(409, body, FILED)
        assert type(got) is crx.SeatBoundOtherPayout and got.details == {"bound": {}, "filed": FILED}


@pytest.mark.parametrize("form", ["unsigned", "signed"])
def test_a_door_that_reads_the_seat_bound_lets_the_three_keys_decide(solnet, solsession, tmp_path, account,
                                                                    monkeypatch, form):
    """409 seat_already_bound names no key: the bind reads GET /bind and judges its three keys."""
    signed = []
    real = crx.client.sign_typed
    monkeypatch.setattr(crx.client, "sign_typed", lambda *a, **k: signed.append(1) or real(*a, **k))
    over = {form + "_post": DOOR_BOUND}
    c = sclient(solsession, tmp_path, account)
    posted, after = bind_then(solsession, c, [UNBOUND, BOUND], **over)
    assert c.bind(AUTH) == BOUND and len(after) == 2
    assert len(signed) == (0 if form == "unsigned" else 1) and len(posted) == (1 if form == "unsigned" else 2)
    c = sclient(solsession, tmp_path / "2", account)
    bind_then(solsession, c, [dict(BOUND, **OTHER_KEYS)], **over)
    with pytest.raises(crx.SeatBoundOtherPayout) as ei:
        c.bind(AUTH)
    assert ei.value.details == {"bound": OTHER_KEYS, "filed": FILED}
    c = sclient(solsession, tmp_path / "3", account)
    bind_then(solsession, c, [UNBOUND], **over)
    with pytest.raises(crx.BindInProgress) as ei:
        c.bind(AUTH)
    assert str(ei.value) == STILL_TEXT


def test_the_bind_errors_are_public():
    for name, code in (("SeatBoundOtherPayout", "seat_bound_other_payout"), ("BindInProgress", "bind_in_progress"),
                       ("BindFailed", "bind_failed")):
        cls = getattr(crx, name)
        assert name in crx.__all__ and cls is getattr(sol, name) and cls.code == code
        assert issubclass(cls, crx.CrxError) and not issubclass(cls, (crx.ServerError, crx.NotWhitelisted))
