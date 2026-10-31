#!/usr/bin/env python3
"""Fetch and verify the scarletbook-rs C library that pysacd links statically (#85).

    python3 tools/scarletbook.py fetch --dest DIR [--cc CC | --target TARGET] [--archive FILE]
    python3 tools/scarletbook.py target [--cc CC]
    python3 tools/scarletbook.py attest [--dir DIR]

`fetch` downloads `scarletbook-ffi-<version>-<target>.tar.gz` from the release named in
scarletbook.lock (or takes it from --archive, or from the directory in
$PYSACD_SCARLETBOOK_CACHE), refuses it unless its SHA-256 is the one pinned there, and
unpacks it into DIR (include/, lib/, native-static-libs.txt). The target is the one the C
compiler builds for (`CC -dumpmachine`), so a cross compiler gets its own target's library;
$SCARLETBOOK_TARGET or --target overrides it, for a compiler wrapper that misreports its target
(Fedora's musl-gcc says x86_64-redhat-linux). It prints DIR. A DIR already holding the pinned archive is left alone.

`attest` downloads every pinned archive, checks its SHA-256, and checks its signed build
provenance with `gh attestation verify`: built by scarletbook-rs's release workflow from the
pinned tag and commit. CI runs it on every pull request, so a pin that isn't an attested
scarletbook release can't be merged. Needs the GitHub CLI, logged in.

Standard library only: setup.py runs it while building a wheel or the sdist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "scarletbook.lock"
STAMP = ".pysacd-scarletbook"       # in DEST: "<version> <target> <sha256>" of what was unpacked
MAX_ARCHIVE = 64 << 20              # the archives are about 8 MB
TIMEOUT = 60


class FetchError(Exception):
    pass


def load_lock(path: Path | None = None) -> dict:
    path = path or LOCK
    lock = json.loads(path.read_text(encoding="utf-8"))
    for key in ("repository", "version", "abi", "commit", "assets"):
        if key not in lock:
            raise FetchError(f"{path}: no {key!r}")
    return lock


def archive_name(lock: dict, target: str) -> str:
    return f"scarletbook-ffi-{lock['version']}-{target}.tar.gz"


def target_of(machine: str) -> str:
    """Map a compiler's -dumpmachine triple (x86_64-redhat-linux, aarch64-alpine-linux-musl,
    arm64-apple-darwin24.0.0, x86_64-w64-mingw32, ...) to a scarletbook release target."""
    m = machine.strip().lower()
    arch = m.split("-", 1)[0]
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(arch, arch)
    if "mingw" in m or "windows" in m:
        os_part = "pc-windows-gnu"
    elif "darwin" in m or "apple" in m:
        os_part = "apple-darwin"
    elif "linux" in m:
        os_part = "unknown-linux-musl" if "musl" in m else "unknown-linux-gnu"
    else:
        raise FetchError(f"no scarletbook build for compiler target {machine.strip()!r}")
    return f"{arch}-{os_part}"


def detect_target(cc: str | None) -> str:
    # the compiler the build uses, as make would run it: $CC is the builder's own setting
    cmd = shlex.split(cc or os.environ.get("CC") or ("gcc" if sys.platform == "win32" else "cc"))
    try:
        out = subprocess.run([*cmd, "-dumpmachine"], capture_output=True, text=True,  # nosemgrep: dangerous-subprocess-use-tainted-env-args  # the builder's own compiler, no shell
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise FetchError(f"can't ask the C compiler ({' '.join(cmd)}) for its target: {e}") from e
    return target_of(out)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    if not url.startswith("https://"):
        raise FetchError(f"refusing a non-HTTPS URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "pysacd-build"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dest, "wb") as f:  # nosemgrep: dynamic-urllib-use-detected  # https only, checked above
            total = 0
            while block := r.read(1 << 20):
                total += len(block)
                if total > MAX_ARCHIVE:
                    raise FetchError(f"{url}: larger than {MAX_ARCHIVE} bytes")
                f.write(block)
    except OSError as e:
        raise FetchError(f"downloading {url} failed: {e}") from e


def obtain(lock: dict, target: str, archive: Path | None, work: Path) -> Path:
    """The pinned archive for target, checked: from --archive, the cache, or the release."""
    want = lock["assets"].get(target)
    if want is None:
        raise FetchError(f"scarletbook {lock['version']} has no build for {target} (it has: "
                         f"{', '.join(sorted(lock['assets']))}); build it from source and set SCARLETBOOK_DIR, "
                         "see docs/development.md")
    name = archive_name(lock, target)
    cache = os.environ.get("PYSACD_SCARLETBOOK_CACHE")
    if archive is None and cache and (Path(cache) / name).is_file():
        archive = Path(cache) / name
    if archive is None:
        url = f"https://github.com/{lock['repository']}/releases/download/v{lock['version']}/{name}"
        archive = work / name
        print(f"scarletbook: downloading {url}", file=sys.stderr)
        download(url, archive)
    got = sha256(archive)
    if got != want:
        raise FetchError(f"{archive}: SHA-256 {got}, but scarletbook.lock pins {want} for {name}; refusing it")
    if cache:
        Path(cache).mkdir(parents=True, exist_ok=True)
        if not (Path(cache) / name).is_file():
            shutil.copyfile(archive, Path(cache) / name)
    return archive


def unpack(archive: Path, top: str, dest: Path) -> None:
    """Unpack archive's top/ directory into dest: regular files and directories only, nothing
    outside top/ (the archive was checked against the pin, but stay strict anyway)."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            parts = Path(m.name).parts
            if not parts or parts[0] != top or ".." in parts or Path(m.name).is_absolute():
                raise FetchError(f"{archive}: unexpected member {m.name!r}")
            if not (m.isfile() or m.isdir()):
                raise FetchError(f"{archive}: {m.name!r} is not a regular file or directory")
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(tmp, members=members, filter="data")
            else:   # Python < 3.11.4: the checks above already did the data filter's job
                tar.extractall(tmp, members=members)  # nosemgrep: tarfile-extractall-traversal  # members checked above
            if dest.exists():
                shutil.rmtree(dest)
            os.replace(Path(tmp) / top, dest)


