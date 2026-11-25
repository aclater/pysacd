#!/usr/bin/env python3
"""Fetch or build the dsd2pcm C library that pysacd links statically for PCM conversion.

    python3 tools/dsd2pcm.py fetch --dest DIR [--cc CC | --target TARGET] [--archive FILE] [--source FILE]
    python3 tools/dsd2pcm.py toolchain --dest DIR [--host HOST]
    python3 tools/dsd2pcm.py attest [--dir DIR]
    python3 tools/dsd2pcm.py vendor --dest DIR [--source FILE]
    python3 tools/dsd2pcm.py source-name

`fetch` puts include/dsd2pcm.h and lib/libdsd2pcm.a into DIR for the target the C compiler
builds for (as tools/scarletbook.py does; $DSD2PCM_TARGET or --target overrides it). It builds the
library from the pinned source: --source, else the copy vendored in an sdist
(vendor/dsd2pcm/), else the one in $PYSACD_DSD2PCM_CACHE, else a download; each is refused unless
its SHA-256 is the one pinned in dsd2pcm.lock. It builds with `cargo rustc --locked` and without
the FLAC reader. Before building, it checks with `cargo tree` that every crate linked in is under
a licence with an MIT alternative (pysacd takes such code under MIT; Apache-2.0-only code is not
GPL-2.0 compatible). It needs Rust: `cargo` 1.83 or later on PATH, or $CARGO; `toolchain` installs
the pinned one. It prints DIR. (The Makefile marks the library's code for CET shadow stacks on
x86-64 glibc Linux: tools/mark_shstk.py.)

A source whose version is older than "min_version", or a pre-release (0.2.0-rc.1), is refused;
$PYSACD_DSD2PCM_ALLOW_PRERELEASE=1 lets a pre-release through, for testing only.

With "use_archives": true in dsd2pcm.lock, a target listed in its assets takes the release's C
library archive instead (--archive, the cache, or a download), checked against its SHA-256.
$DSD2PCM_FROM_SOURCE=1 builds from source even then.

`toolchain` downloads the Rust release pinned in dsd2pcm.lock (rustc, cargo and the standard
library for HOST, default this machine), checks each archive's SHA-256, and installs them
into DIR. It prints DIR/bin.

`attest` checks every pinned archive's digest and signed build provenance (built by
dsd2pcm-rs's release workflow from the pinned tag and commit), that the tag still names the
pinned commit, and the source archive's digest. Needs the GitHub CLI, logged in.

`source-name` prints the pinned source archive's file name (what `vendor` puts in the sdist).

cargo runs with CARGO_HOME set to a directory of its own (a temporary one; for a build that reads
crates.io's index, $PYSACD_DSD2PCM_CACHE/cargo-home when the cache is set), so the user's ~/.cargo/config.toml (registry replacements, wrappers,
rustflags) has no say in the build.

`vendor` copies the pinned source archive (checked against its SHA-256) and its licence files
into DIR. setup.py runs it when it builds the sdist, into vendor/dsd2pcm/, so that building from
the sdist downloads no dsd2pcm source.

The pinned source is one of two archives ("source.archive" in dsd2pcm.lock):
- "capi-src": the C library's source archive that dsd2pcm releases from 0.2.0 on attach
  (libdsd2pcm-<version>-src.tar.gz): dsd2pcm and dsd2pcm-capi as a workspace of their own whose
  Cargo.lock lists those two crates only, so cargo builds it with --offline: no crates.io index,
  no network. `attest` checks its build provenance.
- "repo" (or none): GitHub's archive of the repository at the tag or commit. cargo still resolves
  its Cargo.lock, which lists the optional FLAC reader and test dependencies, against crates.io's
  index; with that index cached (in $PYSACD_DSD2PCM_CACHE/cargo-home), CARGO_NET_OFFLINE=true
  builds offline.

Standard library only: setup.py runs it while building a wheel or the sdist.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "dsd2pcm.lock"
VENDORED = ROOT / "vendor" / "dsd2pcm"   # in an sdist: the pinned source archive and its licences
LICENCE_FILES = ("LICENSE-MIT", "LICENSE-APACHE")
ALLOW_PRERELEASE = "PYSACD_DSD2PCM_ALLOW_PRERELEASE"
STAMP = ".pysacd-dsd2pcm"           # in DEST: "<version> <target> <how> <sha256>" of what is there
MAX_DOWNLOAD = 256 << 20            # a Rust compiler archive is about 110 MB
TIMEOUT = 120

# The shared helpers (target detection, digests, safe unpacking) are scarletbook.py's. Loaded by
# path: setup.py runs both tools with -I, which leaves this directory off sys.path.
_spec = importlib.util.spec_from_file_location("_pysacd_scarletbook_tool", Path(__file__).with_name("scarletbook.py"))
assert _spec and _spec.loader
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)
FetchError = sb.FetchError


def load_lock(path: Path | None = None) -> dict:
    path = path or LOCK
    lock = json.loads(path.read_text(encoding="utf-8"))
    for key in ("repository", "version", "min_version", "abi", "commit", "assets", "source", "rust"):
        if key not in lock:
            raise FetchError(f"{path}: no {key!r}")
    return lock


def archive_name(lock: dict, target: str) -> str:
    return f"libdsd2pcm-{lock['version']}-{lock['assets'][target]['name']}.tar.gz"


def capi_src(lock: dict) -> bool:
    """Whether the pin is the release's C library source archive (offline build), not the repository's."""
    kind = lock["source"].get("archive", "repo")
    if kind not in ("repo", "capi-src"):
        raise FetchError(f"dsd2pcm.lock: unknown source.archive {kind!r} (repo or capi-src)")
    return kind == "capi-src"


def source_name(lock: dict) -> str:
    if capi_src(lock):
        return f"libdsd2pcm-{lock['version']}-src.tar.gz"
    return f"dsd2pcm-rs-{source_ref(lock).removeprefix('v')}.tar.gz"


def source_top(lock: dict) -> str:
    """The archive's top folder."""
    if capi_src(lock):
        return f"libdsd2pcm-{lock['version']}"
    return f"dsd2pcm-rs-{source_ref(lock).removeprefix('v')}"


