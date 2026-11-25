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
from typing import Any

from .toc import Area, SACDImage, area_frame_capacity

# How often the outputs of one run may cover the area, per output format (csrc/sacdconv.c
# RUN_COVERS): an edit master plus the tracks
RUN_COVERS = 2
WAV_MAX_DATA = 0xFFFFFFFF - 60      # csrc/sacdconv.c write_wav_header

FORMATS = {"dsf": 0, "flac": 1, "wav": 2, "dff": 3, "dff-dst": 4}


class _Output(ctypes.Structure):
    # Must match sc_output_t in csrc/sacdconv.c
    _fields_ = [
        ("format", ctypes.c_int),
        ("path", ctypes.c_char_p),
        ("tc_start", ctypes.c_uint32),
        ("tc_end", ctypes.c_uint32),
        ("head_frame", ctypes.c_int32),
        ("layout", ctypes.c_void_p),
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
        ("allow_filler", ctypes.c_int),
        # PCM: the filter (PCM_FILTERS) and the cutoff (PCM_CUTOFFS)
        ("pcm_filter", ctypes.c_int),
        ("pcm_cutoff", ctypes.c_int),
        ("pcm_clipped", ctypes.c_uint64),
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
        _check_abi(lib, path)      # before binding anything an older engine lacks (sacdconv_cancel)
        lib.sacdconv_cancel.argtypes = [ctypes.POINTER(_Area)]
        lib.sacdconv_cancel.restype = None
        _check_scarletbook(lib, path)
        _lib = lib
    return _lib


ABI = 9    # SACDCONV_ABI in csrc/sacdconv.c


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


# The PCM gain the engine takes (dsd2pcm's range): the CLIs check it before anything runs
GAIN_DB_MIN, GAIN_DB_MAX = -120.0, 24.0


def check_gain_db(value: object) -> float:
    """The PCM gain, checked: an int or float (not a bool), finite, from GAIN_DB_MIN to GAIN_DB_MAX.
    The value may come from config.toml as any TOML type. Raises ValueError with a message for
    the user."""
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"PCM gain {value!r}: not a number of dB")  # noqa: TRY004  # callers report ValueError to the user
    if not math.isfinite(value) or not GAIN_DB_MIN <= value <= GAIN_DB_MAX:
        raise ValueError(f"PCM gain {value!r} dB is outside {GAIN_DB_MIN:g} to {GAIN_DB_MAX:+g} dB")
    return float(value)


def gain_db_arg(text: str) -> float:
    """argparse type for --gain-db: a number from GAIN_DB_MIN to GAIN_DB_MAX."""
    import argparse
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    try:
        return check_gain_db(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text} is outside {GAIN_DB_MIN:g} to {GAIN_DB_MAX:+g} dB") from None


# PCM filters and cutoffs, by name, as the engine numbers them (sc_area_t.pcm_filter, and
# dsd2pcm's DSD2PCM_CUTOFF_*). None picks the default for the rate (pcm_settings).
PCM_FILTERS = {"precise": 0, "pysacd": 1}
PCM_CUTOFFS = {"standard": 0, "low": 1, "wide": 2}
PCM_RATES = (88200, 176400)
# The precise filter's cutoff each, in kHz (dsd2pcm's passband edge; -3 dB about 2.6 kHz above it).
# The pysacd filter has "standard" only, and there it is its 0.5.x response, which depends on the
# rate: -3 dB at about 43 kHz at 88.2 kHz and at about 72 kHz at 176.4 kHz (docs/converting.md).
CUTOFF_KHZ = {"standard": 40, "low": 30, "wide": 80}
PYSACD_MINUS_3DB_KHZ = {88200: 43, 176400: 72}


