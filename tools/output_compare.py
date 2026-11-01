#!/usr/bin/env python3
"""pysacd's DSF and DSDIFF files against scarletbook's output API (#94).

    python3 tools/output_compare.py [--modes NAME,...] [--no-values] [--keep DIR] IMAGE...

Runs `pysacd-extract` (the tree this script belongs to, or PYTHONPATH) in each mode below, then
asks scarletbook for the layout of every file it wrote (`sb_output_layout_new`, `_new_dst`,
through ctypes on the engine library) with the writer's choices closest to pysacd's, and
compares the two region by region:

- DSF: scarletbook's header against the file's first bytes, its size against the file's, with
  the file's own ID3 tag given as the raw tag (`sb_output_options_set_raw_tag`). Separately, the
  tag scarletbook makes from the TOC (`sb_tags_from_toc`) against pysacd's (sacd_extract's), by
  ID3 frame.
- DSDIFF: chunk by chunk (FVER, PROP and its chunks, the sound data chunk's header, DSTI, DIIN
  and its chunks, COMT), with pysacd's markers and comments given as the caller's own. Chunks
  only one side writes are listed.

It reports counts per mode and the kinds of difference. --no-values leaves out file names, for
private discs. Exit status 1 when a DSF header differs (DSDIFF differences are expected, see
docs/development.md, "Output through scarletbook").
"""

from __future__ import annotations

import argparse
import collections
import ctypes as C
import difflib
import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pysacd import _sbtoc
from pysacd.sbcompat import _flags4
from pysacd.toc import read_toc, track_range

MODES = {
    "dsf": ["-2", "-s"],
    "dsf-pauses": ["-2", "-s", "-b"],
    "dsf-id3-3": ["-2", "-s", "--id3tag", "3"],
    "dsf-id3-0": ["-2", "-s", "--id3tag", "0"],
    "mch-dsf": ["-m", "-s"],
    "dsdiff": ["-2", "-p"],
    "dsdiff-c": ["-2", "-p", "-c"],
    "edit-master": ["-2", "-e"],
    "edit-master-c": ["-2", "-e", "-c"],
    "mch-dsdiff": ["-m", "-p"],
}

H, S, P, U32, SIZE = C.c_void_p, C.c_int32, C.POINTER, C.c_uint32, C.c_size_t
DSF, DFF_DSD, DFF_DST = 0, 1, 2
# SbOutputOption
UNSPECIFIED_CHANNEL_TYPE, LSCO_UNSPECIFIED = 1, 2
DSF_FIELDS = {"file size": (12, 20), "metadata pointer": (20, 28), "channel type": (48, 52),
              "channel count": (52, 56), "sample count": (64, 72), "data size": (84, 92)}
TEXT_PAD_COUNTED, DIIN, ARTIST_TITLE, SOURCE_TRACK, DST_INDEX_OFFSET, TAGS = 13, 10, 14, 8, 7, 18
ORDER_LOCAL = 1
FOOTER, TERMINATE_LAST = 21, 24


class Info(C.Structure):
    _fields_ = [("size", C.c_uint64), ("memory", C.c_uint64)] + [(n, C.c_uint32) for n in (
        "format", "slot", "first", "end", "channels", "source_track", "input", "regions")]


class Region(C.Structure):
    _fields_ = [("offset", C.c_uint64), ("len", C.c_uint64), ("kind", C.c_uint32), ("reserved", C.c_uint32)]


class Comment(C.Structure):
    _fields_ = [("text", C.c_char_p), ("year", C.c_uint32), ("kind", C.c_uint32), ("reference", C.c_uint32),
                ("month", C.c_uint8), ("day", C.c_uint8), ("hour", C.c_uint8), ("minutes", C.c_uint8)]


class Marker(C.Structure):
    _fields_ = [("text", C.c_char_p), ("frame", C.c_uint32), ("kind", C.c_uint32),
                ("track_flags", C.c_uint32), ("reserved", C.c_uint32)]


def _bind():
    lib = _sbtoc._lib()
    for name, res, *args in (
            ("sb_disc_open_file", S, C.c_char_p, P(H)),
            ("sb_output_options_new", S, P(H)), ("sb_output_options_free", None, H),
            ("sb_output_options_set", S, H, U32, C.c_int64),
            ("sb_output_options_set_order", S, H, U32, P(U32), SIZE),
            ("sb_output_options_set_raw_tag", S, H, C.c_char_p, SIZE),
            ("sb_output_options_add_comment", S, H, P(Comment)),
            ("sb_output_options_add_marker", S, H, P(Marker)),
            ("sb_output_layout_new", S, H, U32, U32, U32, U32, H, P(H)),
            ("sb_output_layout_new_dst", S, H, H, H, P(H)),
            ("sb_output_layout_info", S, H, P(Info)),
            ("sb_output_layout_region", S, H, U32, P(Region)),
            ("sb_output_layout_read", S, H, H, C.c_uint64, C.c_char_p, SIZE, P(SIZE)),
            ("sb_output_layout_free", None, H),
            ("sb_tags_from_toc", S, H, U32, U32, H, P(H)), ("sb_tags_free", None, H),
            ("sb_output_options_set_tags", S, H, H),
            ("sb_dst_lengths_scan", S, H, U32, U32, U32, U32, P(H)), ("sb_dst_lengths_free", None, H)):
        f = getattr(lib, name)
        f.restype, f.argtypes = res, list(args)
    return lib