def source_ref(lock: dict) -> str:
    """The tag (vX.Y.Z) or commit the source archive is made from."""
    return lock["source"].get("ref") or f"v{lock['version']}"


def version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("-")[0].split("+")[0].split("."))
    except ValueError as e:
        raise FetchError(f"not a version: {v!r}") from e


def is_prerelease(v: str) -> bool:
    """A semver pre-release (0.1.2-rc1); build metadata (+...) alone is not one."""
    return "-" in v.split("+", 1)[0]


def check_version(lock: dict, found: str) -> None:
    if version_tuple(found) < version_tuple(lock["min_version"]):
        raise FetchError(f"pysacd requires dsd2pcm {lock['min_version']} or later; this source is {found}")
    if is_prerelease(found) and os.environ.get(ALLOW_PRERELEASE) != "1":
        raise FetchError(f"dsd2pcm {found} is a pre-release; pysacd links releases only "
                         f"(set {ALLOW_PRERELEASE}=1 to test one)")


def workspace_version(src: Path) -> str:
    """The version in the source's Cargo.toml ([workspace.package] version)."""
    section = None
    try:
        lines = (src / "Cargo.toml").read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise FetchError(f"{src}: no Cargo.toml") from e
    for line in lines:      # tomllib is Python 3.11+; this needs only the one key
        line = line.split("#", 1)[0].strip()
        if line.startswith("["):
            section = line
        elif section == "[workspace.package]" and re.fullmatch(r'version\s*=\s*"[^"]+"', line):
            return line.split('"')[1]
    raise FetchError(f"{src}: no [workspace.package] version in Cargo.toml")


def download(url: str, dest: Path) -> None:
    if not url.startswith("https://"):
        raise FetchError(f"refusing a non-HTTPS URL: {url}")
    print(f"dsd2pcm: downloading {url}", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "pysacd-build"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dest, "wb") as f:  # nosemgrep: dynamic-urllib-use-detected  # https only, checked above
            total = 0
            while block := r.read(1 << 20):
                total += len(block)
                if total > MAX_DOWNLOAD:
                    raise FetchError(f"{url}: larger than {MAX_DOWNLOAD} bytes")
                f.write(block)
    except OSError as e:
        raise FetchError(f"downloading {url} failed: {e}") from e


