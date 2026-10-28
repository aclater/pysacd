#!/usr/bin/env python3
"""Smoke test for an installed wheel, run by cibuildwheel in a clean environment (#9).

    python tests/wheel_smoke.py

Builds a small synthetic plain-DSD stereo disc (no real disc data), then runs the
installed console scripts on it: `pysacd toc`, `pysacd-extract -s` (DSF) and
`pysacd convert -f flac`. It checks that the engine loads, has FLAC, and writes
non-empty files. Uses only the installed package plus tests/sacdgen.py.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sacdgen

FRAME = 4704 * 2        # one stereo DSD64 frame


def run(*cmd: str, cwd: Path) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> int:
    from pysacd import _native
    print("engine:", _native.build_info())
    if not _native.has_flac():
        print("the wheel's engine has no FLAC support", file=sys.stderr)
        return 1
    rng = random.Random(9)
    frames = [bytes(rng.getrandbits(8) for _ in range(FRAME)) for _ in range(150)]
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        iso = d / "smoke.iso"
        sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, frames, [75, 75], dst=False, titles=["One", "Two"])],
                          info=sacdgen.DiscInfo(title="Smoke", artist="pysacd"))
        pysacd, extract = shutil.which("pysacd"), shutil.which("pysacd-extract")
        if not pysacd or not extract:
            print("console scripts not installed", file=sys.stderr)
            return 1
        run(pysacd, "toc", str(iso), cwd=d)
        (d / "x").mkdir()
        run(extract, "--no-cfg", "-2", "-s", "-i", str(iso), cwd=d / "x")
        # cibuildwheel runs the Linux tests as root in a container, where convert refuses to run (#30)
        root = ["--allow-root"] if getattr(os, "geteuid", lambda: 1)() == 0 else []
        run(pysacd, "convert", *root, "--no-musicbrainz", "--config", os.devnull, "-f", "flac", "-o", str(d / "c"),
            str(iso), cwd=d)
        dsf = sorted((d / "x").rglob("*.dsf"))
        flac = sorted((d / "c").rglob("*.flac"))
        print("dsf:", [p.name for p in dsf], "flac:", [p.name for p in flac])
        if len(dsf) != 2 or len(flac) != 2 or not all(p.stat().st_size > 1000 for p in dsf + flac):
            print("unexpected output", file=sys.stderr)
            return 1
    print("wheel smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
