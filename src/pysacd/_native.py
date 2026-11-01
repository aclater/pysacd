"""ctypes binding for the native engine (_sacdconv.so/.dylib/.dll, built by `make`)."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .toc import Area, SACDImage, area_frame_capacity

FORMATS = {"dsf": 0, "flac": 1, "wav": 2, "dff": 3, "dff-dst": 4}


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


# int read_fn(void *ctx, uint32_t lsn, uint32_t count, uint8_t *buf)
_READ_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.POINTER(ctypes.c_uint8))
# void log_fn(void *ctx, int level, const char *msg)
_LOG_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p)


class _Area(ctypes.Structure):
    # Must match sc_area_t in csrc/sacdconv.c
    _fields_ = [
        ("iso_path", ctypes.c_char_p),
        ("sector_size", ctypes.c_int),
        ("data_offset", ctypes.c_int),
        ("channels", ctypes.c_int),
        ("pcm_rate", ctypes.c_int),
        ("gain_db", ctypes.c_double),
        ("threads", ctypes.c_int),
        ("iso_out_path", ctypes.c_char_p),
        ("cancel", ctypes.c_int),
        ("frames_done", ctypes.c_uint64),
        ("frames_total", ctypes.c_uint64),
        ("sectors_done", ctypes.c_uint64),
        ("sectors_total", ctypes.c_uint64),
        ("error", ctypes.c_char * 256),
        ("read_fn", _READ_FN),
        ("read_ctx", ctypes.c_void_p),
        ("log_fn", _LOG_FN),
        ("log_ctx", ctypes.c_void_p),
        ("log_level", ctypes.c_int),
        # scarletbook (#85): the area by its TOC's LSN, the image size, the engine's cancel token
        ("toc_lsn", ctypes.c_uint32),
        ("image_sectors", ctypes.c_uint64),
        ("sb_cancel", ctypes.c_void_p),
        # range starts: exact and seek_min_gap (in), provisional and refuted_from (out)
        ("exact", ctypes.c_int),
        ("provisional", ctypes.c_int),
        ("refuted_from", ctypes.c_uint32),
        ("seek_min_gap", ctypes.c_int64),
        # #93 workaround: the frame tested for DSD silence (in) and the result; missing frames
        ("check_frame", ctypes.c_uint32),
        ("check_silent", ctypes.c_int),
        ("missing_count", ctypes.c_uint32),
        ("missing_first", ctypes.c_uint32),
        ("missing_last", ctypes.c_uint32),
    ]


class NativeError(RuntimeError):
    pass


log = logging.getLogger("pysacd.engine")

# Reads 2048-byte sectors (lsn, count) -> bytes, e.g. from a disc on a network server
SectorSource = Callable[[int, int], bytes]


_lib = None
LIBRARY = {"win32": "_sacdconv.dll", "darwin": "_sacdconv.dylib"}.get(sys.platform, "_sacdconv.so")


def _load():
    global _lib
    if _lib is None:
        path = Path(__file__).with_name(LIBRARY)
        if not path.exists():
            raise NativeError(f"{path} not built; run `make` in the pysacd source tree")
        lib = ctypes.CDLL(str(path))
        lib.sacdconv_area_run.argtypes = [ctypes.POINTER(_Area), ctypes.POINTER(_Output), ctypes.c_int]
        lib.sacdconv_area_run.restype = ctypes.c_int
        lib.sacdconv_has_flac.restype = ctypes.c_int
        lib.sacdconv_build_info.restype = ctypes.c_char_p
        lib.sacdconv_cancel.argtypes = [ctypes.POINTER(_Area)]
        lib.sacdconv_cancel.restype = None
        _check_abi(lib, path)
        _check_scarletbook(lib, path)
        _lib = lib
    return _lib


ABI = 5    # SACDCONV_ABI in csrc/sacdconv.c


def _layout(struct) -> list[int]:
    return [ctypes.sizeof(struct)] + [getattr(struct, name).offset for name, _ in struct._fields_]


def _check_abi(lib, path) -> None:
    """Refuse an engine built from different struct definitions than the ctypes mirrors here:
    a mismatch would make the engine read and write the wrong fields."""
    try:
        abi = lib.sacdconv_abi()
    except AttributeError:
        abi = 0
    if abi != ABI:
        raise NativeError(f"{path} has engine ABI {abi}, this pysacd needs {ABI}; rebuild it (make clean all)")
    lib.sacdconv_layout.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
    for which, struct in ((0, _Area), (1, _Output)):
        want = _layout(struct)
        buf = (ctypes.c_uint32 * 64)()
        n = lib.sacdconv_layout(which, buf, 64)
        got = list(buf[:min(n, 64)])
        if got != want:
            raise NativeError(f"{path}: {struct.__name__} layout differs from the engine's "
                              f"(engine {got}, pysacd {want}); rebuild it (make clean all)")


# The scarletbook-rs C library linked into the engine (#85): its C ABI version, and the oldest
# release with every call pysacd uses (0.5.0: cancellation, progress, stored frames in one pass;
# 0.5.1: TOC timecodes as stored; 0.5.2: the whole TOC in one call, sb_disc_toc; 0.5.4: the
# output API's choices for sacd_extract's DSF and DSDIFF files, scarletbook-rs#169)
SB_ABI = 2
SB_MIN_VERSION = (0, 5, 4)


def _version_tuple(text: str) -> tuple[int, ...]:
    core = text.split("+", 1)[0].split("-", 1)[0]
    try:
        return tuple(int(x) for x in core.split("."))
    except ValueError:
        return ()


def _check_scarletbook(lib, path) -> None:
    """Refuse an engine linked with a scarletbook of another C ABI, or older than pysacd needs.
    The build already checks the header's SB_ABI_VERSION; this checks the library itself, which
    a SCARLETBOOK_DIR build may take from anywhere."""
    try:
        lib.sb_abi_version.restype = ctypes.c_uint32
        lib.sb_abi_version.argtypes = []
        lib.sb_version.restype = ctypes.c_char_p
        lib.sb_version.argtypes = []
        lib.sb_build_info.restype = ctypes.c_char_p
        lib.sb_build_info.argtypes = []
    except AttributeError:
        raise NativeError(f"{path} has no scarletbook library linked in; rebuild it (make clean all)") from None
    abi = lib.sb_abi_version()
    if abi != SB_ABI:
        raise NativeError(f"{path} is linked with scarletbook C ABI {abi}, this pysacd needs {SB_ABI}")
    version = lib.sb_version().decode(errors="replace")
    if _version_tuple(version) < SB_MIN_VERSION:
        raise NativeError(f"{path} is linked with scarletbook {version}, this pysacd needs "
                          f"{'.'.join(map(str, SB_MIN_VERSION))} or later")
    log.debug("scarletbook: %s", lib.sb_build_info().decode(errors="replace"))


def scarletbook_version() -> str:
    """The version of the scarletbook-rs library linked into the engine."""
    return _load().sb_version().decode(errors="replace")


def scarletbook_build_info() -> str:
    """How that library was built (version, target, features, compiler, commit), for bug reports."""
    return _load().sb_build_info().decode(errors="replace")


def has_flac() -> bool:
    return bool(_load().sacdconv_has_flac())


def library_path() -> str:
    return str(Path(__file__).with_name(LIBRARY))


def build_info() -> str:
    """Compiler and features the engine was built with, and the scarletbook release linked into
    it, for diagnostic logs."""
    return f"{_load().sacdconv_build_info().decode()}, scarletbook {scarletbook_version()}"


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
class RunInfo:
    """How run_area read the area. A range far into an area starts provisionally unless exact
    reading was asked for (#85): `provisional` says it did, `confirmed` that the background
    check found it exact, and `refuted_from` (when not None) the frame from which a provisional
    reading that turned out wrong was exact again: outputs before it may differ from an exact
    read."""
    provisional: bool = False
    confirmed: bool = False
    refuted_from: int | None = None
    # missing frames in the outputs' ranges: how many, the first and the last (None: none)
    missing: int = 0
    missing_first: int | None = None
    missing_last: int | None = None
    # #93 workaround: the area's next-to-last frame is DSD silence (True; csrc dsd_silent), isn't (False), or
    # wasn't read or can't be tested (None: DST kept as stored)
    before_last_silent: bool | None = None


@dataclass
class Progress:
    frames_done: int
    frames_total: int
    sectors_done: int
    sectors_total: int


def run_area(image: SACDImage, iso_path: str | os.PathLike, area: Area | None, specs: list[OutputSpec],
             pcm_rate: int = 88200, gain_db: float = 0.0, threads: int = 0,
             iso_out: str | os.PathLike | None = None,
             progress: Callable[[Progress], None] | None = None, interval: float = 0.5,
             source: SectorSource | None = None, exact: bool = False,
             seek_min_gap: int | None = None) -> RunInfo:
    """Produce all outputs from one area in a single pass. Blocking, multithreaded.

    `progress` is called from this thread every `interval` seconds while the
    engine runs. Raises NativeError on failure; results are stored on the specs.
    With `source`, sectors come from that callable instead of the file at
    iso_path (which is then only a name for messages). Engine diagnostics go to
    the "pysacd.engine" logger.

    A range that starts far into the area starts provisionally (scarletbook's speculative seek
    policy: exact on a well-formed disc, without reading the area up to the range) unless
    `exact` is true. The returned RunInfo says whether it did, and how it was checked.
    `seek_min_gap` (sectors) overrides how far the exact start must lie before the range for
    a provisional start to be used (scarletbook's default, 2048, when None); tests set it to 0.
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
        # Frames the image lacks become silence: never more of it than the area's sectors in this
        # image could hold, however long the TOC says the tracks are (#17)
        capacity = area_frame_capacity(area, image.total_sectors)
        if max(s.tc_end for s in specs) > capacity:
            raise NativeError(f"the outputs need frames up to {max(s.tc_end for s in specs)}, but the area's "
                              f"audio in this image (LSN {area.track_start}-{area.track_end}, "
                              f"{image.total_sectors} sectors) holds at most {capacity}")
        a.channels = area.channel_count
        a.toc_lsn = area.toc_start      # which area scarletbook reads: the one whose TOC this is
    else:
        a.channels = 2
    a.image_sectors = image.total_sectors
    a.exact = int(exact)
    a.seek_min_gap = -1 if seek_min_gap is None else seek_min_gap
    # #93 workaround: the frame before the area's last one is tested for DSD silence
    a.check_frame = area.total_playtime.total_frames - 2 if area is not None and \
        area.total_playtime.total_frames >= 2 else 0xFFFFFFFF
    a.pcm_rate = pcm_rate
    a.gain_db = gain_db
    a.threads = threads
    if iso_out is not None:
        a.iso_out_path = os.fsencode(iso_out)

    # Callbacks run on engine threads; they must never raise into C.
    def on_log(_ctx, level, msg):
        log.log(level, msg.decode(errors="replace"))

    def on_read(_ctx, lsn, count, buf):
        try:
            data = source(lsn, count)
            if len(data) != count * 2048:
                raise OSError(f"short read: {len(data)} of {count * 2048} bytes")
            ctypes.memmove(buf, data, len(data))
            return 0
        except Exception as e:
            log.exception("sector source failed at LSN %d (+%d)", lsn, count)
            a.error = str(e).encode(errors="replace")[:255]
            return -1

    callbacks = [_LOG_FN(on_log)]
    a.log_fn = callbacks[0]
    a.log_level = log.getEffectiveLevel()
    if source is not None:
        if image.sector_size != 2048:
            raise NativeError("a sector source must deliver plain 2048-byte sectors")
        callbacks.append(_READ_FN(on_read))
        a.read_fn = callbacks[1]
    for s in specs:
        log.debug("output %s %s: frames %d-%d%s", s.format, s.path, s.tc_start, s.tc_end,
                  f", nopad head {s.head_bytes}@{s.head_frame} tail {s.tail_bytes}" if s.head_frame >= 0 or s.tail_bytes
                  else "")

    result = []
    t = threading.Thread(target=lambda: result.append(lib.sacdconv_area_run(ctypes.byref(a), outs, len(specs))),
                         name="engine")
    t.start()
    try:
        while t.is_alive():
            t.join(interval)
            if progress:
                progress(Progress(a.frames_done, a.frames_total, a.sectors_done, a.sectors_total))
    except BaseException:
        # the engine's threads and scarletbook's reads stop within a batch / 32 sectors
        t0 = time.monotonic()
        lib.sacdconv_cancel(ctypes.byref(a))
        t.join()
        log.debug("engine cancelled in %.3f s", time.monotonic() - t0)
        raise

    for o, s in zip(outs, specs):
        s.frames_written, s.frames_missing, s.dst_errors = o.frames_written, o.frames_missing, o.dst_errors
        s.samples, s.data_bytes = o.samples, o.data_bytes
        if s.format == "dff-dst":
            n = min(o.dst_count, o.dst_cap)
            s.dst_index = [(o.dst_offsets[i], o.dst_lengths[i]) for i in range(n)]
    if result and result[0] != 0:
        raise NativeError(a.error.decode(errors="replace"))
    none = lambda v: None if v == 0xFFFFFFFF else v
    info = RunInfo(provisional=a.provisional > 0, confirmed=a.provisional == 2,
                   refuted_from=none(a.refuted_from), missing=a.missing_count,
                   missing_first=none(a.missing_first), missing_last=none(a.missing_last),
                   before_last_silent=None if a.check_silent < 0 else bool(a.check_silent))
    return info
