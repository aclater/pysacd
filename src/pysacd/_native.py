"""ctypes binding for the native engine (_sacdconv.so, built by `make`)."""

from __future__ import annotations

import ctypes
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .toc import Area, SACDImage

FORMATS = {"dsf": 0, "flac": 1, "wav": 2, "dff": 3, "dff-dst": 4}
SCAN_SLACK = 128    # sectors read around the needed range (frame context + boundary frames)


class _Output(ctypes.Structure):
    # Must match sc_output_t in csrc/sacdconv.c
    _fields_ = [
        ("format", ctypes.c_int),
        ("path", ctypes.c_char_p),
        ("tc_start", ctypes.c_uint32),
        ("tc_end", ctypes.c_uint32),
        ("head_frame", ctypes.c_int32),
        ("head_bytes", ctypes.c_uint32),
        ("tail_bytes", ctypes.c_uint32),
        ("header", ctypes.c_char_p),
        ("header_len", ctypes.c_uint32),
        ("footer", ctypes.c_char_p),
        ("footer_len", ctypes.c_uint32),
        ("dst_offsets", ctypes.POINTER(ctypes.c_uint64)),
        ("dst_lengths", ctypes.POINTER(ctypes.c_uint32)),
        ("dst_cap", ctypes.c_uint32),
        ("flac_level", ctypes.c_int),
        ("flac_padding", ctypes.c_uint32),
        ("frames_written", ctypes.c_uint32),
        ("frames_missing", ctypes.c_uint32),
        ("dst_errors", ctypes.c_uint32),
        ("dst_count", ctypes.c_uint32),
        ("samples", ctypes.c_uint64),
        ("data_bytes", ctypes.c_uint64),
        ("error", ctypes.c_char * 256),
    ]


class _Area(ctypes.Structure):
    # Must match sc_area_t in csrc/sacdconv.c
    _fields_ = [
        ("iso_path", ctypes.c_char_p),
        ("sector_size", ctypes.c_int),
        ("data_offset", ctypes.c_int),
        ("scan_lsn_start", ctypes.c_uint32),
        ("scan_lsn_end", ctypes.c_uint32),
        ("channels", ctypes.c_int),
        ("pcm_rate", ctypes.c_int),
        ("gain_db", ctypes.c_double),
        ("threads", ctypes.c_int),
        ("iso_out_path", ctypes.c_char_p),
        ("iso_sectors", ctypes.c_uint32),
        ("cancel", ctypes.c_int),
        ("frames_done", ctypes.c_uint64),
        ("frames_total", ctypes.c_uint64),
        ("sectors_done", ctypes.c_uint64),
        ("sectors_total", ctypes.c_uint64),
        ("error", ctypes.c_char * 256),
    ]


class NativeError(RuntimeError):
    pass


_lib = None


def _load():
    global _lib
    if _lib is None:
        path = Path(__file__).with_name("_sacdconv.so")
        if not path.exists():
            raise NativeError(f"{path} not built; run `make` in the pysacd source tree")
        lib = ctypes.CDLL(str(path))
        lib.sacdconv_area_run.argtypes = [ctypes.POINTER(_Area), ctypes.POINTER(_Output), ctypes.c_int]
        lib.sacdconv_area_run.restype = ctypes.c_int
        lib.sacdconv_has_flac.restype = ctypes.c_int
        _lib = lib
    return _lib


def has_flac() -> bool:
    return bool(_load().sacdconv_has_flac())


@dataclass
class OutputSpec:
    """One file to produce from an area: frames [tc_start, tc_end)."""
    format: str
    path: str | os.PathLike
    tc_start: int
    tc_end: int
    head_frame: int = -1          # DSF nopad: carry-in from this frame's tail
    head_bytes: int = 0
    tail_bytes: int = 0           # DSF nopad: bytes/channel left for the next file
    header: bytes = b""           # DSDIFF header
    footer: bytes = b""           # DSF ID3 tag / DSDIFF trailing chunks
    flac_level: int = 5
    flac_padding: int = 0
    # results
    frames_written: int = 0
    frames_missing: int = 0
    dst_errors: int = 0
    samples: int = 0
    data_bytes: int = 0
    dst_index: list[tuple[int, int]] = field(default_factory=list)   # DSDIFF DST: (offset, length)


