"""Scarletbook (SACD) TOC parser.

Layouts follow sacd-ripper's libsacd (scarletbook.h / scarletbook_read.c).
All multi-byte integers on disc are big-endian; one logical sector is 2048 bytes.
"""

from __future__ import annotations

import io
import os
import re
import struct
from dataclasses import dataclass, field
from typing import BinaryIO, Self, cast

SECTOR_SIZE = 2048
START_OF_MASTER_TOC = 510
MASTER_TOC_LEN = 10
FRAME_RATE = 75  # SACD frames per second (timecode "frames" field)
SAMPLES_PER_FRAME = 588
MAX_LANGUAGE_COUNT = 8
SUPPORTED_VERSION = (1, 20)

# Bounds on values a hostile image controls (#17). Real discs stay far inside them: area TOCs
# of 5-41 sectors, at most 0.6 frames per audio sector, titles under 100 bytes.
MAX_AREA_TOC_SECTORS = 128
MAX_FRAMES_PER_SECTOR = 7   # an audio sector's header can start at most 7 frames
MAX_TEXT_BYTES = 4096       # one TOC string, before its terminating NUL

FRAME_FORMAT_DST = 0
FRAME_FORMAT_DSD_3_IN_14 = 2
FRAME_FORMAT_DSD_3_IN_16 = 3

FRAME_FORMATS = {
    FRAME_FORMAT_DST: "DST",
    FRAME_FORMAT_DSD_3_IN_14: "DSD 3 in 14",
    FRAME_FORMAT_DSD_3_IN_16: "DSD 3 in 16",
}

# Character set codes from the locale tables, mapped to Python codecs.
# Codes 0/1 are ISO 646 IRV (ASCII); latin-1 is used so stray high bytes don't raise.
CHARSETS = {
    0: "latin-1",
    1: "latin-1",
    2: "latin-1",
    3: "shift_jisx0213",  # MusicShiftJIS (RIS-506)
    4: "euc_kr",          # KSC 5601-1987
    5: "gb2312",
    6: "big5",
    7: "latin-1",
}

GENRES = (
    "Not used", "Not defined", "Adult Contemporary", "Alternative Rock",
    "Children's Music", "Classical", "Contemporary Christian", "Country",
    "Dance", "Easy Listening", "Erotic", "Folk", "Gospel", "Hip Hop", "Jazz",
    "Latin", "Musical", "New Age", "Opera", "Operetta", "Pop Music", "RAP",
    "Reggae", "Rock Music", "Rhythm & Blues", "Sound Effects", "Sound Track",
    "Spoken Word", "World Music", "Blues",
)
CATEGORIES = ("Not used", "General", "Japanese")

# Track text item types (SACDTTxt)
TRACK_TEXT_TYPES = {
    0x01: "title",
    0x02: "performer",
    0x03: "songwriter",
    0x04: "composer",
    0x05: "arranger",
    0x06: "message",
    0x07: "extra_message",
    0x81: "title_phonetic",
    0x82: "performer_phonetic",
    0x83: "songwriter_phonetic",
    0x84: "composer_phonetic",
    0x85: "arranger_phonetic",
    0x86: "message_phonetic",
    0x87: "extra_message_phonetic",
}

# Order of the 16 u16 string offsets in a master SACDText sector
_MASTER_TEXT_FIELDS = (
    "album_title", "album_artist", "album_publisher", "album_copyright",
    "album_title_phonetic", "album_artist_phonetic",
    "album_publisher_phonetic", "album_copyright_phonetic",
    "disc_title", "disc_artist", "disc_publisher", "disc_copyright",
    "disc_title_phonetic", "disc_artist_phonetic",
    "disc_publisher_phonetic", "disc_copyright_phonetic",
)


class SACDError(Exception):
    """Raised when an image is not a readable Scarletbook disc."""


# Which parser SACDImage.read_toc() uses, unless PYSACD_TOC_ENGINE says otherwise (#85):
# "scarletbook" (scarletbook-rs, through the native engine) or "python" (pysacd's own, below).
TOC_ENGINES = ("scarletbook", "python")
DEFAULT_TOC_ENGINE = "python"