lib = _bind()


def _ok(status: int, what: str) -> None:
    if status < 0:
        raise RuntimeError(f"{what}: {(lib.sb_last_error_message() or b'').decode(errors='replace')}")


class Layout:
    """A scarletbook layout: size, regions, and the bytes of its header and trailer."""

    def __init__(self, disc, slot, first, end, fmt, opts: dict, raw_tag=None, comments=(), markers=(),
                 local_order=None, toc_tags_track=None):
        o = H()
        _ok(lib.sb_output_options_new(C.byref(o)), "options")
        keep = []
        try:
            for k, v in opts.items():
                _ok(lib.sb_output_options_set(o, k, v), f"option {k}")
            if local_order:
                arr = (U32 * len(local_order))(*local_order)
                _ok(lib.sb_output_options_set_order(o, ORDER_LOCAL, arr, len(local_order)), "order")
            if raw_tag is not None:
                _ok(lib.sb_output_options_set_raw_tag(o, raw_tag, len(raw_tag)), "raw tag")
            for c in comments:
                _ok(lib.sb_output_options_add_comment(o, C.byref(c)), "comment")
            for m in markers:
                _ok(lib.sb_output_options_add_marker(o, C.byref(m)), "marker")
            lay, lengths = H(), H()
            if fmt == DFF_DST:
                _ok(lib.sb_dst_lengths_scan(disc, slot, first, end, 0, C.byref(lengths)), "DST lengths")
                _ok(lib.sb_output_layout_new_dst(disc, lengths, o, C.byref(lay)), "DST layout")
            else:
                _ok(lib.sb_output_layout_new(disc, slot, first, end, fmt, o, C.byref(lay)), "layout")
            keep.append(lay)
            info = Info()
            _ok(lib.sb_output_layout_info(lay, C.byref(info)), "info")
            self.size = info.size
            self.regions = []
            for k in range(info.regions):
                r = Region()
                _ok(lib.sb_output_layout_region(lay, k, C.byref(r)), "region")
                self.regions.append((r.offset, r.len, r.kind))
            self.header = self._read(lay, disc, 0, self.regions[0][1])
            t = self.regions[-1]
            self.trailer = self._read(lay, disc, t[0], t[1]) if t[2] == 2 else b""
            lib.sb_dst_lengths_free(lengths)
        finally:
            for lay in keep:
                lib.sb_output_layout_free(lay)
            lib.sb_output_options_free(o)

    @staticmethod
    def _read(lay, disc, offset: int, n: int) -> bytes:
        buf, got = C.create_string_buffer(n), SIZE()
        _ok(lib.sb_output_layout_read(lay, disc, offset, buf, n, C.byref(got)), "read")
        return buf.raw[:got.value]


def toc_tag(disc, slot: int, track: int) -> bytes:
    """The DSF tag scarletbook makes from the TOC for a file of one track (version 2.4, no footer:
    the nearest to pysacd's)."""
    o, tags = H(), H()
    _ok(lib.sb_output_options_new(C.byref(o)), "options")
    try:
        _ok(lib.sb_output_options_set(o, FOOTER, 0), "footer")
        _ok(lib.sb_output_options_set(o, TERMINATE_LAST, 1), "terminators")
        _ok(lib.sb_tags_from_toc(disc, slot, track, o, C.byref(tags)), "tags")
        _ok(lib.sb_output_options_set_tags(o, tags), "set tags")
        lay = Layout.__new__(Layout)
        info_lay = H()
        _ok(lib.sb_output_layout_new(disc, slot, 0, 1, DSF, o, C.byref(info_lay)), "layout")
        try:
            info = Info()
            _ok(lib.sb_output_layout_info(info_lay, C.byref(info)), "info")
            r = Region()
            _ok(lib.sb_output_layout_region(info_lay, info.regions - 1, C.byref(r)), "region")
            return lay._read(info_lay, disc, r.offset, r.len)
        finally:
            lib.sb_output_layout_free(info_lay)
    finally:
        lib.sb_tags_free(tags)
        lib.sb_output_options_free(o)