def fetch(dest: Path, target: str, archive: Path | None) -> Path:
    lock = load_lock()
    want = f"{lock['version']} {target} {lock['assets'].get(target)}"
    stamp = dest / STAMP
    if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == want \
            and (dest / "include" / "scarletbook.h").is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        path = obtain(lock, target, archive, Path(work))
        unpack(path, archive_name(lock, target).removesuffix(".tar.gz"), dest)
    for need in ("include/scarletbook.h", "native-static-libs.txt"):
        if not (dest / need).is_file():
            raise FetchError(f"{dest}: the archive has no {need}")
    stamp.write_text(want + "\n", encoding="utf-8")
    return dest


def attest(work: Path) -> int:
    """Check every pinned archive's digest and build provenance (CI, on every pull request)."""
    lock = load_lock()
    if not shutil.which("gh"):
        raise FetchError("attest needs the GitHub CLI (gh)")
    work.mkdir(parents=True, exist_ok=True)
    repo, tag = lock["repository"], f"v{lock['version']}"
    workflow = f"{repo}/{lock.get('signer_workflow', '.github/workflows/release.yml')}"
    for target in sorted(lock["assets"]):
        path = obtain(lock, target, None, work)
        print(f"{archive_name(lock, target)}: SHA-256 matches the pin", flush=True)
        out = subprocess.run(["gh", "attestation", "verify", str(path), "--repo", repo,
                              "--signer-workflow", workflow, "--source-ref", f"refs/tags/{tag}",
                              "--deny-self-hosted-runners", "--format", "json"],
                             check=False, capture_output=True, text=True)
        if out.returncode:
            print(out.stdout, out.stderr, sep="\n", file=sys.stderr)
            raise FetchError(f"{path.name}: build provenance does not verify")
        commits = {r["verificationResult"]["signature"]["certificate"]["sourceRepositoryDigest"]
                   for r in json.loads(out.stdout)}
        if commits != {lock["commit"]}:
            raise FetchError(f"{path.name}: built from {sorted(commits)}, scarletbook.lock pins {lock['commit']}")
        print(f"{path.name}: attested, built by {workflow} from {tag} ({lock['commit'][:12]})", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download, check and unpack the pinned library for one target")
    f.add_argument("--dest", type=Path, required=True)
    f.add_argument("--cc", help="the C compiler to build for (default: $CC, else cc)")
    f.add_argument("--target", help="a scarletbook target, instead of asking the compiler")
    f.add_argument("--archive", type=Path, help="a local copy of the release archive (checked like a download)")
    t = sub.add_parser("target", help="print the scarletbook target of the C compiler")
    t.add_argument("--cc")
    a = sub.add_parser("attest", help="check every pinned archive's digest and provenance (needs gh)")
    a.add_argument("--dir", type=Path, default=Path("build/scarletbook-attest"))
    args = ap.parse_args(argv)
    try:
        if args.cmd == "target":
            print(detect_target(args.cc))
        elif args.cmd == "fetch":
            target = args.target or os.environ.get("SCARLETBOOK_TARGET") or detect_target(args.cc)
            print(fetch(args.dest, target, args.archive).as_posix())
        else:
            return attest(args.dir)
    except FetchError as e:
        print(f"scarletbook: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
