#!/usr/bin/env python3
"""Run the command blocks of the Markdown against a pysacd wheel or pip requirement, on generated discs.

    python3 -I tools/docs_commands.py list
    python3 -I tools/docs_commands.py make-discs DIR       # needs numpy and a C compiler
    python3 -I tools/docs_commands.py run --spec pysacd==X.Y.Z --workdir DIR
    python3 -I tools/docs_commands.py run --wheel dist/pysacd-X.whl --workdir DIR

Which blocks run is decided in the Markdown, by the shared docs-run markers of the DSD Toolbox
docs tools (dsd-toolbox tools/docs/docs_run.py; docs/development.md, "Docs commands"): a
`<!-- docs-run: ... -->` line just before the fence runs the block, `<!-- docs-run: skip="reason" -->`
skips it with the reason. There is no list here: the markers are the one list, and the shared
docs-run workflow reads the same ones. A ```sh, ```bash, ```shell or ```console block (`$ `
lines) without a marker is an error. ```python blocks carry no marker and always run here.

This tool is the checkout's side of the same check. docs-run runs the marked blocks in a fresh
container against the release on PyPI; this runs them against any wheel or requirement (a build
of the checkout, a release candidate), each block in a fresh folder of WORKDIR that is also its
HOME (so `~/Music` and `~/bin` stay inside it), with the release under test first on PATH, and
it runs Python blocks too. Each folder holds the discs of tools/docs_fixtures.py under the names
the docs use (`album.iso`, `disc.iso`, `other.iso`).

Changes to what the docs say, and only these (each must still match a docs line):
- `pip install pysacd` installs the release under test (--spec or --wheel);
- a PS3 address (`192.0.2.10:2002`, `127.0.0.1:2002`) is a `pysacd ps3sim` of the release
  serving `album.iso` on a free local port;
- without --musicbrainz, each folder gets `~/.config/pysacd/config.toml` with
  `musicbrainz = false`, so `pysacd convert` makes no network lookups.

A block skipped for a need this tool can supply runs here (NEEDS): a skip reason that starts
with "needs a PS3" runs against the ps3sim above, "needs an ordinary user" when this does not run
as root (docs-run's containers run blocks as root, and `pysacd convert` refuses root), "needs
sacd_extract" with --sacd-extract, and
"needs podman" with --podman. Every block that runs must exit 0, and match its `expect=` patterns.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import venv
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import docs_fixtures

LANGS = {"sh": "sh", "bash": "sh", "shell": "sh", "console": "console", "python": "python"}
DISCS = docs_fixtures.DISCS
PS3_ADDRESSES = ("192.0.2.10:2002", "127.0.0.1:2002")

# Needs a run can supply: a block skipped with a reason that starts with the key runs when the
# need is given (PS3 always is: the run's own ps3sim; so is an ordinary user, unless this runs as root).
PS3, USER, SACD_EXTRACT, PODMAN = "ps3", "user", "sacd_extract", "podman"
NEEDS = {"needs a PS3": PS3, "needs an ordinary user": USER, "needs sacd_extract": SACD_EXTRACT,
         "needs podman": PODMAN}

# The marker syntax of the shared docs-run (dsd-toolbox tools/docs/docs_run.py): a marker applies
# to a fence at most two lines below it. Keys this tool reads: skip, expect; the others (image,
# packages, setup, group, drop, timeout) set up docs-run's container and mean nothing here.
DIRECTIVE_RE = re.compile(r"^\s*<!--\s*docs-run:(.*?)-->\s*$")
MARKER_KEYS = {"skip", "expect", "image", "packages", "setup", "group", "drop", "timeout"}
SKIP_DIRS = {".git", ".docs-tools", "node_modules", "target", ".venv", "venv"}   # as docs_run.py


def _strip_comment(line: str) -> str:
    """A shell line without its trailing `# comment` (a # after a blank, outside quotes)."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line.rstrip()


@dataclass
class Command:
    text: str           # as written, continuation lines joined with a space, comments removed
    script: str         # as run: the original lines, continuations and here-documents kept


class MarkerError(Exception):
    pass


@dataclass
class Block:
    file: str           # relative to the root, POSIX-style on every platform
    line: int           # the opening fence, 1-based
    lang: str
    commands: list[Command] = field(default_factory=list)
    code: str = ""      # python blocks: the code
    status: str = "run"     # run | skip | unmarked (python blocks: always run)
    reason: str = ""        # skip reason
    expects: list[str] = field(default_factory=list)


def doc_files(root: Path = ROOT) -> list[Path]:
    """Every Markdown file docs-run reads: README.md first, then the rest in path order."""
    files = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in SKIP_DIRS)
        files += [Path(dp) / f for f in sorted(fn) if f.endswith(".md")]
    files.sort(key=lambda p: (p.relative_to(root).parts != ("README.md",), p.relative_to(root).as_posix()))
    return files