@dataclass(frozen=True)
class Timecode:
    minutes: int
    seconds: int
    frames: int

    @property
    def total_frames(self) -> int:
        return (self.minutes * 60 + self.seconds) * FRAME_RATE + self.frames

    @property
    def seconds_float(self) -> float:
        return self.total_frames / FRAME_RATE

    def __str__(self) -> str:
        return f"{self.minutes:02d}:{self.seconds:02d}:{self.frames:02d}"


@dataclass(frozen=True)
class Genre:
    category: int
    genre: int

    @property
    def category_name(self) -> str:
        return CATEGORIES[self.category] if self.category < len(CATEGORIES) else f"Unknown({self.category})"

    @property
    def name(self) -> str:
        return GENRES[self.genre] if self.genre < len(GENRES) else f"Unknown({self.genre})"

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class Locale:
    language: str
    charset: int

    @property
    def codec(self) -> str:
        return CHARSETS.get(self.charset & 0x07, "latin-1")


@dataclass
class Track:
    number: int                  # 1-based within the area
    start_lsn: int
    length_lsn: int
    start: Timecode
    duration: Timecode
    flags: int                   # raw flag byte from SACDTRL2 duration entry
    isrc: str | None = None
    genre: Genre | None = None
    text: dict[str, str] = field(default_factory=dict)
    start_flags: int = 0         # raw flag byte from SACDTRL2 start entry

    @property
    def title(self) -> str | None:
        return self.text.get("title")

    @property
    def performer(self) -> str | None:
        return self.text.get("performer")


@dataclass
class Area:
    kind: str                    # "TWOCHTOC" or "MULCHTOC"
    version: tuple[int, int]
    toc_start: int               # LSN of the area TOC that was used
    toc_size: int                # sectors
    max_byte_rate: int
    sample_frequency_code: int   # 4 == 64 * 44.1 kHz
    frame_format: int
    channel_count: int
    area_config: int             # raw byte: speaker config (low 3 bits) + extra bits
    max_available_channels: int
    area_mute_flags: int
    track_attribute: int
    total_playtime: Timecode
    track_offset: int
    track_start: int             # first LSN of audio data
    track_end: int               # last LSN of audio data
    locales: list[Locale]
    description: str | None = None
    copyright: str | None = None
    description_phonetic: str | None = None
    copyright_phonetic: str | None = None
    tracks: list[Track] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_stereo(self) -> bool:
        return self.kind == "TWOCHTOC"

    @property
    def is_multichannel(self) -> bool:
        return self.kind == "MULCHTOC"

    @property
    def is_dst(self) -> bool:
        return self.frame_format == FRAME_FORMAT_DST

    @property
    def frame_format_name(self) -> str:
        return FRAME_FORMATS.get(self.frame_format, f"Unknown({self.frame_format})")

    @property
    def sample_rate(self) -> int:
        # Only 64fs (2.8224 MHz) is defined by Scarletbook
        return 2822400 if self.sample_frequency_code == 4 else 0

    @property
    def speaker_config(self) -> str:
        cfg = self.area_config & 0x07
        if self.channel_count == 2 and cfg == 0:
            return "Stereo"
        if self.channel_count == 5 and cfg == 3:
            return "5ch"
        if self.channel_count == 6 and cfg == 4:
            return "5.1ch"
        return f"{self.channel_count}ch"


@dataclass
class Disc:
    version: tuple[int, int]
    album_set_size: int
    album_sequence_number: int
    album_catalog_number: str
    album_genres: list[Genre]
    disc_catalog_number: str
    disc_genres: list[Genre]
    hybrid: bool
    date: tuple[int, int, int]   # (year, month, day); zeros when unset
    locales: list[Locale]
    text: dict[str, str]         # album_title, disc_artist, ... from the first SACDText
    manufacturer_info: bytes
    areas: list[Area]
    warnings: list[str] = field(default_factory=list)

    @property
    def stereo(self) -> Area | None:
        return next((a for a in self.areas if a.is_stereo), None)

    @property
    def multichannel(self) -> Area | None:
        return next((a for a in self.areas if a.is_multichannel), None)

    @property
    def title(self) -> str | None:
        return self.text.get("disc_title") or self.text.get("album_title")

    @property
    def artist(self) -> str | None:
        return self.text.get("disc_artist") or self.text.get("album_artist")


