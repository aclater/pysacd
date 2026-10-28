"""sacd_extract-compatible metadata: file naming, ID3, DSDIFF headers, CUE, XML.

Ports of sacd-ripper's scarletbook_helpers.c, scarletbook_id3.c, dsdiff.c,
cuesheet.c and scarletbook_xml.c (plus fileutils.c helpers), aiming for
byte-identical output.  Track indices are 0-based, as in sacd-ripper.

Strings are handled as UTF-8 bytes internally because the C code truncates
on byte counts (it can split a multi-byte character); names are returned
as str decoded with "surrogateescape", so writing them to the filesystem
reproduces sacd_extract's exact bytes.

Known, unavoidable differences (see also the module tests):
  * pysacd strips surrounding whitespace from disc text; sacd-ripper keeps it.
  * Where the C code reads past a converted string (ID3 frames for text that
    shrinks in ISO-8859-1/UTF-16), it copies uninitialised heap bytes; we
    write zeros there.
  * Raw catalog numbers and ISRCs come from the stripped TOC fields; pass
    raw_catalog_numbers() to metadata_xml() for the exact padded values.
"""

from __future__ import annotations

import os
import struct
from datetime import datetime

from . import __version__
from .toc import GENRES, CATEGORIES, START_OF_MASTER_TOC, Area, Disc, SACDImage

FRAME_RATE = 75
SAMPLES_PER_FRAME = 588
SACD_SAMPLING_FREQUENCY = 2822400

MAX_DISC_ARTIST_LEN = 40
MAX_ALBUM_TITLE_LEN = 100
MAX_TRACK_TITLE_LEN = 120
MAX_TRACK_ARTIST_LEN = 40
MAX_BUFF_FULL_PATH_LEN = 1024
MAX_FILENAME_LEN = 255

# libsacd's character_set[] (index = locale charset & 7)
CHARSET_NAMES = ("US-ASCII", "ISO646-JP", "ISO-8859-1", "SHIFT_JISX0213",
                 "KSC5601.1987-0", "GB2312.1980-0", "BIG5", "ISO-8859-1")

# scarletbook_id3.c sacd_id3_genres[] resolved through libid3's genre_table[]
ID3_GENRES = (
    "Other", "Other", "Top 40", "AlternRock", "Other", "Classical",
    "Contemporary Christian", "Country", "Dance", "Easy Listening", "Porn Groove",
    "Folk", "Gospel", "Hip-Hop", "Jazz", "Latin", "Musical", "New Age", "Opera",
    "Chamber Music", "Pop", "Rap", "Reggae", "Rock", "R&B", "Sound Clip",
    "Soundtrack", "Speech", "Ethnic", "Blues", "Other",
)

# sacd_extract's version strings; the build used for comparison reports "NA"
SACD_EXTRACT_VERSION_STRING = "NA"
SACD_RIPPER_VERSION_INFO = ("SACD extract 0.3.9.3 \n" + SACD_EXTRACT_VERSION_STRING + "\n"
                            "Copyright (c) 2010-2020 by respective authors.\n")
PYSACD_VERSION_INFO = f"pysacd {__version__}"

_UNSAFE_FILENAME = frozenset(range(0x01, 0x20)) | frozenset(b'"*/:<>?[]\\|\x7f')
_UNSAFE_FILEPATH = frozenset(range(0x01, 0x20)) | frozenset(b'"*<>?|\x7f')
_SPACE = b" \t\n\v\f\r"


def _b(s: str | None) -> bytes:
    return s.encode("utf-8", "surrogateescape") if s is not None else b""


def _s(b: bytes) -> str:
    return b.decode("utf-8", "surrogateescape")


# ------------------------------------------------------------ fileutils.c

def _trim_dots(b: bytes) -> bytes:
    return b.strip(b".")


def _trim_whitespace(b: bytes) -> bytes:
    return b.strip(_SPACE)


def _sanitize(b: bytes, unsafe: frozenset) -> bytes:
    if not b:
        return b
    b = bytes(0x5F if c in unsafe else c for c in b)
    return _trim_whitespace(_trim_dots(b))


def sanitize_filename(name: str) -> str:
    return _s(_sanitize(_b(name), _UNSAFE_FILENAME))


