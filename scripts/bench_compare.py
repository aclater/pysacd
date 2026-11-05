#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compare two builds' speed, for the informational CI benchmark. A coarse alarm, not a gate:
shared runners are noisy, and the release timing table stays the real check.

The same file is in aclater/pysacd and aclater/sacdfs; only scripts/bench_workload.py differs.
Each workload is measured ROUNDS times with each build, alternating the builds and swapping which
goes first every round. The result is each build's median and their ratio (head / base). A ratio
over THRESHOLD is flagged as a warning, and the exit status stays 0: only a measurement that
fails gives a non-zero one.

MEASURE is a command. It is run as `MEASURE BUILD WORKLOAD`, and its last line of output is the
measurement in seconds. The report goes to standard output, and to the job summary in CI.
"""

import argparse
import os
import shlex
import statistics
import subprocess
import sys
import time


def measure(command: list[str], build: str, workload: str) -> float:
    r = subprocess.run([*command, build, workload], capture_output=True, text=True, check=False)
    if r.returncode:
        sys.exit(f"{workload} with {build} failed (status {r.returncode}):\n{r.stdout}{r.stderr}")
    return float(r.stdout.split()[-1])


def order(round_number: int) -> tuple[str, str]:
    return ("base", "head") if round_number % 2 == 0 else ("head", "base")


def is_flagged(base: list[float], head: list[float], threshold: float) -> bool:
    return statistics.median(head) > threshold * statistics.median(base)


def report(results: dict[str, dict[str, list[float]]], threshold: float, elapsed: float) -> tuple[str, list[str]]:
    percent = round((threshold - 1) * 100)
    lines = [
        "## Benchmark: base against head (informational)",
        "",
        "| Workload | Base median (range) | Head median (range) | Head / base | |",
        "|---|---|---|---|---|",
    ]
    flagged = []
    for workload, times in results.items():
        base, head = times["base"], times["head"]
        cells = [f"{statistics.median(t):.3f} s ({min(t):.3f} to {max(t):.3f})" for t in (base, head)]
        flag = is_flagged(base, head, threshold)
        if flag:
            flagged.append(workload)
        ratio = statistics.median(head) / statistics.median(base)
        lines.append(f"| {workload} | {cells[0]} | {cells[1]} | {ratio:.3f} | {'**SLOWER**' if flag else 'ok'} |")
    rounds = len(next(iter(results.values()))["base"])
    lines += [
        "",
        f"Medians of {rounds}, builds alternated, measured in {elapsed:.0f} s. **SLOWER** means head is more than "
        f"{percent}% slower than base. Shared runners are noisy: if the ranges overlap, or the change can't "
        "affect this path, suspect the runner and re-run the job before suspecting the change. Not a required check.",
    ]
    return "\n".join(lines) + "\n", flagged


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="what MEASURE is given as BUILD for the base build")
    ap.add_argument("--head", required=True, help="what MEASURE is given as BUILD for the head build")
    ap.add_argument("--measure", required=True, help="the measuring command")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--threshold", type=float, default=1.10)
    ap.add_argument("workloads", nargs="+")
    args = ap.parse_args()

    builds = {"base": args.base, "head": args.head}
    command = shlex.split(args.measure)
    results: dict[str, dict[str, list[float]]] = {w: {"base": [], "head": []} for w in args.workloads}
    start = time.monotonic()
    for round_number in range(args.rounds):
        for workload in args.workloads:
            for build in order(round_number):
                results[workload][build].append(measure(command, builds[build], workload))
    text, flagged = report(results, args.threshold, time.monotonic() - start)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
        for workload in flagged:
            print(f"::warning title=Benchmark::{workload}: head is over {round((args.threshold - 1) * 100)}% slower "
                  "than base (informational; see the job summary)")


if __name__ == "__main__":
    main()