def obtain(name: str, url: str, want: str, given: Path | None, work: Path) -> Path:
    """A pinned file, checked: from the caller, an sdist's vendored copy, the cache, or the URL."""
    cache = os.environ.get("PYSACD_DSD2PCM_CACHE")
    path = given
    if path is None and (VENDORED / name).is_file():
        path = VENDORED / name
    if path is None and cache and (Path(cache) / name).is_file():
        path = Path(cache) / name
    if path is None:
        path = work / name
        download(url, path)
    got = sb.sha256(path)
    if got != want:
        raise FetchError(f"{path}: SHA-256 {got}, but dsd2pcm.lock pins {want} for {name}; refusing it")
    if cache:
        Path(cache).mkdir(parents=True, exist_ok=True)
        if not (Path(cache) / name).is_file():
            shutil.copyfile(path, Path(cache) / name)
    return path


def release_url(lock: dict, name: str) -> str:
    return f"https://github.com/{lock['repository']}/releases/download/v{lock['version']}/{name}"


def source_url(lock: dict) -> str:
    if capi_src(lock):
        return release_url(lock, source_name(lock))
    ref = source_ref(lock)
    kind = "refs/tags/" if ref.startswith("v") else ""
    return f"https://github.com/{lock['repository']}/archive/{kind}{ref}.tar.gz"


# Licences pysacd may link (each crate's licence must offer one of these as an alternative)
MIT_COMPATIBLE = {"MIT", "0BSD", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Zlib", "Unlicense"}

# Licence expressions with AND or WITH need a person to read them: an MIT alternative inside one
# doesn't make the whole crate MIT-compatible. Each entry maps "crate expression" (as `cargo tree`
# prints them) to the licence pysacd takes that crate under, after a review; none is needed today
# (without default features dsd2pcm links no crate outside Rust's standard library).
REVIEWED_LICENCES: dict[str, str] = {}


def licence_alternatives(pkg: str, lic: str) -> set[str] | None:
    """The licences a crate may be taken under, or None if its expression can't be read safely."""
    name = pkg.split()[0] if pkg.split() else pkg
    if f"{name} {lic}" in REVIEWED_LICENCES:
        return {REVIEWED_LICENCES[f"{name} {lic}"]}
    words = lic.replace("(", " ").replace(")", " ").split()
    if any(w.upper() in ("AND", "WITH") for w in words):
        return None
    # SPDX OR, and the old Cargo form "MIT/Apache-2.0"
    return {a for w in words if w.upper() != "OR" for a in w.split("/") if a}


def rustflags(target: str, src: Path, target_dir: Path | None = None) -> str:
    """CARGO_ENCODED_RUSTFLAGS (0x1f between flags): a path with spaces stays one flag."""
    # As dsd2pcm's own release builds: x86 code for the baseline CPU (its SIMD kernels are
    # chosen at run time). No build paths in the library: the source's, and cargo's target
    # directory's, which Windows objects name in their CodeView records (S_OBJNAME).
    flags = ["-C", "target-cpu=x86-64"] if target.startswith("x86_64-") else []
    remaps = [f"--remap-path-prefix={target_dir}=/target"] if target_dir else []
    return "\x1f".join([*flags, *remaps, f"--remap-path-prefix={src}=/dsd2pcm"])


def cargo_home(work: Path, keep: bool = False) -> Path:
    """A CARGO_HOME of pysacd's own, never the user's ~/.cargo, whose config.toml could replace the
    registry, wrap rustc or add flags: in the work directory, or with `keep` (a build that reads
    crates.io's index: a "repo" pin, the command-line tool) in the cache when there is one, so the
    index is kept for the next build. An offline build writes nothing worth keeping, and a
    read-only cache must do for it."""
    cache = os.environ.get("PYSACD_DSD2PCM_CACHE")
    home = Path(cache) / "cargo-home" if cache and keep else work / "cargo-home"
    home.mkdir(parents=True, exist_ok=True)
    return home


# Environment variables that change what cargo or rustc does, beyond the ones set here
CARGO_ENV_DROP = ("RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "CARGO_BUILD_RUSTFLAGS",
                  "CARGO_BUILD_RUSTC_WRAPPER", "CARGO_ENCODED_RUSTFLAGS", "CARGO_REGISTRIES_CRATES_IO_PROTOCOL")


def cargo_env(target: str, src: Path, work: Path, keep_index: bool = False) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in CARGO_ENV_DROP and not k.startswith("CARGO_SOURCE_")}
    env.update(CARGO_HOME=str(cargo_home(work, keep_index)), CARGO_TARGET_DIR=str(work / "target"),
               CARGO_ENCODED_RUSTFLAGS=rustflags(target, src, work / "target"))
    return env


