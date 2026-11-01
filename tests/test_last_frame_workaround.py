"""The #93 workaround: an area whose only missing frame is its last, after DSD silence.

A workaround for what was seen on early Sony discs (Master TOC 1.00, pressed 2000-2001; four
discs of four titles): the area's last sector holds no audio sector header. The images here
are synthetic, with that sector overwritten by random bytes.
"""

import contextlib
import io
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import sacdgen

from pysacd import _native, extract
from pysacd.toc import read_toc

FRAME = 2 * 4704


def _native_ok() -> bool:
    try:
        _native._load()
        return True
    except (OSError, _native.NativeError):
        return False


@unittest.skipUnless(_native_ok(), "native engine not built")
class LastFrameWorkaround(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def image(self, frames: list[bytes], bad_sectors) -> Path:
        iso = self.dir / "t.iso"
        sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, frames, [len(frames) // 2, len(frames) - len(frames) // 2],
                                                 dst=False)])
        area = read_toc(iso).stereo
        assert area is not None
        b = bytearray(iso.read_bytes())
        rnd = random.Random(5)
        for lsn in bad_sectors(area):
            b[lsn * 2048:(lsn + 1) * 2048] = bytes(rnd.getrandbits(8) for _ in range(2048))
        iso.write_bytes(b)
        return iso

    def extract(self, iso: Path, *args: str) -> tuple[int, str]:
        out = self.dir / "out"
        out.mkdir(exist_ok=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = extract.main([*args, "--no-cfg", "--threads", "2", "-o", str(out), "-i", str(iso)])
        return rc, buf.getvalue()

    def test_last_frame_after_silence_is_not_a_failure(self):
        iso = self.image([bytes([0x69]) * FRAME] * 20, lambda a: [a.track_end])
        for args in (["-2", "-s"], ["-2", "-p"], ["-2", "-e"]):
            with self.subTest(args=args):
                rc, out = self.extract(iso, *args)
                self.assertEqual(rc, 0)
                self.assertIn("workaround, #93", out)

    def test_last_frame_after_music_still_fails(self):
        rnd = random.Random(1)
        iso = self.image([bytes(rnd.getrandbits(8) for _ in range(FRAME)) for _ in range(20)],
                         lambda a: [a.track_end])
        rc, out = self.extract(iso, "-2", "-s")
        self.assertEqual(rc, 1)
        self.assertNotIn("#93", out)

    def test_other_missing_frames_still_fail(self):
        # a frame in the middle, and the middle one together with the last
        for bad in (lambda a: [a.track_start + 40], lambda a: [a.track_start + 40, a.track_end]):
            with self.subTest():
                iso = self.image([bytes([0x69]) * FRAME] * 20, bad)
                rc, out = self.extract(iso, "-2", "-s")
                self.assertEqual(rc, 1)
                self.assertNotIn("#93", out)


if __name__ == "__main__":
    unittest.main()