def id3_frames(tag: bytes) -> list[tuple[str, bytes]]:
    if tag[:3] != b"ID3":
        return []
    v24, out, pos = tag[3] == 4, [], 10
    end = 10 + sum((b & 0x7F) << (7 * (3 - i)) for i, b in enumerate(tag[6:10]))
    while pos + 10 <= min(end, len(tag)) and tag[pos:pos + 4].strip(b"\0"):
        raw = tag[pos + 4:pos + 8]
        n = sum((b & 0x7F) << (7 * (3 - i)) for i, b in enumerate(raw)) if v24 else int.from_bytes(raw, "big")
        out.append((tag[pos:pos + 4].decode("latin-1"), tag[pos + 10:pos + 10 + n]))
        pos += 10 + n
    return out


def dff_chunks(b: bytes, pos: int = 16, end: int | None = None, depth: int = 0, out=None):
    out = [] if out is None else out
    end = len(b) if end is None else end
    while pos + 12 <= end:
        cid, n = b[pos:pos + 4], struct.unpack(">Q", b[pos + 4:pos + 12])[0]
        out.append((("  " * depth) + cid.decode("latin-1"), pos, n))
        if cid in (b"PROP", b"DIIN"):
            dff_chunks(b, pos + 12 + (4 if cid == b"PROP" else 0), min(pos + 12 + n, end), depth + 1, out)
        pos += 12 + n + (n & 1)
    return out


def dff_parts(b: bytes, chunks) -> list[tuple[str, bytes]]:
    out = []
    for name, pos, n in chunks:
        cid = name.strip()
        whole = cid in ("DSD", "DST", "PROP", "DIIN")      # their children are compared one by one
        out.append((name, b[pos:pos + 12] if whole else b[pos:pos + 12 + n]))
    return out


def our_comments(b: bytes, chunks) -> list[Comment]:
    out = []
    for name, pos, _n in chunks:
        if name != "COMT":
            continue
        p = pos + 12
        for _ in range(struct.unpack(">H", b[p:p + 2])[0]):
            y, mo, d, h, mi, kind, ref, cnt = struct.unpack(">HBBBBHHI", b[p + 2:p + 16])
            out.append(Comment(b[p + 16:p + 16 + cnt], y, kind, ref, mo, d, h, mi))
            p += 14 + cnt + (cnt & 1)
    return out


def our_markers(area, track_idx: int | None, first: int, end: int) -> list[Marker]:
    """pysacd's (sacd_extract's) markers as the caller's own: an edit master's program start and
    track starts and stops, a track file's one index marker at its end."""
    if track_idx is not None:
        return [Marker(None, end, 4, 0, 0)]
    out, tracks = [], area.tracks
    for i, t in enumerate(tracks):
        a = t.start.total_frames
        b = a + t.duration.total_frames
        if i == 0:
            out.append(Marker(None, 0, 2, _flags4(t.start_flags), 0))
        out.append(Marker(None, a, 0, _flags4(t.start_flags), 0))
        if i == len(tracks) - 1 or tracks[i + 1].start.total_frames > b:
            out.append(Marker(None, b, 1, _flags4(t.flags), 0))
    return out


def files_of(work: Path, ext: str) -> list[Path]:
    return sorted(p for p in work.rglob(f"*.{ext}") if p.is_file())


def compare_image(image: Path, modes: list[str], keep: Path | None, counts: dict) -> None:
    disc_toc = read_toc(image)
    d = H()
    _ok(lib.sb_disc_open_file(os.fsencode(image), C.byref(d)), "open")
    # each Area's scarletbook slot, from the TOC layout (areas in sb_disc_area order, as read_toc)
    lay = _sbtoc._Layout(_sbtoc._toc_bytes(lib, d))
    slots = [_sbtoc._AREA_INFO.unpack_from(lay.data, lay.areas_offset + k * lay.sizes[1])[0]
             for k in range(lay.area_count)]
    try:
        for mode in modes:
            args = MODES[mode]
            area = disc_toc.multichannel if "-m" in args else disc_toc.stereo
            if area is None:
                continue
            slot = slots[disc_toc.areas.index(area)]
            with tempfile.TemporaryDirectory(dir=keep) as tmp:
                env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
                subprocess.run([sys.executable, "-m", "pysacd.extract", "--no-cfg", *args, "-i", str(image)],
                               cwd=tmp, env=env, capture_output=True, check=False)
                c = counts[mode]
                if "-s" in args:
                    _dsf(d, area, slot, mode, files_of(Path(tmp), "dsf"), c, disc_toc)
                else:
                    _dff(d, area, slot, mode, files_of(Path(tmp), "dff"), c, args)
    finally:
        lib.sb_disc_free(d)


