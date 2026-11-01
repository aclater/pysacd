"""SACD (Scarletbook) disc images: the TOC as Python objects, and the image's sectors.

scarletbook-rs, linked into pysacd's engine, reads everything (#85): the sector layout (2048,
2054 or 2064-byte sectors), the master and area TOCs, and the sectors themselves.
src/pysacd/_sbtoc.py binds its C interface; this module holds the objects and SACDImage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import BinaryIO, Self

SECTOR_SIZE = 2048
START_OF_MASTER_TOC = 510
MASTER_TOC_LEN = 10
FRAME_RATE = 75  # SACD frames per second (timecode "frames" field)
SAMPLES_PER_FRAME = 588
MAX_FRAMES_PER_SECTOR = 7   # an audio sector's header can start at most 7 frames (#17)

FRAME_FORMAT_DST = 0
FRAME_FORMAT_DSD_3_IN_14 = 2
FRAME_FORMAT_DSD_3_IN_16 = 3

FRAME_FORMATS = {
    FRAME_FORMAT_DST: "DST",
    FRAME_FORMAT_DSD_3_IN_14: "DSD 3 in 14",
    FRAME_FORMAT_DSD_3_IN_16: "DSD 3 in 16",
}

# Character set codes of the locale tables, as Python codecs (Locale.codec). Codes 0/1 are
# ISO 646 IRV (ASCII); latin-1 so that stray high bytes don't raise.
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

# Track text item types (SACDTTxt): the keys of Track.text
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


class SACDError(Exception):
    """Raised when an image is not a readable Scarletbook disc."""


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
    slot: int = -1               # scarletbook's slot: 0 the stereo area, 1 the multichannel one

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


class SACDImage:
    """An SACD disc image, opened with scarletbook-rs.

    Accepts a path or an already-open binary file object (anything with seek/read: a file,
    a block device, remote.RemoteDisc). Plain 2048-byte and raw 2064/2054-byte sector images
    are detected by scarletbook. read_sectors() always returns 2048-byte logical sectors.
    Raises SACDError for an image scarletbook can't read as a Scarletbook disc. A handle is
    used by one thread at a time.
    """

    def __init__(self, source: str | os.PathLike | BinaryIO):
        from . import _sbtoc
        self._disc = _sbtoc.SbImage(source)
        self.sector_size = self._disc.sector_size
        self.data_offset = {2048: 0, 2054: 6, 2064: 12}.get(self.sector_size, 0)

    def read_sectors(self, lsn: int, count: int) -> bytes:
        return self._disc.read_sectors(lsn, count)

    @property
    def total_sectors(self) -> int:
        return self._disc.sectors

    def read_toc(self) -> Disc:
        return self._disc.toc()

    def close(self) -> None:
        self._disc.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def area_frame_capacity(area: Area, total_sectors: int) -> int:
    """The most frames the area's audio sectors in an image of total_sectors can hold."""
    last = min(area.track_end, total_sectors - 1)
    return max(0, last - area.track_start + 1) * MAX_FRAMES_PER_SECTOR


def read_toc(source: str | os.PathLike | BinaryIO) -> Disc:
    """Open an SACD ISO (path or binary file object) and return its TOC."""
    with SACDImage(source) as img:
        return img.read_toc()


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
