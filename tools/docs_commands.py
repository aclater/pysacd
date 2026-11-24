#!/usr/bin/env python3
"""Run every command block in README.md and docs/ against an installed pysacd release.

    python3 -I tools/docs_commands.py list
    python3 -I tools/docs_commands.py make-discs DIR       # needs numpy and make test-tools
    python3 -I tools/docs_commands.py run --spec pysacd==X.Y.Z --workdir DIR
    python3 -I tools/docs_commands.py run --wheel dist/pysacd-X.whl --workdir DIR

The commands come from the ```sh, ```bash, ```console (`$ ` lines) and ```python blocks of
README.md and docs/, so a docs change is what runs. Each block runs on its own, as written, in
one bash session (`set -eo pipefail`) or one Python process, in a fresh folder of WORKDIR that
is also its HOME (so `~/Music` and `~/bin` stay inside it), with the release under test first
on PATH. The folder holds generated discs under the names the docs use: `album.iso` (plain
DSD stereo + DST 5 channels), `disc.iso` (DST stereo + DST 6 channels) and `other.iso` (plain
DSD stereo), from tests/sacdgen.py as tests/test_extract_tree.py builds them.

Changes to what the docs say, and only these (SWAPS; each must still match a docs line):
- `pip install pysacd` installs the release under test (--spec or --wheel);
- a PS3 address (`192.0.2.10:2002`, `127.0.0.1:2002`) is a `pysacd ps3sim` of the release
  serving `album.iso` on a free local port;
- without --musicbrainz, each folder gets `~/.config/pysacd/config.toml` with
  `musicbrainz = false`, so `pysacd convert` makes no network lookups.

Commands that can't run here are in SKIP with the reason; some run when their need is given
(--musicbrainz, --sacd-extract). A SKIP entry that matches no docs command is an error, so the
list follows the docs. Every block that runs must exit 0.
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
LANGS = {"sh": "sh", "bash": "sh", "shell": "sh", "console": "console", "python": "python"}
DISCS = {"album.iso": "mixed", "disc.iso": "dst", "other.iso": "plain"}
PS3_ADDRESSES = ("192.0.2.10:2002", "127.0.0.1:2002")

# Needs that a run can supply; a SKIP entry with one of them runs when it is given.
MUSICBRAINZ, SACD_EXTRACT, PODMAN = "musicbrainz", "sacd_extract", "podman"

DEV = "a development command, run from a source checkout by CI and release test plan steps 1 to 4"
SOURCE = ("builds from a source checkout, not the release under test; CI's tests and portability jobs build "
          "the same way")
WINDOWS = "Windows with MSYS2; CI's portability workflow builds it"
SERVER = "a server that runs until Ctrl-C; the run's own ps3sim stands in for it for the PS3 commands"

# (file, command as written, continuation lines joined) -> (reason, need or None)
SKIP: dict[tuple[str, str], tuple[str, str | None]] = {
    ("docs/converting.md", "make"): (SOURCE, None),
    ("docs/converting.md", "pysacd mb-login --client-id CLIENT_ID"):
        ("signs in to the user's own MusicBrainz account in a browser", None),
    ("docs/converting.md", "pysacd submit-isrcs -n album.iso"):
        ("looks the disc up on MusicBrainz (network)", MUSICBRAINZ),
    ("docs/converting.md", "pysacd submit-isrcs album.iso"):
        ("submits to MusicBrainz after asking: needs an account, and would edit MusicBrainz", None),
    ("docs/converting.md", "pysacd convert --submit-isrcs album.iso"):
        ("offers a MusicBrainz submission after the match: needs an account, and would edit MusicBrainz", None),
    ("docs/development.md", "make"): (DEV, None),
    ("docs/development.md", "python3 -I scripts/bench_workload.py generate build/bench-discs"): (DEV, None),
    ("docs/development.md", "make PORTABLE=1"): (DEV, None),
    ("docs/development.md", "make test-tools"): (DEV, None),
    ("docs/development.md", "make HARDEN=0"): (DEV, None),
    ("docs/development.md", "make && PYTHONPATH=src python3 -m unittest discover -s tests"): (DEV, None),
    ("docs/development.md", "PYSACD_TEST_ISO=/path/to/disc.iso PYTHONPATH=src python3 -m unittest discover -s tests"):
        (DEV, None),
    ("docs/development.md", "make fuzz test-tools"): (DEV, None),
    ("docs/development.md", "PYTHONPATH=src:tests python3 fuzz/make_seeds.py build/fuzz-seeds"): (DEV, None),
    ("docs/development.md", "mkdir -p build/fuzz-corpus/engine build/fuzz-tmp"): (DEV, None),
    ("docs/development.md", ("TMPDIR=build/fuzz-tmp build/fuzz_engine -max_len=4194304 -jobs=2 -workers=2 "
     "-rss_limit_mb=1500 -max_total_time=600 build/fuzz-corpus/engine build/fuzz-seeds/engine")): (DEV, None),
    ("docs/development.md", ("BENCH_DISCS=$PWD/build/bench-discs python3 -I scripts/bench_compare.py --base OLD_TREE "
     "--head NEW_TREE --measure \"python3 -I scripts/bench_workload.py\" dst-stereo dst-multichannel plain-dsd")):
        (DEV, None),
    ("docs/development.md", "pysacd ps3sim album.iso --port 2002 --rate 6 --drop-after 500"): (SERVER, None),
    ("docs/development.md", "python3 -I tools/docs_commands.py list"): ("this tool itself", None),
    ("docs/development.md", "python3 -I tools/docs_commands.py run --spec pysacd==VERSION --workdir build/docs-commands"):
        ("this tool itself", None),
    ("docs/development.md", ("python3 tools/extract_compare.py --old PREVIOUS_RELEASE/src --new src --sacd-extract BIN "
     "--no-values --require-sacd-extract IMAGE...")):
        ("a release step on real discs (release test plan step 2)", None),
    ("docs/install/README.md", "gh attestation verify pysacd-0.5.5-py3-none-manylinux_2_28_x86_64.whl --repo aclater/pysacd"):
        ("needs the GitHub CLI signed in and that wheel file; the release steps verify every attestation", None),
    ("docs/install/README.md", "git clone https://github.com/aclater/pysacd && cd pysacd"): (SOURCE, None),
    ("docs/install/README.md", "pip install -e '.[convert]'"): (SOURCE, None),
    ("docs/install/README.md", "make"): (SOURCE, None),
    ("docs/install/README.md", "python tools/scarletbook.py fetch --target x86_64-pc-windows-gnu --dest build/scarletbook"):
        (WINDOWS, None),
    ("docs/install/README.md", ("pacman -S make mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-pkgconf "
     "mingw-w64-ucrt-x86_64-flac")): (WINDOWS, None),
    ("docs/install/README.md", "pip install -e '.[convert]' --no-build-isolation"): (WINDOWS, None),
    ("docs/ps3-protocol.md", "pysacd ps3sim disc.iso"): (SERVER, None),
    ("docs/ps3-protocol.md", "sacd_extract -I -i 127.0.0.1:2002"):
        ("needs sacd_extract; with it, it reads from the run's own ps3sim", SACD_EXTRACT),
    ("docs/security.md", "podman build -t pysacd - <<'END'"):
        ("needs podman and the network; installs pysacd from PyPI into the container", PODMAN),
    ("docs/security.md", "mkdir out"): ("goes with the podman commands", PODMAN),
    ("docs/security.md", ("podman run --rm --network none --userns=keep-id -v ./album.iso:/in.iso:ro,Z -v ./out:/out:Z "
     "-w /out pysacd pysacd-extract -2 -s -i /in.iso")): ("needs podman and the image built above", PODMAN),
    ("docs/sacd_extract.md", "(cd a && sacd_extract   -2 -m -s -i ../album.iso)"): ("needs sacd_extract", SACD_EXTRACT),
    ("docs/sacd_extract.md", "diff -r a b && echo identical"): ("needs sacd_extract", SACD_EXTRACT),
}


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


@dataclass
class Block:
    file: str           # relative to the root, POSIX-style on every platform (the SKIP keys)
    line: int
    lang: str
    commands: list[Command] = field(default_factory=list)
    code: str = ""      # python blocks: the code


def doc_files(root: Path = ROOT) -> list[Path]:
    return [root / "README.md", *sorted((root / "docs").rglob("*.md"))]


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
        lines = path.read_text(encoding="utf-8").splitlines()
        i = 0
        while i < len(lines):
            m = re.match(r"(\s*)```(\w*)\s*$", lines[i])
            if not m:
                i += 1
                continue
            indent, lang, start = len(m.group(1)), m.group(2), i + 1
            body = []
            i += 1
            while i < len(lines) and not re.match(r"\s*```\s*$", lines[i]):
                body.append(lines[i][indent:] if lines[i][:indent].strip() == "" else lines[i])
                i += 1
            i += 1
            if lang == "powershell":
                sys.exit(f"{path.relative_to(root).as_posix()}:{start}: a powershell block; add Windows handling to "
                         f"{Path(__file__).name} first")
            kind = LANGS.get(lang)
            if not kind:
                continue
            b = Block(path.relative_to(root).as_posix(), start, kind)
            if kind == "python":
                b.code = "\n".join(body) + "\n"
            elif kind == "console":
                b.commands = _commands([ln[2:] for ln in body if ln.startswith("$ ")])
            else:
                b.commands = _commands(body)
            out.append(b)
    return out


def skip_of(b: Block, c: Command) -> tuple[str, str | None] | None:
    return SKIP.get((b.file, c.text))


def check_skips(all_blocks: list[Block]) -> list[str]:
    """SKIP entries that match no command: the docs changed, so the list must follow."""
    have = {(b.file, c.text) for b in all_blocks for c in b.commands}
    return [f"{f}: {t!r}" for f, t in SKIP if (f, t) not in have]


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
    all_blocks = blocks()
    runs = skips = 0
    for b in all_blocks:
        print(f"## {b.file}:{b.line} ({b.lang})")
        if b.lang == "python":
            print("  run: the Python block")
            runs += 1
        for c in b.commands:
            s = skip_of(b, c)
            if s:
                skips += 1
                print(f"  skip: {c.text}\n        {s[0]}" + (f" (runs with --{s[1].replace('_', '-')})" if s[1] else ""))
            else:
                runs += 1
                print(f"  run: {c.text}")
    print(f"{len(all_blocks)} blocks: {runs} commands or Python blocks run, {skips} skipped")
    bad = check_skips(all_blocks) + check_swaps(all_blocks)
    for m in bad:
        print(f"error: SKIP entry matches no docs command: {m}" if ":" in m else f"error: {m}")
    return 1 if bad else 0


def cmd_make_discs(a: argparse.Namespace) -> int:
    sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "tools")]
    import test_extract_tree
    a.dir.mkdir(parents=True, exist_ok=True)
    made = test_extract_tree.discs(a.dir)
    for name, kind in DISCS.items():
        os.replace(made[kind], a.dir / name)
        print(f"{a.dir / name}: the {kind!r} disc of tests/test_extract_tree.py")
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
    have = {MUSICBRAINZ} if a.musicbrainz else set()
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

    all_blocks = blocks()
    bad = check_skips(all_blocks) + check_swaps(all_blocks)
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
                todo = []
                for c in b.commands:
                    s = skip_of(b, c)
                    if s and (s[1] is None or s[1] not in have):
                        skipped.append(f"{b.file}:{b.line}: {c.text} ({s[0]})")
                    else:
                        todo.append(c)
                if not todo:
                    continue
                script = ["set -eo pipefail"] + [swap(c.script, spec, port) for c in todo]
                argv, label = ["bash", "--noprofile", "--norc", "-c", "\n".join(script)], \
                    "; ".join(c.text for c in todo)
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
            if rc != "0":
                failed.append(f"{b.file}:{b.line} ({rc})")
                print(f"::error::{b.file}:{b.line} failed ({rc})")
    finally:
        sim.send_signal(signal.SIGINT)
        try:
            sim.wait(10)
        except subprocess.TimeoutExpired:
            sim.kill()
    print(f"\n{ran} blocks ran, {len(failed)} failed; {len(skipped)} commands not run:")
    for line in skipped:
        print(f"- {line}")
    for f in failed:
        print(f"::error::failed: {f}")
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show every block, what runs and what is skipped").set_defaults(fn=cmd_list)
    d = sub.add_parser("make-discs", help="write album.iso, disc.iso and other.iso into DIR")
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
