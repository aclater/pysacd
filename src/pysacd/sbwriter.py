"""DSF and DSDIFF files through scarletbook's output API (#94).

scarletbook lays out every byte of a DSF or DSDIFF file that isn't audio, from the TOC alone
(`sb_output_layout_new`, `sb_output_layout_new_dst`, `sb_output_dsf_chain_plan`). pysacd asks it
for the header and the trailer of each file, with the writer's choices that give
`sacd_extract`'s bytes (scarletbook-rs#169, in scarletbook 0.5.4), and the engine writes them
around the audio it streams. pysacd has no DSF or DSDIFF writer of its own since 0.4.0.

What stays pysacd's is content, not layout: the ID3 tag `sacd_extract` makes from the TOC
(`sbcompat.id3_tag`, passed as the raw tag of a DSF file and as `ID3 ` caller chunks of a
DSDIFF file), and the markers and comments `sacd_extract` writes (passed as the caller's own).
See docs/development.md, "Output through scarletbook".
"""

from __future__ import annotations

import ctypes as C
import datetime
import threading
from dataclasses import dataclass
from typing import Any

from . import _sbtoc
from .toc import Area, Disc, SACDImage

H, S, P, U32, SIZE = C.c_void_p, C.c_int32, C.POINTER, C.c_uint32, C.c_size_t

FRAME_BYTES_PER_CH = 4704
FRAME_RATE = 75

# SbOutputFormat
DSF, DSDIFF_DSD, DSDIFF_DST = 0, 1, 2
# SbOutputRegionKind
HEADER, SOUND_DATA, TRAILER = 0, 1, 2
# SbOutputOption
DSF_UNSPECIFIED_CHANNEL_TYPE = 1
DSDIFF_UNSPECIFIED_LSCO = 2
DSDIFF_DST_INDEX_OFFSET = 7
SOURCE_TRACK = 8
DSDIFF_INFO = 10
DSDIFF_TEXT_PAD_COUNTED = 13
DSDIFF_ARTIST_TITLE = 14
TAGS = 18
DSDIFF_EDITED_MASTER_ID_WRITTEN = 33
DSDIFF_ARTIST_TITLE_SOURCE = 34
DSDIFF_DEPARTURE_D1 = 35
DSDIFF_DEPARTURE_D2 = 36
DSF_CHAIN_FIRST = 37
DSF_CHAIN_END = 38
# SbOutputOrder
ORDER_LOCAL = 1

# sacd_extract's DSF channel type by channel count (W2, for an area of unspecified layout)
_DSF_CHANNEL_TYPE = {1: 1, 2: 2, 3: 3, 4: 5, 5: 6, 6: 7}


class _Info(C.Structure):
    _fields_ = [("size", C.c_uint64), ("memory", C.c_uint64)] + [(n, C.c_uint32) for n in (
        "format", "slot", "first", "end", "channels", "source_track", "input", "regions")]


class _Region(C.Structure):
    _fields_ = [("offset", C.c_uint64), ("len", C.c_uint64), ("kind", C.c_uint32), ("reserved", C.c_uint32)]


class _Comment(C.Structure):
    _fields_ = [("text", C.c_char_p), ("year", C.c_uint32), ("kind", C.c_uint32), ("reference", C.c_uint32),
                ("month", C.c_uint8), ("day", C.c_uint8), ("hour", C.c_uint8), ("minutes", C.c_uint8)]


class _Marker(C.Structure):
    _fields_ = [("text", C.c_char_p), ("frame", C.c_uint32), ("kind", C.c_uint32),
                ("track_flags", C.c_uint32), ("reserved", C.c_uint32)]


class _ChainFile(C.Structure):
    _fields_ = [("byte_first", C.c_uint64), ("byte_end", C.c_uint64)] + [(n, C.c_uint32) for n in (
        "first", "end", "chain_first", "chain_end")] + [("status", C.c_int32), ("reserved", C.c_uint32)]


_SIZES = {_Info: 48, _Region: 24, _ChainFile: 40}
_bound: Any = None
_bind_lock = threading.Lock()


