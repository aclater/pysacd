"""Build a minimal Scarletbook (SACD) ISO image from DST or DSD frames, for tests.

Layout (2048-byte sectors):
  510..519   Master TOC: SACDMTOC, 8 x SACDText, SACD_Man (copies at 520 and 530)
  540..      per area: TOC-1, then audio sectors, then TOC-2
             area TOC = header, SACDTTxt, SACD_IGL (2 sectors), SACDTRL1, SACDTRL2

The field offsets follow src/pysacd/toc.py. Audio sectors carry one packet
per frame per sector, as csrc/demux.c expects: a frame's sector_count (frame
info byte 3, bits 2..6) is the number of sectors, i.e. packets, it occupies.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

S = 2048
MASTER_TOC = 510
FIRST_AREA = 540
FRAME_RATE = 75
AREA_TOC_SECTORS = 6        # header, SACDTTxt, SACD_IGL x2, SACDTRL1, SACDTRL2
AREA_CONFIG = {2: 0, 5: 3, 6: 4}


@dataclass
class AreaSpec:
    channels: int
    frames: list[bytes]                 # encoded DST frames (dst=True) or plain DSD frames
    tracks: list[int]                   # frames per track; sums to len(frames)
    dst: bool = True
    titles: list[str] = field(default_factory=list)
    charset: int = 2            # locale character set code (see CHARSET_CODECS)
    pauses: list[int] = field(default_factory=list)  # per track: leading frames of pause (timecodes only)


def _tc(frames: int) -> bytes:
    return bytes([frames // (60 * FRAME_RATE), frames // FRAME_RATE % 60, frames % FRAME_RATE])


def mux(frames: list[bytes], dst: bool) -> tuple[list[bytes], list[int]]:
    """Pack frames into audio sectors. Returns the sectors and, per frame, the
    index of the sector where it starts."""
    # pass 1: layout; a sector is [(frame, start?, length)] in payload order
    layout, first = [], []
    fi, off = 0, 0
    while fi < len(frames):
        packets, space, starts = [], S - 1, 0
        while fi < len(frames) and len(packets) < 7:
            start = off == 0
            info_len = (4 if dst else 3) if start else 0
            if start and starts == 7:
                break
            n = min(2047, space - 2 - info_len, len(frames[fi]) - off)
            if n <= 0:
                break
            if start:
                first.append(len(layout))
                starts += 1
            packets.append((fi, start, n))
            space -= 2 + info_len + n
            off += n
            if off == len(frames[fi]):
                fi, off = fi + 1, 0
        layout.append((packets, space))

    # pass 2: serialise; sector_count = sectors holding a packet of the frame
    nsec = [0] * len(frames)
    for packets, _ in layout:
        for f, _, _ in packets:
            nsec[f] += 1
    assert max(nsec) < 32, "frame too large for the 5-bit sector count"
    sectors, pos = [], [0] * len(frames)
    for packets, space in layout:
        pk = [(st, 2, n) for _, st, n in packets]
        infos = [_tc(f) + (bytes([nsec[f] << 2]) if dst else b"") for f, st, _ in packets if st]
        payload = b""
        for f, _, n in packets:
            payload += frames[f][pos[f]:pos[f] + n]
            pos[f] += n
        if space > 2 and len(pk) < 7:
            pk.append((0, 7, space - 2))                    # padding packet fills the sector
            payload += bytes(space - 2)
        hdr = bytes([len(pk) << 5 | len(infos) << 2 | (1 if dst else 0)])
        sec = hdr + b"".join(bytes([st << 7 | t << 3 | n >> 8, n & 0xFF]) for st, t, n in pk)
        sec += b"".join(infos) + payload
        assert len(sec) <= S
        sectors.append(sec.ljust(S, b"\0"))
    return sectors, first


@dataclass
class DiscInfo:
    """Master TOC identity fields."""
    title: str = "pysacd DST test"
    artist: str = "pysacd synthetic"
    album_catalog: str = "PYSACD-TEST"
    disc_catalog: str = "PYSACD-0001"
    set_size: int = 1
    sequence: int = 1
    charset: int = 2            # locale character set code (see CHARSET_CODECS)


# Scarletbook character set codes, and a codec that encodes test text in each
CHARSET_CODECS = {2: "latin-1", 3: "shift_jis", 4: "euc_kr", 5: "gb2312", 6: "big5"}


def _master_toc(area_tocs: list[tuple[int, int]], info: DiscInfo) -> bytes:
    """area_tocs: (TOC-1, TOC-2) LSNs of the stereo and multichannel areas."""
    m = bytearray(10 * S)
    m[0:8] = b"SACDMTOC"
    m[8:10] = bytes([1, 20])
    struct.pack_into(">HH", m, 16, info.set_size, info.sequence)    # album set size / sequence
    m[24:40] = info.album_catalog.encode("latin-1").ljust(16)
    m[40:44] = bytes([1, 0, 0, 25])                          # General / Sound Effects
    (a1, b1), (a2, b2) = area_tocs
    struct.pack_into(">IIII", m, 64, a1, b1, a2, b2)
    struct.pack_into(">HH", m, 84, AREA_TOC_SECTORS if a1 else 0, AREA_TOC_SECTORS if a2 else 0)
    m[88:104] = info.disc_catalog.encode("latin-1").ljust(16)
    m[104:108] = bytes([1, 0, 0, 25])
    struct.pack_into(">HBB", m, 120, 2026, 10, 28)
    m[128] = 1                                               # text channels
    m[136:140] = b"en" + bytes([info.charset, 0])            # 2 = ISO 8859-1
    for i in range(1, 9):
        t = i * S
        m[t:t + 8] = b"SACDText"
    t = S
    codec = CHARSET_CODECS[info.charset]
    strings = [info.title.encode(codec), info.artist.encode(codec)]
    pos = 64
    for i, s in enumerate(strings):
        m[t + pos:t + pos + len(s)] = s
        struct.pack_into(">H", m, t + 16 + i * 2, pos)       # album title, album artist
        struct.pack_into(">H", m, t + 16 + (8 + i) * 2, pos)  # disc title, disc artist
        pos += (len(s) + 1 + 3) & ~3
    m[9 * S:9 * S + 8] = b"SACD_Man"
    return bytes(m)


def _area_toc(spec: AreaSpec, track_lsns: list[tuple[int, int]], audio: tuple[int, int]) -> bytes:
    a = bytearray(AREA_TOC_SECTORS * S)
    ntracks = len(spec.tracks)
    a[0:8] = b"TWOCHTOC" if spec.channels == 2 else b"MULCHTOC"
    a[8:10] = bytes([1, 20])
    struct.pack_into(">H", a, 10, AREA_TOC_SECTORS)
    struct.pack_into(">I", a, 16, 2822400 * spec.channels // 8)    # max byte rate
    a[20] = 4                                                # 64 x 44.1 kHz
    a[21] = 0 if spec.dst else 2                             # DST / DSD 3-in-14
    a[32] = spec.channels
    a[33] = AREA_CONFIG.get(spec.channels, 0)
    a[34] = spec.channels
    a[64:67] = _tc(len(spec.frames))
    a[68] = 0                                                # track offset
    a[69] = ntracks
    struct.pack_into(">II", a, 72, *audio)
    a[80] = 1
    a[88:92] = b"en" + bytes([spec.charset, 0])

    tt = S                                                   # SACDTTxt
    a[tt:tt + 8] = b"SACDTTxt"
    pos = 8 + 2 * 255 + 2
    for i in range(ntracks):
        title = (spec.titles[i] if i < len(spec.titles) else f"Track {i + 1}").encode(CHARSET_CODECS[spec.charset])
        struct.pack_into(">H", a, tt + 8 + i * 2, pos)
        item = bytes([1, 0, 0, 0, 0x01, 0x20]) + title + b"\0"
        a[tt + pos:tt + pos + len(item)] = item
        pos += (len(item) + 3) & ~3
    assert pos < S

    ig = 2 * S                                               # SACD_IGL: ISRC + genre
    a[ig:ig + 8] = b"SACD_IGL"
    for i in range(ntracks):
        a[ig + 8 + i * 12:ig + 20 + i * 12] = f"XXPYS26{i + 1:05d}".encode()
        g = ig + 8 + 255 * 12 + 4 + i * 4
        a[g:g + 4] = bytes([1, 0, 0, 25])

    l1 = 4 * S                                               # SACDTRL1: LSNs
    a[l1:l1 + 8] = b"SACDTRL1"
    for i, (start, length) in enumerate(track_lsns):
        struct.pack_into(">I", a, l1 + 8 + i * 4, start)
        struct.pack_into(">I", a, l1 + 8 + 255 * 4 + i * 4, length)

    l2 = 5 * S                                               # SACDTRL2: timecodes
    a[l2:l2 + 8] = b"SACDTRL2"
    t = 0
    for i, n in enumerate(spec.tracks):
        pause = spec.pauses[i] if i < len(spec.pauses) else 0
        assert 0 <= pause < n
        a[l2 + 8 + i * 4:l2 + 11 + i * 4] = _tc(t + pause)
        a[l2 + 8 + 255 * 4 + i * 4:l2 + 11 + 255 * 4 + i * 4] = _tc(n - pause)
        t += n
    return bytes(a)


@dataclass
class AreaLayout:
    """Where build_image() put an area (LSNs)."""
    channels: int
    dst: bool
    toc1: int
    toc2: int
    audio_start: int
    audio_end: int                      # last audio sector


def build_image(areas: list[AreaSpec], info: DiscInfo) -> tuple[bytearray, list[AreaLayout]]:
    """The image build_iso() writes, and where each area lies in it."""
    assert sorted(a.channels == 2 for a in areas) in ([False], [True], [False, True])
    lsn = FIRST_AREA
    blobs, tocs, layout = [], {}, []
    for spec in areas:
        assert sum(spec.tracks) == len(spec.frames)
        sectors, first = mux(spec.frames, spec.dst)
        toc1 = lsn
        audio_start = toc1 + AREA_TOC_SECTORS
        audio_end = audio_start + len(sectors) - 1
        toc2 = audio_end + 1
        starts, f = [], 0
        for n in spec.tracks:
            starts.append(audio_start + first[f])
            f += n
        track_lsns = [(s, (starts[i + 1] if i + 1 < len(starts) else audio_end + 1) - s)
                      for i, s in enumerate(starts)]
        toc = _area_toc(spec, track_lsns, (audio_start, audio_end))
        blobs.append((toc1, toc + b"".join(sectors) + toc))
        tocs[spec.channels == 2] = (toc1, toc2)
        layout.append(AreaLayout(spec.channels, spec.dst, toc1, toc2, audio_start, audio_end))
        lsn = toc2 + AREA_TOC_SECTORS
    img = bytearray(lsn * S)
    master = _master_toc([tocs.get(True, (0, 0)), tocs.get(False, (0, 0))], info)
    for copy in (MASTER_TOC, MASTER_TOC + 10, MASTER_TOC + 20):
        img[copy * S:(copy + 10) * S] = master
    for start, blob in blobs:
        img[start * S:start * S + len(blob)] = blob
    return img, layout


def build_iso(path: str | Path, areas: list[AreaSpec], title: str = "pysacd DST test",
              info: DiscInfo | None = None) -> None:
    """Write an ISO with a stereo area and/or a multichannel area (at most one each)."""
    img, _ = build_image(areas, info or DiscInfo(title=title))
    Path(path).write_bytes(bytes(img))


# ---------------------------------------------------------------- hostile images (#17)
#
# Each case starts from a small valid disc (a stereo DST area and a 6-channel plain DSD area)
# and breaks one thing: the master TOC, an area TOC, the audio sectors or the sector layout.
# Offsets are those of src/pysacd/toc.py and csrc/demux.c.

DSD_SILENCE = 0x69
FRAME_BYTES = 4704                      # per channel


@dataclass
class Hostile:
    name: str
    image: bytes
    note: str = ""


def _dsd_frame(channels: int, seed: int) -> bytes:
    """A plain DSD frame with some variety (not silence), deterministic."""
    return bytes((i * 37 + seed * 101) & 0xFF for i in range(channels * FRAME_BYTES))


def _dst_frame(channels: int, seed: int) -> bytes:
    """The simplest valid DST frame: the 'uncompressed' flag (first bit 0) and plain DSD."""
    return b"\0" + _dsd_frame(channels, seed)


def base_areas() -> list[AreaSpec]:
    return [AreaSpec(2, [_dst_frame(2, i) for i in range(12)], [4, 4, 4], titles=["One", "Two", "Three"]),
            AreaSpec(6, [_dsd_frame(6, i) for i in range(6)], [3, 3], dst=False)]


def base_image() -> tuple[bytearray, list[AreaLayout]]:
    return build_image(base_areas(), DiscInfo(title="pysacd hostile base"))


def _masters(img: bytearray):
    """The three master TOC copies, as memoryviews (patch all of them alike)."""
    return [memoryview(img)[c * S:(c + 10) * S] for c in (MASTER_TOC, MASTER_TOC + 10, MASTER_TOC + 20)]


def _area_tocs(img: bytearray, a: AreaLayout, both: bool = True):
    return [memoryview(img)[t * S:(t + AREA_TOC_SECTORS) * S] for t in ((a.toc1, a.toc2) if both else (a.toc1,))]


def _set(mv, off: int, fmt: str, *v) -> None:
    struct.pack_into(fmt, mv, off, *v)


def _master_case(fn) -> bytes:
    img, _ = base_image()
    for m in _masters(img):
        fn(m)
    return bytes(img)


def _area_case(fn, which: int = 0, both: bool = True) -> bytes:
    img, lay = base_image()
    for t in _area_tocs(img, lay[which], both):
        fn(t)
    return bytes(img)


def _audio_case(fn, which: int = 0) -> bytes:
    """fn(sectors: list[bytearray]) edits the area's audio sectors in place."""
    img, lay = base_image()
    a = lay[which]
    secs = [bytearray(img[s * S:(s + 1) * S]) for s in range(a.audio_start, a.audio_end + 1)]
    fn(secs)
    for i, sec in enumerate(secs):
        img[(a.audio_start + i) * S:(a.audio_start + i + 1) * S] = bytes(sec).ljust(S, b"\0")[:S]
    return bytes(img)


