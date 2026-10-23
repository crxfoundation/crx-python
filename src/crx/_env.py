"""CRX_* settings: the environment first, then a ``.env`` file in the working directory."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from ._keys import ENV_KEY, ENV_KEY_FILE, owner_only
from .errors import ConfigError

DOTENV = ".env"
MAX_BYTES = 1 << 20
_NAME = re.compile(r"CRX_[A-Za-z0-9_]*\Z")
_KEY_NAMES = (ENV_KEY, ENV_KEY_FILE)


class Settings:
    """The CRX_* settings. A name in the environment wins over the ``.env`` file. When the environment holds
    ``CRX_WALLET_PK`` or ``CRX_WALLET_PK_FILE``, the file gives neither."""

    __slots__ = ("_file",)

    def __init__(self, file: dict[str, str] | None = None) -> None:
        self._file = dict(file or {})

    def get(self, name: str) -> str | None:
        if name in os.environ:
            return os.environ[name]
        if name in _KEY_NAMES and any(n in os.environ for n in _KEY_NAMES):
            return None
        return self._file.get(name)

    def __repr__(self) -> str:
        return f"crx.Settings(dotenv={sorted(self._file)!r})"

    def __getstate__(self) -> None:
        raise TypeError("crx settings can hold a key and cannot be pickled or copied")


def load(folder: str | os.PathLike | None = None) -> Settings:
    """The settings, with the ``.env`` file in ``folder`` (default: the working directory)."""
    return Settings(read_dotenv(Path(folder) / DOTENV if folder is not None else Path(DOTENV)))


def read_dotenv(path: Path) -> dict[str, str]:
    """The CRX_* lines of a ``.env`` file; {} when it is absent or no regular file.

    Lines are ``NAME=VALUE``, ``export NAME=VALUE``, ``# comments`` and blank lines. A value in single or
    double quotes is taken as written, without the quotes; an unquoted value ends at `` #``. Nothing is
    expanded. A file with a CRX_* line must be owner-only (see ``owner_only``): else ConfigError. No error
    carries a value.
    """
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_BINARY", 0))
    except OSError:
        pass
    if fd is None:
        return {}
    found: dict[str, str] = {}
    private = True
    try:
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_size <= MAX_BYTES:
            text = None
            with os.fdopen(fd, "rb", closefd=False) as f:
                text = f.read(MAX_BYTES).decode("utf-8")
            found = _parse(text)
            text = None
            if found:
                private = owner_only(fd, st, path)
    except (OSError, UnicodeDecodeError):
        found = {}
    finally:
        os.close(fd)
    if not private:
        found = {}
        raise ConfigError(f"the {DOTENV} file is open to other users; run chmod 600 on it")
    return found


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[7:].lstrip()
        name, eq, value = line.partition("=")
        name = name.strip()
        if not eq or not _NAME.match(name):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            if end < 0:
                continue
            value = value[1:end]
        else:
            cut = re.search(r"\s#", value)
            if cut:
                value = value[:cut.start()].rstrip()
        out[name] = value
    line = value = ""
    return out
