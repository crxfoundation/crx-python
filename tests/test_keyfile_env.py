"""Key-file mode rule (owner-only, set by the SDK) and CRX_* settings from a .env file."""

from __future__ import annotations

import os
import stat

import pytest
from eth_account import Account

import crx
import crx._env as env_mod
import crx._keys as keys_mod
from .conftest import BASE, RPC

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")

CRX_NAMES = ("CRX_WALLET_PK", "CRX_WALLET_PK_FILE", "CRX_BASE", "CRX_RPC", "CRX_STATE_DIR",
             "CRX_ALLOW_MAINNET", "CRX_CHECK_RPC")


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for n in CRX_NAMES:
        monkeypatch.delenv(n, raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def other_acct():
    return Account.from_key("0x" + "22" * 32)


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def key_file(folder, acct, m=0o644):
    f = folder / "seat.key"
    f.write_text(acct.key.hex() + "\n")
    os.chmod(f, m)
    return f


def no_read(monkeypatch):
    """Fail the test when the key loader reads a file."""
    def boom(*a, **k):
        raise AssertionError("the key file was read")
    monkeypatch.setattr(keys_mod.os, "fdopen", boom)


# --- key-file mode rule ---

def test_0644_own_file_becomes_0600_and_loads(clean, account, capsys):
    f = key_file(clean, account, 0o644)
    c = crx.Client(key_file=f, base_url=BASE, rpc_url=RPC)
    assert c.address == account.address.lower()
    assert mode(f) == 0o600
    err = capsys.readouterr().err
    assert err == f"set {f} to owner-only (0600)\n"
    assert account.key.hex().removeprefix("0x") not in err


def test_file_of_another_user_refused_unread(clean, account, monkeypatch, capsys):
    f = key_file(clean, account, 0o644)
    me = os.getuid()
    monkeypatch.setattr(keys_mod.os, "getuid", lambda: me + 1)
    no_read(monkeypatch)
    with pytest.raises(crx.ConfigError, match="the key file is open to other users; run chmod 600 on it"):
        crx.Client(key_file=f, base_url=BASE, rpc_url=RPC)
    assert mode(f) == 0o644 and capsys.readouterr().err == ""


def test_chmod_failure_refused_unread(clean, account, monkeypatch):
    f = key_file(clean, account, 0o644)

    def denied(*a):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(keys_mod.os, "fchmod", denied)
    no_read(monkeypatch)
    with pytest.raises(crx.ConfigError, match="the key file is open to other users; run chmod 600 on it"):
        crx.Client(key_file=f, base_url=BASE, rpc_url=RPC)
    assert mode(f) == 0o644


def test_directory_key_path_refused_unchanged(clean):
    d = clean / "keys"
    d.mkdir()
    os.chmod(d, 0o755)
    with pytest.raises(crx.ConfigError, match="open to other users"):
        crx.Client(key_file=d, base_url=BASE, rpc_url=RPC)
    assert mode(d) == 0o755


def test_deleted_working_directory_has_no_dotenv(clean, monkeypatch, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    assert crx.Client(base_url=BASE, rpc_url=RPC).address is None


def test_missing_key_file(clean):
    with pytest.raises(crx.ConfigError, match="the key file cannot be read"):
        crx.Client(key_file=clean / "none.key", base_url=BASE, rpc_url=RPC)


# --- .env ---

def test_parse_comments_quotes_no_expansion():
    text = "\n".join([
        "# comment",
        "",
        "export CRX_A=1",
        'CRX_B="x # y"',
        "CRX_C='$HOME'",
        "CRX_D=val # trailing comment",
        "CRX_E=a#b",
        "OTHER=1",
        'CRX_F="unterminated',
        "CRX_G=",
        "  CRX_H =  v  ",
        "CRX_I=${CRX_A}",
        "CRX_J=1",
        "CRX_J=2",
        "not a line",
        "crx_lower=1",
    ])
    assert env_mod._parse(text) == {
        "CRX_A": "1", "CRX_B": "x # y", "CRX_C": "$HOME", "CRX_D": "val", "CRX_E": "a#b",
        "CRX_G": "", "CRX_H": "v", "CRX_I": "${CRX_A}", "CRX_J": "2",
    }


def test_dotenv_only(clean, account, capsys):
    (clean / ".env").write_text(
        f'CRX_WALLET_PK="{account.key.hex()}"\nCRX_BASE={BASE}\nCRX_RPC={RPC}\nCRX_STATE_DIR={clean / "st"}\n')
    os.chmod(clean / ".env", 0o600)
    c = crx.Client()
    assert c.address == account.address.lower()
    assert c._gw.base_url == BASE and c._rpc._url == RPC and c._state_dir == clean / "st"
    assert capsys.readouterr().err == ""
    for n in CRX_NAMES:
        assert n not in os.environ


def test_env_wins_over_dotenv(clean, account, other_acct, monkeypatch):
    (clean / ".env").write_text(f"CRX_WALLET_PK={other_acct.key.hex()}\nCRX_STATE_DIR={clean / 'file'}\n"
                                f"CRX_BASE=https://dotenv.test\nCRX_RPC={RPC}\n")
    os.chmod(clean / ".env", 0o600)
    monkeypatch.setenv("CRX_WALLET_PK", account.key.hex())
    monkeypatch.setenv("CRX_STATE_DIR", str(clean / "env"))
    monkeypatch.setenv("CRX_BASE", BASE)
    c = crx.Client()
    assert c.address == account.address.lower()
    assert c._state_dir == clean / "env" and c._gw.base_url == BASE and c._rpc._url == RPC
    monkeypatch.delenv("CRX_WALLET_PK")
    monkeypatch.setenv("CRX_WALLET_PK_FILE", str(key_file(clean, account, 0o600)))
    assert crx.Client().address == account.address.lower()


def test_dotenv_of_another_user_refused_no_value(clean, account, monkeypatch, capsys):
    p = clean / ".env"
    p.write_text(f"CRX_WALLET_PK={account.key.hex()}\n")
    os.chmod(p, 0o644)
    me = os.getuid()
    monkeypatch.setattr(keys_mod.os, "getuid", lambda: me + 1)
    with pytest.raises(crx.ConfigError) as ei:
        crx.Client(base_url=BASE, rpc_url=RPC)
    assert str(ei.value) == "the .env file is open to other users; run chmod 600 on it"
    assert ei.value.__context__ is None and ei.value.__cause__ is None
    k = account.key.hex().removeprefix("0x")
    assert k not in str(ei.value) and k not in capsys.readouterr().err
    assert mode(p) == 0o644