def check_licences(cargo: str, src: Path, target: str, env: dict, offline: bool = False) -> None:
    """Refuse a build that would link a crate without an MIT-compatible licence alternative."""
    out = subprocess.run([cargo, "tree", "--locked", *(["--offline"] if offline else []), "-e", "normal",  # nosemgrep: dangerous-subprocess-use-tainted-env-args  # the builder's own cargo, no shell
                          "--no-default-features", "-p", "dsd2pcm-capi",
                          "--target", target, "--prefix", "none", "--format", "{p}\t{l}"],
                         cwd=src, env=env, check=False, capture_output=True, text=True)
    if out.returncode:
        print(out.stderr, file=sys.stderr)
        raise FetchError("cargo tree failed (see above)")
    for line in sorted(set(out.stdout.splitlines())):
        pkg, _, lic = line.partition("\t")
        alternatives = licence_alternatives(pkg, lic.strip())
        if alternatives is None:
            raise FetchError(f"{' '.join(pkg.split()[:2])} is under {lic}: an AND or WITH expression needs a "
                             "review (REVIEWED_LICENCES in tools/dsd2pcm.py) before pysacd links it")
        if not alternatives & MIT_COMPATIBLE:
            raise FetchError(f"{' '.join(pkg.split()[:2])} is under {lic or 'no stated licence'}, which pysacd can't link")
        print(f"dsd2pcm: links {' '.join(pkg.split()[:2])} ({lic})", file=sys.stderr)


def unpack_source(tarball: Path, top: str, dest: Path) -> None:
    """The source tree: regular files and directories under top/ only. Its symbolic links (licence
    and readme files shared by the crates) aren't needed to build and are left out."""
    with tarfile.open(tarball, "r:gz") as tar:
        members = []
        for m in tar.getmembers():
            parts = Path(m.name).parts
            if not parts or parts[0] != top or ".." in parts or Path(m.name).is_absolute():
                raise FetchError(f"{tarball.name}: unexpected member {m.name!r}")
            if ".cargo" in parts:       # cargo would read a config.toml in the tree
                raise FetchError(f"{tarball.name}: unexpected cargo configuration {m.name!r}")
            if m.isfile() or m.isdir():
                members.append(m)
            elif not m.issym():
                raise FetchError(f"{tarball.name}: {m.name!r} is not a regular file, directory or link")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest.parent / "unpacked", members=members, filter="data")
        else:
            tar.extractall(dest.parent / "unpacked", members=members)  # nosemgrep: tarfile-extractall-traversal  # members checked above
    os.replace(dest.parent / "unpacked" / top, dest)


def build_from_source(lock: dict, target: str, dest: Path, source: Path | None, work: Path) -> None:
    cargo = os.environ.get("CARGO") or shutil.which("cargo")
    if not cargo:
        raise FetchError(f"dsd2pcm {lock['version']} has no C library archive for {target}, and building it "
                         "needs Rust (cargo, 1.83 or later): install Rust, or run `python3 tools/dsd2pcm.py "
                         "toolchain --dest DIR` and put DIR/bin on PATH; see docs/development.md")
    tarball = obtain(source_name(lock), source_url(lock), lock["source"]["sha256"], source, work)
    src = work / "src"
    unpack_source(tarball, source_top(lock), src)
    check_version(lock, workspace_version(src))
    offline = capi_src(lock)
    env = cargo_env(target, src, work, keep_index=not offline)
    if offline:     # the C library's own workspace needs nothing from a registry
        env["CARGO_NET_OFFLINE"] = "true"
    check_licences(cargo, src, target, env, offline)
    # --locked: the dependency versions and checksums of the release's Cargo.lock. Without its
    # default features the library has no dependency outside Rust's standard library. Only the
    # static library: no shared library is linked.
    cmd = [cargo, "rustc", "--release", "--locked", *(["--offline"] if offline else []), "--no-default-features",
           "-p", "dsd2pcm-capi", "--lib", "--target", target, "--crate-type", "staticlib"]

    print(f"dsd2pcm: building {lock['version']} for {target} from source: {' '.join(cmd[1:])}", file=sys.stderr)
    if subprocess.run(cmd, cwd=src, env=env, check=False).returncode:  # nosemgrep: dangerous-subprocess-use-tainted-env-args  # the builder's own cargo, no shell
        raise FetchError(f"building dsd2pcm for {target} failed (see above)")
    (dest / "lib").mkdir(parents=True)
    (dest / "include").mkdir()
    shutil.copyfile(work / "target" / target / "release" / "libdsd2pcm.a", dest / "lib" / "libdsd2pcm.a")
    header = src / "include" / "dsd2pcm.h" if offline else src / "crates" / "dsd2pcm-capi" / "include" / "dsd2pcm.h"
    shutil.copyfile(header, dest / "include" / "dsd2pcm.h")
    for lic in LICENCE_FILES:
        shutil.copyfile(src / lic, dest / lic)


