#!/usr/bin/env python3
"""Check that a released version builds, from the source GitHub serves, to the bytes of the reviewed commit.

    python -I scripts/release-check.py --tag v0.3.19 [--sha COMMIT] [--repo CLONE] [--remote URL]
                                       [--work DIR] [--pypi NAME] [--github OWNER/REPO]

The reviewed commit is --sha, else the commit of the tag in --repo (default: the repo of this script).
The install line `pip install "git+<remote>@<tag>"` builds the wheel on the client from the tag's tree, so the
published source is the commit the tag names on --remote. With no such tag, the published source is the reviewed
commit, fetched by sha, when the remote still serves it.

One line per step:
  version   pyproject.toml of the reviewed commit names the version of the tag.
  tag       the tag's commit on --remote (git ls-remote): match, moved (another commit) or absent.
  source    the commit and tree of the published source, against the reviewed tree.
  build     wheel and sdist, built twice from the reviewed tree and once from the published tree. A wheel compares
            by sha256, an sdist by the sha256 of its members (name, type, mode, bytes).
  pypi      the files PyPI serves for the version (404: none), against the built sha256.
  release   the assets of the GitHub release of the tag (404: none), against the built sha256.

Each build runs `python -m build --no-isolation` in a fresh copy of the tree, with SOURCE_DATE_EPOCH set to the
reviewed commit's time. The running Python needs the packages build and setuptools. setuptools writes the build
time into each sdist's gzip header and generated members, so sdist bytes differ between builds of one tree.

Exit 0: the tag matches and each published artifact equals its reviewed build. Exit 1: a mismatch, a moved or
absent tag, or a published source that the remote no longer serves. Exit 2: usage, or a step that did not run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REMOTE = "https://github.com/crxfoundation/crx-python"
SHA = re.compile(r"[0-9a-f]{40}")


class Stop(Exception):
    """A step that did not run: exit 2."""


def run(args: list[str], cwd: Path | None = None, env: dict | None = None, data: bytes | None = None) -> bytes:
    p = subprocess.run(args, cwd=cwd, env=env, input=data, capture_output=True)
    if p.returncode != 0:
        tail = p.stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
        raise Stop(f"{' '.join(args[:3])} exited {p.returncode}: {tail[0][:200]}")
    return p.stdout


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args]).decode().strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sdist_content(path: Path) -> str:
    """Return the sha256 of an sdist's members: name, mode and bytes of each, in name order.

    setuptools writes the build time into the gzip header and into the mtime of PKG-INFO, the egg-info and the
    directories, so two builds of one tree differ in bytes and agree in content."""
    h = hashlib.sha256()
    with tarfile.open(path) as t:
        for m in sorted(t.getmembers(), key=lambda m: m.name):
            body = t.extractfile(m).read() if m.isfile() else b""
            h.update(f"{m.name}\0{m.type.decode()}\0{m.mode:o}\0{hashlib.sha256(body).hexdigest()}\n".encode())
    return h.hexdigest()


def remote_tag(remote: str, tag: str) -> str | None:
    """Return the commit the tag names on the remote, or None when the remote has no such tag."""
    out = run(["git", "ls-remote", remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"]).decode()
    refs = dict(reversed(line.split("\t")) for line in out.splitlines() if "\t" in line)
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def fetch(remote: str, ref: str, into: Path) -> str | None:
    """Fetch ``ref`` from the remote into a new repo at ``into``. Return the fetched commit, or None."""
    run(["git", "init", "-q", str(into)])
    p = subprocess.run(["git", "-C", str(into), "fetch", "-q", "--depth", "1", remote, ref], capture_output=True)
    if p.returncode != 0:
        return None
    return git(into, "rev-parse", "FETCH_HEAD^{commit}")


def extract(repo: Path, commit: str, into: Path) -> None:
    into.mkdir(parents=True)
    tar = run(["git", "-C", str(repo), "archive", "--format=tar", commit])
    run(["tar", "-x", "-f", "-", "-C", str(into)], data=tar)


def build(repo: Path, commit: str, into: Path, epoch: str) -> dict[str, tuple[str, str]]:
    """Build the sdist and the wheel of ``commit`` in a fresh tree.

    Return {file name: (sha256, key)}. The key is the sha256 for a wheel and the content sha256 for an sdist."""
    tree, out = into / "tree", into / "dist"
    extract(repo, commit, tree)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PIP_"))}
    env["SOURCE_DATE_EPOCH"] = epoch
    for kind in ("--sdist", "--wheel"):
        run([sys.executable, "-m", "build", "--no-isolation", kind, "--outdir", str(out), str(tree)], env=env)
    return {p.name: (sha256(p), sdist_content(p) if p.name.endswith(".tar.gz") else sha256(p))
            for p in sorted(out.iterdir())}


def get_json(url: str) -> dict | None:
    """Return the JSON at ``url``, or None on 404."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "release-check"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise Stop(f"{url} answered {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise Stop(f"{url}: {e}") from None


def compare(label: str, served: dict[str, str], built: dict[str, str]) -> list[str]:
    """Print one line per served file against the build of the same name. Return the names that differ."""
    bad = []
    for name, digest in sorted(served.items()):
        want = built.get(name)
        verdict = "match" if digest == want else ("not built here" if want is None else "MISMATCH")
        print(f"{label}: {name} {digest[:16]} vs built {(want or '-')[:16]}: {verdict}")
        if verdict != "match":
            bad.append(name)
    return bad


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--sha")
    ap.add_argument("--repo", type=Path, default=HERE)
    ap.add_argument("--remote", default=REMOTE)
    ap.add_argument("--work", type=Path)
    ap.add_argument("--pypi", default="crx-python")
    ap.add_argument("--github", default="crxfoundation/crx-python")
    a = ap.parse_args(argv)
    version = a.tag.removeprefix("v")
    work = Path(tempfile.mkdtemp(prefix="release-check-", dir=a.work))
    fail = False
    try:
        reviewed = git(a.repo, "rev-parse", "--verify", f"{a.sha or a.tag}^{{commit}}")
        meta = tomllib.loads(git(a.repo, "show", f"{reviewed}:pyproject.toml"))
        named = meta["project"]["version"]
        print(f"version: {reviewed[:12]} names {named}: {'match' if named == version else 'MISMATCH'}")
        fail |= named != version

        on_remote = remote_tag(a.remote, a.tag)
        word = "absent" if on_remote is None else ("match" if on_remote == reviewed else "MOVED")
        print(f"tag: {a.tag} on {a.remote} -> {(on_remote or '-')[:12]}, reviewed {reviewed[:12]}: {word}")
        fail |= word != "match"

        pub = work / "published"
        got = fetch(a.remote, on_remote or reviewed, pub)
        if got is None:
            print(f"source: {a.remote} does not serve {(on_remote or reviewed)[:12]}: NOT SERVED")
            return 1
        how = "the tag" if on_remote else "sha only, no tag"
        t_rev, t_pub = git(a.repo, "rev-parse", f"{reviewed}^{{tree}}"), git(pub, "rev-parse", f"{got}^{{tree}}")
        print(f"source: {got[:12]} by {how}; tree {t_pub[:12]} vs reviewed {t_rev[:12]}: "
              f"{'match' if t_pub == t_rev else 'MISMATCH'}")
        fail |= t_pub != t_rev

        epoch = git(a.repo, "log", "-1", "--format=%ct", reviewed)
        r1 = build(a.repo, reviewed, work / "r1", epoch)
        r2 = build(a.repo, reviewed, work / "r2", epoch)
        p1 = build(pub, got, work / "p1", epoch)
        import setuptools  # noqa: PLC0415 - the version of the builder, for the record
        print(f"build: setuptools {setuptools.__version__}, SOURCE_DATE_EPOCH {epoch}")
        for name in sorted(r1):
            (raw, key), by = r1[name], "content" if name.endswith(".tar.gz") else "sha256"
            bytes_same, same = raw == r2.get(name, ("",))[0], key == r2.get(name, ("", ""))[1]
            pub_key = p1.get(name, ("-", "-"))[1]
            print(f"build: {name} by {by}: reviewed {key[:16]} twice: {'reproducible' if same else 'NOT REPRODUCIBLE'}"
                  f"{'' if bytes_same else ' (bytes differ)'}; published {pub_key[:16]}: "
                  f"{'match' if pub_key == key else 'MISMATCH'}")
            fail |= not same or pub_key != key
        r1 = {name: raw for name, (raw, _) in r1.items()}

        pypi = get_json(f"https://pypi.org/pypi/{a.pypi}/{version}/json")
        if pypi is None:
            print(f"pypi: {a.pypi} {version}: 404, no published file")
        else:
            fail |= bool(compare("pypi", {u["filename"]: u["digests"]["sha256"] for u in pypi.get("urls", [])}, r1))

        rel = get_json(f"https://api.github.com/repos/{a.github}/releases/tags/{a.tag}")
        if rel is None:
            print(f"release: {a.github} {a.tag}: 404, no release assets")
        else:
            assets = {x["name"]: str(x.get("digest") or "").removeprefix("sha256:") for x in rel.get("assets", [])}
            print(f"release: {a.github} {a.tag}: {len(assets)} assets")
            fail |= bool(compare("release", assets, r1))
    except Stop as e:
        print(f"stopped: {e}")
        return 2
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("result: " + ("FAIL" if fail else "PASS"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