@dataclass
class Progress:
    frames_done: int
    frames_total: int
    sectors_done: int
    sectors_total: int


def _scan_range(area: Area, specs: list[OutputSpec]) -> tuple[int, int]:
    """LSN range covering every frame the outputs need."""
    tracks = area.tracks
    lo_tc = min(min(s.tc_start, s.head_frame if s.head_frame >= 0 else s.tc_start) for s in specs)
    hi_tc = max(s.tc_end for s in specs)
    lo = area.track_start
    hi = area.track_end
    for i, t in enumerate(tracks):
        if t.start.total_frames <= lo_tc:
            lo = t.start_lsn if i else area.track_start
        nxt = tracks[i + 1] if i + 1 < len(tracks) else None
        if nxt and nxt.start.total_frames >= hi_tc and hi == area.track_end:
            hi = nxt.start_lsn
    return max(area.track_start, lo - SCAN_SLACK), min(area.track_end, hi + SCAN_SLACK)


def run_area(image: SACDImage, iso_path: str | os.PathLike, area: Area | None, specs: list[OutputSpec],
             pcm_rate: int = 88200, gain_db: float = 0.0, threads: int = 0,
             iso_out: str | os.PathLike | None = None,
             progress: Callable[[Progress], None] | None = None, interval: float = 0.5) -> None:
    """Produce all outputs from one area in a single pass. Blocking, multithreaded.

    `progress` is called from this thread every `interval` seconds while the
    engine runs. Raises NativeError on failure; results are stored on the specs.
    """
    lib = _load()
    outs = (_Output * max(1, len(specs)))()
    keep = []   # keep ctypes buffers alive
    for o, s in zip(outs, specs):
        o.format = FORMATS[s.format]
        o.path = os.fsencode(s.path)
        o.tc_start, o.tc_end = s.tc_start, s.tc_end
        o.head_frame, o.head_bytes, o.tail_bytes = s.head_frame, s.head_bytes, s.tail_bytes
        o.header, o.header_len = s.header, len(s.header)
        o.footer, o.footer_len = s.footer, len(s.footer)
        o.flac_level, o.flac_padding = s.flac_level, s.flac_padding
        if s.format == "dff-dst":
            cap = s.tc_end - s.tc_start
            offs, lens = (ctypes.c_uint64 * cap)(), (ctypes.c_uint32 * cap)()
            keep += [offs, lens]
            o.dst_offsets = ctypes.cast(offs, ctypes.POINTER(ctypes.c_uint64))
            o.dst_lengths = ctypes.cast(lens, ctypes.POINTER(ctypes.c_uint32))
            o.dst_cap = cap

    a = _Area()
    a.iso_path = os.fsencode(iso_path)
    a.sector_size, a.data_offset = image.sector_size, image.data_offset
    if specs:
        assert area is not None, "outputs need an area"
        a.scan_lsn_start, a.scan_lsn_end = _scan_range(area, specs)
        a.channels = area.channel_count
    else:
        a.channels = 2
    a.pcm_rate = pcm_rate
    a.gain_db = gain_db
    a.threads = threads
    if iso_out is not None:
        a.iso_out_path = os.fsencode(iso_out)
        a.iso_sectors = image.total_sectors

    result = []
    t = threading.Thread(target=lambda: result.append(lib.sacdconv_area_run(ctypes.byref(a), outs, len(specs))))
    t.start()
    try:
        while t.is_alive():
            t.join(interval)
            if progress:
                progress(Progress(a.frames_done, a.frames_total, a.sectors_done, a.sectors_total))
    except BaseException:
        a.cancel = 1
        t.join()
        raise

    for o, s in zip(outs, specs):
        s.frames_written, s.frames_missing, s.dst_errors = o.frames_written, o.frames_missing, o.dst_errors
        s.samples, s.data_bytes = o.samples, o.data_bytes
        if s.format == "dff-dst":
            n = min(o.dst_count, o.dst_cap)
            s.dst_index = [(o.dst_offsets[i], o.dst_lengths[i]) for i in range(n)]
    if result and result[0] != 0:
        raise NativeError(a.error.decode(errors="replace"))