def repo_archive_name(lock: dict) -> str:
    """GitHub's archive of the repository at the pinned commit (the CLI's source)."""
    return f"dsd2pcm-rs-{lock['commit']}.tar.gz"


def repo_tree(lock: dict, work: Path, given: Path | None = None) -> Path:
    """The repository at the pinned commit, unpacked into work/repo, for what the C library source
    archive doesn't carry (dsd2pcm's command-line tool). Taken from `given`, the cache, or
    GitHub's archive of the commit (HTTPS only). Its digest can't be pinned (GitHub doesn't keep
    an archive's bytes), so it is checked against the pinned source instead: with a "capi-src" pin,
    every file of the two library crates and the C header must be byte-identical to that archive's
    (each crate's Cargo.toml is rewritten there and is left out), and the workspace version must be
    the pinned one. Refused otherwise. With a "repo" pin the pinned archive is the repository
    itself, checked by its digest."""
    if not capi_src(lock):
        tarball = obtain(source_name(lock), source_url(lock), lock["source"]["sha256"], given, work)
        unpack_source(tarball, source_top(lock), work / "repo")
        check_version(lock, workspace_version(work / "repo"))
        return work / "repo"
    name = repo_archive_name(lock)
    cache = os.environ.get("PYSACD_DSD2PCM_CACHE")
    path = given
    if path is None and cache and (Path(cache) / name).is_file():
        path = Path(cache) / name
    if path is None:
        path = work / name
        download(f"https://github.com/{lock['repository']}/archive/{lock['commit']}.tar.gz", path)
    unpack_source(path, name.removesuffix(".tar.gz"), work / "repo")
    pinned = obtain(source_name(lock), source_url(lock), lock["source"]["sha256"], None, work)
    unpack_source(pinned, source_top(lock), work / "capi")
    repo, capi = work / "repo", work / "capi"
    pairs = [(capi / "include" / "dsd2pcm.h", repo / "crates" / "dsd2pcm-capi" / "include" / "dsd2pcm.h")]
    for crate in ("dsd2pcm", "dsd2pcm-capi"):
        for f in sorted((capi / "crates" / crate).rglob("*")):
            if f.is_file() and f.relative_to(capi / "crates" / crate) != Path("Cargo.toml"):
                pairs.append((f, repo / f.relative_to(capi)))
    for a, b in pairs:
        if not b.is_file() or b.read_bytes() != a.read_bytes():
            raise FetchError(f"{name}: {b.relative_to(repo)} differs from the pinned {source_name(lock)}; refusing it")
    check_version(lock, workspace_version(repo))
    if workspace_version(repo) != lock["version"]:
        raise FetchError(f"{name}: version {workspace_version(repo)}, dsd2pcm.lock pins {lock['version']}")
    print(f"dsd2pcm: {name}: the library's {len(pairs)} source files equal the pinned {source_name(lock)}",
          file=sys.stderr)
    return repo


