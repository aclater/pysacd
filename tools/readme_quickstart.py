#!/usr/bin/env python3
"""Run the README's "Quick start" as written, against a wheel built from this checkout.

    python3 -I tools/readme_quickstart.py print
    python3 -I tools/readme_quickstart.py make-disc DIR/album.iso      # needs numpy and make test-tools
    python3 -I tools/readme_quickstart.py run --wheel dist/pysacd-X.whl --workdir DIR --tracks 3

The commands come from the ```sh blocks under "## Quick start" in README.md, so a change to
the README is what CI runs: nothing here repeats them. They run in order, in one bash session
in DIR (`set -eo pipefail`), so the venv step 1 activates stays active for the later steps.

One line is swapped, and only that one: `pip install pysacd` installs from PyPI, so it becomes
`pip install <the built wheel>`. If the README no longer has exactly that line, the run fails,
so the swap can't silently stop applying. The wheel's dependency (mutagen) still comes from PyPI,
as for a user.

Checks: every `literal` in a step's "*You should see*" sentence must appear in that step's
output; one that names a file extension (`.dsf`) must instead be the extension of files in one
new folder of DIR, --tracks of them when given. `make-disc` writes the disc the README calls
`album.iso`: a DST stereo area of three tracks from the repository's test generators
(tests/sacdgen.py and the test DST encoder).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SECTION = "Quick start"
# README line -> replacement; each must match exactly one command line
SWAPS = {"pip install pysacd": "pip install {wheel}"}
MARK = "@@readme-quickstart-step"


class Step:
    def __init__(self, lines: list[str], expect: list[str]):
        self.lines, self.expect = lines, expect


def section(text: str, title: str = SECTION) -> list[str]:
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.fullmatch(rf"##\s+{re.escape(title)}\s*", ln)), None)
    if start is None:
        sys.exit(f"README.md has no '## {title}' section")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start + 1:end]


def steps(text: str) -> list[Step]:
    """The shell code blocks of the section, each with the literals its "You should see" names."""
    out: list[Step] = []
    lines = section(text)
    i = 0
    prose: list[str] = []
    while i < len(lines):
        m = re.match(r"(\s*)```(\w*)\s*$", lines[i])
        if not m:
            prose.append(lines[i])
            i += 1
            continue
        if out:
            out[-1].expect = should_see(prose)
        prose = []
        indent, lang = len(m.group(1)), m.group(2)
        body = []
        i += 1
        while i < len(lines) and not re.match(r"\s*```\s*$", lines[i]):
            body.append(lines[i][indent:] if lines[i][:indent].strip() == "" else lines[i])
            i += 1
        i += 1
        if lang not in ("sh", "bash", "shell", "console"):
            sys.exit(f"Quick start: a ```{lang} block; this tool runs only sh blocks")
        out.append(Step(body, []))
    if out:
        out[-1].expect = should_see(prose)
    if not out:
        sys.exit("README.md's Quick start has no ```sh blocks")
    return out


def should_see(prose: list[str]) -> list[str]:
    """The `literals` of the "*You should see*" sentence in this prose, if any. Only that sentence:
    the ones after it may describe other discs ("On a disc that also has ...")."""
    text = "\n".join(prose)
    m = re.search(r"\*You should see\*(.*?\.)(?=\s|$)", text, re.DOTALL)
    return re.findall(r"`([^`]+)`", m.group(1)) if m else []


def swapped(all_steps: list[Step], wheel: str) -> list[Step]:
    used = dict.fromkeys(SWAPS, 0)
    for st in all_steps:
        for n, ln in enumerate(st.lines):
            if ln.strip() in SWAPS:
                used[ln.strip()] += 1
                st.lines[n] = ln.replace(ln.strip(), SWAPS[ln.strip()].format(wheel=wheel))
    wrong = [k for k, v in used.items() if v != 1]
    if wrong:
        sys.exit(f"README.md's Quick start must have each of these lines exactly once: {wrong}; "
                 f"update SWAPS in {Path(__file__).name} to match the README")
    return all_steps


def cmd_print(_: argparse.Namespace) -> int:
    for n, st in enumerate(steps(README.read_text(encoding="utf-8")), 1):
        print(f"# step {n}; expects {st.expect}")
        print("\n".join(st.lines))
    return 0


def cmd_make_disc(a: argparse.Namespace) -> int:
    sys.path.insert(0, str(ROOT / "tests"))
    import sacdgen
    import test_dst_full as dst
    frames = [f for _, _, f in dst.encoded(2)]
    n = len(frames)
    tracks = [n // 3, n // 3, n - 2 * (n // 3)]
    sacdgen.build_iso(a.iso, [sacdgen.AreaSpec(2, frames, tracks, titles=["First", "Second", "Third"])],
                      info=sacdgen.DiscInfo(title="Quick Start Album", artist="pysacd"))
    print(f"{a.iso}: DST stereo, {len(tracks)} tracks")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    wheel = Path(a.wheel).resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        sys.exit(f"not a wheel: {wheel}")
    work = Path(a.workdir).resolve()
    all_steps = swapped(steps(README.read_text(encoding="utf-8")), str(wheel))
    before = {p for p in work.iterdir() if p.is_dir()}
    script = ["set -eo pipefail"]
    for n, st in enumerate(all_steps, 1):
        script += [f"echo '{MARK} {n}'", *st.lines]
    print("\n".join(script), "\n----", flush=True)
    proc = subprocess.Popen(["bash", "--noprofile", "--norc", "-c", "\n".join(script)], cwd=work,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    out = []
    for ln in proc.stdout:
        sys.stdout.write(ln)
        out.append(ln)
    if proc.wait() != 0:
        print(f"::error::the Quick start failed (exit {proc.returncode})")
        return 1
    parts = re.split(rf"^{re.escape(MARK)} (\d+)\n", "".join(out), flags=re.MULTILINE)
    output = {int(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    bad = []
    for n, st in enumerate(all_steps, 1):
        for lit in st.expect:
            if re.fullmatch(r"\.\w+", lit):
                new = [d for d in work.iterdir() if d.is_dir() and d not in before]
                found = [sorted(d.rglob(f"*{lit}")) for d in new]
                found = [f for f in found if f]
                if len(found) != 1 or (a.tracks and len(found[0]) != a.tracks):
                    bad.append(f"step {n}: expected one new folder of {a.tracks or 'some'} {lit} files, "
                               f"found {[[p.name for p in f] for f in found]}")
            elif lit not in output.get(n, ""):
                bad.append(f"step {n}: the README says you should see {lit!r}; it isn't in the output")
    for b in bad:
        print(f"::error::{b}")
    if not bad:
        print(f"README Quick start: {len(all_steps)} steps ran as written; "
              f"checked {sum(len(s.expect) for s in all_steps)} 'You should see' items")
    return 1 if bad else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("print", help="show the extracted steps").set_defaults(fn=cmd_print)
    d = sub.add_parser("make-disc", help="write a DST stereo test disc")
    d.add_argument("iso", type=Path)
    d.set_defaults(fn=cmd_make_disc)
    r = sub.add_parser("run", help="run the steps in WORKDIR with the wheel")
    r.add_argument("--wheel", required=True)
    r.add_argument("--workdir", required=True)
    r.add_argument("--tracks", type=int, default=0, help="tracks on the disc (files the DSF folder must hold)")
    r.set_defaults(fn=cmd_run)
    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
