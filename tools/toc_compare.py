#!/usr/bin/env python3
"""TOC identity harness across engine changes (#94): every structure and `toc --json`.

    python3 tools/toc_compare.py --old OLD_SRC --new NEW_SRC [--mutations N] [--no-values] IMAGE_OR_DIR...

Reads every image (a directory: every *.iso under it) with two pysacd source trees (each a
directory holding the `pysacd` package and its built engine, run with PYTHONPATH), in one
subprocess per tree. For each image it records the repr of `read_toc()`'s Disc (every Area,
Track, Timecode, Genre, Locale, text, warning and the manufacturer bytes), the bytes of
`pysacd toc --json`, or the exception's type and text when the image is refused. Old and new
must agree on all of them.

--mutations N also compares N mutated copies of each of the first images given: a few random
bytes of the master TOC, a text sector or an area TOC replaced (seeded, so runs repeat). The
copies are written to a temporary directory (TMPDIR) and removed.

--no-values reports counts only (no file names), for private discs. Exit status 1 on any
difference.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

# run inside each tree: one JSON line per image, {"repr" | "error", "json"}
CHILD = r"""
import io, json, sys, contextlib
from pysacd import read_toc
from pysacd.__main__ import _to_json
for line in sys.stdin:
    path = line.rstrip("\n")
    try:
        d = read_toc(path)
        r = {"repr": repr(d), "json": json.dumps({"path": "x", **_to_json(d)}, ensure_ascii=False, indent=2)}
    except Exception as e:
        r = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(r), flush=True)
"""

SECTOR = 2048


def mutate(src: Path, dest_dir: Path, n: int, seed: int) -> list[Path]:
    """n copies of src with a few bytes changed in its TOC sectors."""
    data = src.read_bytes()
    rng = random.Random(seed)
    # master TOC (510..512), its text sectors (511..518), area TOC starts named in it
    regions = [(510 * SECTOR, 519 * SECTOR)]
    for off in (64, 72):    # 2ch and multichannel area TOC-1 start LSNs in the master TOC (big-endian)
        lsn = int.from_bytes(data[510 * SECTOR + off:510 * SECTOR + off + 4], "big")
        if lsn and (lsn + 4) * SECTOR <= len(data):
            regions.append((lsn * SECTOR, (lsn + 4) * SECTOR))
    out = []
    for i in range(n):
        b = bytearray(data[:max(r[1] for r in regions)])
        lo, hi = rng.choice(regions)
        for _ in range(rng.randint(1, 8)):
            b[rng.randrange(lo, hi)] = rng.randrange(256)
        p = dest_dir / f"{src.stem}-m{i}.iso"
        p.write_bytes(bytes(b) + data[len(b):])
        out.append(p)
    return out


def run_tree(src: str, images: list[Path]) -> list[dict]:
    env = {**os.environ, "PYTHONPATH": src}
    r = subprocess.run([sys.executable, "-c", CHILD], input="".join(f"{p}\n" for p in images),
                       capture_output=True, text=True, env=env, check=False)
    rows = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
    if len(rows) != len(images):
        raise SystemExit(f"{src}: {len(rows)} results for {len(images)} images\n{r.stderr[-2000:]}")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--mutations", type=int, default=0)
    ap.add_argument("--mutate-first", type=int, default=5, help="mutate this many of the images (default 5)")
    ap.add_argument("--no-values", action="store_true")
    args = ap.parse_args(argv)
    images: list[Path] = []
    for p in args.paths:
        images += sorted(p.rglob("*.iso")) if p.is_dir() else [p]
    with tempfile.TemporaryDirectory() as tmp:
        mutated: list[Path] = []
        if args.mutations:
            for k, img in enumerate(images[:args.mutate_first]):
                mutated += mutate(img, Path(tmp), args.mutations, seed=k)
        allimgs = images + mutated
        old = run_tree(str(Path(args.old).resolve()), allimgs)
        new = run_tree(str(Path(args.new).resolve()), allimgs)
    bad = refused = 0
    for n, (img, o, w) in enumerate(zip(allimgs, old, new, strict=True), 1):
        refused += "error" in o
        if o != w:
            bad += 1
            what = [k for k in ("repr", "json", "error") if o.get(k) != w.get(k)]
            name = f"image {n}" if args.no_values else str(img)
            print(f"DIFFERS: {name}: {', '.join(what)}")
            if not args.no_values:
                for k in what:
                    print(f"  old {k}: {str(o.get(k))[:400]}\n  new {k}: {str(w.get(k))[:400]}")
    print(f"{len(allimgs)} images ({len(images)} given, {len(mutated)} mutated; {refused} refused by both "
          f"or old): {len(allimgs) - bad} identical, {bad} differ")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
