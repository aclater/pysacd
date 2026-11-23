#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""The by-release timing table (docs/performance.md, docs/release-test-plan.md step 8) on real discs.

  bench_release.py setup --work DIR REF...     a worktree, a venv and `make` per tag or branch (or NAME=REF)
  bench_release.py run --tree NAME=PATH... --disc ROLE=ISO... --out DIR
  bench_release.py report --out DIR            the tables and the threshold check from DIR/runs.csv

`run` times every row with every tree, ROUNDS times. Trees alternate, and the order rotates each
round. Each run is one row with one tree: under `benchlock run` when the host has benchlock (one
lock per run, so other queued work can take turns), in its own memory-capped scope with
--memory-max. A run reads the disc into the page cache, writes to a fresh folder under --tmp,
and is pinned with taskset. It records the wall time, the peak RSS (getrusage of the run), the
bytes written, the CPU temperature and throttle counters around it, and the load.

Rows (ROLE is the disc a row reads):

  dst2-1t   dst2   -2 -s --threads 1                 one fast core
  dst2-nt   dst2   -2 -s --threads N                 N cores, one thread each
  dst6-nt   dst6   -m -s --threads N
  dst6-1t   dst6   -m -s -t 1 --threads 1            track 1
  plain-1t  plain  -2 -s --threads 1                 a plain-DSD stereo disc
  flac-1t   plain  -2 --flac --pcm-rate 88200 --threads 1
  dst2-low  dst2   -2 -s --threads 1                 the low-end core (see below)

The low-end core is the first E-core on a hybrid Intel CPU, else the core with the lowest
preferred-core ranking (AMD) or the lowest maximum frequency. On a CPU whose cores all rank
the same, there is no low-end row unless --cpu-low names one. The report says which was used.

Results go to DIR/runs.csv (appended; a run already in it is not repeated) and DIR/table.md.
The newest tree is the last --tree. Threshold (release-test-plan step 8): a row slower than the
previous tree with ranges that do not overlap is flagged; more than 5 % slower, or more than
10 % more peak memory, is a FAIL, and `run` and `report` then exit 2.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

SLOWER_FAIL = 1.05
MEMORY_FAIL = 1.10


@dataclass(frozen=True)
class Row:
    key: str
    label: str
    disc: str
    args: tuple[str, ...]
    cpus: str               # "one", "n" or "low"


def rows(n: int) -> list[Row]:
    return [
        Row("dst2-1t", "DST stereo (`-2 -s`) to DSF, 1 thread", "dst2", ("-2", "-s", "--threads", "1"), "one"),
        Row("dst2-nt", f"DST stereo to DSF, {n} threads", "dst2", ("-2", "-s", "--threads", str(n)), "n"),
        Row("dst6-nt", f"DST 6-channel (`-m -s`) to DSF, {n} threads", "dst6",
            ("-m", "-s", "--threads", str(n)), "n"),
        Row("dst6-1t", "DST 6-channel to DSF, 1 thread, track 1", "dst6", ("-m", "-s", "-t", "1", "--threads", "1"),
            "one"),
        Row("plain-1t", "Plain DSD stereo to DSF, 1 thread", "plain", ("-2", "-s", "--threads", "1"), "one"),
        Row("flac-1t", "Plain DSD stereo to FLAC 88.2 kHz, 1 thread", "plain",
            ("-2", "--flac", "--pcm-rate", "88200", "--threads", "1"), "one"),
        Row("dst2-low", "DST stereo to DSF, 1 thread, low-end core", "dst2", ("-2", "-s", "--threads", "1"), "low"),
    ]


CSV_FIELDS = ["round", "row", "tree", "position", "cpus", "wall_s", "rc", "maxrss_kb", "bytes", "temp_before_c",
              "temp_after_c", "throttle_delta", "load1", "platform_profile", "started"]

# ------------------------------------------------------------------------------------------ the CPU


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


CPU = Path("/sys/devices/system/cpu")


def online_cpus() -> list[int]:
    return sorted(os.sched_getaffinity(0))


def first_threads(cpus: list[int]) -> list[int]:
    """One logical CPU per physical core, in order."""
    seen: set[str] = set()
    out = []
    for c in cpus:
        sib = _read(CPU / f"cpu{c}/topology/thread_siblings_list") or str(c)
        if sib not in seen:
            seen.add(sib)
            out.append(c)
    return out