def pcm_settings(rate: int, pcm_filter: str | None = None, pcm_cutoff: str | None = None) -> tuple[str, str]:
    """The PCM filter and cutoff for a run at `rate`, checked: (filter, cutoff).

    The default filter is precise, the best measured (docs/quality), with the standard cutoff
    (40 kHz) at both rates: the wide one (80 kHz, 176.4 kHz only) measures within 0.2 dB of it in
    band (docs/quality/0.6.0.md) and passes mostly DSD's ultrasonic noise. The pysacd filter
    (pysacd's own design, dithered) has the standard cutoff only, which is its 0.5.x response:
    -3 dB at about 43 kHz at 88.2 kHz (three stages) and at about 72 kHz at 176.4 kHz (two stages,
    flat to about 60 kHz). The wide cutoff needs 176.4 kHz. Raises ValueError, with a message for the user, for anything else."""
    # the values may come from config.toml as any TOML type: compare as given, never hash them
    if not isinstance(rate, int) or isinstance(rate, bool) or rate not in PCM_RATES:
        raise ValueError(f"PCM rate {rate!r}: pysacd writes {' or '.join(map(str, PCM_RATES))} Hz")
    f = "precise" if pcm_filter is None else pcm_filter
    if not isinstance(f, str) or f not in PCM_FILTERS:
        raise ValueError(f"unknown PCM filter {pcm_filter!r}: {' or '.join(PCM_FILTERS)}")
    if pcm_cutoff is None:
        c = "standard"
    else:
        c = pcm_cutoff
    if not isinstance(c, str) or c not in PCM_CUTOFFS:
        raise ValueError(f"unknown PCM cutoff {pcm_cutoff!r}: {', '.join(PCM_CUTOFFS)}")
    if f == "pysacd" and c != "standard":
        raise ValueError(f"the pysacd PCM filter has the standard cutoff only (its 0.5.x response: about 43 kHz "
                         f"at 88.2 kHz, about 72 kHz at 176.4 kHz), not {c}")
    if c == "wide" and rate < 176400:
        raise ValueError(f"the wide PCM cutoff (80 kHz) needs --pcm-rate 176400, not {rate}")
    return f, c


def library_path() -> str:
    return str(Path(__file__).with_name(LIBRARY))


def default_filters() -> str:
    """The PCM filter and cutoff used when none is chosen, per rate."""
    return "; ".join(f"{f} with the {c} cutoff ({CUTOFF_KHZ[c]} kHz) at {r / 1000:g} kHz"
                     for r, (f, c) in ((r, pcm_settings(r)) for r in PCM_RATES))


def build_info() -> str:
    """Compiler and features the engine was built with, the dsd2pcm and scarletbook releases
    linked into it, and the default PCM filters, for diagnostic logs."""
    return (f"{_load().sacdconv_build_info().decode()}, scarletbook {scarletbook_version()}; "
            f"PCM by default: {default_filters()}")


@dataclass
class OutputSpec:
    """One file to produce from an area: frames [tc_start, tc_end)."""
    format: str
    path: str | os.PathLike
    tc_start: int
    tc_end: int
    head_frame: int = -1          # DSF -z: this frame's content stands in for frame tc_start - 1
    layout: Any = None            # DSF: scarletbook's layout of the file (sbwriter.dsf; default: no tag)
    header: bytes = b""           # DSDIFF header
    footer: bytes = b""           # DSDIFF trailing chunks
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
    # the run's progress count at the end: frames processed of the frames the outputs need (#205)
    frames_done: int = 0
    frames_total: int = 0
    # PCM samples clamped to 24 bits in the frames the outputs cover (a gain above 0 dB can clip)
    pcm_clipped: int = 0


@dataclass
class Progress:
    frames_done: int
    frames_total: int
    sectors_done: int
    sectors_total: int


