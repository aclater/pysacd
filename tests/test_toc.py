"""Tests against a synthetic Scarletbook image, plus an optional real ISO.

Run: python -m unittest discover -s tests   (with src/ on PYTHONPATH)
Set PYSACD_TEST_ISO=/path/to/disc.iso to also parse a real image.
"""

import io
import os
import struct
import unittest

from pysacd import SACDError, SACDImage, read_toc

S = 2048
AREA_TOC = 540
AREA_SIZE = 5  # header, SACDTTxt, SACDTRL1, SACDTRL2, padding


def _master() -> bytes:
    m = bytearray(10 * S)
    m[0:8] = b"SACDMTOC"
    m[8:10] = bytes([1, 20])
    struct.pack_into(">HH", m, 16, 1, 1)
    m[24:40] = b"ALB-001".ljust(16)
    m[40:44] = bytes([1, 0, 0, 14])  # General / Jazz
    struct.pack_into(">IIII", m, 64, AREA_TOC, AREA_TOC + AREA_SIZE, 0, 0)
    m[80] = 0x80  # hybrid
    struct.pack_into(">HH", m, 84, AREA_SIZE, 0)
    m[88:104] = b"DISC-001".ljust(16)
    struct.pack_into(">HBB", m, 120, 2004, 5, 6)
    m[128] = 1
    m[136:140] = b"en\x02\x00"
    for i in range(1, 9):
        m[i * S:i * S + 8] = b"SACDText"
    t = S
    m[t + 48:t + 48 + 13] = b"Album Title\0\0"
    m[t + 64:t + 64 + 12] = b"Caf\xe9 Artist\0"  # latin-1
    struct.pack_into(">HH", m, t + 16, 48, 64)        # album title, album artist
    struct.pack_into(">H", m, t + 16 + 8 * 2, 48)     # disc title
    m[9 * S:9 * S + 8] = b"SACD_Man"
    return bytes(m)


def _area() -> bytes:
    a = bytearray(AREA_SIZE * S)
    a[0:8] = b"TWOCHTOC"
    a[8:10] = bytes([1, 20])
    struct.pack_into(">H", a, 10, AREA_SIZE)
    a[20] = 4
    a[21] = 0  # DST
    a[32] = 2
    a[64:67] = bytes([5, 0, 0])
    a[69] = 2
    struct.pack_into(">II", a, 72, 600, 1000)
    a[80] = 1
    a[88:92] = b"en\x02\x00"

    tt = S
    a[tt:tt + 8] = b"SACDTTxt"
    struct.pack_into(">HH", a, tt + 8, 16, 64)
    # track 1: two items (title, performer), zero padding between them
    a[tt + 16:tt + 20] = bytes([2, 0, 0, 0])
    a[tt + 20:tt + 36] = b"\x01\x20First\0\0\0\x02\x20Bob\0"
    # track 2: one item
    a[tt + 64:tt + 68] = bytes([1, 0, 0, 0])
    a[tt + 68:tt + 77] = b"\x01\x20Second\0"

    l1 = 2 * S
    a[l1:l1 + 8] = b"SACDTRL1"
    struct.pack_into(">II", a, l1 + 8, 600, 800)
    struct.pack_into(">II", a, l1 + 8 + 255 * 4, 200, 200)

    l2 = 3 * S
    a[l2:l2 + 8] = b"SACDTRL2"
    a[l2 + 8:l2 + 16] = bytes([0, 0, 0, 0, 2, 30, 10, 0])
    a[l2 + 8 + 1020:l2 + 8 + 1028] = bytes([2, 30, 10, 0, 2, 29, 65, 0])
    return bytes(a)


def make_image(sector: int = 2048, offset: int = 0) -> bytes:
    img = bytearray((AREA_TOC + 2 * AREA_SIZE) * S)
    # the master TOC and its two copies, as on a disc (scarletbook compares them)
    for lsn in (510, 520, 530):
        img[lsn * S:(lsn + 10) * S] = _master()
    img[AREA_TOC * S:(AREA_TOC + AREA_SIZE) * S] = _area()
    img[(AREA_TOC + AREA_SIZE) * S:] = _area()
    if sector == 2048:
        return bytes(img)
    out = bytearray()
    for i in range(0, len(img), S):
        raw = bytearray(sector)
        raw[offset:offset + S] = img[i:i + S]
        out += raw
    return bytes(out)


class SyntheticTOC(unittest.TestCase):
    def check(self, disc):
        self.assertEqual(disc.version, (1, 20))
        self.assertTrue(disc.hybrid)
        self.assertEqual(disc.date, (2004, 5, 6))
        self.assertEqual(disc.album_catalog_number, "ALB-001")
        self.assertEqual(disc.disc_catalog_number, "DISC-001")
        self.assertEqual(disc.album_genres[0].name, "Jazz")
        self.assertEqual(disc.title, "Album Title")
        self.assertEqual(disc.text["album_artist"], "Café Artist")

        self.assertIsNone(disc.multichannel)
        area = disc.stereo
        self.assertTrue(area.is_dst)
        self.assertEqual(area.speaker_config, "Stereo")
        self.assertEqual(str(area.total_playtime), "05:00:00")
        self.assertEqual([t.title for t in area.tracks], ["First", "Second"])
        self.assertEqual(area.tracks[0].performer, "Bob")
        self.assertEqual(area.tracks[1].start_lsn, 800)
        self.assertEqual(area.tracks[1].duration.total_frames, (2 * 60 + 29) * 75 + 65)
        self.assertEqual(disc.warnings, [])

    def test_plain_iso(self):
        self.check(read_toc(io.BytesIO(make_image())))

    def test_raw_2064(self):
        with SACDImage(io.BytesIO(make_image(2064, 12))) as img:
            self.assertEqual(img.sector_size, 2064)
            self.check(img.read_toc())

    def test_raw_2054(self):
        self.check(read_toc(io.BytesIO(make_image(2054, 6))))

    def test_not_sacd(self):
        with self.assertRaises(SACDError):
            read_toc(io.BytesIO(bytes(600 * S)))

    def test_toc_mismatch_warns(self):
        img = bytearray(make_image())
        img[(AREA_TOC + AREA_SIZE) * S + 100] ^= 0xFF
        disc = read_toc(io.BytesIO(bytes(img)))
        # pysacd's parser files it under the disc, scarletbook under the area (#85)
        warnings = disc.warnings + [w for a in disc.areas for w in a.warnings]
        self.assertTrue(any("differ" in w for w in warnings), warnings)


@unittest.skipUnless(os.environ.get("PYSACD_TEST_ISO"), "PYSACD_TEST_ISO not set")
class RealISO(unittest.TestCase):
    def test_parse(self):
        disc = read_toc(os.environ["PYSACD_TEST_ISO"])
        for area in disc.areas:
            last = area.tracks[-1]
            end = last.start.total_frames + last.duration.total_frames
            self.assertLessEqual(abs(end - area.total_playtime.total_frames), 75)


if __name__ == "__main__":
    unittest.main()