def _parse_list(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += range(int(a), int(b) + 1)
        elif part:
            out.append(int(part))
    return out


def pick_cpus(n: int, low: int | None) -> tuple[int, list[int], int | None, str]:
    """(fast core, N cores, low-end core or None, how the low-end core was chosen)."""
    cpus = first_threads(online_cpus())
    atom = _read(Path("/sys/devices/cpu_atom/cpus"))
    core = _read(Path("/sys/devices/cpu_core/cpus"))
    how = ""
    if atom and core:
        fast = [c for c in cpus if c in set(_parse_list(core))]
        ecores = [c for c in cpus if c in set(_parse_list(atom))]
        if low is None and ecores:
            low, how = ecores[0], f"an E-core (cpu{ecores[0]})"
    else:
        fast = cpus
        if low is None:
            for name, what in (("cpufreq/amd_pstate_prefcore_ranking", "preferred-core ranking"),
                               ("cpufreq/cpuinfo_max_freq", "maximum frequency")):
                vals = {c: _read(CPU / f"cpu{c}/{name}") for c in cpus}
                nums = {c: int(v) for c, v in vals.items() if v and v.isdigit()}
                if len(nums) == len(cpus) and len(set(nums.values())) > 1:
                    low = min(nums, key=lambda c: (nums[c], -c))
                    how = (f"the lowest {what} (cpu{low}: {nums[low]}, highest {max(nums.values())}); "
                           "a uniform CPU, so not an E-core")
                    break
    if low is not None and not how:
        how = f"cpu{low}, given with --cpu-low"
    fast = [c for c in fast if c != low] or cpus
    return fast[0], fast[:n], low, how


def cpu_temp() -> float | None:
    """The package temperature: k10temp's Tctl, coretemp's package, or the first thermal zone."""
    for hw in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        if _read(hw / "name") in ("k10temp", "coretemp", "zenpower"):
            t = _read(hw / "temp1_input")
            if t:
                return int(t) / 1000
    t = _read(Path("/sys/class/thermal/thermal_zone0/temp"))
    return int(t) / 1000 if t else None


def throttle_count(cpus: list[int]) -> int | None:
    """The package count of the first CPU plus the core counts of `cpus` (Intel); None elsewhere."""
    first = _read(CPU / f"cpu{cpus[0]}/thermal_throttle/package_throttle_count")
    if first is None:
        return None
    total = int(first)
    for c in cpus:
        total += int(_read(CPU / f"cpu{c}/thermal_throttle/core_throttle_count") or 0)
    return total


def cpu_name() -> str:
    for line in (_read(Path("/proc/cpuinfo")) or "").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "unknown CPU"

# ------------------------------------------------------------------------------------- one timed run


def one(args: argparse.Namespace) -> None:
    """Time one row with one tree; print a JSON line. Runs inside the lock and the memory scope."""
    cpus = _parse_list(args.cpus)
    with open(args.disc, "rb") as f:                # into the page cache, as a user's second run
        while f.read(1 << 24):
            pass
    out = Path(tempfile.mkdtemp(prefix="bench-", dir=args.tmp))
    env = dict(os.environ, PYTHONPATH=str(Path(args.tree).resolve() / "src"))
    cmd = ["taskset", "-c", args.cpus, args.python, "-m", "pysacd.extract", "--no-cfg", *shlex.split(args.extract),
           "-i", str(Path(args.disc).resolve()), "-y", str(out)]
    temp0, thr0 = cpu_temp(), throttle_count(cpus)
    start = time.perf_counter()
    with open(os.devnull, "wb") as null, open(args.log, "ab") as log:
        p = subprocess.Popen(cmd, cwd=out, env=env, stdout=null, stderr=log)  # nosemgrep: dangerous-subprocess-use-tainted-env-args  # a list, no shell; the developer's own paths
        _, status, usage = os.wait4(p.pid, 0)
    wall = time.perf_counter() - start
    temp1, thr1 = cpu_temp(), throttle_count(cpus)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    shutil.rmtree(out, ignore_errors=True)
    print(json.dumps({
        "wall_s": round(wall, 4), "rc": os.waitstatus_to_exitcode(status), "maxrss_kb": usage.ru_maxrss,
        "bytes": size, "temp_before_c": temp0, "temp_after_c": temp1,
        "throttle_delta": None if thr0 is None or thr1 is None else thr1 - thr0,
        "load1": os.getloadavg()[0], "platform_profile": _read(Path("/sys/firmware/acpi/platform_profile")),
    }))

# ----------------------------------------------------------------------------------------- the trees


def tree_python(tree: Path) -> str:
    venv = tree.resolve() / ".bench-venv/bin/python"
    return str(venv) if venv.exists() else sys.executable


def scarletbook_version(tree: Path) -> str:
    try:
        return str(json.loads((tree / "scarletbook.lock").read_text())["version"])
    except (OSError, ValueError, KeyError):
        return "?"


def setup(args: argparse.Namespace) -> None:
    """A worktree, a venv (the tree's own hash-pinned requirements) and `make` for each ref."""
    work = Path(args.work).resolve()
    (work / "trees").mkdir(parents=True, exist_ok=True)
    specs = []
    for spec in args.refs:
        name, _, ref = spec.rpartition("=")
        name = name or ref.removeprefix("v").replace("/", "-")
        tree = work / "trees" / name
        if not tree.exists():
            subprocess.run(["git", "-C", args.repo, "worktree", "add", "-q", "--detach", str(tree), ref], check=True)
        venv = tree / ".bench-venv"
        if not venv.exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
            subprocess.run([str(venv / "bin/pip"), "install", "-q", "--require-hashes", "-r",
                            str(tree / "requirements-test.txt")], check=True)
        with open(tree / "bench-build.log", "wb") as log:
            subprocess.run(["make", f"-j{args.jobs}", f"PYTHON={venv / 'bin/python'}"], cwd=tree, check=True,
                           stdout=log, stderr=subprocess.STDOUT)
        specs.append(f"--tree {name}={tree}")
        head = subprocess.run(["git", "-C", str(tree), "log", "--oneline", "-1"], capture_output=True, text=True,
                              check=False).stdout.strip()
        print(f"{name}: {head}, scarletbook {scarletbook_version(tree)}")
    print(" ".join(specs))


def remove(args: argparse.Namespace) -> None:
    for tree in sorted((Path(args.work) / "trees").glob("*")):
        subprocess.run(["git", "-C", args.repo, "worktree", "remove", "--force", str(tree)], check=False)
    subprocess.run(["git", "-C", args.repo, "worktree", "prune"], check=False)

# --------------------------------------------------------------------------------------------- run


def order(trees: list[str], round_number: int) -> list[str]:
    k = round_number % len(trees)
    return trees[k:] + trees[:k]


def has_benchlock() -> bool:
    return shutil.which("benchlock") is not None


def wait_for_yield(pattern: str) -> None:
    """Don't queue while a run matching `pattern` holds or waits for the lock (resources.md)."""
    while pattern:
        r = subprocess.run(["benchlock", "status"], capture_output=True, text=True, check=False)
        if pattern not in r.stdout + r.stderr:
            return
        time.sleep(30)


def load_done(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def run(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    trees = {n: str(Path(p).resolve()) for n, p in (t.split("=", 1) for t in args.tree)}
    discs = {n: str(Path(p).resolve()) for n, p in (d.split("=", 1) for d in args.disc)}
    fast, many, low, how = pick_cpus(args.threads, args.cpu_low)
    cpus_for = {"one": str(fast), "n": ",".join(map(str, many)), "low": "" if low is None else str(low)}
    selected = [r for r in rows(len(many)) if (not args.rows or r.key in args.rows) and r.disc in discs
                and (r.cpus != "low" or low is not None)]
    lock = has_benchlock() and not args.no_benchlock
    meta = {"cpu": cpu_name(), "kernel": os.uname().release, "low_core": how or "none (uniform CPU)",
            "trees": {n: {"path": p, "scarletbook": scarletbook_version(Path(p))} for n, p in trees.items()},
            "discs": discs, "cpus": cpus_for, "benchlock": lock, "memory_max": args.memory_max,
            "rows": [r.key for r in selected]}
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    csv_path = out / "runs.csv"
    done = {(d["round"], d["row"], d["tree"]) for d in load_done(csv_path) if d["rc"] == "0"}
    last: dict[tuple[str, str], float] = {}
    deadline = args.deadline or float("inf")
    started = time.time()
    ran = failed = 0
    for rnd in range(args.rounds):
        for row in selected:
            for pos, name in enumerate(order(list(trees), rnd)):
                if (str(rnd + 1), row.key, name) in done:
                    continue
                guess = last.get((row.key, name), 0.0) * 1.2
                if time.time() + guess > deadline:
                    print(f"deadline: stopping before round {rnd + 1} {row.key} {name}", file=sys.stderr)
                    return finish(out, args, ran, failed, started)
                cmd = [sys.executable, str(Path(__file__).resolve()), "_one", "--tree", trees[name],
                       "--python", tree_python(Path(trees[name])), "--disc", discs[row.disc],
                       "--cpus", cpus_for[row.cpus], "--extract", shlex.join(row.args), "--tmp", args.tmp,
                       "--log", str(out / "stderr.log")]
                if lock:
                    wait_for_yield(args.yield_to)
                    cmd = ["benchlock", "run", f"{args.label}-r{rnd + 1}-{row.key}-{name}", "--", *cmd]
                if args.memory_max:
                    cmd = ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={args.memory_max}",
                           "-p", "MemorySwapMax=0", *cmd]
                if args.dry_run:
                    print(shlex.join(cmd))
                    continue
                when = time.strftime("%Y-%m-%dT%H:%M:%S")
                r = subprocess.run(cmd, capture_output=True, text=True, check=False)
                lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
                res = json.loads(lines[-1]) if lines else {"rc": r.returncode or -1}
                rec = {"round": rnd + 1, "row": row.key, "tree": name, "position": pos + 1,
                       "cpus": cpus_for[row.cpus], "started": when, **res}
                new = not csv_path.exists()
                with open(csv_path, "a", newline="") as f:
                    w = csv.DictWriter(f, CSV_FIELDS, extrasaction="ignore")
                    if new:
                        w.writeheader()
                    w.writerow(rec)
                if "wall_s" in res:
                    last[(row.key, name)] = res["wall_s"]
                ran += 1
                print(f"{when} round {rnd + 1} {row.key} {name}: {res.get('wall_s')} s, "
                      f"{res.get('maxrss_kb')} KB, rc {res.get('rc')}", flush=True)
                if res.get("rc") != 0:
                    failed += 1
                    print(r.stderr[-2000:], file=sys.stderr)
    return finish(out, args, ran, failed, started)


def finish(out: Path, args: argparse.Namespace, ran: int, failed: int, started: float) -> int:
    if args.dry_run:
        return 0
    print(f"{ran} runs ({failed} failed, see stderr.log) in {time.time() - started:.0f} s", file=sys.stderr)
    status = report_dir(out)
    return status or (1 if failed else 0)

# -------------------------------------------------------------------------------------------- report


def summary(values: list[float]) -> tuple[float, float, float]:
    return statistics.median(values), min(values), max(values)


def check(prev: list[float], new: list[float], mem_prev: list[float], mem_new: list[float]) -> str:
    """The release-plan verdict for one row, newest tree against the previous one."""
    if min(len(prev), len(new)) < 3:
        return "not checked: under 3 runs"
    (mp, _, hip), (mn, lon, _) = summary(prev), summary(new)
    verdict = []
    if mn > SLOWER_FAIL * mp:
        verdict.append(f"FAIL: {100 * (mn / mp - 1):.1f} % slower")
    elif mn > mp and lon > hip:
        verdict.append("slower, ranges apart")
    if mem_prev and mem_new and max(mem_new) > MEMORY_FAIL * max(mem_prev):
        verdict.append(f"FAIL: peak memory +{100 * (max(mem_new) / max(mem_prev) - 1):.0f} %")
    return "; ".join(verdict) or "ok"


def report_dir(out: Path) -> int:
    meta = json.loads((out / "meta.json").read_text())
    data = [d for d in load_done(out / "runs.csv") if d["rc"] == "0"]
    trees = list(meta["trees"])
    by_key = {r.key: r for r in rows(len(meta["cpus"]["n"].split(",")))}
    wall: dict[tuple[str, str], list[float]] = {}
    mem: dict[tuple[str, str], list[float]] = {}
    for d in data:
        wall.setdefault((d["row"], d["tree"]), []).append(float(d["wall_s"]))
        mem.setdefault((d["row"], d["tree"]), []).append(int(d["maxrss_kb"]) / 1024)
    heads = [f"{t} ({meta['trees'][t]['scarletbook']})" for t in trees]
    newest, prev, oldest = trees[-1], trees[-2] if len(trees) > 1 else trees[-1], trees[0]
    lines = [f"| Wall s | {' | '.join(heads)} | vs {prev} | vs {oldest} | Peak MiB ({newest}) | Check |",
             "|---" * (len(trees) + 5) + "|"]
    mlines = [f"| Peak RSS MiB | {' | '.join(heads)} | vs {prev} | vs {oldest} |", "|---" * (len(trees) + 3) + "|"]
    failed = False
    rounds = set()
    for key in meta["rows"]:
        if not all((key, t) in wall for t in trees):
            continue
        cells, mcells = [], []
        for t in trees:
            m, lo, hi = summary(wall[(key, t)])
            cells.append(f"{m:.2f} ({lo:.2f}-{hi:.2f})")
            mm, mlo, mhi = summary(mem[(key, t)])
            mcells.append(f"{mm:.0f} ({mlo:.0f}-{mhi:.0f})")
            rounds.add(len(wall[(key, t)]))

        def pct(a: str, src: dict[tuple[str, str], list[float]], key: str = key) -> str:
            return f"{100 * (statistics.median(src[(key, newest)]) / statistics.median(src[(key, a)]) - 1):+.1f} %"
        verdict = check(wall[(key, prev)], wall[(key, newest)], mem[(key, prev)], mem[(key, newest)])
        failed |= verdict.startswith("FAIL") or "; FAIL" in verdict
        label = by_key[key].label
        if key == "dst2-low":
            label += f" (cpu{meta['cpus']['low']})"
        lines.append(f"| {label} | {' | '.join(cells)} | {pct(prev, wall)} | {pct(oldest, wall)} | "
                     f"{max(mem[(key, newest)]):.0f} | {verdict} |")
        mlines.append(f"| {label} | {' | '.join(mcells)} | {pct(prev, mem)} | {pct(oldest, mem)} |")
    temps = [float(d["temp_after_c"]) for d in data if d.get("temp_after_c")]
    thr = [int(d["throttle_delta"]) for d in data if d.get("throttle_delta")]
    n = f"{min(rounds)}" if rounds and len(rounds) == 1 else f"{min(rounds, default=0)} to {max(rounds, default=0)}"
    notes = [
        "",
        (f"{meta['cpu']}, kernel {meta['kernel']}. Median of {n} runs, range in brackets; trees alternated, "
        f"order rotated each round; {'one run per `benchlock` lock' if meta['benchlock'] else 'no benchlock'}"
        f"{', memory-capped at ' + meta['memory_max'] if meta['memory_max'] else ''}. Image in the page cache, "
        "output to a tmpfs, start-up included. Pinned with `taskset`: "
        f"`-c {meta['cpus']['one']}` for one thread, `-c {meta['cpus']['n']}` for several."),
        f"Low-end core: {meta['low_core']}.",
        ("Peak memory is the run's peak RSS (getrusage). Check: newest against the previous tree; FAIL above "
        f"{round((SLOWER_FAIL - 1) * 100)} % slower or {round((MEMORY_FAIL - 1) * 100)} % more peak memory."),
    ]
    if temps:
        notes.append(f"CPU temperature after runs: {min(temps):.0f} to {max(temps):.0f} °C"
                     + (f"; throttle counters changed in {sum(1 for t in thr if t)} runs." if thr else
                        "; this CPU has no throttle counters."))
    text = "\n".join(lines + [""] + mlines + notes) + "\n"
    (out / "table.md").write_text(text)
    print(text)
    return 2 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="build trees")
    s.add_argument("--work", required=True)
    s.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    s.add_argument("--jobs", type=int, default=4)
    s.add_argument("refs", nargs="+", help="tags or branches, or NAME=REF")
    c = sub.add_parser("clean", help="remove the trees setup made")
    c.add_argument("--work", required=True)
    c.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    r = sub.add_parser("run", help="time the rows")
    r.add_argument("--tree", action="append", required=True, help="NAME=PATH, oldest first, newest last")
    r.add_argument("--disc", action="append", required=True, help="ROLE=ISO, ROLE one of dst2, dst6, plain")
    r.add_argument("--out", required=True)
    r.add_argument("--rows", nargs="*", help="row keys (default: all whose disc is given)")
    r.add_argument("--rounds", type=int, default=5)
    r.add_argument("--threads", type=int, default=5, help="N for the several-thread rows")
    r.add_argument("--cpu-low", type=int, help="the low-end core, instead of detecting one")
    r.add_argument("--tmp", default="/dev/shm" if Path("/dev/shm").is_dir() else tempfile.gettempdir())
    r.add_argument("--memory-max", default="", help="systemd MemoryMax for each run, e.g. 12G")
    r.add_argument("--label", default="pysacd-bench")
    r.add_argument("--yield-to", default="dsd2pcm", help="wait while benchlock status mentions this")
    r.add_argument("--deadline", type=float, help="epoch seconds: start no run that would end after it")
    r.add_argument("--no-benchlock", action="store_true")
    r.add_argument("--dry-run", action="store_true", help="print the commands only")
    p = sub.add_parser("report", help="the tables from DIR/runs.csv")
    p.add_argument("--out", required=True)
    o = sub.add_parser("_one")
    for name in ("--tree", "--python", "--disc", "--cpus", "--extract", "--tmp", "--log"):
        o.add_argument(name, required=True)
    args = ap.parse_args()
    if args.cmd == "setup":
        setup(args)
    elif args.cmd == "clean":
        remove(args)
    elif args.cmd == "run":
        sys.exit(run(args))
    elif args.cmd == "report":
        sys.exit(report_dir(Path(args.out)))
    else:
        one(args)


if __name__ == "__main__":
    main()
