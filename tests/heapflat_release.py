"""Release-time memory runs: the soak and the bytes-at-exit check (docs/release-test-plan.md, step 4).

    python3 tests/heapflat_release.py soak [--conversions N] [--every K] [--scenario NAME ...] [--plant BYTES/EVERY]
    python3 tests/heapflat_release.py exit [--plant BYTES/EVERY]

Needs a built engine (make), a C compiler, glibc on Linux, and an interpreter with mutagen (the running
one, or set HEAPFLAT_PYTHON). It builds tests/native/countmalloc.c, makes the synthetic discs, and runs
tests/heapflat_child.py under the counting shim, the same referee as tests/test_heap_flat.py.

soak: per scenario one process converts the same disc N times (default 1,102,696; about one hour each on
the calibration host) and samples the exact live bytes every K conversions (default 1000). PASS when the last
sample is within SOAK_MARGIN of the first. A run with fewer than 1,000,000 conversions is printed as an
OBSERVATION: the margin's detection floor only holds at that length.
exit: separate processes convert 10 and 130 times, three runs each, and report the bytes still allocated at
exit. PASS when every run is within EXIT_MARGIN of the median of the three 10-conversion runs.
--plant BYTES/EVERY leaks BYTES every EVERY conversions on purpose. With --plant the exit status is 0 only
if the gate FAILS (the positive control); without it, only if the gate passes. A soak of fewer than
1,000,000 conversions is an observation and always exits 3, never 0.

Calibration the margins come from (the lab's run of 2026-11-27 on an idle host, head 9f93032, 4 scenarios x
1,102,696 conversions, sampled per conversion; first -> last level in bytes, then the range seen):
  ok         40,393,552 -> 40,379,800   range 40,379,544 .. 40,393,760
  cancel     40,331,096 -> 40,316,128   (min 40,315,832)
  dstbad     40,333,096 -> 40,319,264
  truncated  40,333,592 -> 40,319,808
  The widest range was about 15 KB, so SOAK_MARGIN = 3 x 15 KB = 45,000 B over 1.1 million conversions: the
  smallest leak it detects is about 0.04 B per conversion (41 B per thousand conversions).
  Bytes at exit, 3 runs each: n=1 262,960 / 262,992 / 262,960; n=10 273,768 .. 273,816; n=130 273,752 ..
  273,800. The widest spread between runs was 64 B, so EXIT_MARGIN = 3 x 64 = 192 B.
The margins were fixed before the first gate run; changing them goes back to the reviewer of the gate.
"""
import argparse
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sacdgen

ROOT = Path(__file__).resolve().parents[1]
CHILD = ROOT / "tests" / "heapflat_child.py"
SHIM_C = ROOT / "tests" / "native" / "countmalloc.c"
SOAK_MARGIN = 45_000          # bytes, |last - first| over at least 1,000,000 conversions
EXIT_MARGIN = 192             # bytes, at exit, against the median of the 10-conversion runs
GATE_LENGTH = 1_000_000
DEFAULT_CONVERSIONS = 1_102_696
SCENARIOS = ("ok", "cancel", "dstbad", "truncated")


def make_discs(tmp: Path) -> dict[str, Path]:
    import random
    rng = random.Random(61)
    frames = [bytes(rng.getrandbits(8) for _ in range(4704 * 2)) for _ in range(40)]
    img, _ = sacdgen.build_image([sacdgen.AreaSpec(2, frames, [20, 20], dst=False, titles=["One", "Two"])],
                                 sacdgen.DiscInfo(title="Heap flat", artist="pysacd"))
    good = tmp / "disc.iso"
    good.write_bytes(bytes(img))
    bad = [bytes(rng.getrandbits(8) for _ in range(900)) for _ in range(20)]
    img, _ = sacdgen.build_image([sacdgen.AreaSpec(2, bad, [10, 10], dst=True, titles=["One", "Two"])],
                                 sacdgen.DiscInfo(title="DST garbage", artist="pysacd"))
    garbage = tmp / "dst-garbage.iso"
    garbage.write_bytes(bytes(img))
    return {"ok": good, "cancel": good, "truncated": good, "dstbad": garbage}


def python_with_mutagen() -> str:
    for exe in (os.environ.get("HEAPFLAT_PYTHON"), sys.executable):
        if exe and subprocess.run([exe, "-I", "-c", "import mutagen"], capture_output=True,
                                  check=False).returncode == 0:
            return exe
    raise SystemExit("no interpreter with mutagen: set HEAPFLAT_PYTHON")


