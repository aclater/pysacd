"""pysacd convert --pauses: the same track ranges as pysacd-extract -b (#62)."""

import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sacdgen
import test_dst_full as dst

SRC = Path(__file__).resolve().parents[1] / "src"
FRAME = 4704 * 2            # one stereo DSD64 frame


def dsd_data(path: Path) -> bytes:
    """The DSD payload of a DSF file (its data chunk), without header or tags."""
    raw = path.read_bytes()
    at = raw.index(b"data")
    size = int.from_bytes(raw[at + 4:at + 12], "little") - 12
    return raw[at + 12:at + 12 + size]


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
class ConvertPauses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        rng = random.Random(62)
        frames = [bytes(rng.getrandbits(8) for _ in range(FRAME)) for _ in range(90)]
        self.iso = self.dir / "pauses.iso"
        sacdgen.build_iso(self.iso, [sacdgen.AreaSpec(2, frames, [30, 30, 30], dst=False,
                                                      titles=["One", "Two", "Three"], pauses=[4, 7, 5])],
                          info=sacdgen.DiscInfo(title="Pauses", artist="pysacd"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_py(self, *args: str, cwd: Path | None = None) -> None:
        r = subprocess.run([sys.executable, *args], env=dict(os.environ, PYTHONPATH=str(SRC)), cwd=cwd,
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def convert(self, name: str, *flags: str) -> list[bytes]:
        out = self.dir / name
        self.run_py("-m", "pysacd", "convert", "--config", os.devnull, "--no-musicbrainz", "--no-cover",
                    "-o", str(out), *flags, str(self.iso))
        return [dsd_data(p) for p in sorted(out.rglob("*.dsf"))]

    def extract(self, name: str, *flags: str) -> list[bytes]:
        out = self.dir / name
        out.mkdir()
        self.run_py("-m", "pysacd.extract", "--no-cfg", "-2", "-s", *flags, "-i", str(self.iso), cwd=out)
        return [dsd_data(p) for p in sorted(out.rglob("*.dsf"))]

    def test_pauses_match_extract_b(self):
        with_pauses = self.convert("conv-b", "--pauses")
        self.assertEqual(len(with_pauses), 3)
        self.assertEqual(with_pauses, self.extract("ext-b", "-b"))

    def test_default_trims_like_extract(self):
        trimmed = self.convert("conv")
        self.assertEqual(trimmed, self.extract("ext"))
        # With pauses, a pause belongs to the track before it, and track 1 also starts at the area
        # start: tracks 1 and 2 grow, the last track ends at the area end either way
        paused = self.convert("conv-b2", "--pauses")
        frames = [len(p) // FRAME - len(t) // FRAME for t, p in zip(trimmed, paused, strict=True)]
        self.assertEqual(frames, [4 + 7, 5, 0])


if __name__ == "__main__":
    unittest.main()
