"""crx-key: the firm makes its seat key on its own machine; only the address leaves it."""

from __future__ import annotations

import os
import re
import stat

import pytest

import crx
from crx import key as crx_key
from .conftest import BASE, RPC

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


def test_new_key_file_is_owner_only_loads_to_the_printed_address_and_never_prints_the_key(
        tmp_path, capsys):
    f = tmp_path / "new-folder" / "seat.key"
    assert crx_key.main(["new", str(f)]) == 0
    out, err = capsys.readouterr()
    address = re.search(r"^Address:\s+(0x[0-9a-fA-F]{40})$", out, re.M).group(1)
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(f.parent).st_mode) & 0o077 == 0
    key = f.read_text().strip().removeprefix("0x").lower()
    assert len(key) == 64
    assert key not in (out + err).lower()
    assert crx.Client(key_file=f, base_url=BASE, rpc_url=RPC).address == address.lower()
    assert crx_key.main(["address", str(f)]) == 0
    assert capsys.readouterr().out == address + "\n"


def test_new_refuses_an_existing_path_and_leaves_it_unchanged(tmp_path, capsys):
    f = tmp_path / "seat.key"
    f.write_text("funded key\n")
    os.chmod(f, 0o600)
    assert crx_key.main(["new", str(f)]) == 2
    assert f.read_text() == "funded key\n"
    assert "exists" in capsys.readouterr().err
    link = tmp_path / "link.key"
    link.symlink_to(tmp_path / "elsewhere.key")
    assert crx_key.main(["new", str(link)]) == 2
    assert not (tmp_path / "elsewhere.key").exists()