def _lib():
    global _bound
    with _bind_lock:
        if _bound is not None:
            return _bound
        lib = _sbtoc._lib()
        for struct, size in _SIZES.items():
            if C.sizeof(struct) != size:
                raise RuntimeError(f"{struct.__name__} is {C.sizeof(struct)} bytes, scarletbook.h says {size}")
        for name, res, *args in (
                ("sb_output_options_new", S, P(H)), ("sb_output_options_free", None, H),
                ("sb_output_options_set", S, H, U32, C.c_int64),
                ("sb_output_options_set_order", S, H, U32, P(U32), SIZE),
                ("sb_output_options_set_raw_tag", S, H, C.c_char_p, SIZE),
                ("sb_output_options_add_comment", S, H, P(_Comment)),
                ("sb_output_options_add_marker", S, H, P(_Marker)),
                ("sb_output_options_add_chunk", S, H, C.c_char_p, C.c_char_p, SIZE),
                ("sb_output_layout_new", S, H, U32, U32, U32, U32, H, P(H)),
                ("sb_output_layout_new_dst", S, H, H, H, P(H)),
                ("sb_output_layout_info", S, H, P(_Info)),
                ("sb_output_layout_region", S, H, U32, P(_Region)),
                ("sb_output_layout_fill", S, H, C.c_uint64, C.c_char_p, SIZE, C.c_void_p, SIZE, P(SIZE)),
                ("sb_output_layout_free", None, H),
                ("sb_output_dsf_chain_plan", S, H, U32, P(U32), SIZE, P(_ChainFile), SIZE, P(SIZE)),
                ("sb_dst_lengths_load", S, H, U32, U32, U32, U32, P(U32), SIZE, P(H)),
                ("sb_dst_lengths_free", None, H)):
            f = getattr(lib, name)
            f.restype, f.argtypes = res, list(args)
        _bound = lib
        return lib


class OutputError(RuntimeError):
    """scarletbook refused a layout pysacd asked for."""


def _ok(lib, status: int, what: str) -> None:
    try:
        _sbtoc._check(lib, status, what)
    except _sbtoc.SbFailure as e:
        raise OutputError(str(e)) from None


def slot(area: Area) -> int:
    """The area's scarletbook slot (0 stereo, 1 multichannel), as the TOC was read."""
    if area.slot < 0:
        raise OutputError("the area wasn't read by scarletbook: no slot")
    return area.slot


@dataclass
class Layout:
    """A file's size and the bytes before and after its sound data."""
    size: int
    header: bytes
    sound_offset: int
    sound_len: int
    trailer: bytes


class _Options:
    def __init__(self, lib):
        self.lib, self.h, self._keep = lib, H(), []
        _ok(lib, lib.sb_output_options_new(C.byref(self.h)), "output options")

    def set(self, option: int, value: int) -> None:
        _ok(self.lib, self.lib.sb_output_options_set(self.h, option, value), f"output option {option}")

    def order(self, which: int, order: list[int]) -> None:
        arr = (U32 * len(order))(*order)
        _ok(self.lib, self.lib.sb_output_options_set_order(self.h, which, arr, len(order)), "chunk order")

    def raw_tag(self, tag: bytes) -> None:
        _ok(self.lib, self.lib.sb_output_options_set_raw_tag(self.h, tag, len(tag)), "raw tag")

    def comment(self, c: _Comment) -> None:
        self._keep.append(c)
        _ok(self.lib, self.lib.sb_output_options_add_comment(self.h, C.byref(c)), "comment")

    def marker(self, m: _Marker) -> None:
        _ok(self.lib, self.lib.sb_output_options_add_marker(self.h, C.byref(m)), "marker")

    def chunk(self, cid: bytes, data: bytes) -> None:
        _ok(self.lib, self.lib.sb_output_options_add_chunk(self.h, cid, data, len(data)), "caller chunk")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.lib.sb_output_options_free(self.h)


