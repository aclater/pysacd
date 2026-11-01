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

The TOC comes in one call, `sb_disc_toc` (scarletbook 0.5.2): the whole TOC as one buffer in a
fixed, versioned little-endian layout (scarletbook.h, `SbTocHeader` and the records after it),
unpacked here with `struct`. It replaced about 100 to 160 per-field ctypes calls per disc, which
made a TOC read 4.5 times slower than pysacd 0.2's parser (#94, scarletbook-rs#132).

Bindings follow scarletbook's README ("Using from Python (ctypes)") and scarletbook.h.
"""

from __future__ import annotations

import ctypes as C
import logging
import os
import struct
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .toc import Disc

log = logging.getLogger("pysacd.toc")

# SbStatus values used here
OK, END, ABSENT = 0, 1, 2
BUFFER_TOO_SMALL = -11
IO = -12
NO_SUCH_AREA = -8


class SbDiscInfo(C.Structure):
    _fields_ = [("sectors", C.c_uint64), ("sector_size", C.c_uint32), ("version_major", C.c_uint8),
                ("version_minor", C.c_uint8), ("album_set_size", C.c_uint16),
                ("album_sequence_number", C.c_uint16), ("hybrid", C.c_uint8),
                ("manufacturer_sector", C.c_uint8), ("year", C.c_uint16), ("month", C.c_uint8),
                ("day", C.c_uint8), ("area_count", C.c_uint32), ("warning_count", C.c_uint32)]


# Sizes stated in scarletbook.h: a mismatch means this mirror is wrong for the library
_SIZES = {SbDiscInfo: 32}

# The sb_disc_toc layout (scarletbook.h), format SB_TOC_MAJOR 1. Every integer is little-endian.
TOC_MAGIC = b"SBTOC\0\0\0"
TOC_MAJOR = 1
TOC_ABSENT = 0xFFFFFFFF
TOC_FIRST = 64 << 10        # scarletbook's advice: 64 KiB is enough for typical discs
MAX_TOC = 64 << 20          # a bound on what a buggy size could ask for
_HEADER = struct.Struct("<8sHHII4HIIIIIII")                 # SbTocHeader, 56 bytes
_DISC_INFO = struct.Struct("<QIBBHHBBHBBII")                 # SbDiscInfo, 32 bytes
_AREA_INFO = struct.Struct("<13I")                           # SbAreaInfo, 52 bytes
_TRACK_INFO = struct.Struct("<5I5B3x")                       # SbTrackInfo, 28 bytes
_WARNING = struct.Struct("<qiIII")                           # SbTocWarning, 24 bytes
_STR = struct.Struct("<II")                                  # SbTocStr
_STRS: dict[int, struct.Struct] = {}                         # runs of SbTocStr, by count
# the records' sizes in format 1.0: a layout's own sizes may only be larger (a later minor
# version appends fields)
_RECORD_SIZES = (256, 152, 168, 24)
_DISC_STRINGS, _DISC_CATALOGS, _DISC_MANUFACTURER = 88, 232, 248   # SbTocDisc offsets
_AREA_DETAILS, _AREA_PLAYTIME, _AREA_TRACKS, _AREA_STRINGS, _AREA_CHANNELS = 52, 70, 76, 80, 112
_TRACK_STORED, _TRACK_STRINGS = 28, 36

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
        bind("sb_disc_open_file_with_log", S, C.c_char_p, LOG_FN, H, C.c_int32, P(H))
        bind("sb_disc_free", None, H)
        bind("sb_disc_info", S, H, P(SbDiscInfo))
        bind("sb_disc_read_sectors", S, H, C.c_uint64, U32, C.c_char_p, SIZE)
        bind("sb_disc_toc", S, H, U32, C.c_char_p, SIZE, P(SIZE))                   # 0.5.2
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


def _language(raw: bytes) -> str:
    # the two bytes as latin-1, trailing NULs and spaces removed (pysacd 0.2's rendering)
    return raw.decode("latin-1").rstrip("\0 ")


def _fixed_str(raw: bytes) -> str:
    # bytes already cut at the first NUL: latin-1, whitespace trimmed (pysacd 0.2's rendering)
    return raw.split(b"\0", 1)[0].decode("latin-1").strip()


def _warning_text(code: str, slot: int, track: int, lsn: int) -> str:
    where = []
    if slot >= 0:
        where.append(f"{SLOT_NAMES.get(slot, slot)} area")
    if track:
        where.append(f"track {track}")
    if lsn >= 0:
        where.append(f"LSN {lsn}")
    return f"{code} ({', '.join(where)})" if where else code


def _on_log(_user, level, message):
    try:
        # at debug: what scarletbook warns about also comes back as the disc's warnings
        log.log(min(_LEVELS.get(level, logging.DEBUG), logging.DEBUG), "scarletbook: %s",
                (message or b"").decode(errors="replace"))
    except Exception:  # noqa: BLE001, S110 - a log line that can't be logged mustn't unwind into C
        pass


_LOG_CALLBACK = LOG_FN(_on_log)     # one for every disc, alive as long as the module


def _utf8_path(source) -> bytes | None:
    """source as the UTF-8 path sb_disc_open_file takes, or None when it has none (bytes or
    undecodable names on POSIX: those go through a Python file object instead)."""
    path = os.fspath(source)
    if not isinstance(path, str):
        return None
    try:
        return path.encode("utf-8")
    except UnicodeEncodeError:
        return None


class SbImage:
    """An image opened with scarletbook: toc.SACDImage's engine. A path is opened by the library
    itself (sb_disc_open_file); a Python file object is read through a callback. Raises
    toc.SACDError when scarletbook rejects the image, OSError when reading it fails, NativeError
    when the engine can't be loaded."""

    def __init__(self, source):
        from . import _native
        from .toc import SACDError

        self._disc = C.c_void_p()
        self._owns = False
        self._fh = None
        self._failure: list[str] = []
        try:
            self._lib = _lib()
        except _native.NativeError as e:
            raise SACDError(f"reading a disc image needs pysacd's native engine, which isn't available ({e})") from e
        if isinstance(source, (str, os.PathLike)):
            # The library reads the file itself: no Python call per read, which was most of the
            # cost of opening (#94). If it can't read the file, it is opened here instead, so a
            # missing or unreadable file raises the OSError it always did.
            path = _utf8_path(source)
            if path is not None and self._open_file(path, source):
                return
            self._fh = open(source, "rb")  # noqa: SIM115 - closed by close()
            self._owns = True
        else:
            self._fh, self._owns = source, False
        self._open_callbacks(self._fh)

    def _open_file(self, path: bytes, source) -> bool:
        """Open path with the library; False when it can't read it (an I/O error)."""
        lib = self._lib
        level = 4 if log.isEnabledFor(logging.DEBUG) else 0
        status = lib.sb_disc_open_file_with_log(path, _LOG_CALLBACK, None, level, C.byref(self._disc))
        if status == IO:
            log.debug("scarletbook can't read %s itself (%s); reading it through Python", source,
                      (lib.sb_last_error_message() or b"").decode(errors="replace"))
            self._disc = C.c_void_p()
            return False
        try:
            self._opened(status)
        except BaseException:
            self.close()
            raise
        return True

    def _opened(self, status: int) -> None:
        from .toc import SACDError
        lib = self._lib
        if status < 0:
            self._raise_io()
            msg = (lib.sb_last_error_message() or b"").decode(errors="replace")
            name = (lib.sb_status_name(status) or b"?").decode(errors="replace")
            raise SACDError(f"not a readable Scarletbook disc ({name}: {msg})" if msg else
                            f"not a readable Scarletbook disc ({name})")
        info = SbDiscInfo()
        _check(lib, lib.sb_disc_info(self._disc, C.byref(info)), "sb_disc_info")
        self.sector_size, self.sectors = info.sector_size, info.sectors

    def _open_callbacks(self, fh) -> None:
        lib = self._lib
        self._lock = lock = threading.Lock()
        failure = self._failure

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

        self._callback = READ_AT(read_at)    # alive until the disc is freed
        try:
            pos = fh.tell()
            size = fh.seek(0, os.SEEK_END)
            fh.seek(pos)
            level = 4 if log.isEnabledFor(logging.DEBUG) else 0
            self._opened(lib.sb_disc_open_callbacks_at_with_log(self._callback, size, None, _LOG_CALLBACK,
                                                                None, level, C.byref(self._disc)))
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
            if status == IO:    # the library reading a file itself
                msg = (self._lib.sb_last_error_message() or b"").decode(errors="replace")
                raise OSError(f"reading the image failed: {msg}")
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
        if getattr(self, "_owns", False) and self._fh is not None and not self._fh.closed:
            self._fh.close()