def parse_marker(text: str, where: str) -> tuple[dict[str, str], list[str]]:
    try:
        parts = shlex.split(text.strip())
    except ValueError as e:
        raise MarkerError(f"{where}: cannot parse the docs-run marker: {e}") from e
    opts: dict[str, str] = {}
    expects: list[str] = []
    for part in parts:
        k, eq, v = part.partition("=")
        if not eq or k not in MARKER_KEYS:
            raise MarkerError(f"{where}: {part!r} is not a docs-run key=value")
        if k == "expect":
            expects.append(v)
        else:
            opts[k] = v
    if "skip" in opts and not opts["skip"].strip():
        raise MarkerError(f"{where}: skip= needs a reason")
    return opts, expects


def _commands(lines: list[str]) -> list[Command]:
    """Shell lines -> commands: comment-only and blank lines dropped, `\\` continuations
    joined, a here-document kept with the line that opens it."""
    out: list[Command] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip() or ln.lstrip().startswith("#"):
            i += 1
            continue
        raw = [ln]
        while raw[-1].rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            raw.append(lines[i])
        text = " ".join(_strip_comment(r.strip().removesuffix("\\").strip()) for r in raw)
        m = re.search(r"<<-?\s*'?(\w+)'?", text)
        if m:
            while i + 1 < len(lines):
                i += 1
                raw.append(lines[i])
                if lines[i].strip() == m.group(1):
                    break
        out.append(Command(text, "\n".join(r.strip() if n == 0 else r for n, r in enumerate(raw))))
        i += 1
    return out


def blocks(root: Path = ROOT) -> list[Block]:
    out: list[Block] = []
    for path in doc_files(root):
        rel = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        marker: tuple[dict[str, str], list[str], int] | None = None
        i = 0
        while i < len(lines):
            d = DIRECTIVE_RE.match(lines[i])
            if d:
                marker = (*parse_marker(d.group(1), f"{rel}:{i + 1}"), i)
                i += 1
                continue
            m = re.match(r"(\s*)```(\w*)\s*$", lines[i])
            if not m:
                i += 1
                continue
            indent, lang, start = len(m.group(1)), m.group(2), i + 1
            near = marker if marker and i - marker[2] <= 2 else None
            marker = None
            body = []
            i += 1
            while i < len(lines) and not re.match(r"\s*```\s*$", lines[i]):
                body.append(lines[i][indent:] if lines[i][:indent].strip() == "" else lines[i])
                i += 1
            i += 1
            if lang == "powershell":
                raise MarkerError(f"{rel}:{start}: a powershell block; add Windows handling to "
                                  f"{Path(__file__).name} first")
            kind = LANGS.get(lang)
            if not kind:
                continue
            b = Block(rel, start, kind)
            if kind == "python":
                b.code = "\n".join(body) + "\n"
            else:
                b.commands = _commands([ln[2:] for ln in body if ln.startswith("$ ")] if kind == "console"
                                       else body)
                if near is None:
                    b.status = "unmarked"
                elif "skip" in near[0]:
                    b.status, b.reason = "skip", near[0]["skip"]
                else:
                    b.expects = near[1]
            out.append(b)
    return out


def need_of(b: Block) -> str | None:
    """The need that lets a skipped block run here, from the start of its skip reason."""
    return next((n for prefix, n in NEEDS.items() if b.reason.startswith(prefix)), None)


