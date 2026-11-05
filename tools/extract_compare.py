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

Each run also says, per file format, how the output compares with sacd_extract's, for both
trees ("vs_sacd_extract"): identical; identical apart from the documented exceptions
(docs/sacd_extract.md: the DSDIFF creating-machine comment and text pad bytes, the version lines
of cue and XML); or
different, with what differs. Anything in "different" is a difference nobody has explained.

--no-values reports counts and mode names only (no file names), for private discs. Exit
status 1 on any difference between the trees, or, with --require-sacd-extract, on any
unexplained difference from sacd_extract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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


# Where a cue sheet and an XML file name the writing program (docs/sacd_extract.md)
VERSION = re.compile(rb"(REM File created by SACD Extract, version: |\(created by sacd_extract, version: )[^\n)]*")


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


def _sans_version(data: bytes) -> bytes:
    """A cue sheet or XML file without the version that names the writing program."""
    return VERSION.sub(rb"\1", data, count=1)


def _first_difference(a: Path, b: Path) -> dict:
    """Where two files part, without loading them: sizes, first differing offset, bytes differing."""
    first, count, pos = None, 0, 0
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(1 << 20), fb.read(1 << 20)
            if not x and not y:
                break
            if x != y:
                for i, (p, q) in enumerate(zip(x, y)):
                    if p != q:
                        first = pos + i if first is None else first
                        count += 1
            pos += max(len(x), len(y))
    return {"sizes": (a.stat().st_size, b.stat().st_size), "first": first, "bytes": count}