def _layout(lib, lay) -> Layout:
    try:
        info = _Info()
        _ok(lib, lib.sb_output_layout_info(lay, C.byref(info)), "layout info")
        regions = []
        for k in range(info.regions):
            r = _Region()
            _ok(lib, lib.sb_output_layout_region(lay, k, C.byref(r)), "layout region")
            regions.append((r.offset, r.len, r.kind))

        def fill(offset: int, n: int) -> bytes:
            buf, got = C.create_string_buffer(n), SIZE()
            _ok(lib, lib.sb_output_layout_fill(lay, offset, buf, n, None, 0, C.byref(got)), "layout bytes")
            return buf.raw[:got.value]

        sound = [r for r in regions if r[2] == SOUND_DATA]
        if len(sound) != 1 or regions[0][2] != HEADER or regions[0][0] != 0:
            raise OutputError(f"unexpected layout regions {regions}")
        trailer = b"".join(fill(o, n) for o, n, kind in regions if kind == TRAILER)
        return Layout(info.size, fill(0, regions[0][1]), sound[0][0], sound[0][1], trailer)
    finally:
        lib.sb_output_layout_free(lay)


def _disc(img: SACDImage):
    return img._disc._disc


# ------------------------------------------------------------ DSF

@dataclass
class ChainFile:
    """One DSF file of a gapless chain (-z): its range and the bytes per channel it holds."""
    first: int
    end: int
    chain_first: int
    chain_end: int
    head_bytes: int     # bytes per channel carried in from frame first - 1
    tail_bytes: int     # bytes per channel of frame end - 1 left to the next file


def dsf_chain(img: SACDImage, area: Area, bounds: list[int]) -> list[ChainFile]:
    """scarletbook's plan of the DSF files of consecutive ranges [bounds[i], bounds[i + 1])."""
    lib = _lib()
    n = len(bounds) - 1
    files, got = (_ChainFile * n)(), SIZE()
    arr = (U32 * len(bounds))(*bounds)
    _ok(lib, lib.sb_output_dsf_chain_plan(_disc(img), slot(area), arr, len(bounds), files, n, C.byref(got)),
        "DSF chain plan")
    out = []
    for f in files[:got.value]:
        _ok(lib, f.status, f"DSF chain range {f.first}-{f.end}")
        out.append(ChainFile(f.first, f.end, f.chain_first, f.chain_end,
                             f.first * FRAME_BYTES_PER_CH - f.byte_first, f.end * FRAME_BYTES_PER_CH - f.byte_end))
    return out


def dsf(img: SACDImage, area: Area, first: int, end: int, tag: bytes,
        chain: ChainFile | None = None) -> Layout:
    """A DSF file of frames [first, end) with `tag` (an ID3v2.3 or v2.4 tag, or b"" for none) as
    its metadata, as sacd_extract writes it; with `chain`, the byte range of a gapless chain."""
    lib = _lib()
    with _Options(lib) as o:
        o.set(DSF_UNSPECIFIED_CHANNEL_TYPE, _DSF_CHANNEL_TYPE.get(area.channel_count, 0))
        if tag:
            o.raw_tag(tag)
        else:
            o.set(TAGS, 1)
        if chain is not None:
            o.set(DSF_CHAIN_FIRST, chain.chain_first)
            o.set(DSF_CHAIN_END, chain.chain_end)
        lay = H()
        _ok(lib, lib.sb_output_layout_new(_disc(img), slot(area), first, end, DSF, o.h, C.byref(lay)), "DSF layout")
    return _layout(lib, lay)


# ------------------------------------------------------------ DSDIFF

def _flags4(flag_byte: int) -> int:
    """A TOC track's flag byte as DSDIFF track flags (tmf4, tmf1, tmf2, tmf3)."""
    return ((flag_byte >> 6) & 1) | ((flag_byte >> 3) & 1) << 1 | ((flag_byte >> 4) & 1) << 2 | \
           ((flag_byte >> 5) & 1) << 3