def check_marks(all_blocks: list[Block]) -> list[str]:
    """Every command block carries a docs-run marker."""
    return [f"{b.file}:{b.line}: command block without a docs-run marker (run, or skip=\"reason\")"
            for b in all_blocks if b.status == "unmarked"]


def swap(script: str, spec: str, port: int) -> str:
    script = re.sub(r"(?m)^(\s*pip install )pysacd(?=\s|$)", lambda m: m.group(1) + shlex.quote(spec), script)
    for addr in PS3_ADDRESSES:
        script = script.replace(addr, f"127.0.0.1:{port}")
    return script


def check_swaps(all_blocks: list[Block]) -> list[str]:
    """Each swap must still apply to some docs line, or it silently stopped doing anything."""
    text = "\n".join(c.script for b in all_blocks for c in b.commands)
    bad = []
    if not re.search(r"(?m)^\s*pip install pysacd(\s|$)", text):
        bad.append("no `pip install pysacd` line is left to swap")
    for addr in PS3_ADDRESSES:
        if addr not in text:
            bad.append(f"no PS3 address {addr} is left to swap")
    return bad


def cmd_list(_: argparse.Namespace) -> int:
    try:
        all_blocks = blocks()
    except MarkerError as e:
        print(f"error: {e}")
        return 1
    runs = skips = 0
    for b in all_blocks:
        if b.status == "skip":
            skips += 1
            n = need_of(b)
            print(f"skip  {b.file}:{b.line} ({b.lang}): {b.reason}"
                  + (f" [runs here {'always' if n in (PS3, USER) else 'with --' + n.replace('_', '-')}]" if n else ""))
        elif b.status == "run":
            runs += 1
            print(f"run   {b.file}:{b.line} ({b.lang})")
    print(f"{len(all_blocks)} blocks: {runs} run, {skips} skipped with a reason")
    bad = check_marks(all_blocks) + check_swaps(all_blocks)
    for m in bad:
        print(f"error: {m}")
    return 1 if bad else 0


def cmd_make_discs(a: argparse.Namespace) -> int:
    for name, path in docs_fixtures.make(a.dir).items():
        print(f"{path}: {DISCS[name]}")
    return 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, proc: subprocess.Popen, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            sys.exit(f"ps3sim exited ({proc.returncode})")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    sys.exit("ps3sim did not start listening")