def sanitize_filepath(path: str) -> str:
    return _s(_sanitize(_b(path), _UNSAFE_FILEPATH))


def parse_format(fmt: str, tracknum: int, year: str = "", artist: str = "", album: str = "",
                 title: str = "") -> str:
    """fileutils.c parse_format(): %A artist, %L album, %N 2-digit track (1-99 only),
    %Y year, %T title, %% percent."""
    out = []
    i = 0
    while i < len(fmt):
        if fmt[i] == "%" and i + 1 < len(fmt):
            c = fmt[i + 1]
            if c == "A":
                out.append(artist)
            elif c == "L":
                out.append(album)
            elif c == "N":
                if 0 < tracknum < 100:
                    out.append(f"{tracknum:02d}")
            elif c == "Y":
                out.append(year)
            elif c == "T":
                out.append(title)
            elif c == "%":
                out.append("%")
            i += 2
        else:
            out.append(fmt[i])
            i += 1
    return "".join(out)


def make_filename(directory: str | None, name: str | None, ext: str | None) -> str:
    """fileutils.c make_filename(NULL, dir, filename, ext)."""
    buf = b""
    if directory:
        d = _b(directory)
        buf = d[:MAX_BUFF_FULL_PATH_LEN - 5]
        if not buf.endswith(b"/"):
            buf += b"/"
    buf = _sanitize(buf, _UNSAFE_FILEPATH)
    if name:
        n = _sanitize(_b(name)[:MAX_FILENAME_LEN - 1], _UNSAFE_FILENAME)
        buf += n[:max(0, MAX_BUFF_FULL_PATH_LEN - len(buf) - 5)]
    if ext:
        buf += b"." + _b(ext)[:max(0, MAX_BUFF_FULL_PATH_LEN - len(buf) - 2)]
    return _s(buf)


def unique_filename(directory: str | None, name: str, ext: str) -> str:
    """fileutils.c get_unique_filename(): append " (N)" while the file exists (max 20)."""
    path = make_filename(directory, name, ext)
    count = 1
    while os.path.exists(path):
        path = make_filename(directory, f"{name} ({count})", ext)
        count += 1
        if count > 20:
            break
    return path


# ------------------------------------------------------------ scarletbook_helpers.c

def _first(disc: Disc, *keys: str) -> bytes | None:
    for k in keys:
        if k in disc.text:
            return _b(disc.text[k])
    return None


def _cut_artist(b: bytes, maxlen: int) -> bytes:
    end = len(b)
    for sep in (b";", b"/", b",", b" -"):
        p = b.find(sep)
        if p != -1 and p < end:
            end = p
    return _sanitize(b[:min(end, maxlen)], _UNSAFE_FILENAME)


def _cut_semicolon(b: bytes, maxlen: int) -> bytes:
    p = b.find(b";")
    return _sanitize(b[:min(p if p != -1 else len(b), maxlen)], _UNSAFE_FILENAME)


def _set_album_title(disc: Disc) -> bytes | None:
    if disc.album_set_size > 1:
        return _first(disc, "album_title", "album_title_phonetic", "disc_title")
    return _first(disc, "disc_title", "disc_title_phonetic", "album_title", "album_title_phonetic")


def _disc_artist(disc: Disc) -> bytes | None:
    return _first(disc, "disc_artist", "disc_artist_phonetic", "album_artist", "album_artist_phonetic")


def album_dir(disc: Disc, artist_flag: bool = False) -> str:
    """get_album_dir(): base name for .iso, edit-master .dff, .cue and .xml files."""
    artist = _disc_artist(disc)
    title = _set_album_title(disc)
    a = _cut_artist(artist, MAX_DISC_ARTIST_LEN) if artist is not None else b""
    t = _cut_semicolon(title, MAX_ALBUM_TITLE_LEN) if title is not None else b""
    final = t
    if disc.album_set_size > 1:
        final = t + b" (disc %d-%d)" % (disc.album_sequence_number, disc.album_set_size)
    if a and t and artist_flag:
        return _s(a + b" - " + final)
    if a and artist_flag:
        return _s(a)
    if t:
        return _s(final)
    return "Unknown Album"