def _custom_audio(spec_fn) -> bytes:
    """An image built from base_areas() after spec_fn(areas) rewrote them."""
    areas = base_areas()
    spec_fn(areas)
    img, _ = build_image(areas, DiscInfo(title="pysacd hostile"))
    return bytes(img)


def _sector(packets: list[tuple[int, int, bytes]], infos: list[bytes], dst: bool) -> bytes:
    """An audio sector from raw parts: packets are (start, type, payload), infos are frame
    info records (3 bytes plain, 4 bytes DST). No checks: the point is to break things."""
    hdr = bytes([(len(packets) & 7) << 5 | (len(infos) & 7) << 2 | int(dst)])
    pk = b"".join(bytes([st << 7 | t << 3 | (len(p) >> 8) & 7, len(p) & 0xFF]) for st, t, p in packets)
    return (hdr + pk + b"".join(infos) + b"".join(p for _, _, p in packets)).ljust(S, b"\0")[:S]


def _raw_layout(img: bytes, size: int, offset: int, shift: int = 0) -> bytes:
    """Re-pack 2048-byte sectors as size-byte raw sectors with the data at offset. With
    shift, every sector except the master TOC's first is misplaced by that many bytes, so
    detection succeeds but the rest of the image is read from the wrong place."""
    out = bytearray()
    for i in range(0, len(img), S):
        raw = bytearray(size)
        o = offset if i // S == MASTER_TOC else max(0, min(size - S, offset + shift))
        raw[o:o + S] = img[i:i + S]
        out += raw
    return bytes(out)