def _dsf(d, area, slot, mode, files, c, disc_toc) -> None:
    trimming = "-b" not in MODES[mode]
    for i, f in enumerate(files):
        b = f.read_bytes()
        meta = int.from_bytes(b[20:28], "little")
        tag = b[meta:] if meta else None
        first, end = track_range(area, i, trimming)
        c["files"] += 1
        try:
            # an area of unspecified layout: the channel type pysacd wrote (W2)
            opts = {UNSPECIFIED_CHANNEL_TYPE: int.from_bytes(b[48:52], "little")}
            if not tag:
                opts[TAGS] = 1
            lay = Layout(d, slot, first, end, DSF, opts, raw_tag=tag)
        except RuntimeError as e:
            c[f"refused: {str(e).split(': ', 1)[-1]}"] += 1
            continue
        same = lay.header == b[:len(lay.header)] and lay.size == len(b)
        c["identical" if same else "header differs"] += 1
        if not same:
            fields = {k for k in range(min(len(lay.header), len(b))) if lay.header[k] != b[k]}
            for name, (lo, hi) in DSF_FIELDS.items():
                if fields & set(range(lo, hi)):
                    c[f"header: {name} differs"] += 1
            if lay.size != len(b):
                c["size differs"] += 1
        if mode == "dsf":
            ours = id3_frames(tag or b"")
            theirs = id3_frames(toc_tag(d, slot, i + 1))
            if ours == theirs:
                c["TOC tag identical"] += 1
            else:
                c["TOC tag differs"] += 1
                o, t = dict(ours), dict(theirs)
                for fid in sorted(set(o) | set(t)):
                    if fid not in t:
                        c[f"TOC tag: only pysacd writes {fid}"] += 1
                    elif fid not in o:
                        c[f"TOC tag: only scarletbook writes {fid}"] += 1
                    elif o[fid] != t[fid]:
                        c[f"TOC tag: {fid} differs"] += 1
                if [x for x, _ in ours if x in t] != [x for x, _ in theirs if x in o]:
                    c["TOC tag: frame order differs"] += 1


def _dff(d, area, slot, mode, files, c, args) -> None:
    edit_master = "-e" in args
    dst = "-c" not in args and area.is_dst
    for i, f in enumerate(files):
        b = f.read_bytes()
        ours = dff_chunks(b)
        if edit_master:
            first, end, idx = 0, area.total_playtime.total_frames, None
        else:
            first, end = track_range(area, i, True)
            idx = i
        # a track file's DIAR and DITI come from its track, an edit master's from the album
        opts = {DIIN: 1, ARTIST_TITLE: 1, TEXT_PAD_COUNTED: 1, SOURCE_TRACK: 0 if idx is None else idx + 1, LSCO_UNSPECIFIED: 1,
                DST_INDEX_OFFSET: 1}
        order = [0, 1, 2, 3, 4] if dst else [0, 1, 3, 4]
        c["files"] += 1
        try:
            lay = Layout(d, slot, first, end, DFF_DST if dst else DFF_DSD, opts, comments=our_comments(b, ours),
                         markers=our_markers(area, idx, first, end), local_order=order)
        except RuntimeError as e:
            c[f"refused: {str(e).split(': ', 1)[-1]}"] += 1
            continue
        sound = next(r for r in lay.regions if r[2] == 1)
        whole = lay.header + bytes(sound[1]) + lay.trailer
        A = dff_parts(whole, dff_chunks(whole))
        B = dff_parts(b, ours)
        diffs = set()
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, A, B, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            sa = collections.Counter(n.strip() for n, _ in A[i1:i2])
            sb = collections.Counter(n.strip() for n, _ in B[j1:j2])
            for n in sorted(set(sa) | set(sb)):
                if not sb[n]:
                    diffs.add(f"only scarletbook writes {n}")
                elif not sa[n]:
                    diffs.add(f"only pysacd writes {n}")
                else:
                    diffs.add(f"{n} differs")
        c["identical" if not diffs else "differs"] += 1
        for x in diffs:
            c[x] += 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--no-values", action="store_true")
    ap.add_argument("--keep", type=Path, help="work under this directory (large outputs: not a tmpfs)")
    args = ap.parse_args(argv)
    modes = args.modes.split(",")
    counts: dict = {m: collections.Counter() for m in modes}
    for n, image in enumerate(args.images, 1):
        try:
            compare_image(image.resolve(), modes, args.keep, counts)
        except Exception as e:  # noqa: BLE001 - report and go on with the next image
            print(f"{'image ' + str(n) if args.no_values else image}: {type(e).__name__}: {e}", file=sys.stderr)
    bad = 0
    for mode, c in counts.items():
        print(json.dumps({"mode": mode, **dict(sorted(c.items()))}))
        bad += c["header differs"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