def _dff_sans_machine(path: Path) -> str:
    """A DSDIFF file's hash without what may differ (docs/sacd_extract.md): the time the file was
    written, the comments after the first (the creating machine), the sizes of COMT and FRM8, which
    those change (FRM8's must still agree with the file), and the pad bytes after odd-length text
    and ID3 tags, which sacd_extract leaves uninitialised."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        end = f.seek(0, 2)
        f.seek(0)
        h.update(f.read(4))
        h.update(b"sizes agree" if int.from_bytes(f.read(8), "big") + 12 == end else b"FRM8's size is wrong")
        h.update(f.read(4))
        pos = 16
        while pos + 12 <= end:
            f.seek(pos)
            head = f.read(12)
            size = int.from_bytes(head[4:12], "big")
            h.update(head[:4])
            if head[:4] == b"COMT":
                h.update(f.read(2))
                f.seek(6, 1)
                rest = f.read(8)
                h.update(rest)
                h.update(f.read(int.from_bytes(rest[4:8], "big")))
            elif head[:4] == b"ID3 ":
                h.update(head[4:12])
                body = f.read(size)
                if body[:3] == b"ID3" and len(body) >= 10:   # the tag's own length, without the pad byte
                    n = (body[6] << 21) | (body[7] << 14) | (body[8] << 7) | body[9]
                    body = body[:10 + n]
                h.update(body)
            elif head[:4] == b"DIIN":
                h.update(head[4:12])
                body = f.read(size)
                q = 0
                while q + 12 <= len(body):
                    n = int.from_bytes(body[q + 4:q + 12], "big")
                    # DIAR and DITI: a 4-byte length, the text, and a pad byte within the chunk's size
                    text = 4 + int.from_bytes(body[q + 12:q + 16], "big") if body[q:q + 4] in (b"DIAR", b"DITI") else n
                    h.update(body[q:q + 12 + min(n, text)])
                    q += 12 + n + (n & 1)
            else:
                h.update(head[4:12])
                left = size + (size & 1)
                while left > 0:
                    block = f.read(min(left, 1 << 20))
                    if not block:
                        break
                    h.update(block)
                    left -= len(block)
            pos += 12 + size + (size & 1)
    return h.hexdigest()


def documented_only(ours: Path, theirs: Path) -> bool:
    """True if the files differ only where docs/sacd_extract.md says they do."""
    ext = ours.suffix.lower()
    if ext in (".cue", ".xml"):
        return _sans_version(ours.read_bytes()) == _sans_version(theirs.read_bytes())
    return ext == ".dff" and _dff_sans_machine(ours) == _dff_sans_machine(theirs)


def against_sacd_extract(root: Path, se_root: Path, files: dict[str, str], se_files: dict[str, str],
                         hide_names: bool) -> dict:
    """Per file format, how a tree compares with sacd_extract's."""
    out: dict[str, dict] = {}

    def entry(name: str) -> dict:
        return out.setdefault(Path(name).suffix.lower().lstrip(".") or "(none)",
                              {"theirs": 0, "identical": 0, "documented": 0, "different": []})

    for name, h in se_files.items():
        e = entry(name)
        e["theirs"] += 1
        if name not in files:
            e["different"].append({"file": "" if hide_names else name, "why": "only sacd_extract wrote it"})
        elif files[name] == h:
            e["identical"] += 1
        elif documented_only(root / name, se_root / name):
            e["documented"] += 1
        else:
            d = _first_difference(root / name, se_root / name)
            e["different"].append({"file": "" if hide_names else name, **d})
    for name in files.keys() - se_files.keys():
        entry(name)["different"].append({"file": "" if hide_names else name, "why": "only pysacd wrote it"})
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
            keep: Path | None, hide_names: bool = False) -> dict:
    kind, args = MODES[mode]
    if "LAST" in args or "MIDDLE" in args:
        n = last_track(srcs["new"], image, "m" if "-m" in args else "2")
        args = [str(n) if a == "LAST" else str(max(1, (n + 1) // 2)) if a == "MIDDLE" else a for a in args]
    work = Path(tempfile.mkdtemp(prefix="cmp-", dir=keep))
    vs = {}
    try:
        same = srcs["old"] == srcs["new"]       # one tree, compared with sacd_extract only: run it once
        res = {w: one(kind, w, image, args, work, srcs, sacd_extract, threads)
               for w in (("new", "sacd_extract") if same else ("old", "new", "sacd_extract"))}
        if same:
            res["old"] = res["new"]
        if res["sacd_extract"] is not None:
            vs = {w: against_sacd_extract(work / w, work / "sacd_extract", res[w]["files"], res["sacd_extract"]["files"],
                                          hide_names) for w in (("new",) if same else ("old", "new"))}
            if same:
                vs["old"] = vs["new"]
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
        # The header before the disc information names the program (docs/sacd_extract.md)
        body = lambda t: t[t.find("Disc Information:"):] if "Disc Information:" in t else ""
        r["print_matches_sacd_extract"] = bool(body(se["stdout"])) and body(se["stdout"]) == body(new["stdout"]) or None
        r["rc_sacd_extract"] = se["rc"]
        r["vs_sacd_extract"] = vs
        r["seconds"] = (*r["seconds"], round(se["seconds"], 2))
    return r


def sacd_extract_problems(r: dict, real_disc: bool = False) -> list[str]:
    """What --require-sacd-extract fails on. A comparison that compared nothing is a failure too:
    a sacd_extract that couldn't run must never read as "no differences". On a real disc, -b modes
    are reported but don't fail: where a pause falls inside a sector, sacd_extract repeats frames
    in both tracks and pysacd cuts on the timecode (docs/sacd_extract.md)."""
    kind = MODES[r["mode"]][0]
    if kind != "extract":
        return []                               # pysacd's own extensions: nothing to compare with
    if "rc_sacd_extract" not in r:
        return ["no sacd_extract to compare with"]
    out = []
    if r["rc_sacd_extract"] != r["rc"][1]:
        out.append(f"exit status {r['rc'][1]}, sacd_extract's {r['rc_sacd_extract']}")
    if r["mode"] == "print" and not r["print_matches_sacd_extract"]:
        out.append("-P text differs from sacd_extract")
    # A disc with no multichannel area gives neither program anything to write in a -m mode
    nothing_to_write = "-m" in MODES[r["mode"]][1] and not r["files"]
    if r["mode"] != "print" and not r["sacd_extract_files"] and not nothing_to_write:
        out.append("sacd_extract wrote nothing")
    if real_disc and "-b" in MODES[r["mode"]][1]:
        return out
    for fmt, e in r["vs_sacd_extract"]["new"].items():
        if e["different"]:
            out.append(f"{len(e['different'])} {fmt} file(s) differ from sacd_extract")
    return out


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
    ap.add_argument("--require-sacd-extract", action="store_true",
                    help="also fail on any difference from sacd_extract the docs don't explain")
    args = ap.parse_args(argv)
    srcs = {"old": str(Path(args.old).resolve()), "new": str(Path(args.new).resolve())}
    bad = 0
    for n, image in enumerate(args.images, 1):
        for mode in args.modes.split(","):
            r = compare(image.resolve(), mode, srcs, args.sacd_extract, args.threads, args.keep, args.no_values)
            r["image"] = f"image {n}" if args.no_values else str(image)
            if args.no_values:
                r["problems"] = [p.split(":", 1)[0] for p in r["problems"]]
            if args.require_sacd_extract:
                r["problems"] += sacd_extract_problems(r, real_disc=True)
            bad += bool(r["problems"])
            print(json.dumps(r, ensure_ascii=False), flush=True)
    print(f"{bad} run(s) with differences", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
