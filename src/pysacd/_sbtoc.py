"""Disc images through scarletbook-rs (#85): ctypes on the engine's sb_* functions.

scarletbook-rs is linked into the native engine (docs/development.md, "The scarletbook
library"), which exports its C ABI 2. SbImage opens an image with it, reads its 2048-byte
logical sectors whatever its sector size, and builds toc.py's Disc, Area and Track objects.
Those are the objects pysacd's own parser built up to 0.2, field for field (it was compared
on the conformance corpus, real discs and a library before it was removed), so names, tags,
cue, XML, -P text, JSON and beets-pysacd fingerprints are unchanged. Where scarletbook hands
back stored bytes (catalogue numbers, language codes, manufacturer data, ISRCs, timecodes),
they are rendered as that parser rendered them. Warnings are the sacd-spec specification's
diagnostic codes, with their area, track and LSN.

Bindings follow scarletbook's README ("Using from Python (ctypes)") and scarletbook.h.
"""

from __future__ import annotations

import ctypes as C
import logging
import os
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .toc import Disc

log = logging.getLogger("pysacd.toc")

# SbStatus values used here
OK, END, ABSENT = 0, 1, 2
BUFFER_TOO_SMALL = -11
NO_SUCH_AREA = -8
MANUFACTURER_BYTES = 2040   # SB_MANUFACTURER_BYTES
MAX_STRING = 1 << 20        # far beyond any TOC string; a bound on what a buggy length could ask for


class SbDiscInfo(C.Structure):
    _fields_ = [("sectors", C.c_uint64), ("sector_size", C.c_uint32), ("version_major", C.c_uint8),
                ("version_minor", C.c_uint8), ("album_set_size", C.c_uint16),
                ("album_sequence_number", C.c_uint16), ("hybrid", C.c_uint8),
                ("manufacturer_sector", C.c_uint8), ("year", C.c_uint16), ("month", C.c_uint8),
                ("day", C.c_uint8), ("area_count", C.c_uint32), ("warning_count", C.c_uint32)]


class SbAreaInfo(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in (
        "slot", "kind_channels", "toc_lsn", "toc_sectors", "channel_count", "area_config",
        "sample_frequency_code", "frame_format", "max_byte_rate", "playtime_frames",
        "audio_start_lsn", "audio_end_lsn", "track_count")]


class SbTrackInfo(C.Structure):
    _fields_ = [("number", C.c_uint32), ("start_lsn", C.c_uint32), ("length_sectors", C.c_uint32),
                ("start_frame", C.c_uint32), ("duration_frames", C.c_uint32),
                ("start_flags", C.c_uint8), ("duration_flags", C.c_uint8), ("has_genre", C.c_uint8),
                ("genre_category", C.c_uint8), ("genre_index", C.c_uint8), ("reserved", C.c_uint8 * 3)]


class SbWarning(C.Structure):
    _fields_ = [("lsn", C.c_int64), ("slot", C.c_int32), ("track", C.c_uint32), ("code", C.c_char_p)]


class SbGenre(C.Structure):
    _fields_ = [("category", C.c_uint8), ("index", C.c_uint8)]


class SbTextChannel(C.Structure):
    _fields_ = [("language", C.c_uint8 * 2), ("charset", C.c_uint8), ("reserved", C.c_uint8)]


class SbTimecode(C.Structure):
    _fields_ = [("minutes", C.c_uint8), ("seconds", C.c_uint8), ("frames", C.c_uint8)]


class SbAreaDetails(C.Structure):
    _fields_ = [(name, C.c_uint8) for name in (
        "version_major", "version_minor", "max_available_channels", "mute_flags",
        "track_attribute", "track_offset", "text_channel_count", "text_charset")]


# Sizes stated in scarletbook.h: a mismatch means these mirrors are wrong for the library
_SIZES = {SbDiscInfo: 32, SbAreaInfo: 52, SbTrackInfo: 28, SbGenre: 2, SbTextChannel: 4, SbAreaDetails: 8,
          SbTimecode: 3}

# int64_t (*SbReadAtFn)(void *user, uint64_t offset, uint8_t *buf, size_t len)
READ_AT = C.CFUNCTYPE(C.c_int64, C.c_void_p, C.c_uint64, C.POINTER(C.c_uint8), C.c_size_t)
# void (*SbLogFn)(void *user, int32_t level, const char *message)
LOG_FN = C.CFUNCTYPE(None, C.c_void_p, C.c_int32, C.c_char_p)