def _rng_bytes(seed: int, n: int) -> bytes:
    import random
    return random.Random(seed).randbytes(n)


def hostile_corpus() -> list[Hostile]:
    """Every hostile image, built in memory (about 3 MB each)."""
    out: list[Hostile] = []

    def add(name: str, image: bytes, note: str = "") -> None:
        out.append(Hostile(name, image, note))

    valid, lay = base_image()
    st, mc = lay
    add("valid", bytes(valid), "the base disc, unchanged")

    # --- master TOC
    add("master-version-99", _master_case(lambda m: m.__setitem__(8, 99)))
    add("master-no-areas", _master_case(lambda m: _set(m, 64, ">IIIIHH", 0, 0, 0, 0, 0, 0)))
    add("master-area-size-zero", _master_case(lambda m: _set(m, 84, ">HH", 0, 0)))
    add("master-area-size-max", _master_case(lambda m: _set(m, 84, ">HH", 0xFFFF, 0xFFFF)),
        "65535-sector area TOCs: 128 MiB each, mostly past the end")
    add("master-area-size-one", _master_case(lambda m: _set(m, 84, ">HH", 1, 1)), "no room for the track list")
    add("master-toc-past-end", _master_case(lambda m: _set(m, 64, ">IIII", 0xFFFFFFF0, 0xFFFFFFFF,
                                                           0x7FFFFFFF, 0xFFFFFFFE)))
    add("master-toc-at-master", _master_case(lambda m: _set(m, 64, ">IIII", MASTER_TOC, MASTER_TOC,
                                                            MASTER_TOC, MASTER_TOC)))
    add("master-areas-same-toc", _master_case(lambda m: _set(m, 72, ">II", st.toc1, st.toc2)),
        "both area pointers name the stereo TOC")
    add("master-areas-swapped", _master_case(lambda m: _set(m, 64, ">IIII", mc.toc1, mc.toc2, st.toc1, st.toc2)))
    add("master-toc1-garbage-toc2-ok", _master_case(lambda m: _set(m, 64, ">I", st.audio_start)),
        "TOC-1 points into the audio, TOC-2 is fine")
    add("master-toc-overlaps-audio", _master_case(lambda m: _set(m, 64, ">II", st.toc1 + 3, st.toc2 + 3)))

    def wild_text(m):
        t = S
        m[t + 8:t + 48] = b"\xff\xff" * 4 + struct.pack(">16H", *([S - 1, 0xFFFF, 8, 9] * 4))[:32]
        m[t + 48:t + S] = b"\xfe" * (S - 48)
    add("master-text-wild", _master_case(wild_text), "text offsets past the sector, unterminated strings")
    add("master-text-unterminated", _master_case(lambda m: m.__setitem__(slice(S + 64, 2 * S), b"A" * (S - 64))))
    add("master-charset-sjis-garbage", _master_case(
        lambda m: (m.__setitem__(slice(136, 140), b"ja\x03\x00"),
                   m.__setitem__(slice(S + 64, S + 96), b"\x81\xff\xfc\xfc\x80\xa0\xef\xff" * 4))))
    add("master-charset-ff", _master_case(lambda m: m.__setitem__(slice(136, 168), b"\xff" * 32)))
    add("master-text-count-ff", _master_case(lambda m: m.__setitem__(128, 0xFF)))
    add("master-no-text-sector", _master_case(lambda m: m.__setitem__(slice(S, 2 * S), bytes(S))))
    add("master-catalog-binary", _master_case(lambda m: m.__setitem__(slice(24, 40), bytes(range(240, 256)))))
    add("master-genres-ff", _master_case(lambda m: (m.__setitem__(slice(40, 56), b"\xff" * 16),
                                                     m.__setitem__(slice(104, 120), b"\xff" * 16))))
    add("master-date-ff", _master_case(lambda m: m.__setitem__(slice(120, 124), b"\xff" * 4)))
    add("truncated-in-master", bytes(valid[:(MASTER_TOC + 3) * S]))
    add("truncated-before-master", bytes(valid[:MASTER_TOC * S + 7]))

    # --- area TOCs (stereo unless named)
    add("area-bad-signature", _area_case(lambda a: a.__setitem__(slice(0, 8), b"TWOCHTOX")))
    add("area-mch-signature-in-stereo-slot", _area_case(lambda a: a.__setitem__(slice(0, 8), b"MULCHTOC")))
    add("area-toc1-toc2-differ", _area_case(lambda a: a.__setitem__(69, 1), both=False))
    add("area-tracks-0", _area_case(lambda a: a.__setitem__(69, 0)))
    add("area-tracks-255", _area_case(lambda a: a.__setitem__(69, 255)), "252 tracks of zero length")
    add("area-audio-reversed", _area_case(lambda a: _set(a, 72, ">II", st.audio_end, st.audio_start)))
    add("area-audio-past-end", _area_case(lambda a: _set(a, 76, ">I", 0xFFFFFFFF)))
    add("area-audio-start-past-end", _area_case(lambda a: _set(a, 72, ">II", 0xFFFFFF00, 0xFFFFFFFF)))
    add("area-audio-in-master", _area_case(lambda a: _set(a, 72, ">II", 0, st.audio_end)))
    add("area-audio-one-sector", _area_case(lambda a: _set(a, 72, ">II", st.audio_start, st.audio_start)))
    add("area-playtime-max", _area_case(lambda a: a.__setitem__(slice(64, 67), b"\xff\xff\xff")))
    add("area-playtime-zero", _area_case(lambda a: a.__setitem__(slice(64, 67), bytes(3))))
    add("area-channels-0", _area_case(lambda a: a.__setitem__(32, 0)))
    add("area-channels-255", _area_case(lambda a: a.__setitem__(32, 255)))
    add("area-channels-6-in-stereo", _area_case(lambda a: a.__setitem__(32, 6)), "6ch area, 2ch frames")
    add("area-mch-channels-2", _area_case(lambda a: a.__setitem__(32, 2), which=1), "2ch area, 6ch frames")
    add("area-mch-channels-5", _area_case(lambda a: a.__setitem__(32, 5), which=1))
    add("area-format-unknown", _area_case(lambda a: a.__setitem__(21, 0x0F)))
    add("area-format-dsd-for-dst", _area_case(lambda a: a.__setitem__(21, 2)), "DST frames, TOC says plain DSD")
    add("area-format-dst-for-dsd", _area_case(lambda a: a.__setitem__(21, 0), which=1))
    add("area-rate-code-0", _area_case(lambda a: a.__setitem__(20, 0)))
    add("area-text-offsets-wild", _area_case(lambda a: _set(a, 144, ">4H", 0xFFFF, S - 1, 1, 8)))

    def durations(values):
        def fn(a):
            l2 = 5 * S
            for i, v in enumerate(values):
                a[l2 + 8 + 255 * 4 + i * 4:l2 + 11 + 255 * 4 + i * 4] = v
        return fn

    def starts(values):
        def fn(a):
            for i, v in enumerate(values):
                a[5 * S + 8 + i * 4:5 * S + 11 + i * 4] = v
        return fn

    add("track-duration-max", _area_case(durations([b"\xff\xff\xff"] * 3)), "each track claims 255+ minutes")
    add("track-duration-zero", _area_case(durations([bytes(3)] * 3)))
    add("track-duration-one-zero", _area_case(durations([_tc(4), bytes(3), _tc(4)])))
    add("track-start-max", _area_case(starts([b"\xff\xff\xff"] * 3)))
    add("track-starts-reversed", _area_case(starts([_tc(8), _tc(4), _tc(0)])))
    add("track-starts-overlap", _area_case(lambda a: (starts([_tc(0)] * 3)(a), durations([_tc(12)] * 3)(a))),
        "three tracks covering the whole area")
    add("track-start-past-playtime", _area_case(starts([_tc(0), _tc(4), _tc(5000)])))
    add("track-duration-max-area-past-end", _area_case(lambda a: (
        _set(a, 76, ">I", 0xFFFFFFFF), a.__setitem__(slice(64, 67), b"\xff\xff\xff"),
        durations([b"\xff\xff\xff"] * 3)(a))),
        "hours-long tracks in an area that claims to run to LSN 2^32-1: only the image bounds them")

    def lsns(values):
        def fn(a):
            for i, (s, n) in enumerate(values):
                _set(a, 4 * S + 8 + i * 4, ">I", s)
                _set(a, 4 * S + 8 + 255 * 4 + i * 4, ">I", n)
        return fn

    add("track-lsn-past-end", _area_case(lsns([(0xFFFFFFF0, 0xFFFFFFFF)] * 3)))
    add("track-lsn-zero", _area_case(lsns([(0, 0)] * 3)))
    add("track-lsn-reversed", _area_case(lsns([(st.audio_end, 1), (st.audio_start + 2, 1), (st.audio_start, 1)])))
    add("track-lsn-outside-area", _area_case(lsns([(mc.audio_start, mc.audio_end - mc.audio_start + 1)] * 3)),
        "the stereo tracks point into the multichannel audio")

    def track_text(fill):
        def fn(a):
            tt = S
            a[tt + 8:tt + S] = bytes(S - 8)
            fill(a, tt)
        return fn

    add("text-offsets-past-toc", _area_case(track_text(lambda a, tt: _set(a, tt + 8, ">3H", 0xFFFF, 0xFFF0, 6 * S))))
    add("text-offset-last-byte", _area_case(track_text(lambda a, tt: _set(a, tt + 8, ">3H", S - 1, S - 2, S - 4))))
    add("text-items-255-unterminated", _area_case(track_text(
        lambda a, tt: (_set(a, tt + 8, ">3H", 520, 520, 520), a.__setitem__(520 + tt, 255),
                       a.__setitem__(slice(tt + 524, tt + S), b"\x01" * (S - 524))))),
        "255 items with no NUL anywhere")
    add("text-items-255-zero", _area_case(track_text(
        lambda a, tt: (_set(a, tt + 8, ">3H", 520, 520, 520), a.__setitem__(520 + tt, 255)))),
        "255 items in a sea of zeros")
    add("text-unknown-types", _area_case(track_text(
        lambda a, tt: (_set(a, tt + 8, ">3H", 520, 520, 520),
                       a.__setitem__(slice(tt + 520, tt + 540), bytes([8, 0, 0, 0]) + b"\x55\x20xx\0\0\xee\x20yy\0\0"
                                     + b"\x00\x20zz")))))

    def text_everywhere(a):
        # every sector after the header carries SACDTTxt text across its whole length
        for k in range(1, AREA_TOC_SECTORS):
            p = k * S
            a[p:p + 8] = b"SACDTTxt"
            a[p + 8:p + S] = b"\xc0" * (S - 8)
    add("text-everywhere", _area_case(text_everywhere), "no track list: every sub-structure is text")
    add("text-sjis-garbage", _area_case(lambda a: (a.__setitem__(slice(88, 92), b"ja\x03\x00"),
                                                   a.__setitem__(slice(S + 520, S + 600), b"\x81\xff" * 40))))

    def acc_chain(a):
        a[S:S + 8] = b"SACD_ACC"                     # claims 32 sectors: runs past the TOC
    add("subchain-acc-past-end", _area_case(acc_chain))
    add("subchain-trl1-last-sector", _area_case(lambda a: (a.__setitem__(slice(S, S + 8), b"SACDTRL2"),
                                                           a.__setitem__(slice(5 * S, 5 * S + 8), b"SACDTRL1"))),
        "TRL2 first, TRL1 last")
    add("subchain-no-trl", _area_case(lambda a: (a.__setitem__(slice(4 * S, 4 * S + 8), bytes(8)),
                                                 a.__setitem__(slice(5 * S, 5 * S + 8), bytes(8)))))

    # --- audio sectors (stereo DST area unless named)
    add("audio-zero", _audio_case(lambda secs: [s.__setitem__(slice(0, S), bytes(S)) for s in secs]))
    add("audio-random", _audio_case(lambda secs: [s.__setitem__(slice(0, S), _rng_bytes(i, S))
                                                  for i, s in enumerate(secs)]))
    add("audio-mch-random", _audio_case(lambda secs: [s.__setitem__(slice(0, S), _rng_bytes(1000 + i, S))
                                                      for i, s in enumerate(secs)], which=1))
    add("audio-headers-random", _audio_case(lambda secs: [s.__setitem__(slice(0, 16), _rng_bytes(2000 + i, 16))
                                                          for i, s in enumerate(secs)]),
        "packet and frame info headers randomised, payload intact")
    add("audio-packets-overrun", _audio_case(lambda secs: [s.__setitem__(slice(0, S), _sector(
        [(1, 2, b"\xaa" * 2047)] * 7, [_tc(i) + b"\x04"] * 7, True)) for i, s in enumerate(secs)]),
        "seven 2047-byte packets per sector")
    add("audio-frame-info-no-packets", _audio_case(lambda secs: [s.__setitem__(slice(0, S), _sector(
        [], [_tc(i) + b"\x7c"] * 7, True)) for i, s in enumerate(secs)]))
    add("audio-start-without-info", _audio_case(lambda secs: [s.__setitem__(slice(0, S), _sector(
        [(1, 2, b"\x00" * 100)] * 3, [], True)) for s in secs]))
    add("audio-tc-max", _audio_case(lambda secs: [s.__setitem__(slice(0, S), _sector(
        [(1, 2, b"\0" + b"\x69" * 200)], [b"\xff\xff\xff\x04"], True)) for s in secs]),
        "every frame claims timecode 255:255:255")
    add("audio-tc-backwards", _audio_case(lambda secs: [s.__setitem__(slice(0, S), _sector(
        [(1, 2, b"\0" + b"\x69" * 200)], [_tc(len(secs) - i) + b"\x04"], True)) for i, s in enumerate(secs)]))

    def endless_frame(secs):
        # one DST frame that claims 31 sectors and never ends: it grows past MAX_FRAME_SIZE
        secs[0][:] = _sector([(1, 2, b"\x80" * 2000)], [_tc(0) + bytes([31 << 2])], True)
        for s in secs[1:]:
            s[:] = _sector([(0, 2, b"\x80" * 2040)], [], True)
    add("audio-dst-endless-frame", _audio_case(endless_frame))

    def plain_endless(secs):
        secs[0][:] = _sector([(1, 2, b"\x69" * 2000)], [_tc(0)], False)
        for s in secs[1:]:
            s[:] = _sector([(0, 2, b"\x69" * 2040)], [], False)
    add("audio-dsd-endless-frame", _audio_case(plain_endless, which=1))

    add("dst-garbage-frames", _custom_audio(
        lambda areas: areas[0].frames.__setitem__(slice(None), [_rng_bytes(3000 + i, 3000) for i in range(12)])))
    add("dst-tiny-frames", _custom_audio(
        lambda areas: areas[0].frames.__setitem__(slice(None), [bytes([0x80 | i]) for i in range(12)])))
    add("dst-oversized-frames", _custom_audio(
        lambda areas: areas[0].frames.__setitem__(slice(None), [b"\0" + _rng_bytes(i, 60000) for i in range(12)])),
        "frames of 60000 bytes: bigger than any plain frame")
    add("dst-short-plain", _custom_audio(
        lambda areas: areas[0].frames.__setitem__(slice(None), [b"\0" + bytes(100)] * 12)),
        "'uncompressed' DST frames that are too short")
    add("dsd-wrong-size", _custom_audio(
        lambda areas: areas[1].frames.__setitem__(slice(None), [bytes(9000)] * 6)), "plain frames of 9000 bytes")
    add("dsd-2ch-frames-in-6ch", _custom_audio(
        lambda areas: areas[1].frames.__setitem__(slice(None), [_dsd_frame(2, i) for i in range(6)])))
    add("dsd-sectors-say-dst", _audio_case(lambda secs: [s.__setitem__(0, s[0] | 1) for s in secs], which=1))
    add("dst-sectors-say-dsd", _audio_case(lambda secs: [s.__setitem__(0, s[0] & ~1) for s in secs]))

    # --- sector layout
    add("layout-2064", _raw_layout(bytes(valid), 2064, 12), "valid disc in raw 2064-byte sectors")
    add("layout-2054", _raw_layout(bytes(valid), 2054, 6), "valid disc in raw 2054-byte sectors")
    add("layout-2064-shifted", _raw_layout(bytes(valid), 2064, 12, shift=-12), "only the master TOC is aligned")
    add("layout-2054-shifted", _raw_layout(bytes(valid), 2054, 6, shift=6))
    add("layout-2064-truncated", _raw_layout(bytes(valid), 2064, 12)[:(st.audio_start + 5) * 2064 + 100])
    add("layout-2048-trailing-bytes", bytes(valid) + b"\xff" * 1000)
    add("truncated-in-toc", bytes(valid[:(st.toc1 + 3) * S]))
    add("truncated-in-audio", bytes(valid[:(st.audio_start + 7) * S + 5]))
    add("truncated-before-toc2", bytes(valid[:st.toc2 * S]))
    return out
