"""Make the seat key on this machine. Only its address goes to CRX.

    crx-key new [PATH]       write a new key file (default ~/.crx/seat.key) and print its address
    crx-key address [PATH]   print the address of a key file

The key is never printed, logged or sent.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from eth_account import Account
from eth_utils import to_checksum_address

from ._keys import ENV_KEY_FILE, load_account
from .errors import ConfigError

DEFAULT_KEY_FILE = "~/.crx/seat.key"


def new_key_file(path: str | os.PathLike = DEFAULT_KEY_FILE) -> str:
    """Write a new private key to ``path`` and return its address (EIP-55).

    The key comes from the operating system's random source. ``path`` must not exist: an existing path
    raises ``config`` and stays unchanged. On POSIX the file is owner-only (0600), and a missing folder
    is made owner-only (0700). The file loads with ``key_file=`` or ``CRX_WALLET_PK_FILE``.
    """
    p = Path(path).expanduser()
    if not p.parent.exists():
        try:
            p.parent.mkdir(mode=0o700, parents=True)
        except OSError:
            raise ConfigError(f"the folder {p.parent} cannot be made") from None
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_BINARY", 0))
    try:
        fd = os.open(p, flags, 0o600)
    except FileExistsError:
        raise ConfigError(f"{p} exists; the key file must be new. Choose another path") from None
    except OSError:
        raise ConfigError(f"{p} cannot be written") from None
    account = Account.create()
    data = ("0x" + bytes(account.key).hex() + "\n").encode()
    written = False
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        written = True
    except OSError:
        pass
    finally:
        os.close(fd)
        data = None
    if not written:
        _remove(p)
        raise ConfigError(f"{p} cannot be written")
    address = account.address
    account = None
    try:
        same = load_account(key_file=p).address == address
    except ConfigError:
        same = False
    if not same:
        _remove(p)
        raise ConfigError(f"{p} does not read back to the new key")
    return to_checksum_address(address)


def key_address(path: str | os.PathLike = DEFAULT_KEY_FILE) -> str:
    """Return the address (EIP-55) of the key in ``path``. Same file rules as ``key_file=``."""
    account = load_account(key_file=path)
    if account is None:
        raise ConfigError("no key file")
    return to_checksum_address(account.address)


def _remove(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    """Run ``crx-key``. Return 0 on success and 2 on a refusal."""
    parser = argparse.ArgumentParser(
        prog="crx-key",
        description="Make the seat key on this machine. Only its address goes to CRX.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, text in (("new", "write a new key file and print its address"),
                       ("address", "print the address of a key file")):
        cmd = sub.add_parser(name, help=text, description=text[0].upper() + text[1:] + ".")
        cmd.add_argument("path", nargs="?", default=DEFAULT_KEY_FILE,
                         help=f"key file (default {DEFAULT_KEY_FILE})")
    a = parser.parse_args(argv)
    path = Path(a.path).expanduser().absolute()
    try:
        address = new_key_file(path) if a.cmd == "new" else key_address(path)
    except ConfigError as e:
        print(f"crx-key: {e}", file=sys.stderr)
        return 2
    if a.cmd == "address":
        print(address)
        return 0
    mode = " (owner-only)" if os.name == "posix" else ""
    print(f"Address:  {address}")
    print(f"Key file: {path}{mode}")
    print("Next:     enter the address in Discover (Trading wallet > Use your own wallet) or send it to CRX.")
    print("The key stays on this machine: CRX never asks for it and cannot recover it. Keep an offline backup.")
    use = (f"export {ENV_KEY_FILE}={shlex.quote(str(path))}" if os.name == "posix"
           else f'set "{ENV_KEY_FILE}={path}"')
    print(f"Use it:   {use}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