def album_path(disc: Disc, artist_flag: bool = False) -> str:
    """get_path_disc_album(): album directory, "Title/Disc N" for multi-disc sets."""
    title = _set_album_title(disc)
    if title is not None:
        t = _sanitize(title[:MAX_ALBUM_TITLE_LEN], _UNSAFE_FILENAME)
    else:
        t = b"unknown album title"
    artist = _disc_artist(disc)
    a = _cut_artist(artist, MAX_DISC_ARTIST_LEN) if artist is not None else b""
    out = a + b" - " + t if artist_flag and a else t
    if disc.album_set_size > 1:
        out += b"/Disc %d" % disc.album_sequence_number
    return _s(out)


def area_dir_name(area: Area) -> str:
    """get_speaker_config_string()."""
    extra = area.area_config & 0x07
    if area.channel_count == 2 and extra == 0:
        return "Stereo"
    if area.channel_count == 5 and extra == 3:
        return "5ch"
    if area.channel_count == 6 and extra == 4:
        return "6ch"
    return "Unkn-ch"


def music_filename(disc: Disc, area: Area, track_idx: int, override_title: str = "",
                   performer_flag: bool = False) -> str:
    """get_music_filename(); override_title is the "[NN-MM]" concatenation tag."""
    tt = area.tracks[track_idx].text
    perf = performer_flag
    if "performer" in tt:
        c = _b(tt["performer"])
    else:
        c = _first(disc, "disc_artist", "album_artist")
        if c is None:
            perf = False
    artist = _cut_artist(c, MAX_TRACK_ARTIST_LEN) if c is not None else b""

    if "title" in tt:
        title = _sanitize(_b(tt["title"])[:MAX_TRACK_TITLE_LEN], _UNSAFE_FILENAME)
    else:
        title = b"unknown track title"
    at = _first(disc, "disc_title", "disc_title_phonetic", "album_title", "album_title_phonetic")
    album = _cut_semicolon(at, MAX_ALBUM_TITLE_LEN) if at is not None else b"unknown title"
    year = "%04d" % disc.date[0]
    n = track_idx + 1
    A, L, T = _s(artist), _s(album), _s(title)
    if override_title:
        fmt = "%N - %L - %A -%T" if perf else "%N - %L -%T"
        return parse_format(fmt, n, year, A, L, override_title)
    if artist and title and perf:
        return parse_format("%N - %A - %T", n, year, A, L, T)
    if artist and perf:
        return parse_format("%N - %A", n, year, A, L, T)
    if title:
        return parse_format("%N - %T", n, year, A, L, T)
    if album:
        return parse_format("%N - %L", n, year, A, L, T)
    return parse_format("%N - Unknown Artist", n, year, A, L, T)


def output_dir(disc: Disc, area: Area, base_dir: str | None, artist_flag: bool = False) -> str:
    """create_path_output() without the mkdir: base/album_path[/speaker-config]."""
    p = _b(album_path(disc, artist_flag))
    if base_dir:
        b = _b(base_dir)
        p = b + (b"" if b.endswith(b"/") else b"/") + p
    if disc.multichannel is not None:
        p += b"/" + _b(area_dir_name(area))
    return _s(p)


# ------------------------------------------------------------ ID3 (scarletbook_id3.c + libid3)

def _latin1(b: bytes) -> bytes:
    # iconv with sacd-ripper's EILSEQ handling drops unconvertible characters
    return _s(b).encode("latin-1", "ignore")


def _utf16(b: bytes) -> bytes:
    return _s(b).encode("utf-16-le", "surrogatepass")


def _fit(data: bytes, size: int) -> bytes:
    # The C copies a fixed size computed from the UTF-8 length; shorter
    # converted text is followed by iconv's NUL padding (or heap garbage).
    return data[:size].ljust(size, b"\0")


def _text_frame_data(text: bytes, mode: int, iso_only: bool = False) -> bytes:
    """id3_set_text_wraper() / id3_set_text() frame payloads, sizes as in libid3."""
    n = len(text)
    if iso_only or mode == 3:
        return b"\x00" + _fit(_latin1(text), n + 1)
    if mode in (1, 2):
        return b"\x01\xff\xfe" + _fit(_utf16(text), 2 * n + 2)
    if mode in (4, 5):
        return b"\x03" + text + b"\x00"
    return b""  # mode 0: frame added without data


