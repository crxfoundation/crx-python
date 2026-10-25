"""Key-file mode rule (owner-only, set by the SDK) and CRX_* settings from a .env file."""

from __future__ import annotations

import os
import stat

import pytest

import crx
from .conftest import BASE, RPC

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")

CRX_NAMES = ("CRX_WALLET_PK", "CRX_WALLET_PK_FILE", "CRX_BASE", "CRX_RPC", "CRX_STATE_DIR",
             "CRX_ALLOW_MAINNET")


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for n in CRX_NAMES:
        monkeypatch.delenv(n, raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def key_file(folder, acct, m=0o644):
    f = folder / "seat.key"
    f.write_text(acct.key.hex() + "\n")
    os.chmod(f, m)
    return f


# --- key-file mode rule ---

def test_0644_own_file_becomes_0600_and_loads(clean, account, capsys):
    f = key_file(clean, account, 0o644)
    c = crx.Client(key_file=f, base_url=BASE, rpc_url=RPC)
    assert c.address == account.address.lower()
    assert mode(f) == 0o600
    err = capsys.readouterr().err
    assert err == f"set {f} to owner-only (0600)\n"
    assert account.key.hex().removeprefix("0x") not in err


# --- .env ---