def cmd_run(a: argparse.Namespace) -> int:
    work = Path(a.workdir).resolve()
    discs = work / "discs"
    if not all((discs / n).is_file() for n in DISCS):
        cmd_make_discs(argparse.Namespace(dir=discs))
    if a.wheel:
        wheel = Path(a.wheel).resolve()
        if not wheel.is_file() or wheel.suffix != ".whl":
            sys.exit(f"not a wheel: {wheel}")
        spec = str(wheel)
    else:
        spec = a.spec
    env_dir = work / "release-env"
    if not env_dir.exists():
        venv.EnvBuilder(with_pip=True).create(env_dir)
        subprocess.run([str(env_dir / "bin" / "pip"), "install", "-q", spec], check=True)
    bin_dir = env_dir / "bin"
    tools = work / "tools-bin"
    tools.mkdir(exist_ok=True)
    have = {PS3} | ({USER} if not hasattr(os, "geteuid") or os.geteuid() != 0 else set())
    if a.podman:
        have.add(PODMAN)
    if a.sacd_extract:
        target = tools / "sacd_extract"
        target.unlink(missing_ok=True)
        target.symlink_to(Path(a.sacd_extract).resolve())
        have.add(SACD_EXTRACT)
    path = os.pathsep.join([str(bin_dir), str(tools), os.environ.get("PATH", "/usr/bin:/bin")])
    version = subprocess.run([str(bin_dir / "pysacd"), "--version"], check=False, capture_output=True,
                             text=True).stdout.strip()
    print(f"release under test: {version}")
    release = version.split()[1] if version.startswith("pysacd ") and len(version.split()) > 1 else version

    try:
        all_blocks = blocks()
    except MarkerError as e:
        print(f"::error::{e}")
        return 1
    bad = check_marks(all_blocks) + check_swaps(all_blocks)
    if bad:
        for m in bad:
            print(f"::error::{m}")
        return 1
    port = _free_port()
    sim = subprocess.Popen([str(bin_dir / "pysacd"), "ps3sim", str(discs / "album.iso"), "--port", str(port),
                            "--idle-timeout", "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failed: list[str] = []
    skipped: list[str] = []
    ran = 0
    try:
        _wait_port(port, sim)
        for n, b in enumerate(all_blocks, 1):
            need = need_of(b) if b.status == "skip" else None
            if b.status == "skip" and need not in have:
                skipped.append(f"{b.file}:{b.line}: {b.reason}")
                continue
            folder = work / "blocks" / f"{n:02d}-{b.file.replace('/', '-').removesuffix('.md')}-{b.line}"
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True)
            for name in DISCS:
                shutil.copyfile(discs / name, folder / name)
            if not a.musicbrainz:
                cfg = folder / ".config" / "pysacd"
                cfg.mkdir(parents=True)
                (cfg / "config.toml").write_text("musicbrainz = false\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME",
                                                                    "XDG_CONFIG_HOME", "PYSACD_LOG")}
            env.update(HOME=str(folder), PATH=path)
            # podman keeps its image store under XDG_DATA_HOME, else HOME: keep it in the real one, so
            # no store (with files of other user ids) is made in the block's folder
            env.setdefault("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
            if b.lang == "python":
                (folder / "block.py").write_text(b.code, encoding="utf-8")
                argv, label = [str(bin_dir / "python"), "-I", "block.py"], "the Python block"
            else:
                script = ["set -eo pipefail"] + [swap(c.script, spec, port) for c in b.commands]
                argv, label = ["bash", "--noprofile", "--norc", "-c", "\n".join(script)], \
                    "; ".join(c.text for c in b.commands)
            print(f"\n== {b.file}:{b.line}: {label}", flush=True)
            try:
                p = subprocess.run(argv, check=False, cwd=folder, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=a.timeout)
                out, rc = p.stdout, str(p.returncode)
            except subprocess.TimeoutExpired as e:
                got = e.stdout or b""
                out = got.decode(errors="replace") if isinstance(got, bytes) else got
                rc = f"timeout after {a.timeout} s"
            tail = out.splitlines()[-a.lines:]
            print("\n".join(tail))
            ran += 1
            problems = [] if rc == "0" else [f"exit {rc}"]
            problems += [f"output does not match expect={rx!r}" for rx in b.expects
                         if not re.search(rx.replace("{version}", re.escape(release)), out, re.MULTILINE)]
            if problems:
                failed.append(f"{b.file}:{b.line} ({'; '.join(problems)})")
                print(f"::error::{b.file}:{b.line} failed ({'; '.join(problems)})")
    finally:
        sim.send_signal(signal.SIGINT)
        try:
            sim.wait(10)
        except subprocess.TimeoutExpired:
            sim.kill()
    print(f"\n{ran} blocks ran, {len(failed)} failed; {len(skipped)} blocks not run:")
    for line in skipped:
        print(f"- {line}")
    for f in failed:
        print(f"::error::failed: {f}")
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show every block, what runs and what is skipped").set_defaults(fn=cmd_list)
    d = sub.add_parser("make-discs", help="write album.iso, disc.iso and other.iso into DIR (tools/docs_fixtures.py)")
    d.add_argument("dir", type=Path)
    d.set_defaults(fn=cmd_make_discs)
    r = sub.add_parser("run", help="run the blocks in WORKDIR against a release")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--wheel", help="install this wheel as the release under test")
    src.add_argument("--spec", help="install this pip requirement (e.g. pysacd==0.5.5) as the release under test")
    r.add_argument("--workdir", required=True)
    r.add_argument("--musicbrainz", action="store_true", help="let pysacd convert look discs up on MusicBrainz")
    r.add_argument("--sacd-extract", help="a sacd_extract binary, for the comparison commands")
    r.add_argument("--podman", action="store_true", help="run the container example (needs podman and the network)")
    r.add_argument("--timeout", type=int, default=600, help="seconds per block (default 600)")
    r.add_argument("--lines", type=int, default=15, help="output lines shown per block (default 15)")
    r.set_defaults(fn=cmd_run)
    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
