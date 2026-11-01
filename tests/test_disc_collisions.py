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

    def iso(self, name: str, titles: list[str] | None = None, **info) -> Path:
        path = self.dir / f"{name}.iso"
        sacdgen.build_iso(path, [sacdgen.AreaSpec(2, self.frames, [3, 3], titles=titles or [])],
                          info=sacdgen.DiscInfo(title="Live-Evil", artist="Miles Davis", **info))
        return path

    def convert(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "pysacd", "convert", "--config", os.devnull,
                               "--no-musicbrainz", "-o", str(self.out), *args],
                              env=dict(os.environ, PYTHONPATH=str(SRC)), capture_output=True, text=True, check=False)

    def files(self) -> list[str]:
        return sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*.dsf"))

    def test_mastered_as_one_disc_sets_are_separated_with_hint(self):
        a = self.iso("d1", disc_catalog="CS065232")
        b = self.iso("d2", disc_catalog="CS065233")
        self.assertEqual(self.convert(str(a)).returncode, 0)
        r = self.convert(str(b))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already holds a different disc (CS065232)", r.stderr)
        self.assertIn("--disc 1/2 for CS065232, --disc 2/2 for CS065233", r.stderr)
        dirs = sorted({Path(f).parent.as_posix() for f in self.files()})
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
        self.assertEqual({Path(f).parent.as_posix() for f in self.files()}, {"Miles Davis/Live-Evil [SACD]"})

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


SACD_EXTRACT = os.environ.get("SACD_EXTRACT")


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class ExtractDiscCollisions(DiscCollisions):
    """pysacd-extract: same policy, a documented exception to byte-identity with sacd_extract."""

    def extract(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "pysacd.extract", "--no-cfg", *args], cwd=cwd or self.out,
                              env=dict(os.environ, PYTHONPATH=str(SRC)), capture_output=True, text=True, check=False)

    def setUp(self):
        super().setUp()
        self.out.mkdir()

    def tree(self, root: Path) -> dict[str, bytes]:
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    def test_mastered_as_one_disc_sets_are_separated_with_hint(self):
        # As on the real Live-Evil: same album title, different track titles
        a = self.iso("d1", disc_catalog="CS065232", titles=["Sivad", "Little Church"])
        b = self.iso("d2", disc_catalog="CS065233", titles=["Selim", "Funky Tonk"])
        self.assertEqual(self.extract("-s", "-i", str(a)).returncode, 0)
        r = self.extract("-s", "-i", str(b))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already holds tracks of another disc", r.stderr)
        self.assertIn("--disc N/M", r.stderr)
        self.assertEqual(sorted({Path(f).parent.as_posix() for f in self.files()}), ["Live-Evil", "Live-Evil [CS065233]"])

    def test_disc_override_joins_the_set(self):
        a = self.iso("d1", disc_catalog="CS065232")
        b = self.iso("d2", disc_catalog="CS065233")
        self.assertEqual(self.extract("--disc", "1/2", "-s", "-i", str(a)).returncode, 0)
        r = self.extract("--disc", "2/2", "-s", "-i", str(b))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("WARNING", r.stderr)
        self.assertEqual(sorted({Path(f).parent.as_posix() for f in self.files()}), ["Live-Evil/Disc 1", "Live-Evil/Disc 2"])

    def test_properly_mastered_set_shares_directory(self):
        a = self.iso("d1", disc_catalog="X-CD01", set_size=2, sequence=1)
        b = self.iso("d2", disc_catalog="X-CD02", set_size=2, sequence=2)
        self.extract("-s", "-i", str(a))
        r = self.extract("-s", "-i", str(b))
        self.assertNotIn("WARNING", r.stderr)

    def test_same_disc_rerun_overwrites_like_sacd_extract(self):
        a = self.iso("d1", disc_catalog="CS065232")
        self.extract("-s", "-i", str(a))
        r = self.extract("-s", "-i", str(a))
        self.assertNotIn("WARNING", r.stderr)
        self.assertEqual(sorted({Path(f).parent.as_posix() for f in self.files()}), ["Live-Evil"])

    def test_same_names_different_disc_not_overwritten(self):
        # Same title and track names, different ISRC-free identity: track numbers differ
        a = self.iso("d1", disc_catalog="AAA")
        self.extract("-s", "-i", str(a))
        frames = [f for _, _, f in dst.encoded(2)][:6]
        b = self.dir / "d2.iso"
        sacdgen.build_iso(b, [sacdgen.AreaSpec(2, frames, [2, 2, 2])],
                          info=sacdgen.DiscInfo(title="Live-Evil", artist="Miles Davis", disc_catalog="BBB"))
        r = self.extract("-s", "-i", str(b))
        self.assertIn("already holds tracks of another disc", r.stderr)
        self.assertIn("Live-Evil [BBB]", {Path(f).parent.as_posix() for f in self.files()})

    def test_bad_disc_value(self):
        r = self.extract("--disc", "0/2", "-s", "-i", str(self.iso("d1")))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--disc must be N/M", r.stderr)

    @unittest.skip("convert-only")
    def test_collision_detected_in_flac_tags(self):
        pass

    @unittest.skip("convert-only")
    def test_rerun_reports_skips(self):
        pass

    @unittest.skip("convert-only")
    def test_unrelated_discs_same_title_get_no_hint(self):
        pass

    @unittest.skipUnless(SACD_EXTRACT, "set SACD_EXTRACT to compare with the real sacd_extract")
    def test_disc_override_matches_sacd_extract_set_layout(self):
        # A 1/1 disc with --disc 2/2 must produce exactly what sacd_extract
        # produces for the same disc mastered as disc 2 of a 2-disc set.
        one = self.iso("d1", disc_catalog="CS065233")
        two = self.iso("d2", disc_catalog="CS065233", set_size=2, sequence=2)
        ours, theirs = self.dir / "ours", self.dir / "theirs"
        ours.mkdir(), theirs.mkdir()
        boot = ("import sys;from pysacd import sbcompat as s, extract;"
                "s.PYSACD_VERSION_INFO=s.SACD_RIPPER_VERSION_INFO;"
                "sys.exit(extract.main())")
        for args in (["-s"], ["-p"]):
            r = subprocess.run([sys.executable, "-c", boot, "--no-cfg", "--disc", "2/2", *args, "-i", str(one)],
                               cwd=ours, env=dict(os.environ, PYTHONPATH=str(SRC)), capture_output=True, text=True,
                               check=False)
            self.assertEqual(r.returncode, 0, r.stderr)
            subprocess.run([SACD_EXTRACT, *args, f"-i{two}"], cwd=theirs, capture_output=True, check=False)
        a, b = self.tree(ours), self.tree(theirs)
        self.assertEqual(sorted(a), sorted(b))
        for k in a:
            if k.endswith(".dff"):
                continue    # creating-machine COMT timestamp differs
            self.assertEqual(a[k], b[k], k)


if __name__ == "__main__":
    unittest.main()