def vendor(dest: Path, source: Path | None) -> Path:
    """The pinned source archive, checked, and its licence files, into dest (for the sdist)."""
    lock = load_lock()
    check_version(lock, lock["version"])
    name = source_name(lock)
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        path = obtain(name, source_url(lock), lock["source"]["sha256"], source, Path(tmp))
        top = source_top(lock)
        with tarfile.open(path, "r:gz") as tar:
            for lic in LICENCE_FILES:
                m = tar.getmember(f"{top}/{lic}")
                f = tar.extractfile(m) if m.isfile() else None
                if f is None:
                    raise FetchError(f"{name}: {lic} is not a regular file")
                (dest / lic).write_bytes(f.read())
        if path.resolve() != (dest / name).resolve():
            shutil.copyfile(path, dest / name)
    if sb.sha256(dest / name) != lock["source"]["sha256"]:
        raise FetchError(f"{dest / name}: SHA-256 changed while copying")
    return dest


def fetch(dest: Path, target: str, archive: Path | None, source: Path | None) -> Path:
    lock = load_lock()
    check_version(lock, lock["version"])
    from_source = not lock.get("use_archives") or os.environ.get("DSD2PCM_FROM_SOURCE") == "1" \
        or target not in lock["assets"]
    digest = lock["source"]["sha256"] if from_source else lock["assets"][target]["sha256"]
    want = f"{lock['version']} {target} {'source' if from_source else 'release'} {digest}"
    stamp = dest / STAMP
    if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == want \
            and (dest / "lib" / "libdsd2pcm.a").is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if from_source:
            if archive:
                raise FetchError(f"--archive: dsd2pcm {lock['version']} is built from source for {target}")
            built = work / "out"
            build_from_source(lock, target, built, source, work)
        else:
            name = archive_name(lock, target)
            path = obtain(name, release_url(lock, name), digest, archive, work)
            built = work / "out"
            sb.unpack(path, name.removesuffix(".tar.gz"), built)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(built), str(dest))
    for need in ("include/dsd2pcm.h", "lib/libdsd2pcm.a"):
        if not (dest / need).is_file():
            raise FetchError(f"{dest}: no {need}")
    stamp.write_text(want + "\n", encoding="utf-8")
    return dest


def host_triple() -> str:
    m = platform.machine().lower()
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(m, m)
    if sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    if sys.platform == "win32" or "mingw" in sys.platform:
        return f"{arch}-pc-windows-gnu"
    libc = platform.libc_ver()[0]
    return f"{arch}-unknown-linux-{'gnu' if libc == 'glibc' else 'musl'}"


def toolchain(dest: Path, host: str) -> Path:
    lock = load_lock()
    rust = lock["rust"]
    comps = rust["hosts"].get(host)
    if comps is None:
        raise FetchError(f"no Rust toolchain pinned for {host} (pinned: {', '.join(sorted(rust['hosts']))})")
    want = f"{rust['version']} {host} " + " ".join(f"{c}={h}" for c, h in sorted(comps.items()))
    stamp = dest / ".pysacd-rust"
    if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == want:
        return dest / "bin"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    parts = [(f"{comp}-{rust['version']}-{host}", digest) for comp, digest in comps.items()]
    with tempfile.TemporaryDirectory() as tmp:
        for top, digest in parts:
            path = obtain(f"{top}.tar.xz", f"https://static.rust-lang.org/dist/{rust['date']}/{top}.tar.xz",
                          digest, None, Path(tmp))
            with tarfile.open(path, "r:xz") as tar:
                members = []
                for m in tar.getmembers():
                    bits = Path(m.name).parts
                    if not bits or bits[0] != top or ".." in bits or Path(m.name).is_absolute() \
                            or not (m.isfile() or m.isdir()):
                        raise FetchError(f"{path.name}: unexpected member {m.name!r}")
                    members.append(m)
                out = Path(tmp) / "x"
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(out, members=members, filter="data")
                else:
                    tar.extractall(out, members=members)  # nosemgrep: tarfile-extractall-traversal  # members checked above
            # an archive holds one directory per component (rustc/, cargo/, rust-std-HOST/), each
            # laid out like the installed prefix
            for part in (out / top).iterdir():
                if part.is_dir():
                    shutil.copytree(part, dest, dirs_exist_ok=True)
            shutil.rmtree(out)
            path.unlink(missing_ok=True)
    stamp.write_text(want + "\n", encoding="utf-8")
    return dest / "bin"


