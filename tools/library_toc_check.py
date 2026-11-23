#!/usr/bin/env python3
"""TOC check of a whole library of SACD images (#213).

    python3 tools/library_toc_check.py DIR [--out RUN.tsv] [--compare OLD.tsv]
                                       [--timeout SECONDS] [--index PATHS.tsv]

Finds every `.iso` under DIR (anything else is skipped) and reads each one's TOC with
`pysacd.read_toc`, one image at a time, in a single worker process. The worker is the
"in-process" reader; the parent only watches it. An image that takes longer than --timeout, or
that kills the worker, is recorded as `timeout` or `crash`, and a new worker takes the next image.
Run it with the pysacd to be checked on PYTHONPATH (or installed in the interpreter).

The TSV (--out, default stdout) has one row per image and no path, name or title: the id is the
first 12 hex digits of the SHA-256 of the image's path relative to DIR, so the same image has
the same id in every run. Columns: id, status (`ok`, or the exception's type, `timeout`,
`crash`), areas (`2ch`, `mc`, `2ch+mc`), tracks (per area, in that order, `/`-separated), dst
(`dst`, `plain`, `mixed`), text (`yes`/`no`), read_ms, error (the message, with the image's path
removed). --index writes `id<TAB>relative path` for finding an image again; keep that file local.

The summary goes to stderr: images checked, the count per status, and with --compare OLD.tsv the
images whose status got worse (ok or absent before, an error now), those that got better, and
those that changed in areas, tracks, dst or text while ok. Exit status 1 on any new error or
change, 2 on a bad argument.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

COLUMNS = ["id", "status", "areas", "tracks", "dst", "text", "read_ms", "error"]
COMPARED = ["areas", "tracks", "dst", "text"]


def image_id(rel: str) -> str:
    return hashlib.sha256(rel.encode("utf-8", "surrogateescape")).hexdigest()[:12]


def find_images(root: Path) -> list[Path]:
    """Every regular file named *.iso under root (symlinks are not followed), sorted."""
    found = []
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            p = Path(dirpath) / name
            if name.lower().endswith(".iso") and p.is_file() and not p.is_symlink():
                found.append(p)
    return found


def describe(path: str) -> dict:
    """Read one image's TOC (runs in the worker)."""
    from pysacd import read_toc

    d = read_toc(path)
    kinds = []
    counts = []
    formats = set()
    text = bool(d.text)
    for a in d.areas:
        kinds.append("mc" if a.is_multichannel else "2ch")
        counts.append(str(len(a.tracks)))
        formats.add("dst" if a.frame_format == 0 else "plain")
        text = text or any(t.text for t in a.tracks)
    return {"areas": "+".join(kinds) or "-", "tracks": "/".join(counts) or "-",
            "dst": formats.pop() if len(formats) == 1 else ("mixed" if formats else "-"),
            "text": "yes" if text else "no"}


def worker() -> None:
    """Read paths from stdin, one per line; answer one JSON line each."""
    for line in sys.stdin:
        path = line.rstrip("\n")
        t0 = time.monotonic()
        try:
            r = describe(path)
            r["status"] = "ok"
        except BaseException as e:      # noqa: BLE001 - any failure is a result, not a crash
            r = {"status": type(e).__name__, "error": str(e)}
        r["read_ms"] = round((time.monotonic() - t0) * 1000)
        sys.stdout.write(json.dumps(r) + "\n")
        sys.stdout.flush()


class Worker:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                     encoding="utf-8", errors="surrogateescape")

    def stop(self) -> None:
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None

    def read(self, path: str, timeout: float) -> dict:
        t0 = time.monotonic()
        if self.proc is None:
            self.start()
        p = self.proc
        assert p and p.stdin and p.stdout
        try:
            p.stdin.write(path + "\n")
            p.stdin.flush()
            ready, _, _ = select.select([p.stdout], [], [], timeout)
            if not ready:
                self.stop()
                return {"status": "timeout", "error": f"no answer in {timeout:g} s",
                        "read_ms": round((time.monotonic() - t0) * 1000)}
            line = p.stdout.readline()
            if line:
                return json.loads(line)
        except (BrokenPipeError, ValueError):
            pass
        code = p.poll() if p.poll() is not None else p.wait()
        self.proc = None
        return {"status": "crash", "error": f"worker exited with status {code}",
                "read_ms": round((time.monotonic() - t0) * 1000)}