def _markers(area: Area, track_idx: int | None, first: int, end: int) -> list[_Marker]:
    """sacd_extract's markers: an edited master's program start and its track starts and stops; a
    track file's one index marker at the track's duration."""
    if track_idx is not None:
        return [_Marker(None, min(first + area.tracks[track_idx].duration.total_frames, end), 4, 0, 0)]
    out, tracks = [], area.tracks
    for i, t in enumerate(tracks):
        a = t.start.total_frames
        b = a + t.duration.total_frames
        if i == 0:
            out.append(_Marker(None, 0, 2, _flags4(t.start_flags), 0))
        out.append(_Marker(None, a, 0, _flags4(t.start_flags), 0))
        if i == len(tracks) - 1 or tracks[i + 1].start.total_frames > b:
            out.append(_Marker(None, b, 1, _flags4(t.flags), 0))
    # frames past a DST range that lost unreadable frames can't be marked (docs/development.md)
    return [m for m in out if m.frame <= end]


def _comments(disc: Disc, now: datetime.datetime, machine: str) -> list[_Comment]:
    """sacd_extract's two file-history comments: the disc's date, and the creating machine with
    the month counted from 0."""
    from .sbcompat import _first, _s
    title = _s(_first(disc, "disc_title", "album_title") or b"Unknown")
    y, m, d = disc.date
    return [_Comment(("Material ripped from SACD: " + title).encode("utf-8", "ignore"), y, 3, 0, m, d, 0, 0),
            _Comment(machine.encode("utf-8", "ignore"), now.year, 3, 2, now.month - 1, now.day, now.hour,
                     now.minute)]


def dsdiff(img: SACDImage, disc: Disc, area: Area, track_idx: int | None, first: int, end: int, *,
           id3_tags: list[bytes], now: datetime.datetime, machine: str,
           dst_lengths: list[int] | None = None) -> Layout:
    """A DSDIFF file of frames [first, end) as sacd_extract writes it: plain DSD, or with
    `dst_lengths` the DST frames of those lengths (one per frame from `first`). track_idx None
    is an edited master. `id3_tags` go in `ID3 ` chunks after `COMT`."""
    lib = _lib()
    dst = dst_lengths is not None
    lengths_in = list(dst_lengths or ())
    with _Options(lib) as o:
        o.set(DSDIFF_INFO, 1)
        o.set(DSDIFF_ARTIST_TITLE, 1)
        o.set(DSDIFF_TEXT_PAD_COUNTED, 1)
        o.set(DSDIFF_UNSPECIFIED_LSCO, 1)
        o.set(DSDIFF_DST_INDEX_OFFSET, 1)
        o.set(DSDIFF_DEPARTURE_D1, 1)       # CMPR counts its pad byte
        o.set(DSDIFF_DEPARTURE_D2, 1)       # text as ISO 8859-1
        o.set(SOURCE_TRACK, 0 if track_idx is None else track_idx + 1)
        if track_idx is not None:
            o.set(DSDIFF_EDITED_MASTER_ID_WRITTEN, 0)
            o.set(DSDIFF_ARTIST_TITLE_SOURCE, 2)     # the track's, or else the album's
        # PROP, sound data, DSTI, DIIN, COMT
        o.order(ORDER_LOCAL, [0, 1, 2, 3, 4] if dst else [0, 1, 3, 4])
        for c in _comments(disc, now, machine):
            o.comment(c)
        if dst:
            end = first + len(lengths_in)
        for m in _markers(area, track_idx, first, end):
            o.marker(m)
        for tag in id3_tags:
            # sacd_extract counts the pad byte of an odd tag in the chunk's size
            o.chunk(b"ID3 ", tag + b"\0" * (len(tag) & 1))
        lay = H()
        if dst:
            lengths = H()
            arr = (U32 * max(1, len(lengths_in)))(*lengths_in)
            _ok(lib, lib.sb_dst_lengths_load(_disc(img), slot(area), first, end, 0, arr, len(lengths_in),
                                             C.byref(lengths)), "DST lengths")
            try:
                _ok(lib, lib.sb_output_layout_new_dst(_disc(img), lengths, o.h, C.byref(lay)), "DSDIFF DST layout")
            finally:
                lib.sb_dst_lengths_free(lengths)
        else:
            _ok(lib, lib.sb_output_layout_new(_disc(img), slot(area), first, end, DSDIFF_DSD, o.h, C.byref(lay)),
                "DSDIFF layout")
    return _layout(lib, lay)
