"""SACDImage through scarletbook-rs (#85): sectors, errors and the TOC helpers in _sbtoc."""

from __future__ import annotations

import ctypes
import io
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sacdgen

from pysacd import _sbtoc
from pysacd.toc import SACDError, SACDImage, Timecode, read_toc

FRAME = bytes(range(256)) * (2 * 4704 // 256) + bytes(2 * 4704 % 256)


def _native_ok() -> bool:
    try:
        from pysacd import _native
        _native._load()
        return True
    except (ImportError, OSError, RuntimeError):
        return False


def _image() -> bytes:
    with tempfile.TemporaryDirectory() as d:
        iso = Path(d) / "d.iso"
        sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [FRAME] * 30, [10, 20], dst=False, titles=["A", "B"])])
        return iso.read_bytes()


def _raw(plain: bytes, size: int, offset: int) -> bytes:
    out = bytearray()
    for i in range(0, len(plain), 2048):
        sector = bytearray(size)
        sector[offset:offset + 2048] = plain[i:i + 2048]
        out += sector
    return bytes(out)


@unittest.skipUnless(_native_ok(), "native engine not built")
class Image(unittest.TestCase):
    def test_raw_layouts_read_as_plain_sectors(self):
        plain = _image()
        for size, offset in ((2064, 12), (2054, 6)):
            with self.subTest(size=size), SACDImage(io.BytesIO(_raw(plain, size, offset))) as img:
                self.assertEqual((img.sector_size, img.data_offset), (size, offset))
                self.assertEqual(img.total_sectors, len(plain) // 2048)
                self.assertEqual(img.read_sectors(500, 40), plain[500 * 2048:540 * 2048])
                self.assertEqual(img.read_toc(), read_toc(io.BytesIO(plain)))

    def test_reads_past_the_end_are_refused(self):
        plain = _image()
        with SACDImage(io.BytesIO(plain)) as img:
            n = img.total_sectors
            self.assertEqual(len(img.read_sectors(n - 1, 1)), 2048)
            for lsn, count in ((n, 1), (n - 1, 2), (-1, 1)):
                with self.subTest(lsn=lsn, count=count), self.assertRaisesRegex(SACDError, "short read"):
                    img.read_sectors(lsn, count)

    def test_not_sacd(self):
        with self.assertRaisesRegex(SACDError, "not a readable Scarletbook disc"):
            SACDImage(io.BytesIO(bytes(600 * 2048)))

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            SACDImage(Path(tempfile.gettempdir()) / "no-such-pysacd-image.iso")

    def test_read_failure_is_an_os_error(self):
        plain = _image()

        class Flaky(io.BytesIO):
            broken = False

            def read(self, n=-1):
                if self.broken:
                    raise OSError("connection reset")
                return super().read(n)

        src = Flaky(plain)
        img = SACDImage(src)
        self.addCleanup(img.close)
        src.broken = True
        with self.assertRaisesRegex(OSError, "connection reset"):
            img.read_sectors(0, 16)

    def test_a_path_is_read_by_the_library_with_the_same_result(self):
        # a path is opened by scarletbook itself, a file object through the callback: same TOC
        plain = _image()
        with tempfile.TemporaryDirectory() as d:
            iso = Path(d) / "d.iso"
            iso.write_bytes(plain)
            with SACDImage(iso) as img:
                self.assertIsNone(img._disc._fh)
                self.assertEqual(img.read_sectors(500, 4), plain[500 * 2048:504 * 2048])
                by_path = img.read_toc()
            self.assertEqual(by_path, read_toc(io.BytesIO(plain)))
            iso.write_bytes(bytes(600 * 2048))
            with self.assertRaisesRegex(SACDError, "not a readable Scarletbook disc"):
                SACDImage(iso)

    def test_toc_from_one_call_with_a_small_first_buffer(self):
        # a layout larger than the first buffer is fetched again at its exact size
        plain = _image()
        want = read_toc(io.BytesIO(plain))
        old = _sbtoc.TOC_FIRST
        _sbtoc.TOC_FIRST = 64
        try:
            self.assertEqual(read_toc(io.BytesIO(plain)), want)
        finally:
            _sbtoc.TOC_FIRST = old

    def test_a_bad_layout_is_refused(self):
        with SACDImage(io.BytesIO(_image())) as img:
            data = _sbtoc._toc_bytes(img._disc._lib, img._disc._disc)
        self.assertEqual(data[:8], _sbtoc.TOC_MAGIC)
        _sbtoc._Layout(data)
        bad = {
            "short": data[:40],
            "magic": b"X" + data[1:],
            "major": data[:8] + (2).to_bytes(2, "little") + data[10:],
            "size": data[:12] + (len(data) + 1).to_bytes(4, "little") + data[16:],
            "record size": data[:20] + (100).to_bytes(2, "little") + data[22:],
        }
        for name, b in bad.items():
            with self.subTest(name), self.assertRaises(_sbtoc.SbFailure):
                _sbtoc._Layout(b)
        lay = _sbtoc._Layout(data)
        with self.assertRaisesRegex(_sbtoc.SbFailure, "past the end"):
            lay.record(len(data) - 4, 8)
        # a string pointing past the end
        d0 = lay.disc_offset + _sbtoc._DISC_STRINGS
        broken = bytearray(data)
        broken[d0:d0 + 8] = _sbtoc._STR.pack(len(data) - 2, 10)
        with self.assertRaisesRegex(_sbtoc.SbFailure, "past the end"):
            _sbtoc._Layout(bytes(broken)).string(d0)

    def test_closed_twice(self):
        img = SACDImage(io.BytesIO(_image()))
        img.close()
        img.close()


class Helpers(unittest.TestCase):
    def test_warning_text(self):
        self.assertEqual(_sbtoc._warning_text("track-past-playtime", 1, 3, -1),
                         "track-past-playtime (multichannel area, track 3)")
        self.assertEqual(_sbtoc._warning_text("audio-sector-malformed", -1, 0, 600), "audio-sector-malformed (LSN 600)")
        self.assertEqual(_sbtoc._warning_text("x", -1, 0, -1), "x")

    def test_layout_struct_sizes_match_the_header(self):
        # scarletbook.h, format 1.0: SbTocHeader 56, SbDiscInfo 32, SbAreaInfo 52, SbTrackInfo 28,
        # SbTocWarning 24, SbTocStr 8
        self.assertEqual([s.size for s in (_sbtoc._HEADER, _sbtoc._DISC_INFO, _sbtoc._AREA_INFO,
                                           _sbtoc._TRACK_INFO, _sbtoc._WARNING, _sbtoc._STR)],
                         [56, 32, 52, 28, 24, 8])

    def test_struct_sizes_match_the_header(self):
        for struct, size in _sbtoc._SIZES.items():
            self.assertEqual(ctypes.sizeof(struct), size, struct.__name__)

    def test_stored_timecodes_only_where_the_count_agrees(self):
        # 255:255:255 is 1166880 frames: written as stored, as sacd_extract and pysacd 0.2 do
        self.assertEqual(_sbtoc._as_stored(1166880, (255, 255, 255)), Timecode(255, 255, 255))
        # a count scarletbook clamped, or no stored bytes: the count, normalised
        self.assertEqual(_sbtoc._as_stored(1166880 - 75, (255, 255, 255)), Timecode(259, 17, 30))
        self.assertEqual(_sbtoc._as_stored(75, None), Timecode(0, 1, 0))
        self.assertEqual(_sbtoc._as_stored(75, (0, 1)), Timecode(0, 1, 0))


if __name__ == "__main__":
    unittest.main()
