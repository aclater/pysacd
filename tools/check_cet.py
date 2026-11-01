#!/usr/bin/env python3
"""Check that the x86-64 Linux engine keeps the Intel CET shadow-stack (SHSTK) mark (#94).

    python3 tools/check_cet.py FILE...

Each FILE is an engine (`_sacdconv*.so`) or a wheel holding one. The check is `readelf -n`
(binutils): the engine's GNU property note must list `x86 feature: ... SHSTK`. A program that
runs with shadow stacks (glibc's `glibc.cpu.x86_shstk` tunable, a kernel with user shadow
stacks) loads only libraries that carry that mark, and the linker gives it to the engine only
when every object linked into it has it. Since scarletbook-rs 0.5.2 its C library's objects
carry it (scarletbook-rs#125), so the engine is marked again; this keeps it so. Indirect branch
tracking (IBT) isn't checked: Rust's standard library has no ENDBR64, so the engine can't be
IBT-marked.

Files for another platform or architecture fail: pass only x86-64 glibc ones (manylinux). A
musllinux engine can't be marked: Alpine's startup objects (`crti.o`, `crtbeginS.o`), which every
shared library links, carry no CET property, and musl never enables shadow stacks, so the mark
would change nothing there. Standard library plus readelf only.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def engines(path: Path, work: Path) -> list[Path]:
    if path.suffix != ".whl":
        return [path]
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if Path(n).name.startswith("_sacdconv") and n.endswith(".so")]
        out = []
        for i, n in enumerate(names):
            dest = work / f"{i}-{Path(n).name}"
            dest.write_bytes(z.read(n))
            out.append(dest)
    return out


def check(so: Path) -> str | None:
    """None when so is SHSTK-marked, else why not."""
    r = subprocess.run(["readelf", "-n", "-W", str(so)], capture_output=True, text=True, check=False)
    if r.returncode:
        return f"readelf failed: {r.stderr.strip()}"
    features = [line.split("x86 feature:", 1)[1] for line in r.stdout.splitlines() if "x86 feature:" in line]
    if not features:
        return "no x86 feature property note"
    if not any("SHSTK" in f.replace(",", " ").split() for f in features):
        return f"x86 feature:{features[0]}, no SHSTK"
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    if not shutil.which("readelf"):
        print("check_cet: needs readelf (binutils)", file=sys.stderr)
        return 2
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        for arg in argv:
            found = engines(Path(arg), Path(tmp))
            if not found:
                print(f"{arg}: no engine (_sacdconv*.so) in it")
                bad += 1
            for so in found:
                why = check(so)
                print(f"{arg}: {'SHSTK-marked' if why is None else why}")
                bad += why is not None
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