# SB_LOG_* -> logging levels
_LEVELS = {1: logging.ERROR, 2: logging.WARNING, 3: logging.INFO, 4: logging.DEBUG, 5: 5}

# SbDiscString 0..15: the keys of Disc.text
_DISC_TEXT = (
    "album_title", "album_artist", "album_publisher", "album_copyright",
    "album_title_phonetic", "album_artist_phonetic", "album_publisher_phonetic", "album_copyright_phonetic",
    "disc_title", "disc_artist", "disc_publisher", "disc_copyright",
    "disc_title_phonetic", "disc_artist_phonetic", "disc_publisher_phonetic", "disc_copyright_phonetic",
)
ALBUM_CATALOG, DISC_CATALOG = 16, 17
# SbTrackString 0..13: toc.TRACK_TEXT_TYPES' names in type order (0x01-0x07, 0x81-0x87); 14 is the ISRC
_TRACK_TEXT = (
    "title", "performer", "songwriter", "composer", "arranger", "message", "extra_message",
    "title_phonetic", "performer_phonetic", "songwriter_phonetic", "composer_phonetic",
    "arranger_phonetic", "message_phonetic", "extra_message_phonetic",
)
TRACK_ISRC = 14
SLOT_NAMES = {0: "2ch", 1: "multichannel"}

_bound: Any = None
_bind_lock = threading.Lock()


def _lib():
    """The engine library, with the sb_* functions this module calls given their types."""
    global _bound
    with _bind_lock:
        if _bound is not None:
            return _bound
        from . import _native
        lib = _native._load()
        for struct, size in _SIZES.items():
            if C.sizeof(struct) != size:
                raise _native.NativeError(f"{struct.__name__} is {C.sizeof(struct)} bytes, scarletbook.h says {size}")
        H, S, P, U32, SIZE = C.c_void_p, C.c_int32, C.POINTER, C.c_uint32, C.c_size_t

        def bind(name, restype, *argtypes):
            f = getattr(lib, name)
            f.restype, f.argtypes = restype, list(argtypes)

        bind("sb_last_error_message", C.c_char_p)
        bind("sb_status_name", C.c_char_p, C.c_int32)
        bind("sb_disc_open_callbacks_at_with_log", S, READ_AT, C.c_uint64, H, LOG_FN, H, C.c_int32, P(H))
        bind("sb_disc_free", None, H)
        bind("sb_disc_info", S, H, P(SbDiscInfo))
        bind("sb_disc_read_sectors", S, H, C.c_uint64, U32, C.c_char_p, SIZE)
        bind("sb_disc_string", S, H, U32, C.c_char_p, SIZE, P(SIZE))
        bind("sb_disc_area", S, H, U32, P(SbAreaInfo))
        bind("sb_disc_warning", S, H, U32, P(SbWarning))
        bind("sb_disc_genre", S, H, U32, U32, P(SbGenre))
        bind("sb_disc_text_channel_count", S, H, P(U32))
        bind("sb_disc_text_channel", S, H, U32, P(SbTextChannel))
        bind("sb_disc_catalog_number_stored", S, H, U32, C.c_char_p, SIZE, P(SIZE))
        bind("sb_disc_manufacturer", S, H, C.c_char_p, SIZE, P(SIZE))
        bind("sb_area_details", S, H, U32, P(SbAreaDetails))
        bind("sb_area_text_channel", S, H, U32, U32, P(SbTextChannel))
        bind("sb_area_string", S, H, U32, U32, C.c_char_p, SIZE, P(SIZE))
        bind("sb_track_info", S, H, U32, U32, P(SbTrackInfo))
        bind("sb_track_string", S, H, U32, U32, U32, C.c_char_p, SIZE, P(SIZE))
        bind("sb_area_playtime_stored", S, H, U32, P(SbTimecode))          # 0.5.1
        bind("sb_track_timecodes_stored", S, H, U32, U32, P(SbTimecode), P(SbTimecode))
        _bound = lib
        return lib