def _check_toc(lib, status: int, what: str) -> None:
    from .toc import SACDError
    try:
        _check(lib, status, what)
    except SbFailure as e:
        raise SACDError(str(e)) from e


def _as_stored(frames: int, stored):
    """scarletbook's frame count, written as the disc stores it when that's the same count: a
    stored 255:255:255 stays that (as sacd_extract and pysacd 0.2 print it in -P, cue, XML and
    DSDIFF markers), and a time scarletbook cut to the area's play time is written as cut."""
    from .toc import FRAME_RATE, Timecode
    if stored is not None and len(stored) == 3:
        m, s, f = stored
        if (m * 60 + s) * FRAME_RATE + f == frames:
            return Timecode(m, s, f)
    return Timecode(frames // (60 * FRAME_RATE), frames // FRAME_RATE % 60, frames % FRAME_RATE)


def _toc_bytes(lib, disc) -> bytes:
    """The disc's sb_disc_toc layout: one call into 64 KiB, a second of the exact size when
    that is too small."""
    n = C.c_size_t()
    buf = C.create_string_buffer(TOC_FIRST)
    status = lib.sb_disc_toc(disc, 0, buf, len(buf), C.byref(n))
    if status == BUFFER_TOO_SMALL:
        if n.value > MAX_TOC:
            raise SbFailure(BUFFER_TOO_SMALL, f"sb_disc_toc: a layout of {n.value} bytes")
        buf = C.create_string_buffer(n.value)
        status = lib.sb_disc_toc(disc, 0, buf, len(buf), C.byref(n))
    _check(lib, status, "sb_disc_toc")
    if n.value > len(buf):
        raise SbFailure(BUFFER_TOO_SMALL, f"sb_disc_toc: {n.value} bytes in a buffer of {len(buf)}")
    return buf.raw[:n.value]


class _Layout:
    """Bounds-checked reads of a sb_disc_toc layout. It comes from the library, not the disc,
    but every offset and length is checked anyway: a bad one is an SbFailure, never a wrong
    field."""

    def __init__(self, data: bytes):
        self.data = data
        if len(data) < _HEADER.size:
            self.fail(f"{len(data)} bytes, shorter than its header")
        (magic, major, _minor, size, self.disc_offset, *self.sizes, self.areas_offset, self.area_count,
         self.warnings_offset, self.warning_count, _strings, _strings_size, _reserved) = _HEADER.unpack_from(data)
        if magic != TOC_MAGIC or major != TOC_MAJOR or size != len(data):
            self.fail(f"format {major}, size {size} of {len(data)}: not a layout this pysacd reads")
        if any(got < want for got, want in zip(self.sizes, _RECORD_SIZES, strict=True)):
            self.fail(f"record sizes {self.sizes}, smaller than format 1.0's {_RECORD_SIZES}")

    @staticmethod
    def fail(why: str):
        raise SbFailure(-1, f"sb_disc_toc: {why}")

    def record(self, offset: int, size: int) -> int:
        if offset + size > len(self.data):
            self.fail(f"a record at {offset} (+{size}) past the end ({len(self.data)})")
        return offset

    def string(self, at: int) -> bytes | None:
        """The SbTocStr at byte at: None when absent, else its bytes."""
        return self.strings(at, 1)[0]

    def strings(self, at: int, count: int) -> list[bytes | None]:
        """count SbTocStr from byte at, as string() gives each."""
        st = _STRS.get(count) or _STRS.setdefault(count, struct.Struct(f"<{2 * count}I"))
        words = st.unpack_from(self.data, at)
        data, end = self.data, len(self.data)
        pairs = list(zip(words[0::2], words[1::2], strict=True))
        for offset, length in pairs:
            if offset != TOC_ABSENT and offset + length > end:
                self.fail(f"a string at {offset} (+{length}) past the end ({end})")
        return [None if offset == TOC_ABSENT else data[offset:offset + length] for offset, length in pairs]

    def text(self, at: int) -> str | None:
        return _decode(self.string(at))


def _decode(raw: bytes | None) -> str | None:
    # UTF-8 from the library; an empty string is no string
    return raw.decode("utf-8", errors="replace") or None if raw else None


def _build(lib, disc) -> Disc:
    from .toc import Area, Disc, Genre, Locale, Track

    L = _Layout(_toc_bytes(lib, disc))
    data = L.data
    dsize, asize, tsize, wsize = L.sizes
    d0 = L.record(L.disc_offset, dsize)
    (_sectors, _sector_size, v_major, v_minor, set_size, set_number, hybrid, manufacturer_sector,
     year, month, day, area_count, warning_count) = _DISC_INFO.unpack_from(data, d0)
    if area_count != L.area_count or warning_count != L.warning_count:
        L.fail(f"{L.area_count} area and {L.warning_count} warning records for a disc of "
               f"{area_count} and {warning_count}")

    def genres(at: int) -> list[Genre]:
        return [Genre(data[at + 2 * i], data[at + 2 * i + 1]) for i in range(4)]

    locales = [Locale(_language(data[at:at + 2]), data[at + 2])
               for at in range(d0 + 52, d0 + 52 + 4 * min(data[d0 + 48], 8), 4)]
    text = {}
    for name, raw in zip(_DISC_TEXT, L.strings(d0 + _DISC_STRINGS, len(_DISC_TEXT)), strict=True):
        s = _decode(raw)
        if s:
            text[name] = s
    manufacturer = (L.string(d0 + _DISC_MANUFACTURER) or b"") if manufacturer_sector else b""

    out = Disc(
        version=(v_major, v_minor),
        album_set_size=set_size,
        album_sequence_number=set_number,
        album_catalog_number=_fixed_str(L.string(d0 + _DISC_CATALOGS) or b""),
        album_genres=genres(d0 + 32),
        disc_catalog_number=_fixed_str(L.string(d0 + _DISC_CATALOGS + 8) or b""),
        disc_genres=genres(d0 + 40),
        hybrid=bool(hybrid),
        date=(year, month, day),
        locales=locales,
        text=text,
        manufacturer_info=manufacturer,
        areas=[],
    )

    by_slot = {}
    for index in range(L.area_count):
        a0 = L.record(L.areas_offset + index * asize, asize)
        (slot, kind_channels, toc_lsn, toc_sectors, channel_count, area_config, fs_code, frame_format,
         max_byte_rate, playtime_frames, audio_start, audio_end, track_count) = _AREA_INFO.unpack_from(data, a0)
        (d_major, d_minor, max_channels, mute_flags, track_attribute, track_offset, text_channel_count,
         _charset) = data[a0 + _AREA_DETAILS:a0 + _AREA_DETAILS + 8]
        tracks_offset = int.from_bytes(data[a0 + _AREA_TRACKS:a0 + _AREA_TRACKS + 4], "little")
        area_text = L.strings(a0 + _AREA_STRINGS, 4)
        area = Area(
            kind="TWOCHTOC" if kind_channels == 2 else "MULCHTOC",
            version=(d_major, d_minor),
            toc_start=toc_lsn,
            toc_size=toc_sectors,
            max_byte_rate=max_byte_rate,
            sample_frequency_code=fs_code,
            frame_format=frame_format & 0x0F,
            channel_count=channel_count,
            area_config=area_config,
            max_available_channels=max_channels,
            area_mute_flags=mute_flags,
            track_attribute=track_attribute,
            total_playtime=_as_stored(playtime_frames, tuple(data[a0 + _AREA_PLAYTIME:a0 + _AREA_PLAYTIME + 3])),
            track_offset=track_offset,
            track_start=audio_start,
            track_end=audio_end,
            locales=[Locale(_language(data[at:at + 2]), data[at + 2])
                     for at in range(a0 + _AREA_CHANNELS, a0 + _AREA_CHANNELS + 4 * min(text_channel_count, 10), 4)],
            description=_decode(area_text[0]),
            copyright=_decode(area_text[1]),
            description_phonetic=_decode(area_text[2]),
            copyright_phonetic=_decode(area_text[3]),
        )
        L.record(tracks_offset, track_count * tsize)
        for k in range(track_count):
            t0 = tracks_offset + k * tsize
            (number, start_lsn, length_sectors, start_frame, duration_frames, start_flags, duration_flags,
             has_genre, genre_category, genre_index) = _TRACK_INFO.unpack_from(data, t0)
            raws = L.strings(t0 + _TRACK_STRINGS, TRACK_ISRC + 1)
            track_text = {}
            for name, raw in zip(_TRACK_TEXT, raws, strict=False):
                if raw:
                    s = _decode(raw)
                    if s:
                        track_text[name] = s
            isrc = None
            if has_genre:     # the ISRCs and genres share the SACD_IGL sectors
                raw_isrc = raws[TRACK_ISRC] or b""
                # UTF-8 from the library; then cut at a NUL, spaces trimmed (pysacd 0.2's rendering)
                isrc = raw_isrc.decode("utf-8", errors="replace").split("\0", 1)[0].strip() or None
            st = t0 + _TRACK_STORED
            area.tracks.append(Track(
                number=number,
                start_lsn=start_lsn,
                length_lsn=length_sectors,
                start=_as_stored(start_frame, tuple(data[st:st + 3])),
                duration=_as_stored(duration_frames, tuple(data[st + 3:st + 6])),
                flags=duration_flags,
                isrc=isrc,
                genre=Genre(genre_category, genre_index) if has_genre else None,
                text=track_text,
                start_flags=start_flags,
            ))
        by_slot[slot] = area
        out.areas.append(area)

    for i in range(L.warning_count):
        w0 = L.record(L.warnings_offset + i * wsize, wsize)
        lsn, slot, track, _offset, _length = _WARNING.unpack_from(data, w0)
        raw = L.string(w0 + 16)
        code = raw.decode(errors="replace") if raw else "?"
        target = by_slot.get(slot)
        (target.warnings if target is not None else out.warnings).append(_warning_text(code, slot, track, lsn))
    return out
