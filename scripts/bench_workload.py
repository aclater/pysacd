#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""pysacd's half of the informational CI benchmark (#123), for scripts/bench_compare.py.

  bench_workload.py generate DIR         write the synthetic discs into DIR (tests/sacdgen.py)
  bench_workload.py TREE WORKLOAD        run TREE's pysacd-extract once, print the seconds

The discs repeat a few frames from the test encoder, which makes long discs quick to generate:
each frame still goes through the decoder and the writer. TREE is a built source tree, and
BENCH_DISCS is the directory the discs were written to. Python's start-up is timed too, as a
user would see it; it is small next to the disc.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Workload: channels, DST, distinct frames, frames on the disc, what to extract.
WORKLOADS = {
    "dst-stereo": (2, True, 60, 15000, "-2"),
    "dst-multichannel": (6, True, 60, 3000, "-m"),
    "plain-dsd": (2, False, 60, 60000, "-2"),
}


def generate(directory: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / "tests")]
    import dstenc
    import sacdgen

    directory.mkdir(parents=True, exist_ok=True)
    for workload, (channels, dst, distinct, frames, _) in WORKLOADS.items():
        size = channels * dstenc.FL
        dsd = dstenc.synth_dsd(channels, distinct)
        chunks = [dsd[i * size:(i + 1) * size] for i in range(distinct)]
        coded = [dstenc.encode_frame(c, channels) for c in chunks] if dst else chunks
        area = sacdgen.AreaSpec(channels, (coded * (frames // distinct + 1))[:frames],
                                [frames // 2, frames - frames // 2], dst=dst)
        sacdgen.build_iso(directory / f"{workload}.iso", [area], title=workload)


def measure(tree: Path, workload: str) -> float:
    with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP")) as out:
        command = [sys.executable, "-m", "pysacd.extract", "--no-cfg", WORKLOADS[workload][4], "-s", "-i",
                   str(Path(os.environ["BENCH_DISCS"]) / f"{workload}.iso"), "-y", out]
        start = time.perf_counter()
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=out,  # nosemgrep: dangerous-subprocess-use-tainted-env-args  # a list, no shell; the developer's own paths
                       env=dict(os.environ, PYTHONPATH=str(tree.resolve() / "src")))
        return time.perf_counter() - start


def main() -> None:
    if sys.argv[1] == "generate":
        generate(Path(sys.argv[2]))
    else:
        print(measure(Path(sys.argv[1]), sys.argv[2]))


if __name__ == "__main__":
    main()
