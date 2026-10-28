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


def _master_toc(area_tocs: list[tuple[int, int]], title: str) -> bytes:
    """area_tocs: (TOC-1, TOC-2) LSNs of the stereo and multichannel areas."""
    m = bytearray(10 * S)
    m[0:8] = b"SACDMTOC"
    m[8:10] = bytes([1, 20])
    struct.pack_into(">HH", m, 16, 1, 1)                    # album set size / sequence
    m[24:40] = b"PYSACD-TEST".ljust(16)
    m[40:44] = bytes([1, 0, 0, 25])                          # General / Sound Effects
    (a1, b1), (a2, b2) = area_tocs
    struct.pack_into(">IIII", m, 64, a1, b1, a2, b2)
    struct.pack_into(">HH", m, 84, AREA_TOC_SECTORS if a1 else 0, AREA_TOC_SECTORS if a2 else 0)
    m[88:104] = b"PYSACD-0001".ljust(16)
    m[104:108] = bytes([1, 0, 0, 25])
    struct.pack_into(">HBB", m, 120, 2026, 10, 28)
    m[128] = 1                                               # text channels
    m[136:140] = b"en\x02\x00"                               # ISO 8859-1
    for i in range(1, 9):
        t = i * S
        m[t:t + 8] = b"SACDText"
    t = S
    strings = [title.encode("latin-1"), b"pysacd synthetic"]
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
    a[88:92] = b"en\x02\x00"

    tt = S                                                   # SACDTTxt
    a[tt:tt + 8] = b"SACDTTxt"
    pos = 8 + 2 * 255 + 2
    for i in range(ntracks):
        title = (spec.titles[i] if i < len(spec.titles) else f"Track {i + 1}").encode("latin-1")
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
        a[l2 + 8 + i * 4:l2 + 11 + i * 4] = _tc(t)
        a[l2 + 8 + 255 * 4 + i * 4:l2 + 11 + 255 * 4 + i * 4] = _tc(n)
        t += n
    return bytes(a)


def build_iso(path: str | Path, areas: list[AreaSpec], title: str = "pysacd DST test") -> None:
    """Write an ISO with a stereo area and/or a multichannel area (at most one each)."""
    assert sorted(a.channels == 2 for a in areas) in ([False], [True], [False, True])
    lsn = FIRST_AREA
    blobs, tocs = [], {}
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
        lsn = toc2 + AREA_TOC_SECTORS
    img = bytearray(lsn * S)
    master = _master_toc([tocs.get(True, (0, 0)), tocs.get(False, (0, 0))], title)
    for copy in (MASTER_TOC, MASTER_TOC + 10, MASTER_TOC + 20):
        img[copy * S:(copy + 10) * S] = master
    for start, blob in blobs:
        img[start * S:start * S + len(blob)] = blob
    Path(path).write_bytes(bytes(img))