# ---------------------------------------------------------------- helpers

def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from(">H", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from(">I", buf, off)[0]


def _cstring(buf: bytes, off: int) -> bytes:
    # Bounded: an unterminated string must not cost a scan of the whole TOC per lookup
    end = buf.find(b"\0", off, off + MAX_TEXT_BYTES)
    return buf[off:off + MAX_TEXT_BYTES] if end < 0 else buf[off:end]


_NONZERO = re.compile(rb"[^\0]")


def _skip_zeros(buf: bytes, off: int) -> int:
    m = _NONZERO.search(buf, off)
    return m.start() if m else len(buf)


def _decode(raw: bytes, codec: str) -> str:
    # Decode first, then check characters, never bytes: in Shift-JIS or Big5 a trail byte can
    # be 0x5C, which isn't a backslash there (#47). Invalid sequences become U+FFFD, never lone
    # surrogates, which can't be written or put in JSON.
    # Some discs carry control chars in text (sacd-ripper replaces them with spaces).
    # Leading/trailing spaces are kept: sacd_extract writes them into tags and files.
    return "".join(max(c, " ") for c in raw.decode(codec, errors="replace"))


def _text_at(buf: bytes, off: int, codec: str) -> str | None:
    if not off or off >= len(buf):
        return None
    s = _decode(_cstring(buf, off), codec)
    return s or None


def _fixed_str(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin-1").strip()


def _genres(buf: bytes, off: int, count: int) -> list[Genre]:
    return [Genre(buf[off + i * 4], buf[off + i * 4 + 3]) for i in range(count)]


def _locales(buf: bytes, off: int, count: int) -> list[Locale]:
    out = []
    for i in range(count):
        p = off + i * 4
        out.append(Locale(buf[p:p + 2].decode("latin-1").rstrip("\0 "), buf[p + 2]))
    return out


def _timecode(buf: bytes, off: int) -> Timecode:
    return Timecode(buf[off], buf[off + 1], buf[off + 2])


# ---------------------------------------------------------------- reader

# (physical sector size, offset of the 2048 user-data bytes within it)
SECTOR_LAYOUTS = (
    (2048, 0),    # plain ISO (sacd_extract -I, most rips)
    (2064, 12),   # raw DVD sectors: ID+IED+CPR_MAI header, 2048 data, 4-byte EDC
    (2054, 6),    # raw sectors with only the ID+IED header
)


class SACDImage:
    """Random-access reader for an SACD ISO image.

    Accepts a path or an already-open binary file object (anything with
    seek/read, e.g. a block device opened by the caller). Plain 2048-byte
    and raw 2064/2054-byte sector images are detected automatically.
    read_sectors() always returns 2048-byte logical sectors.
    """

    def __init__(self, source: str | os.PathLike | BinaryIO | io.RawIOBase):
        if isinstance(source, (str, os.PathLike)):
            # Owned for the object's lifetime; closed by close() / __exit__
            self._fh: BinaryIO = open(source, "rb")  # noqa: SIM115
            self._owns = True
        else:
            self._fh = cast(BinaryIO, source)   # anything with seek/read: files, RemoteDisc
            self._owns = False
        try:
            self.sector_size, self.data_offset = self._detect_layout()
        except Exception:
            self.close()
            raise

    def _detect_layout(self) -> tuple[int, int]:
        for size, offset in SECTOR_LAYOUTS:
            self._fh.seek(START_OF_MASTER_TOC * size + offset)
            if self._fh.read(8) == b"SACDMTOC":
                return size, offset
        raise SACDError("not a Scarletbook disc (no SACDMTOC at LSN 510)")

    def read_sectors(self, lsn: int, count: int) -> bytes:
        size = self.sector_size
        self._fh.seek(lsn * size)
        raw = self._fh.read(count * size)
        if len(raw) != count * size:
            raise SACDError(f"short read at LSN {lsn} (+{count}): image truncated?")
        if size == SECTOR_SIZE:
            return raw
        off = self.data_offset
        return b"".join(raw[i + off:i + off + SECTOR_SIZE] for i in range(0, len(raw), size))

    @property
    def total_sectors(self) -> int:
        pos = self._fh.tell()
        size = self._fh.seek(0, os.SEEK_END)
        self._fh.seek(pos)
        return size // self.sector_size

    def close(self) -> None:
        if self._owns:
            self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ parsing

    def read_toc(self, engine: str | None = None) -> Disc:
        """Parse the TOC with `engine`: "scarletbook" (scarletbook-rs, through the native engine)
        or "python" (pysacd's own parser below). The default is $PYSACD_TOC_ENGINE, else
        DEFAULT_TOC_ENGINE (#85)."""
        engine = engine or os.environ.get("PYSACD_TOC_ENGINE") or DEFAULT_TOC_ENGINE
        if engine == "python":
            return self.read_toc_python()
        if engine != "scarletbook":
            raise ValueError(f"unknown TOC engine {engine!r} (scarletbook or python)")
        from . import _native, _sbtoc
        try:
            return _sbtoc.read_toc(self)
        except _native.NativeError as e:
            raise SACDError(f"the TOC needs the native engine, which isn't available ({e}); "
                            "PYSACD_TOC_ENGINE=python uses pysacd's own parser") from e

    def read_toc_python(self) -> Disc:
        """pysacd's own TOC parser, the default before 0.3.0 (#85)."""
        mtoc = self.read_sectors(START_OF_MASTER_TOC, MASTER_TOC_LEN)
        disc = _parse_master_toc(mtoc)

        a1 = (_u32(mtoc, 64), _u32(mtoc, 68), _u16(mtoc, 84))
        a2 = (_u32(mtoc, 72), _u32(mtoc, 76), _u16(mtoc, 86))
        for label, (toc1, toc2, size) in (("2ch", a1), ("multichannel", a2)):
            if not toc1 or not size:
                continue
            area = self._read_area(label, toc1, toc2, size, disc.warnings)
            if area is not None:
                disc.areas.append(area)

        if not disc.areas:
            raise SACDError("no readable audio areas")
        return disc

    def _read_area(self, label: str, toc1: int, toc2: int, size: int,
                   warnings: list[str]) -> Area | None:
        if size > MAX_AREA_TOC_SECTORS:
            warnings.append(f"{label} area TOC size {size} sectors is implausible (at most "
                            f"{MAX_AREA_TOC_SECTORS}); area skipped")
            return None
        # Primary copy first; fall back to the backup (TOC-2) like sacd-ripper
        data = start = None
        try:
            data, start = self.read_sectors(toc1, size), toc1
        except SACDError as e:
            warnings.append(f"{label} area TOC-1 unreadable: {e}")
        if toc2:
            try:
                backup = self.read_sectors(toc2, size)
                if data is None:
                    data, start = backup, toc2
                elif backup != data:
                    warnings.append(f"{label} area TOC-1 and TOC-2 differ; using TOC-1")
            except SACDError as e:
                warnings.append(f"{label} area TOC-2 unreadable: {e}")
        if data is None or start is None:
            return None
        try:
            area = _parse_area_toc(data, start, size)
            _bound_area(area)
            return area
        except SACDError as e:
            warnings.append(f"{label} area: {e}")
            return None
        except (IndexError, struct.error) as e:    # a field points past the end of the TOC
            warnings.append(f"{label} area: malformed TOC ({e})")
            return None


def _parse_master_toc(mtoc: bytes) -> Disc:
    if mtoc[0:8] != b"SACDMTOC":
        raise SACDError("not a Scarletbook disc (no SACDMTOC at LSN 510)")
    version = (mtoc[8], mtoc[9])
    # sacd-ripper also rejects minor > 20; we only reject an unknown major version
    if version[0] > SUPPORTED_VERSION[0]:
        raise SACDError(f"unsupported Master TOC version {version[0]}.{version[1]:02d}")

    locales = _locales(mtoc, 136, MAX_LANGUAGE_COUNT)
    text_area_count = mtoc[128]
    codec = locales[0].codec

    warnings: list[str] = []
    text: dict[str, str] = {}
    # Sectors 1..8 are SACDText (one per language); sacd-ripper only uses the first.
    tsec = mtoc[SECTOR_SIZE:2 * SECTOR_SIZE]
    if tsec[0:8] == b"SACDText":
        for i, name in enumerate(_MASTER_TEXT_FIELDS):
            s = _text_at(tsec, _u16(tsec, 16 + i * 2), codec)
            if s:
                text[name] = s
    elif text_area_count:
        warnings.append("master SACDText sector missing")

    man = mtoc[9 * SECTOR_SIZE:10 * SECTOR_SIZE]
    if man[0:8] != b"SACD_Man":
        warnings.append("SACD_Man sector missing")

    return Disc(
        version=version,
        album_set_size=_u16(mtoc, 16),
        album_sequence_number=_u16(mtoc, 18),
        album_catalog_number=_fixed_str(mtoc[24:40]),
        album_genres=_genres(mtoc, 40, 4),
        disc_catalog_number=_fixed_str(mtoc[88:104]),
        disc_genres=_genres(mtoc, 104, 4),
        hybrid=bool(mtoc[80] & 0x80),
        date=(_u16(mtoc, 120), mtoc[122], mtoc[123]),
        locales=locales[:text_area_count] if text_area_count else [],
        text=text,
        manufacturer_info=man[8:] if man[0:8] == b"SACD_Man" else b"",
        areas=[],
        warnings=warnings,
    )


def _parse_area_toc(data: bytes, toc_start: int, toc_size: int) -> Area:
    kind = data[0:8].decode("latin-1")
    if kind not in ("TWOCHTOC", "MULCHTOC"):
        raise SACDError(f"invalid area TOC signature {data[0:8]!r}")

    track_count = data[69]
    text_area_count = data[80]
    locales = _locales(data, 88, 10)
    codec = locales[0].codec

    area = Area(
        kind=kind,
        version=(data[8], data[9]),
        toc_start=toc_start,
        toc_size=toc_size,
        max_byte_rate=_u32(data, 16),
        sample_frequency_code=data[20],
        frame_format=data[21] & 0x0F,
        channel_count=data[32],
        area_config=data[33],
        max_available_channels=data[34],
        area_mute_flags=data[35],
        track_attribute=data[48] & 0x0F,
        total_playtime=_timecode(data, 64),
        track_offset=data[68],
        track_start=_u32(data, 72),
        track_end=_u32(data, 76),
        locales=locales[:text_area_count] if text_area_count else [],
    )
    area.description = _text_at(data, _u16(data, 144), codec)
    area.copyright = _text_at(data, _u16(data, 146), codec)
    area.description_phonetic = _text_at(data, _u16(data, 148), codec)
    area.copyright_phonetic = _text_at(data, _u16(data, 150), codec)

    lsn_starts = lsn_lengths = None
    times = None
    texts: list[dict[str, str]] = [{} for _ in range(track_count)]
    isrc_genre = None
    seen_text = False

    # Walk the sub-structures that follow the area header sector
    end = toc_size * SECTOR_SIZE
    p = SECTOR_SIZE
    while p + 8 <= end:
        sig = data[p:p + 8]
        if sig == b"SACDTTxt":
            if not seen_text:  # first language only, like sacd-ripper
                _parse_track_text(data, p, track_count, codec, texts, area.warnings)
                seen_text = True
            p += SECTOR_SIZE
        elif sig == b"SACD_IGL":
            isrc_genre = p
            p += 2 * SECTOR_SIZE
        elif sig == b"SACD_ACC":
            p += 32 * SECTOR_SIZE
        elif sig == b"SACDTRL1":
            lsn_starts = struct.unpack_from(f">{track_count}I", data, p + 8)
            lsn_lengths = struct.unpack_from(f">{track_count}I", data, p + 8 + 255 * 4)
            p += SECTOR_SIZE
        elif sig == b"SACDTRL2":
            times = p
            p += SECTOR_SIZE
        else:
            break

    if lsn_starts is None or lsn_lengths is None or times is None:
        raise SACDError(f"{kind}: track list (SACDTRL1/2) missing")

    for i in range(track_count):
        start = _timecode(data, times + 8 + i * 4)
        dur_off = times + 8 + 255 * 4 + i * 4
        isrc = genre = None
        if isrc_genre is not None:
            isrc = _fixed_str(data[isrc_genre + 8 + i * 12:isrc_genre + 20 + i * 12]) or None
            g = isrc_genre + 8 + 255 * 12 + 4 + i * 4
            genre = Genre(data[g], data[g + 3])
        area.tracks.append(Track(
            number=i + 1,
            start_lsn=lsn_starts[i],
            length_lsn=lsn_lengths[i],
            start=start,
            duration=_timecode(data, dur_off),
            flags=data[dur_off + 3],
            isrc=isrc,
            genre=genre,
            text=texts[i],
            start_flags=data[times + 8 + i * 4 + 3],
        ))
    return area


def area_frame_capacity(area: Area, total_sectors: int) -> int:
    """The most frames the area's audio sectors in an image of total_sectors can hold."""
    last = min(area.track_end, total_sectors - 1)
    return max(0, last - area.track_start + 1) * MAX_FRAMES_PER_SECTOR


def _clamp_tc(tc: Timecode, frames: int) -> Timecode:
    if tc.total_frames <= frames:
        return tc
    return Timecode(frames // (60 * FRAME_RATE), frames // FRAME_RATE % 60, frames % FRAME_RATE)


def _bound_area(area: Area) -> None:
    """Check an area's audio range and hold its tracks to its playtime (#17).

    Track timecodes decide how much each output gets, and frames the disc doesn't have become
    silence. A track that runs past the area's playtime is cut to it; real discs never do that.
    How much the playtime itself may ask for is bounded by the image when audio is extracted
    (see _native.run_area). Raises SACDError for an audio range that ends before it starts.
    """
    if area.track_end < area.track_start:
        raise SACDError(f"audio LSN range {area.track_start}-{area.track_end} ends before it starts")
    play = area.total_playtime.total_frames
    for t in area.tracks:
        start, end = t.start.total_frames, t.start.total_frames + t.duration.total_frames
        if end > play:
            area.warnings.append(f"track {t.number}: frames {start}-{end} run past the area's playtime "
                                 f"({play} frames); cut")
            t.start = _clamp_tc(t.start, play)
            t.duration = _clamp_tc(t.duration, play - t.start.total_frames)
        if not area.track_start <= t.start_lsn <= area.track_end:
            area.warnings.append(f"track {t.number}: LSN {t.start_lsn} lies outside the area's audio "
                                 f"(LSN {area.track_start}-{area.track_end})")


def _parse_track_text(data: bytes, base: int, track_count: int, codec: str,
                      out: list[dict[str, str]], warnings: list[str]) -> None:
    for i in range(track_count):
        pos = _u16(data, base + 8 + i * 2)
        if not pos:
            continue
        q = base + pos
        if q + 4 > len(data):
            warnings.append(f"track {i + 1}: text offset {pos} lies outside the area TOC")
            continue
        item_count = data[q]
        q += 4
        for j in range(item_count):
            if q + 2 > len(data):
                break
            ttype = data[q]
            q += 2  # type byte + padding byte (0x20)
            raw = _cstring(data, q)
            if raw:
                name = TRACK_TEXT_TYPES.get(ttype)
                if name is None:
                    warnings.append(f"track {i + 1}: unknown text type 0x{ttype:02x}")
                else:
                    out[i][name] = _decode(raw, codec)
            if j < item_count - 1:
                # Skip the string, then zero padding up to the next item. A string longer than
                # MAX_TEXT_BYTES was cut, so this lands inside it and the next items misparse.
                # That's deliberate: such a string is hostile, and the result stays bounded.
                # Don't "fix" it with an unbounded scan.
                q = _skip_zeros(data, q + len(raw))


def read_toc(source: str | os.PathLike | BinaryIO, engine: str | None = None) -> Disc:
    """Open an SACD ISO (path or binary file object) and return its parsed TOC (see
    SACDImage.read_toc for `engine`)."""
    with SACDImage(source) as img:
        return img.read_toc(engine)


def track_range(area: Area, i: int, trimming: bool) -> tuple[int, int]:
    """Frame range of track i: [start, start+duration) without pauses; with
    pauses, from the track's start up to the next track (track 1 from the
    area start, the last track to the area end)."""
    t = area.tracks[i]
    if trimming:
        return t.start.total_frames, t.start.total_frames + t.duration.total_frames
    start = t.start.total_frames if i else 0
    end = area.tracks[i + 1].start.total_frames if i + 1 < len(area.tracks) else area.total_playtime.total_frames
    return start, max(end, t.start.total_frames + t.duration.total_frames)