def run_child(python: str, shim: Path, scenario: str, iso: Path, out: Path, n: int, extra: dict[str, str]):
    env = dict(os.environ, LD_PRELOAD=str(shim), PYTHONMALLOC="malloc", PYTHONPATH=str(ROOT / "src"),
               PYTHONDONTWRITEBYTECODE="1", HEAPFLAT_WARMUP="3")
    env.update(extra)
    out.mkdir(exist_ok=True)
    return subprocess.run([python, str(CHILD), scenario, str(iso), str(out), str(n)], env=env,
                          capture_output=True, text=True, check=False)


def soak(args, python, shim, discs, tmp) -> bool:
    ok = True
    for scenario in args.scenario:
        extra = {"HEAPFLAT_EVERY": str(args.every)}
        if args.plant:
            extra["HEAPFLAT_PLANT"] = args.plant
        r = run_child(python, shim, scenario, discs[scenario], tmp / f"out-{scenario}", args.conversions, extra)
        line = next((x for x in r.stdout.splitlines() if x.startswith("LEVELS ")), None)
        if r.returncode or line is None:
            print(f"soak {scenario}: the child failed\n{r.stderr[-2000:]}")
            return False
        levels = [int(x) for x in line.split()[1:]]
        change = levels[-1] - levels[0]
        verdict = "PASS" if abs(change) <= SOAK_MARGIN else "FAIL"
        kind = "gate" if args.conversions >= GATE_LENGTH else "OBSERVATION (fewer than 1,000,000 conversions)"
        print(f"soak {scenario}: {args.conversions} conversions, first {levels[0]} last {levels[-1]} "
              f"change {change} B (limit {SOAK_MARGIN}) {verdict} [{kind}]")
        ok = ok and verdict == "PASS"
    return ok


def at_exit(args, python, shim, discs, tmp) -> bool:
    runs: dict[int, list[int]] = {10: [], 130: []}
    for n, values in runs.items():
        for k in range(3):
            extra = {"CM_REPORT": "1", "HEAPFLAT_WARMUP": "0"}
            if args.plant:
                extra["HEAPFLAT_PLANT"] = args.plant
            r = run_child(python, shim, "ok", discs["ok"], tmp / f"exit-{n}-{k}", n, extra)
            got = [x for x in r.stderr.splitlines() if x.startswith("CM_AT_EXIT")]
            if r.returncode or not got:
                print(f"exit n={n} run {k}: the child failed\n{r.stderr[-2000:]}")
                return False
            values.append(int(got[-1].split("bytes=")[1].split()[0]))
    median = statistics.median(runs[10])
    ok = True
    for n, values in runs.items():
        for v in values:
            if abs(v - median) > EXIT_MARGIN:
                ok = False
        print(f"exit n={n}: {values} (median of n=10 {median:.0f}, limit {EXIT_MARGIN} B) "
              f"{'PASS' if all(abs(v - median) <= EXIT_MARGIN for v in values) else 'FAIL'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("soak")
    s.add_argument("--conversions", type=int, default=DEFAULT_CONVERSIONS)
    s.add_argument("--every", type=int, default=1000)
    s.add_argument("--scenario", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    s.add_argument("--plant", metavar="BYTES/EVERY")
    e = sub.add_parser("exit")
    e.add_argument("--plant", metavar="BYTES/EVERY")
    args = ap.parse_args()
    if not sys.platform.startswith("linux") or shutil.which("cc") is None:
        raise SystemExit("needs Linux with glibc and a C compiler (cc)")
    if not any((ROOT / "src" / "pysacd").glob("_sacdconv.*")):
        raise SystemExit("the native engine is not built: run make")
    python = python_with_mutagen()
    tmp = Path(tempfile.mkdtemp(prefix="heapflat-release-"))
    try:
        shim = tmp / "countmalloc.so"
        subprocess.run(["cc", "-O1", "-shared", "-fPIC", "-o", str(shim), str(SHIM_C), "-lpthread", "-ldl"],
                       check=True)
        discs = make_discs(tmp)
        passed = (soak if args.mode == "soak" else at_exit)(args, python, shim, discs, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if args.mode == "soak" and args.conversions < GATE_LENGTH:
        print(f"exit status 3: {args.conversions} conversions is an observation, not a gate result "
              f"(a gate run needs at least {GATE_LENGTH})")
        return 3
    if args.plant:
        print("positive control:", "seen (the gate failed, as it must)" if not passed else "NOT SEEN: the gate passed")
        return 0 if not passed else 1
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