def check_wav_sizes(specs: list[OutputSpec], channels: int, pcm_rate: int) -> None:
    """A WAV file holds at most 4 GiB of audio (24-bit: a 6-channel track of about 41 minutes
    at 88.2 kHz). The engine finds out when it closes the file, after the whole run is written,
    and the run's other files go with it: refuse before it starts."""
    for s in specs:
        if s.format == "wav":
            size = (s.tc_end - s.tc_start) * (pcm_rate // 75) * channels * 3
            if size > WAV_MAX_DATA:
                raise NativeError(f"a WAV file can't hold the {size} bytes of audio of frames {s.tc_start}-"
                                  f"{s.tc_end} (4 GiB at most); use FLAC for this track")


def run_area(image: SACDImage, iso_path: str | os.PathLike, area: Area | None, specs: list[OutputSpec],
             pcm_rate: int = 88200, gain_db: float = 0.0, threads: int = 0,
             iso_out: str | os.PathLike | None = None,
             progress: Callable[[Progress], None] | None = None, interval: float = 0.5,
             source: SectorSource | None = None, exact: bool = False, allow_silence: bool = False,
             seek_min_gap: int | None = None, pcm_filter: str | None = None,
             pcm_cutoff: str | None = None) -> RunInfo:
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

    A run stops once it has written more silence and filler (frames the image lacks, frames
    that don't decode) than audio, plus 10 seconds: an image of holes can't turn into gigabytes.
    `allow_silence` lifts that for a run, to rescue what a damaged rip still holds.

    FLAC and WAV outputs use `pcm_filter` ("precise" or "pysacd") with `pcm_cutoff`
    ("standard", "low" or "wide"); None picks the default for the rate (pcm_settings), and a
    combination the filter doesn't have, or a gain_db that isn't a finite number from -120 to
    +24 dB, raises NativeError before anything runs.
    """
    filt, cut, gain = "precise", "standard", 0.0
    if any(s.format in ("flac", "wav") for s in specs):
        try:       # before anything runs: the filter, cutoff, rate and gain
            filt, cut = pcm_settings(pcm_rate, pcm_filter, pcm_cutoff)
            gain = check_gain_db(gain_db)
        except ValueError as e:
            raise NativeError(str(e)) from None
    lib = _load()
    outs = (_Output * max(1, len(specs)))()
    keep = []   # keep ctypes buffers alive
    for o, s in zip(outs, specs):
        o.format = FORMATS[s.format]
        o.path = os.fsencode(s.path)
        o.tc_start, o.tc_end = s.tc_start, s.tc_end
        o.head_frame = s.head_frame
        if s.format == "dsf":
            if s.layout is None:
                from . import sbwriter
                assert area is not None, "outputs need an area"
                try:
                    s.layout = sbwriter.dsf(image, area, s.tc_start, s.tc_end, b"")
                except sbwriter.OutputError as e:
                    raise NativeError(f"{s.path}: {e}") from None
            o.layout = s.layout.handle
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
        # ... and not per output only: the outputs of one run may add up to RUN_COVERS times that per
        # format (an edit master and the tracks in the same format cover the area twice), with
        # overlapping tracks counted each time they are written (GHSA-q45f-7qmf-37x3). The engine
        # holds the same bound against the whole image, and limits the silence it writes.
        per_format: dict[str, int] = {}
        for s in specs:
            per_format[s.format] = per_format.get(s.format, 0) + s.tc_end - s.tc_start
        for fmt, total in per_format.items():
            if total > RUN_COVERS * capacity:
                n = sum(s.format == fmt for s in specs)
                raise NativeError(f"the {n} {fmt} outputs need {total} frames in all, more than {RUN_COVERS} times "
                                  f"the {capacity} the area's audio in this image (LSN {area.track_start}-"
                                  f"{area.track_end}, {image.total_sectors} sectors) could hold: tracks that "
                                  f"overlap or claim more than the area's play time")
        check_wav_sizes(specs, area.channel_count, pcm_rate)
        a.channels = area.channel_count
        a.toc_lsn = area.toc_start      # which area scarletbook reads: the one whose TOC this is
    else:
        a.channels = 2
    a.image_sectors = image.total_sectors
    a.exact = int(exact)
    a.allow_filler = int(allow_silence)
    a.seek_min_gap = -1 if seek_min_gap is None else seek_min_gap
    # #93 workaround: the frame before the area's last one is tested for DSD silence
    a.check_frame = area.total_playtime.total_frames - 2 if area is not None and \
        area.total_playtime.total_frames >= 2 else 0xFFFFFFFF
    a.pcm_rate = pcm_rate
    if any(s.format in ("flac", "wav") for s in specs):
        a.pcm_filter, a.pcm_cutoff = PCM_FILTERS[filt], PCM_CUTOFFS[cut]
        a.gain_db = gain
    if threads <= 0 and area is not None and not area.is_dst and specs and all(
            s.format in ("dsf", "dff") for s in specs):
        # Plain DSD copied into DSD files: nothing to decode or filter, so a pool of workers
        # only costs wake-ups (#137). One worker, unless the caller chose a count.
        threads = 1
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
                  f", -z head frame {s.head_frame}" if s.head_frame >= 0 else "")

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
    if not result:
        raise NativeError("the engine did not return a result")      # the call itself failed
    if result[0] != 0:
        raise NativeError(a.error.decode(errors="replace"))
    none = lambda v: None if v == 0xFFFFFFFF else v
    info = RunInfo(provisional=a.provisional > 0, confirmed=a.provisional == 2,
                   refuted_from=none(a.refuted_from), missing=a.missing_count,
                   missing_first=none(a.missing_first), missing_last=none(a.missing_last),
                   before_last_silent=None if a.check_silent < 0 else bool(a.check_silent),
                   frames_done=a.frames_done, frames_total=a.frames_total, pcm_clipped=a.pcm_clipped)
    return info
