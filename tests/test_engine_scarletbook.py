"""The engine reads audio through scarletbook-rs (#85, phase 3): cancellation, sector sources,
stored and decoded outputs in one pass, and an ISO copy before the audio."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import sacdgen

from pysacd import _native
from pysacd.toc import SACDImage, read_toc

FRAME = 4704


def _native_ok() -> bool:
    try:
        _native._load()
        return True
    except (ImportError, OSError, RuntimeError):
        return False


def _frames(n: int, channels: int = 2) -> list[bytes]:
    return [bytes((i * 31 + k * 7) & 0xFF for i in range(channels * FRAME)) for k in range(n)]


@unittest.skipUnless(_native_ok(), "native engine not built")
class Engine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def image(self, frames: int = 300, tracks: tuple[int, ...] = (100, 100, 100)) -> Path:
        iso = self.dir / "d.iso"
        sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, _frames(frames), list(tracks), dst=False)])
        return iso

    def spec(self, area, i: int, fmt: str, name: str) -> _native.OutputSpec:
        t = area.tracks[i]
        start = t.start.total_frames
        return _native.OutputSpec(fmt, self.dir / name, start, start + t.duration.total_frames)

    def test_cancel_reacts_within_a_second(self):
        # A slow sector source (a PS3 over a poor link, say): Ctrl-C (here, an exception from the
        # progress callback) must stop the run quickly, wherever scarletbook is reading
        iso = self.image(1500, (500, 500, 500))
        data = iso.read_bytes()

        def slow(lsn: int, count: int) -> bytes:
            time.sleep(0.02)
            return data[lsn * 2048:(lsn + count) * 2048]

        raised: list[float] = []

        def progress(_p):
            if not raised:
                raised.append(time.monotonic())
                raise KeyboardInterrupt

        with SACDImage(iso) as img:
            area = read_toc(iso).stereo
            assert area is not None
            spec = self.spec(area, 2, "dsf", "x.dsf")
            with self.assertRaises(KeyboardInterrupt):
                _native.run_area(img, iso, area, [spec], source=slow, progress=progress, interval=0.2)
        self.assertTrue(raised)
        self.assertLess(time.monotonic() - raised[0], 1.0)

    def test_cancel_is_reusable(self):
        # a run after a cancelled one starts afresh (the cancel request belongs to one run)
        iso = self.image()
        area = read_toc(iso).stereo
        with SACDImage(iso) as img:
            spec = self.spec(area, 0, "dsf", "a.dsf")
            _native.run_area(img, iso, area, [spec])
        self.assertEqual(spec.frames_written, 100)

    def test_sector_source_matches_the_file(self):
        iso = self.image()
        area = read_toc(iso).stereo
        data = iso.read_bytes()
        with SACDImage(iso) as img:
            a = self.spec(area, 1, "dsf", "file.dsf")
            _native.run_area(img, iso, area, [a])
            b = self.spec(area, 1, "dsf", "source.dsf")
            _native.run_area(img, iso, area, [b], source=lambda lsn, n: data[lsn * 2048:(lsn + n) * 2048])
        self.assertEqual((self.dir / "file.dsf").read_bytes(), (self.dir / "source.dsf").read_bytes())

    def test_iso_copy_then_audio_from_it(self):
        # the copy is written in one pass; the audio is then read from the copy, so a sector
        # source is read once
        iso = self.image()
        area = read_toc(iso).stereo
        data = iso.read_bytes()
        calls: list[tuple[int, int]] = []
        lock = threading.Lock()

        def source(lsn, n):
            with lock:
                calls.append((lsn, n))
            return data[lsn * 2048:(lsn + n) * 2048]

        with SACDImage(iso) as img:
            spec = self.spec(area, 0, "dsf", "t.dsf")
            _native.run_area(img, iso, area, [spec], iso_out=self.dir / "copy.iso", source=source)
        self.assertEqual((self.dir / "copy.iso").read_bytes(), data)
        # the image once, plus the TOC sectors scarletbook reads when it opens the source
        self.assertGreaterEqual(sum(n for _, n in calls), len(data) // 2048)
        self.assertLess(sum(n for _, n in calls), len(data) // 2048 + 128)
        self.assertEqual(spec.frames_written, 100)

    def test_dst_passthrough_and_decoded_in_one_pass(self):
        # an edit master (stored DST frames) next to decoded DSF: both from one read
        iso = self.dir / "dst.iso"
        frames = [sacdgen_dst(f) for f in _frames(60)]
        sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, frames, [30, 30], dst=True)])
        area = read_toc(iso).stereo
        assert area is not None and area.is_dst
        with SACDImage(iso) as img:
            dst = _native.OutputSpec("dff-dst", self.dir / "em.dff", 0, 60)
            dsf = self.spec(area, 1, "dsf", "t.dsf")
            _native.run_area(img, iso, area, [dst, dsf])
        self.assertEqual(len(dst.dst_index), 60)
        self.assertEqual((dsf.frames_written, dsf.frames_missing, dsf.dst_errors), (30, 0, 0))
        em = (self.dir / "em.dff").read_bytes()
        for (off, length), f in zip(dst.dst_index, frames):
            self.assertEqual(em[off:off + length], f)

    def test_wrong_area_is_refused(self):
        iso = self.image()
        area = read_toc(iso).stereo
        assert area is not None
        area.toc_start += 1
        with SACDImage(iso) as img, self.assertRaisesRegex(_native.NativeError, "no area with its TOC"):
            _native.run_area(img, iso, area, [self.spec(area, 0, "dsf", "x.dsf")])


def sacdgen_dst(plain: bytes) -> bytes:
    """An uncoded DST frame (the 0 bit, then plain DSD): valid in a DST area."""
    return b"\x00" + plain


if __name__ == "__main__":
    unittest.main()