def clean(text: str, path: str) -> str:
    """One line, without the image's path."""
    for p in (path, os.path.basename(path)):
        if p:
            text = text.replace(p, "<image>")
    return " ".join(text.split())


def write_tsv(rows: list[dict], out) -> None:
    out.write("\t".join(COLUMNS) + "\n")
    for r in rows:
        out.write("\t".join(str(r.get(c, "")) for c in COLUMNS) + "\n")


def read_tsv(path: str) -> dict[str, dict]:
    rows = {}
    with open(path, encoding="utf-8") as f:
        head = f.readline().rstrip("\n").split("\t")
        for line in f:
            vals = line.rstrip("\n").split("\t")
            r = dict(zip(head, vals + [""] * (len(head) - len(vals))))
            rows[r["id"]] = r
    return rows


def summarize(rows: list[dict], old: dict[str, dict] | None, out) -> int:
    """Print the summary; return the number of new errors and changes."""
    counts = Counter(r["status"] for r in rows)
    print(f"images checked: {len(rows)}", file=out)
    for status, n in sorted(counts.items(), key=lambda kv: (kv[0] != "ok", kv[0])):
        print(f"  {status}: {n}", file=out)
    print(f"read time: {sum(int(r['read_ms']) for r in rows) / 1000:.1f} s total, "
          f"{max((int(r['read_ms']) for r in rows), default=0)} ms slowest", file=out)
    if old is None:
        return 0
    worse, better, changed = [], [], []
    for r in rows:
        o = old.get(r["id"])
        if o is None:
            if r["status"] != "ok":
                worse.append(r)
        elif r["status"] != "ok" and o["status"] == "ok":
            worse.append(r)
        elif r["status"] == "ok" and o["status"] != "ok":
            better.append(r)
        elif r["status"] != o["status"]:
            changed.append((r, f"{o['status']} -> {r['status']}"))
        elif any(o.get(c, "") != r.get(c, "") for c in COMPARED):
            changed.append((r, "; ".join(f"{c} {o.get(c)} -> {r.get(c, '')}" for c in COMPARED if o.get(c, "") != r.get(c, ""))))
    gone = [i for i in old if i not in {r["id"] for r in rows}]
    print(f"against the previous run ({len(old)} images):", file=out)
    print(f"  new errors: {len(worse)}", file=out)
    for r in worse:
        print(f"    {r['id']}  {r['status']}: {r['error']}", file=out)
    print(f"  fixed: {len(better)}", file=out)
    print(f"  changed: {len(changed)}", file=out)
    for r, what in changed:
        print(f"    {r['id']}  {what}", file=out)
    print(f"  no longer present: {len(gone)}", file=out)
    return len(worse) + len(changed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TOC check of a library of SACD images")
    ap.add_argument("dir", type=Path, nargs="?")
    ap.add_argument("--out", help="TSV file (default: stdout)")
    ap.add_argument("--compare", metavar="OLD.tsv", help="a previous run's TSV")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds per image (default 60)")
    ap.add_argument("--index", metavar="FILE", help="write id and relative path here (local only)")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker:
        worker()
        return 0
    if args.dir is None or not args.dir.is_dir():
        print(f"not a directory: {args.dir}", file=sys.stderr)
        return 2
    old = None
    if args.compare:
        try:
            old = read_tsv(args.compare)
        except (OSError, KeyError) as e:
            print(f"cannot read {args.compare}: {e}", file=sys.stderr)
            return 2
    rows, index = [], []
    w = Worker()
    try:
        for p in find_images(args.dir):
            rel = p.relative_to(args.dir).as_posix()
            r = w.read(str(p), args.timeout)
            r["id"] = image_id(rel)
            r["error"] = clean(r.get("error", ""), str(p))
            rows.append(r)
            index.append((r["id"], rel))
    finally:
        w.stop()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            write_tsv(rows, f)
    else:
        write_tsv(rows, sys.stdout)
    if args.index:
        with open(args.index, "w", encoding="utf-8", errors="surrogateescape") as f:
            for i, rel in index:
                f.write(f"{i}\t{rel}\n")
    bad = summarize(rows, old, sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
