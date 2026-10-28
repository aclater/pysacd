"""pysacd convert: different discs rendering to the same directory (#4)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sacdgen
import test_dst_full as dst

SRC = Path(__file__).resolve().parents[1] / "src"


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class DiscCollisions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "out"
        self.frames = [f for _, _, f in dst.encoded(2)][:6]

    def tearDown(self):
        self.tmp.cleanup()

    def iso(self, name: str, **info) -> Path:
        path = self.dir / f"{name}.iso"
        sacdgen.build_iso(path, [sacdgen.AreaSpec(2, self.frames, [3, 3])],
                          info=sacdgen.DiscInfo(title="Live-Evil", artist="Miles Davis", **info))
        return path

    def convert(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "pysacd", "convert", "--config", os.devnull,
                               "--no-musicbrainz", "-o", str(self.out), *args],
                              env=dict(os.environ, PYTHONPATH=str(SRC)), capture_output=True, text=True, check=False)

    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.out)) for p in self.out.rglob("*.dsf"))

    def test_mastered_as_one_disc_sets_are_separated_with_hint(self):
        a = self.iso("d1", disc_catalog="CS065232")
        b = self.iso("d2", disc_catalog="CS065233")
        self.assertEqual(self.convert(str(a)).returncode, 0)
        r = self.convert(str(b))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already holds a different disc (CS065232)", r.stderr)
        self.assertIn("--disc 1/2 for CS065232, --disc 2/2 for CS065233", r.stderr)
        dirs = sorted({str(Path(f).parent) for f in self.files()})
        self.assertEqual(dirs, ["Miles Davis/Live-Evil [SACD]", "Miles Davis/Live-Evil [SACD] [CS065233]"])

    @unittest.skipUnless(dst._native_has_flac(), "engine built without libFLAC")
    def test_collision_detected_in_flac_tags(self):
        a = self.iso("d1", disc_catalog="CS065232")
        b = self.iso("d2", disc_catalog="CS065233")
        self.assertEqual(self.convert("-f", "flac", str(a)).returncode, 0)
        r = self.convert("-f", "flac", str(b))
        self.assertIn("already holds a different disc (CS065232)", r.stderr)
        self.assertEqual(len(list(self.out.rglob("*.flac"))), 4)

    def test_disc_override_joins_the_set(self):
        a = self.iso("d1", disc_catalog="CS065232")
        b = self.iso("d2", disc_catalog="CS065233")
        self.assertEqual(self.convert("--disc", "1/2", str(a)).returncode, 0)
        r = self.convert("--disc", "2/2", str(b))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("warning", r.stderr)
        self.assertEqual(self.files(), [
            "Miles Davis/Live-Evil [SACD]/1-01 - Track 1.dsf",
            "Miles Davis/Live-Evil [SACD]/1-02 - Track 2.dsf",
            "Miles Davis/Live-Evil [SACD]/2-01 - Track 1.dsf",
            "Miles Davis/Live-Evil [SACD]/2-02 - Track 2.dsf",
        ])

    def test_properly_mastered_set_shares_directory(self):
        a = self.iso("d1", disc_catalog="X-CD01", set_size=2, sequence=1)
        b = self.iso("d2", disc_catalog="X-CD02", set_size=2, sequence=2)
        self.assertEqual(self.convert(str(a)).returncode, 0)
        r = self.convert(str(b))
        self.assertNotIn("warning", r.stderr)
        self.assertEqual(len(self.files()), 4)
        self.assertEqual({str(Path(f).parent) for f in self.files()}, {"Miles Davis/Live-Evil [SACD]"})

    def test_unrelated_discs_same_title_get_no_hint(self):
        a = self.iso("d1", disc_catalog="ABC-1")
        b = self.iso("d2", disc_catalog="XYZ")
        self.convert(str(a))
        r = self.convert(str(b))
        self.assertIn("already holds a different disc", r.stderr)
        self.assertNotIn("hint:", r.stderr)

    def test_rerun_reports_skips(self):
        a = self.iso("d1", disc_catalog="CS065232")
        self.convert(str(a))
        r = self.convert(str(a))
        self.assertNotIn("different disc", r.stderr)
        self.assertIn("2 tracks skipped (already exist; --overwrite replaces them)", r.stderr)

    def test_bad_disc_value(self):
        r = self.convert("--disc", "3/2", str(self.iso("d1")))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--disc must be N/M", r.stderr)


if __name__ == "__main__":
    unittest.main()
