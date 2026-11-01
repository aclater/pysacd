#!/usr/bin/env python3
"""Byte-identity harness for pysacd's outputs across engine changes (#85, phase 3).

    python3 tools/extract_compare.py --old OLD_SRC --new NEW_SRC [--sacd-extract BIN]
        [--modes NAME,...] [--threads N] [--no-values] [--keep DIR] IMAGE...

Runs every mode below on every image with two pysacd source trees (OLD_SRC and NEW_SRC, each
a directory holding the `pysacd` package and its built engine, run with PYTHONPATH) and,
for the sacd_extract-compatible modes, with a sacd_extract binary used as a black box. Each
run starts in an empty directory; its output is every file it wrote (relative path and
SHA-256) plus its standard output for -P.

- new must equal old: the same files, names and bytes, in every mode (a DSDIFF file's comment
  timestamps, the minute it was written, are left out);
- new must equal sacd_extract wherever old equalled it (file by file, and -P's text):
  the compatibility pysacd had before is kept, and nothing is claimed beyond it.

--no-values reports counts and mode names only (no file names), for private discs. Exit
status 1 on any difference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# LAST and MIDDLE stand for the area's last and middle track.
# name: (tool, arguments); tool is "extract" (pysacd-extract, also run with sacd_extract) or
# "extract+" (pysacd-extract only: pysacd extensions) or "convert" (pysacd convert)
MODES = {
    "dsf": ("extract", ["-2", "-s"]),
    "dsdiff": ("extract", ["-2", "-p"]),
    "dsdiff-c": ("extract", ["-2", "-p", "-c"]),
    "edit-master": ("extract", ["-2", "-e"]),
    "edit-master+dsf": ("extract", ["-2", "-e", "-s"]),
    "edit-master-c+cue": ("extract", ["-2", "-e", "-c", "-C"]),
    "dsf-pauses": ("extract", ["-2", "-s", "-b"]),
    "dsf-nopad": ("extract", ["-2", "-s", "-z"]),
    "dsf-last-track": ("extract", ["-2", "-s", "-t", "LAST"]),
    "dsdiff-tracks-concat": ("extract", ["-2", "-p", "-k", "-t", "2,3"]),
    "mch-dsf": ("extract", ["-m", "-s"]),
    "mch-dsdiff": ("extract", ["-m", "-p"]),
    "print": ("extract", ["-P"]),
    "iso": ("extract", ["-I"]),
    "iso+dsf": ("extract", ["-I", "-2", "-s"]),
    "flac": ("extract+", ["-2", "--flac"]),
    "flac-last-track": ("extract+", ["-2", "--flac", "-t", "LAST"]),
    "dsdiff-c-middle-track": ("extract", ["-2", "-p", "-c", "-t", "MIDDLE"]),
    "wav-176k": ("extract+", ["-2", "--wav", "--pcm-rate", "176400"]),
    "mch-flac": ("extract+", ["-m", "--flac"]),
    "convert-flac": ("convert", ["-f", "flac"]),
    "convert-wav-pauses": ("convert", ["-f", "wav", "--pauses"]),
    "convert-dsf-last-track": ("convert", ["-f", "dsf", "-t", "LAST"]),
    # the writer's choices of #94, one by one: ID3 modes, -z on a selection, pauses in DSDIFF
    "dsf-id3-0": ("extract+", ["-2", "-s", "--id3tag", "0"]),
    "dsf-id3-1": ("extract+", ["-2", "-s", "--id3tag", "1"]),
    "dsf-id3-2": ("extract+", ["-2", "-s", "--id3tag", "2"]),
    "dsf-id3-3": ("extract+", ["-2", "-s", "--id3tag", "3"]),
    "dsf-id3-5": ("extract+", ["-2", "-s", "--id3tag", "5"]),
    "dsdiff-id3-0": ("extract+", ["-2", "-p", "--id3tag", "0"]),
    "dsdiff-id3-3": ("extract+", ["-2", "-p", "--id3tag", "3"]),
    "dsf-nopad-tracks": ("extract", ["-2", "-s", "-z", "-t", "2,3"]),
    "dsf-nopad-pauses": ("extract", ["-2", "-s", "-z", "-b"]),
    "mch-dsf-nopad": ("extract", ["-m", "-s", "-z"]),
    "dsdiff-pauses": ("extract", ["-2", "-p", "-b"]),
    "mch-edit-master": ("extract", ["-m", "-e"]),
}


def _dff_timestamps(f) -> list[int]:
    """Offsets of the creation timestamps (year, month, day, hour, minute: 6 bytes) of a DSDIFF
    file's COMT comments. They hold the time the file was written, so two runs a minute apart
    differ there and nowhere else; the hash leaves them out."""
    out: list[int] = []
    f.seek(0, 2)
    end = f.tell()
    f.seek(0)
    if f.read(4) != b"FRM8":
        return out
    pos = 16
    while pos + 12 <= end:
        f.seek(pos)
        head = f.read(12)
        size = int.from_bytes(head[4:12], "big")
        if head[:4] == b"COMT":
            f.seek(pos + 12)
            count = int.from_bytes(f.read(2), "big")
            q = pos + 14
            for _ in range(count):
                f.seek(q)
                c = f.read(14)
                if len(c) < 14:
                    break
                out.append(q)
                n = int.from_bytes(c[10:14], "big")
                q += 14 + n + (n & 1)
        pos += 12 + size + (size & 1)
    return out


def tree(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h = hashlib.sha256()
            with open(p, "rb") as f:
                stamps = _dff_timestamps(f) if p.suffix.lower() == ".dff" else []
                f.seek(0)
                pos = 0
                for block in iter(lambda: f.read(1 << 20), b""):
                    b = bytearray(block)
                    for t in stamps:
                        for k in range(t, t + 6):
                            if pos <= k < pos + len(b):
                                b[k - pos] = 0
                    h.update(b)
                    pos += len(block)
            out[p.relative_to(root).as_posix()] = h.hexdigest()
    return out


def last_track(src: str, image: Path, area: str) -> int:
    code = ("import sys; from pysacd.toc import read_toc; d = read_toc(sys.argv[1]); "
            "a = d.multichannel if sys.argv[2] == 'm' else d.stereo; print(len(a.tracks) if a else 0)")
    r = subprocess.run([sys.executable, "-c", code, str(image), area], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": src}, check=False)
    return int(r.stdout.strip() or 0)


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, float]:
    t = time.monotonic()
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, errors="replace", check=False)
    return r.returncode, r.stdout, time.monotonic() - t


def one(kind: str, which: str, image: Path, args: list[str], work: Path, srcs: dict[str, str], sacd_extract: str | None,
        threads: int) -> dict | None:
    out = work / which
    out.mkdir()
    env = {**os.environ}
    if which == "sacd_extract":
        if kind != "extract" or not sacd_extract:
            return None
        cmd = [sacd_extract, *args, "-i", str(image)]
    else:
        env["PYTHONPATH"] = srcs[which]
        if kind == "convert":
            root = ["--allow-root"] if getattr(os, "geteuid", lambda: 1)() == 0 else []
            cmd = [sys.executable, "-m", "pysacd", "convert", *root, "--no-musicbrainz", "--config", os.devnull,
                   "-j", str(threads), "-o", str(out), *args, str(image)]
        else:
            cmd = [sys.executable, "-m", "pysacd.extract", "--no-cfg", "--threads", str(threads), *args,
                   "-i", str(image)]
    rc, stdout, secs = run(cmd, out, env)
    # Timings and the working directory aren't output: drop the lines that carry them
    lines = [ln for ln in stdout.splitlines()
             if not ln.lstrip().startswith(("Started at", "Ended at", "Current (working) directory"))]
    return {"rc": rc, "files": tree(out), "stdout": "\n".join(lines) if args == ["-P"] else "", "seconds": secs}


def compare(image: Path, mode: str, srcs: dict[str, str], sacd_extract: str | None, threads: int,
            keep: Path | None) -> dict:
    kind, args = MODES[mode]
    if "LAST" in args or "MIDDLE" in args:
        n = last_track(srcs["new"], image, "m" if "-m" in args else "2")
        args = [str(n) if a == "LAST" else str(max(1, (n + 1) // 2)) if a == "MIDDLE" else a for a in args]
    work = Path(tempfile.mkdtemp(prefix="cmp-", dir=keep))
    try:
        res = {w: one(kind, w, image, args, work, srcs, sacd_extract, threads) for w in ("old", "new", "sacd_extract")}
    finally:
        if keep is None:
            shutil.rmtree(work, ignore_errors=True)
    old, new, se = res["old"], res["new"], res["sacd_extract"]
    assert old is not None and new is not None
    r = {"mode": mode, "rc": (old["rc"], new["rc"]), "files": len(new["files"]),
         "seconds": (round(old["seconds"], 2), round(new["seconds"], 2)), "problems": []}
    if old["rc"] != new["rc"]:
        r["problems"].append(f"exit status {old['rc']} -> {new['rc']}")
    if old["files"].keys() != new["files"].keys():
        r["problems"].append(f"file set differs: only old {sorted(old['files'].keys() - new['files'].keys())}, "
                             f"only new {sorted(new['files'].keys() - old['files'].keys())}")
    for name in sorted(old["files"].keys() & new["files"].keys()):
        if old["files"][name] != new["files"][name]:
            r["problems"].append(f"differs from old: {name}")
    if old["stdout"] != new["stdout"]:
        r["problems"].append("-P text differs from old")
    if se is not None:
        r["sacd_extract_matched_by_old"] = sum(1 for n, h in se["files"].items() if old["files"].get(n) == h)
        r["sacd_extract_files"] = len(se["files"])
        for name, h in se["files"].items():
            if old["files"].get(name) == h and new["files"].get(name) != h:
                r["problems"].append(f"no longer matches sacd_extract: {name}")
        if se["stdout"] and se["stdout"] == old["stdout"] and new["stdout"] != se["stdout"]:
            r["problems"].append("-P text no longer matches sacd_extract")
        r["print_matches_sacd_extract"] = bool(se["stdout"]) and se["stdout"] == new["stdout"] or None
        r["seconds"] = (*r["seconds"], round(se["seconds"], 2))
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--old", required=True, help="source tree of the reference pysacd (e.g. 0.2.2)")
    ap.add_argument("--new", required=True, help="source tree of the pysacd under test")
    ap.add_argument("--sacd-extract", help="sacd_extract binary (a black box)")
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--no-values", action="store_true")
    ap.add_argument("--keep", type=Path, help="work under this directory and keep the outputs")
    args = ap.parse_args(argv)
    srcs = {"old": str(Path(args.old).resolve()), "new": str(Path(args.new).resolve())}
    bad = 0
    for n, image in enumerate(args.images, 1):
        for mode in args.modes.split(","):
            r = compare(image.resolve(), mode, srcs, args.sacd_extract, args.threads, args.keep)
            r["image"] = f"image {n}" if args.no_values else str(image)
            if args.no_values:
                r["problems"] = [p.split(":", 1)[0] for p in r["problems"]]
            bad += bool(r["problems"])
            print(json.dumps(r, ensure_ascii=False), flush=True)
    print(f"{bad} run(s) with differences", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