class SbFailure(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _check(lib, status: int, what: str) -> int:
    if status < 0:
        name = (lib.sb_status_name(status) or b"?").decode(errors="replace")
        msg = (lib.sb_last_error_message() or b"").decode(errors="replace")
        raise SbFailure(status, f"{what}: {name}: {msg}" if msg else f"{what}: {name}")
    return status


def _copy(lib, fn, *args, raw: bool = False) -> bytes | None:
    """A copy-out string (scarletbook.h, "Strings"): None when absent, else its bytes."""
    n = C.c_size_t()
    status = fn(*args, None, 0, C.byref(n))
    if status == ABSENT:
        return None
    if status != BUFFER_TOO_SMALL:
        _check(lib, status, fn.__name__)
    if n.value > MAX_STRING:
        raise SbFailure(BUFFER_TOO_SMALL, f"{fn.__name__}: a string of {n.value} bytes")
    buf = C.create_string_buffer(n.value + 1)
    _check(lib, fn(*args, buf, len(buf), C.byref(n)), fn.__name__)
    return buf.raw[:n.value]


def _text(lib, fn, *args) -> str | None:
    data = _copy(lib, fn, *args)
    # UTF-8 from the library; an empty string is no string
    return data.decode("utf-8", errors="replace") or None if data is not None else None


def _language(ch: SbTextChannel) -> str:
    # the two bytes as latin-1, trailing NULs and spaces removed (pysacd 0.2's rendering)
    return bytes(ch.language).decode("latin-1").rstrip("\0 ")


def _fixed_str(raw: bytes) -> str:
    # bytes already cut at the first NUL: latin-1, whitespace trimmed (pysacd 0.2's rendering)
    return raw.split(b"\0", 1)[0].decode("latin-1").strip()


def _warning_text(w: SbWarning) -> str:
    code = (w.code or b"?").decode(errors="replace")
    where = []
    if w.slot >= 0:
        where.append(f"{SLOT_NAMES.get(w.slot, w.slot)} area")
    if w.track:
        where.append(f"track {w.track}")
    if w.lsn >= 0:
        where.append(f"LSN {w.lsn}")
    return f"{code} ({', '.join(where)})" if where else code


class SbImage:
    """An image opened with scarletbook, read through a Python file object (a path is opened
    here): toc.SACDImage's engine. Raises toc.SACDError when scarletbook rejects the image,
    OSError when reading it fails, NativeError when the engine can't be loaded."""

    def __init__(self, source):
        from . import _native
        from .toc import SACDError

        self._disc = C.c_void_p()
        self._owns = False
        try:
            self._lib = lib = _lib()
        except _native.NativeError as e:
            raise SACDError(f"reading a disc image needs pysacd's native engine, which isn't available ({e})") from e
        if isinstance(source, (str, os.PathLike)):
            self._fh = open(source, "rb")  # noqa: SIM115 - closed by close()
            self._owns = True
        else:
            self._fh, self._owns = source, False
        self._lock = threading.Lock()
        self._failure: list[str] = []
        fh, lock, failure = self._fh, self._lock, self._failure

        def read_at(_user, offset, buf, length):
            # never raise into C (scarletbook.h: callbacks must not unwind)
            try:
                with lock:
                    fh.seek(offset)
                    data = fh.read(length)
                n = min(len(data), length)
                C.memmove(buf, data, n)
                return n
            except Exception as e:  # noqa: BLE001 - nothing may unwind into C; the failure is re-raised below
                log.debug("read of %d bytes at offset %d failed: %s", length, offset, e)
                failure.append(str(e))
                return -1

        def on_log(_user, level, message):
            try:
                # at debug: what scarletbook warns about also comes back as the disc's warnings
                log.log(min(_LEVELS.get(level, logging.DEBUG), logging.DEBUG), "scarletbook: %s",
                        (message or b"").decode(errors="replace"))
            except Exception:  # noqa: BLE001, S110 - a log line that can't be logged mustn't unwind into C
                pass

        self._callbacks = (READ_AT(read_at), LOG_FN(on_log))    # alive until the disc is freed
        try:
            pos = fh.tell()
            size = fh.seek(0, os.SEEK_END)
            fh.seek(pos)
            level = 4 if log.isEnabledFor(logging.DEBUG) else 0
            status = lib.sb_disc_open_callbacks_at_with_log(self._callbacks[0], size, None, self._callbacks[1],
                                                            None, level, C.byref(self._disc))
            if status < 0:
                self._raise_io()
                msg = (lib.sb_last_error_message() or b"").decode(errors="replace")
                name = (lib.sb_status_name(status) or b"?").decode(errors="replace")
                raise SACDError(f"not a readable Scarletbook disc ({name}: {msg})" if msg else
                                f"not a readable Scarletbook disc ({name})")
            info = SbDiscInfo()
            _check(lib, lib.sb_disc_info(self._disc, C.byref(info)), "sb_disc_info")
            self.sector_size, self.sectors = info.sector_size, info.sectors
        except BaseException:
            self.close()
            raise

    def _raise_io(self) -> None:
        if self._failure:
            msg = self._failure[0]
            self._failure.clear()
            raise OSError(f"reading the image failed: {msg}")

    def read_sectors(self, lsn: int, count: int) -> bytes:
        """count 2048-byte logical sectors from lsn, whatever the image's sector size."""
        from .toc import SECTOR_SIZE, SACDError

        if lsn < 0 or count < 0 or lsn + count > self.sectors:
            raise SACDError(f"short read at LSN {lsn} (+{count}): the image has {self.sectors} sectors")
        buf = C.create_string_buffer(count * SECTOR_SIZE)
        status = self._lib.sb_disc_read_sectors(self._disc, lsn, count, buf, len(buf))
        if status < 0:
            self._raise_io()
            _check_toc(self._lib, status, f"reading LSN {lsn} (+{count})")
        return buf.raw

    def toc(self) -> Disc:
        try:
            return _build(self._lib, self._disc)
        except SbFailure as e:
            from .toc import SACDError
            self._raise_io()
            raise SACDError(str(e)) from e

    def close(self) -> None:
        if getattr(self, "_disc", None):
            self._lib.sb_disc_free(self._disc)
            self._disc = C.c_void_p()
        if getattr(self, "_owns", False) and not self._fh.closed:
            self._fh.close()


def _check_toc(lib, status: int, what: str) -> None:
    from .toc import SACDError
    try:
        _check(lib, status, what)
    except SbFailure as e:
        raise SACDError(str(e)) from e


def _timecode(frames: int):
    from .toc import FRAME_RATE, Timecode
    return Timecode(frames // (60 * FRAME_RATE), frames // FRAME_RATE % 60, frames % FRAME_RATE)


def _as_stored(frames: int, stored):
    """scarletbook's frame count, written as the disc stores it when that's the same count: a
    stored 255:255:255 stays that (as sacd_extract and pysacd 0.2 print it in -P, cue, XML and
    DSDIFF markers), and a time scarletbook cut to the area's play time is written as cut."""
    from .toc import Timecode
    if stored is not None and len(stored) == 3:
        t = Timecode(*stored)
        if t.total_frames == frames:
            return t
    return _timecode(frames)


def _build(lib, disc) -> Disc:
    from .toc import Area, Disc, Genre, Locale, Track

    info = SbDiscInfo()
    _check(lib, lib.sb_disc_info(disc, C.byref(info)), "sb_disc_info")

    count = C.c_uint32()
    _check(lib, lib.sb_disc_text_channel_count(disc, C.byref(count)), "sb_disc_text_channel_count")
    locales = []
    for i in range(min(count.value, 8)):
        ch = SbTextChannel()
        _check(lib, lib.sb_disc_text_channel(disc, i, C.byref(ch)), "sb_disc_text_channel")
        locales.append(Locale(_language(ch), ch.charset))

    text = {}
    for i, name in enumerate(_DISC_TEXT):
        s = _text(lib, lib.sb_disc_string, disc, i)
        if s:
            text[name] = s

    def genres(which: int) -> list[Genre]:
        out = []
        for i in range(4):
            g = SbGenre()
            _check(lib, lib.sb_disc_genre(disc, which, i, C.byref(g)), "sb_disc_genre")
            out.append(Genre(g.category, g.index))
        return out

    def catalog(field: int) -> str:
        return _fixed_str(_copy(lib, lib.sb_disc_catalog_number_stored, disc, field) or b"")

    manufacturer = b""
    if info.manufacturer_sector:
        manufacturer = _copy(lib, lib.sb_disc_manufacturer, disc) or b""

    out = Disc(
        version=(info.version_major, info.version_minor),
        album_set_size=info.album_set_size,
        album_sequence_number=info.album_sequence_number,
        album_catalog_number=catalog(ALBUM_CATALOG),
        album_genres=genres(0),
        disc_catalog_number=catalog(DISC_CATALOG),
        disc_genres=genres(1),
        hybrid=bool(info.hybrid),
        date=(info.year, info.month, info.day),
        locales=locales,
        text=text,
        manufacturer_info=manufacturer,
        areas=[],
    )

    by_slot = {}
    for index in range(info.area_count):
        a = SbAreaInfo()
        _check(lib, lib.sb_disc_area(disc, index, C.byref(a)), "sb_disc_area")
        d = SbAreaDetails()
        _check(lib, lib.sb_area_details(disc, a.slot, C.byref(d)), "sb_area_details")
        stored = SbTimecode()
        _check(lib, lib.sb_area_playtime_stored(disc, a.slot, C.byref(stored)), "sb_area_playtime_stored")
        stored_play = (stored.minutes, stored.seconds, stored.frames)
        area_locales = []
        for i in range(min(d.text_channel_count, 10)):
            ch = SbTextChannel()
            _check(lib, lib.sb_area_text_channel(disc, a.slot, i, C.byref(ch)), "sb_area_text_channel")
            area_locales.append(Locale(_language(ch), ch.charset))
        area = Area(
            kind="TWOCHTOC" if a.kind_channels == 2 else "MULCHTOC",
            version=(d.version_major, d.version_minor),
            toc_start=a.toc_lsn,
            toc_size=a.toc_sectors,
            max_byte_rate=a.max_byte_rate,
            sample_frequency_code=a.sample_frequency_code,
            frame_format=a.frame_format & 0x0F,
            channel_count=a.channel_count,
            area_config=a.area_config,
            max_available_channels=d.max_available_channels,
            area_mute_flags=d.mute_flags,
            track_attribute=d.track_attribute,
            total_playtime=_as_stored(a.playtime_frames, stored_play),
            track_offset=d.track_offset,
            track_start=a.audio_start_lsn,
            track_end=a.audio_end_lsn,
            locales=area_locales,
            description=_text(lib, lib.sb_area_string, disc, a.slot, 0),
            copyright=_text(lib, lib.sb_area_string, disc, a.slot, 1),
            description_phonetic=_text(lib, lib.sb_area_string, disc, a.slot, 2),
            copyright_phonetic=_text(lib, lib.sb_area_string, disc, a.slot, 3),
        )
        for number in range(1, a.track_count + 1):
            t = SbTrackInfo()
            _check(lib, lib.sb_track_info(disc, a.slot, number, C.byref(t)), "sb_track_info")
            ts, td = SbTimecode(), SbTimecode()
            _check(lib, lib.sb_track_timecodes_stored(disc, a.slot, number, C.byref(ts), C.byref(td)),
                   "sb_track_timecodes_stored")
            track_text = {}
            for i, name in enumerate(_TRACK_TEXT):
                s = _text(lib, lib.sb_track_string, disc, a.slot, number, i)
                if s:
                    track_text[name] = s
            isrc = None
            if t.has_genre:     # the ISRCs and genres share the SACD_IGL sectors
                raw_isrc = _copy(lib, lib.sb_track_string, disc, a.slot, number, TRACK_ISRC) or b""
                # UTF-8 from the library; then cut at a NUL, spaces trimmed (pysacd 0.2's rendering)
                isrc = raw_isrc.decode("utf-8", errors="replace").split("\0", 1)[0].strip() or None
            area.tracks.append(Track(
                number=t.number,
                start_lsn=t.start_lsn,
                length_lsn=t.length_sectors,
                start=_as_stored(t.start_frame, (ts.minutes, ts.seconds, ts.frames)),
                duration=_as_stored(t.duration_frames, (td.minutes, td.seconds, td.frames)),
                flags=t.duration_flags,
                isrc=isrc,
                genre=Genre(t.genre_category, t.genre_index) if t.has_genre else None,
                text=track_text,
                start_flags=t.start_flags,
            ))
        by_slot[a.slot] = area
        out.areas.append(area)

    for i in range(info.warning_count):
        w = SbWarning()
        _check(lib, lib.sb_disc_warning(disc, i, C.byref(w)), "sb_disc_warning")
        target = by_slot.get(w.slot)
        (target.warnings if target is not None else out.warnings).append(_warning_text(w))
    return out