def _performer_frame_data(text: bytes, mode: int) -> bytes:
    """id3_set_text__performer_wraper(): TXXX with description PERFORMER."""
    n = len(text)
    if mode == 3:
        return b"\x00PERFORMER\x00" + _fit(_latin1(text), n + 1)
    if mode in (1, 2):
        return (b"\x01\xff\xfe" + "PERFORMER".encode("utf-16-le") + b"\0\0" +
                b"\xff\xfe" + _fit(_utf16(text), 2 * n + 2))
    if mode in (4, 5):
        return b"\x03PERFORMER\x00" + text + b"\x00"
    return b""


def _syncsafe(n: int) -> bytes:
    return bytes(((n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F))


def _render_id3(disc: Disc, area: Area, track_idx: int, mode: int) -> bytes:
    """scarletbook_id3_tag_render() + libid3 id3_write_tag() (no padding)."""
    tr = area.tracks[track_idx]
    tt = tr.text
    frames: list[tuple[bytes, bytes]] = []
    text = lambda fid, s: frames.append((fid, _text_frame_data(s, mode)))
    album_title = _first(disc, "album_title", "album_title_phonetic", "disc_title", "disc_title_phonetic")

    if "title" in tt:
        text(b"TIT2", _b(tt["title"]))
    elif mode != 2 and album_title is not None:
        text(b"TIT2", album_title)
    if album_title is not None:
        text(b"TALB", album_title)
    if "performer" in tt:
        text(b"TPE1", _b(tt["performer"]))
    else:
        artist = _disc_artist(disc)
        if artist is not None:
            text(b"TPE1", artist)

    if mode not in (2, 5):
        if "album_artist" in disc.text:
            text(b"TPE2", _b(disc.text["album_artist"]))
        if "performer" in tt:
            frames.append((b"TXXX", _performer_frame_data(_b(tt["performer"]), mode)))
        if "composer" in tt:
            text(b"TCOM", _b(tt["composer"]))
        if tr.genre is not None or tr.isrc is not None:  # area has an SACD_IGL list
            text(b"TSRC", _b(tr.isrc or ""))
        if "album_publisher" in disc.text:
            text(b"TPUB", _b(disc.text["album_publisher"]))
        if "album_copyright" in disc.text:
            text(b"TCOP", _b(disc.text["album_copyright"]))
        text(b"TPOS", b"%d/%d" % (disc.album_sequence_number, disc.album_set_size))
        g = tr.genre.genre & 0x1F if tr.genre is not None else 0
        text(b"TCON", ID3_GENRES[g].encode() if g < len(ID3_GENRES) else b"Other")
        y, m, d = disc.date
        frames.append((b"TYER", _text_frame_data(b"%04d" % y, mode, iso_only=True)))
        frames.append((b"TDAT", _text_frame_data(b"%02d%02d" % (m, d), mode, iso_only=True)))
    text(b"TRCK", b"%d/%d" % (track_idx + 1, len(area.tracks)))

    v24 = mode in (4, 5)
    body = b"".join(fid + (_syncsafe(len(data)) if v24 else struct.pack(">I", len(data))) + b"\0\0" + data
                    for fid, data in frames)
    return b"ID3" + bytes((4 if v24 else 3, 0, 0)) + _syncsafe(len(body)) + body


def id3_tag(disc: Disc, area: Area, track_idx: int, mode: int = 4) -> bytes:
    """The ID3 tag sacd_extract appends to a DSF (id3tag=mode in sacd_extract.cfg).

    0: none, 1: v2.3 UTF-16, 2: v2.3 UTF-16 minimal, 3: v2.3 ISO-8859-1,
    4: v2.4 UTF-8 (default), 5: v2.4 UTF-8 minimal.
    """
    if mode == 0:
        return b""
    return _render_id3(disc, area, track_idx, mode)


# ------------------------------------------------------------ DSDIFF (dsdiff.c)

def _ceil_odd(x: int) -> int:
    return x + 1 if x % 2 else x


def _chunk_size(x: int) -> bytes:
    return struct.pack(">Q", _ceil_odd(x))


_CHANNEL_IDS = {2: (b"SLFT", b"SRGT"),
                5: (b"MLFT", b"MRGT", b"C   ", b"LS  ", b"RS  "),
                6: (b"MLFT", b"MRGT", b"C   ", b"LFE ", b"LS  ", b"RS  ")}
_LS_CONFIG = {2: 0, 5: 3, 6: 4}


def dsdiff_header_size(area: Area, dst: bool) -> int:
    """Bytes before the first audio byte (DSD) or first DSTF chunk (DST)."""
    ch = area.channel_count
    n = 16 + 16 + 16 + 16 + 14 + 4 * ch + (28 if dst else 32) + 14
    n += 12 + 18 if dst else 12
    return _ceil_odd(n)


def dsdiff_header(disc: Disc, area: Area, *, dst: bool, audio_data_size: int, frame_count: int,
                  footer_size: int) -> bytes:
    """FRM8 header up to the sound data; audio_data_size includes any odd-size pad byte."""
    ch = area.channel_count
    out = bytearray()
    out += b"FRM8" + b"\0" * 8 + b"DSD "                      # size patched below
    out += b"FVER" + _chunk_size(4) + struct.pack(">I", 0x01050000)
    prop = len(out)
    out += b"PROP" + b"\0" * 8 + b"SND "
    out += b"FS  " + _chunk_size(4) + struct.pack(">I", SACD_SAMPLING_FREQUENCY)
    ids = _CHANNEL_IDS.get(ch) or tuple(b"C%03d" % i for i in range(ch))
    out += b"CHNL" + _chunk_size(2 + 4 * ch) + struct.pack(">H", ch) + b"".join(ids)
    name = b"DST Encoded" if dst else b"not compressed"
    cmpr = b"CMPR" + _chunk_size(5 + len(name)) + (b"DST " if dst else b"DSD ") + bytes((len(name),)) + name
    out += cmpr.ljust(_ceil_odd(len(cmpr)), b"\0")
    out += b"LSCO" + _chunk_size(2) + struct.pack(">H", _LS_CONFIG.get(ch, 65535))
    out[prop + 4:prop + 12] = _chunk_size(len(out) - prop - 12)
    if dst:
        out += b"DST " + _chunk_size(audio_data_size + 18)
        out += b"FRTE" + _chunk_size(6) + struct.pack(">IH", frame_count, FRAME_RATE)
    else:
        out += b"DSD " + _chunk_size(audio_data_size)
    if len(out) % 2:
        out += b"\0"
    out[4:12] = _chunk_size(len(out) + footer_size + audio_data_size - 12)
    return bytes(out)


def _flags4(flag_byte: int) -> int:
    """area_tracklist_time_t flags -> DSDIFF track_flags (tmf4, tmf1, tmf2, tmf3)."""
    return ((flag_byte >> 6) & 1) | ((flag_byte >> 3) & 1) << 1 | ((flag_byte >> 4) & 1) << 2 | \
           ((flag_byte >> 5) & 1) << 3


class _Buf:
    """A reusable, zero-initialised buffer: C's footer buffer is filled twice
    (at create and at close) and padding bytes keep what the first pass left."""

    def __init__(self):
        self.b = bytearray()
        self.pos = 0

    def put(self, data: bytes) -> None:
        end = self.pos + len(data)
        if end > len(self.b):
            self.b.extend(b"\0" * (end - len(self.b)))
        self.b[self.pos:end] = data
        self.pos = end

    def skip(self, n: int) -> None:
        self.pos += n
        if self.pos > len(self.b):
            self.b.extend(b"\0" * (self.pos - len(self.b)))


def _marker(buf: _Buf, frames: int, mark_type: int, track_flags: int) -> None:
    secs = frames // FRAME_RATE
    rem = secs % 3600
    buf.put(b"MARK" + _chunk_size(22) + struct.pack(
        ">HBBIiHHHI", secs // 3600, rem // 60, rem % 60,
        (frames % FRAME_RATE) * SAMPLES_PER_FRAME * 64, 0, mark_type, 0, track_flags, 0))


def _footer_pass(buf: _Buf, disc: Disc, area: Area, track_idx: int | None, dst: bool,
                 frame_index: list[tuple[int, int]], id3_mode: int, now: datetime,
                 machine: str) -> int:
    buf.pos = 0
    size = 0
    edit_master = track_idx is None
    if dst and frame_index:
        buf.put(b"DSTI" + _chunk_size(12 * len(frame_index)))
        for off, length in frame_index:
            buf.put(struct.pack(">QI", off, length))
        size = _ceil_odd(buf.pos)

    start = size
    buf.pos = start
    buf.put(b"DIIN")
    buf.skip(8)
    if edit_master:
        buf.put(b"EMID" + _chunk_size(0))
        tracks = area.tracks
        for i, t in enumerate(tracks):
            abs_start = t.start.total_frames
            abs_stop = abs_start + t.duration.total_frames
            if i == 0:
                _marker(buf, 0, 2, _flags4(t.start_flags))
            _marker(buf, abs_start, 0, _flags4(t.start_flags))
            if i == len(tracks) - 1 or tracks[i + 1].start.total_frames > abs_stop:
                _marker(buf, abs_stop, 1, _flags4(t.flags))
    else:
        d = area.tracks[track_idx].duration
        buf.put(b"MARK" + _chunk_size(22) + struct.pack(
            ">HBBIiHHHI", d.minutes // 60, d.minutes % 60, d.seconds,
            d.frames * SAMPLES_PER_FRAME * 64, 0, 4, 0, 0, 0))

    tt = {} if edit_master else area.tracks[track_idx].text
    c = _b(tt["performer"]) if "performer" in tt else _first(
        disc, "disc_artist", "disc_artist_phonetic", "album_artist", "album_artist_phonetic",
        "disc_title", "disc_title_phonetic", "album_title", "album_title_phonetic")
    for cid, s in ((b"DIAR", c), (b"DITI", _b(tt["title"]) if "title" in tt else _first(
            disc, "album_title", "album_title_phonetic", "disc_title", "disc_title_phonetic"))):
        if s is None:
            continue
        s = _latin1(s)
        buf.put(cid + _chunk_size(4 + len(s)) + struct.pack(">I", len(s)) + s)
        buf.skip(_ceil_odd(len(s)) - len(s))
    em_end = buf.pos
    buf.pos = start + 4
    buf.put(_chunk_size(em_end - start - 12))
    size += _ceil_odd(em_end - start)

    # COMT: disc date + "creating machine"
    start = size
    buf.pos = start
    buf.put(b"COMT")
    buf.skip(8)
    buf.put(struct.pack(">H", 2))
    title = _first(disc, "disc_title", "album_title") or b"Unknown"
    y, m, d = disc.date
    for stamp, ref, text in (((y, m, d, 0, 0), 0, b"Material ripped from SACD: " + _latin1(title)),
                             ((now.year, now.month - 1, now.day, now.hour, now.minute), 2,
                              machine.encode("latin-1", "ignore"))):
        buf.put(struct.pack(">HBBBBHHI", *stamp, 3, ref, len(text)) + text)
        buf.skip(_ceil_odd(14 + len(text)) - 14 - len(text))
    end = buf.pos
    buf.pos = start + 4
    buf.put(_chunk_size(end - start - 12))
    size += _ceil_odd(end - start)

    for idx in (range(len(area.tracks)) if edit_master else (track_idx,)):
        tag = _render_id3(disc, area, idx, id3_mode)
        buf.pos = size
        buf.put(b"ID3 " + _chunk_size(len(tag)) + tag)
        size += _ceil_odd(12 + len(tag))
    return size


def dsdiff_footer(disc: Disc, area: Area, track_idx: int | None, *, dst: bool,
                  frame_index: list[tuple[int, int]] = (), id3_mode: int = 4,
                  now: datetime | None = None, machine: str = PYSACD_VERSION_INFO) -> bytes:
    """Chunks after the sound data: DSTI (DST), DIIN, COMT and ID3.

    track_idx=None writes the edit-master variant (all-track markers, one ID3
    chunk per track). frame_index holds (absolute file offset of DSTF data,
    length) per DST frame. Unlike DSF, id3_mode 0 still writes an (empty) tag,
    as sacd_extract does.
    """
    now = now or datetime.now()
    buf = _Buf()
    # First pass as at file creation (no frames yet), then the real one.
    _footer_pass(buf, disc, area, track_idx, dst, [], id3_mode, now, machine)
    size = _footer_pass(buf, disc, area, track_idx, dst, list(frame_index), id3_mode, now, machine)
    return bytes(buf.b[:size].ljust(size, b"\0"))


# ------------------------------------------------------------ CUE (cuesheet.c)

def _cue_escape(b: bytes) -> bytes:
    return _trim_whitespace(b.replace(b'"', b'\\"')[:511])


def cue_sheet(disc: Disc, area: Area, dff_filename: str,
              version: str = PYSACD_VERSION_INFO) -> bytes:
    """write_cue_sheet(); dff_filename is the bare edit-master name (album_dir + ".dff")."""
    out = [b"\xef\xbb\xbf", b"\nREM File created by SACD Extract, version: " + _b(version) + b"\n"]
    g = disc.disc_genres[0].genre
    if g > 0:
        out.append(b"REM GENRE " + (GENRES[g] if g < len(GENRES) else "").encode() + b"\n")
    y, m, d = disc.date
    if y:
        out.append(b"REM DATE %04d-%02d-%02d\n" % (y, m, d))
    if disc.album_set_size > 1:
        out.append(b"REM DISC %d / %d\n" % (disc.album_sequence_number, disc.album_set_size))
    out.append(b"REM AREA: " + area_dir_name(area).encode() + b"\n")
    for key, a, b in (("PERFORMER", "disc_artist", "album_artist"), ("TITLE", "disc_title", "album_title")):
        v = _first(disc, a, b)
        if v is not None:
            out.append(key.encode() + b' "' + _cue_escape(v) + b'"\n')
    if disc.disc_catalog_number:
        out.append(b'CATALOG "' + _cue_escape(_b(disc.disc_catalog_number)[:16]) + b'"\n')
    out.append(b'FILE "' + _cue_escape(_b(dff_filename)) + b'" WAVE\n')
    prev_end = 0
    for i, t in enumerate(area.tracks):
        out.append(b"  TRACK %02d AUDIO\n" % (i + 1))
        if "title" in t.text:
            out.append(b'      TITLE "' + _cue_escape(_b(t.text["title"])) + b'"\n')
        if "performer" in t.text:
            out.append(b'      PERFORMER "' + _cue_escape(_b(t.text["performer"])) + b'"\n')
        if t.isrc:
            out.append(b"      ISRC " + _cue_escape(_b(t.isrc)[:12]) + b"\n")
        s = t.start
        if s.total_frames > prev_end:
            sec = prev_end // FRAME_RATE
            out.append(b"      INDEX 00 %02d:%02d:%02d\n" % (sec // 60, sec % 60, prev_end % FRAME_RATE))
        out.append(b"      INDEX 01 %02d:%02d:%02d\n" % (s.minutes, s.seconds, s.frames))
        prev_end = s.total_frames + t.duration.total_frames
    return b"".join(out)


# ------------------------------------------------------------ XML (scarletbook_xml.c / libxml2)

def raw_catalog_numbers(image: SACDImage) -> tuple[str, str]:
    """(album, disc) catalog numbers exactly as stored (16 bytes up to NUL, padding kept)."""
    m = image.read_sectors(START_OF_MASTER_TOC, 1)
    return tuple(m[o:o + 16].split(b"\0", 1)[0].decode("latin-1") for o in (24, 88))


def _attr_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            .replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;"))


def _text_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            .replace("\r", "&#13;"))


def metadata_xml(disc: Disc, version: str = PYSACD_VERSION_INFO,
                 raw_catalog: tuple[str, str] | None = None) -> bytes:
    """write_metadata_xml() output (libxml2 text writer, no indentation).

    Without raw_catalog the stored space padding of catalog numbers is
    reconstructed (non-empty values padded to 16 with spaces).
    """
    album_cat, disc_cat = raw_catalog or tuple(
        c.ljust(16) if c else "" for c in (disc.album_catalog_number, disc.disc_catalog_number))
    o: list[str] = []

    def attrs(**kw) -> str:
        return "".join(f' {k}="{_attr_escape(str(v))}"' for k, v in kw.items())

    def elem(name: str, value: str | None) -> None:
        if value is None:
            o.append(f"<{name}/>")
        else:
            o.append(f"<{name}>{_text_escape(value)}</{name}>")

    def meta(name: str, value: str, comment: bool = False) -> None:
        if comment:
            o.append(f'<meta{attrs(name=name, value=value)}><!--[mins:secs:frames]--></meta>')
        else:
            o.append(f"<meta{attrs(name=name, value=value)}/>")

    tx = disc.text.get
    o.append(f"<AudioMetadata><!--SACD metadata file (created by sacd_extract, version: {version})-->")
    o.append(f"<Album{attrs(catalog_number=album_cat, set_size=disc.album_set_size, sequence_number=disc.album_sequence_number)}>")
    elem("Title", tx("album_title", ""))
    elem("Artist", tx("album_artist"))
    elem("Publisher", tx("album_publisher"))
    elem("Copyright", tx("album_copyright"))

    loc = disc.locales[0] if disc.locales else None
    cs = loc.charset if loc else 0
    if loc and len(loc.language) == 2:
        locale = f"{loc.language}, code_character_set:[{cs}], {CHARSET_NAMES[cs & 7]}"
    else:
        locale = f"unspecified, asume code_character_set:[{cs}], {CHARSET_NAMES[cs & 7]}"
    y, m, d = disc.date
    a = dict(disc_version="%2d.%02d" % disc.version, catalog_number=disc_cat,
             creation_date="%4d-%02d-%02d" % (y, m, d), locale=locale)
    g = disc.disc_genres[0]
    if g.category:
        a.update(category=CATEGORIES[g.category] if g.category < len(CATEGORIES) else "",
                 genre=GENRES[g.genre] if g.genre < len(GENRES) else "")
    o.append(f"<Disc{attrs(**a)}>")
    elem("Title", tx("disc_title"))
    elem("Artist", tx("disc_artist"))
    elem("Publisher", tx("disc_publisher"))
    elem("Copyright", tx("disc_copyright"))

    first_version = disc.areas[0].version  # sacd-ripper uses area[0]'s version for every area
    for idx, area in enumerate(disc.areas):
        spk = {"Stereo": "2 Channel", "5ch": "5 Channel", "6ch": "6 Channel"}.get(area_dir_name(area), "Unknown")
        a = {"id": idx, "version": "%2d.%02d" % first_version, "speaker_configuration": spk}
        for k, v in (("area_description", area.description),
                     ("area_description_Phonetic", area.description_phonetic),
                     ("area_copyright", area.copyright),
                     ("area_copyright_Phonetic", area.copyright_phonetic)):
            if v is not None:
                a[k] = v
        tp = area.total_playtime
        a.update(totaltracks=len(area.tracks),
                 total_play_time=f"{tp.minutes:02d}:{tp.seconds:02d}:{tp.frames:02d} [mins:secs:frames]")
        o.append(f"<Area{attrs(**a)}><!--[mins:secs:frames]-->")
        for i, t in enumerate(area.tracks):
            tt = t.text
            o.append(f'<track id="{i + 1}">')
            meta("TITLE", tt.get("title", "unknown title"))
            meta("PERFORMER", tt.get("performer", ""))
            meta("ALBUM", tx("album_title", ""))
            meta("ALBUM ARTIST", tx("album_artist", ""))
            for key, name in (("composer", "COMPOSER"), ("arranger", "CONDUCTOR"),
                              ("songwriter", "SONGWRITER"), ("message", "COMMENT"),
                              ("extra_message", "EXTRAMESSAGE")):
                if key in tt:
                    meta(name, tt[key])
            meta("TRACKNUMBER", str(i + 1))
            meta("TOTALTRACKS", str(len(area.tracks)))
            gi = t.genre.genre if t.genre is not None else 0
            meta("GENRE", GENRES[gi] if gi < len(GENRES) else "")
            meta("ISRC", t.isrc or "")
            meta("DATE", t.isrc[5:7] if t.isrc and len(t.isrc) >= 7 else "%4d" % y)
            s, du = t.start, t.duration
            meta("Track_Start_Time_Code", f"{s.minutes:02d}:{s.seconds:02d}:{s.frames:02d}", comment=True)
            meta("Duration", f"{du.minutes:02d}:{du.seconds:02d}:{du.frames:02d}", comment=True)
            o.append("</track>")
        o.append("</Area>")
    o.append("</Disc></Album></AudioMetadata>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + "".join(o) + "\n").encode("utf-8", "surrogateescape")