def verify_provenance(path: Path, repo: str, workflow: str, tag: str, commit: str) -> None:
    """The file's signed build provenance: built by `workflow` from `tag`, at `commit`."""
    out = subprocess.run(["gh", "attestation", "verify", str(path), "--repo", repo,
                          "--signer-workflow", workflow, "--source-ref", f"refs/tags/{tag}",
                          "--deny-self-hosted-runners", "--format", "json"],
                         check=False, capture_output=True, text=True)
    if out.returncode:
        print(out.stdout, out.stderr, sep="\n", file=sys.stderr)
        raise FetchError(f"{path.name}: build provenance does not verify")
    commits = {r["verificationResult"]["signature"]["certificate"]["sourceRepositoryDigest"]
               for r in json.loads(out.stdout)}
    if commits != {commit}:
        raise FetchError(f"{path.name}: built from {sorted(commits)}, dsd2pcm.lock pins {commit}")
    print(f"{path.name}: attested, built by {workflow} from {tag} ({commit[:12]})", flush=True)


def attest(work: Path) -> int:
    lock = load_lock()
    if not shutil.which("gh"):
        raise FetchError("attest needs the GitHub CLI (gh)")
    work.mkdir(parents=True, exist_ok=True)
    repo, tag, ref = lock["repository"], f"v{lock['version']}", source_ref(lock)
    workflow = f"{repo}/{lock['signer_workflow']}"
    for target in sorted(lock["assets"]) if lock.get("use_archives") else []:
        name = archive_name(lock, target)
        path = obtain(name, release_url(lock, name), lock["assets"][target]["sha256"], None, work)
        print(f"{name}: SHA-256 matches the pin", flush=True)
        verify_provenance(path, repo, workflow, tag, lock["commit"])
    # the source archive: its ref (a tag, or a commit before the release is tagged) names the
    # pinned commit, and the archive is the pinned one
    out = subprocess.run(["gh", "api", f"repos/{repo}/commits/{ref}", "--jq", ".sha"],
                         check=False, capture_output=True, text=True)
    if out.returncode or out.stdout.strip() != lock["commit"]:
        raise FetchError(f"{ref} names {out.stdout.strip() or '(unknown)'}, dsd2pcm.lock pins {lock['commit']}")
    path = obtain(source_name(lock), source_url(lock), lock["source"]["sha256"], None, work)
    print(f"{source_name(lock)}: SHA-256 matches the pin; {ref} is {lock['commit'][:12]}", flush=True)
    if capi_src(lock):      # a release asset: built by the release workflow from the tag and commit
        verify_provenance(path, repo, workflow, tag, lock["commit"])
    check_version(lock, lock["version"])
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download (or build) and check the pinned library for one target")
    f.add_argument("--dest", type=Path, required=True)
    f.add_argument("--cc", help="the C compiler to build for (default: $CC, else cc)")
    f.add_argument("--target", help="a Rust target triple, instead of asking the compiler")
    f.add_argument("--archive", type=Path, help="a local copy of the release archive (checked like a download)")
    f.add_argument("--source", type=Path, help="a local copy of the source archive (checked like a download)")
    t = sub.add_parser("toolchain", help="install the pinned Rust toolchain")
    t.add_argument("--dest", type=Path, required=True)
    t.add_argument("--host", help="the Rust host triple (default: this machine)")
    a = sub.add_parser("attest", help="check every pinned archive's digest and provenance (needs gh)")
    a.add_argument("--dir", type=Path, default=Path("build/dsd2pcm-attest"))
    v = sub.add_parser("vendor", help="copy the pinned source and its licences into DIR (sdist builds)")
    v.add_argument("--dest", type=Path, required=True)
    v.add_argument("--source", type=Path, help="a local copy of the source archive (checked like a download)")
    sub.add_parser("source-name", help="print the pinned source archive's file name")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "fetch":
            target = args.target or os.environ.get("DSD2PCM_TARGET") or os.environ.get("SCARLETBOOK_TARGET") \
                or sb.detect_target(args.cc)
            print(fetch(args.dest, target, args.archive, args.source).as_posix())
        elif args.cmd == "toolchain":
            print(toolchain(args.dest, args.host or host_triple()).as_posix())
        elif args.cmd == "vendor":
            print(vendor(args.dest, args.source).as_posix())
        elif args.cmd == "source-name":
            print(source_name(load_lock()))
        else:
            return attest(args.dir)
    except FetchError as e:
        print(f"dsd2pcm: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
