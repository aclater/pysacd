"""Names made from disc text never lead out of the output folder (docs/security.md,
"Hostile text"): titles that are only dots, whitespace and control characters, and a
relative -o that starts with "..".
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sacdgen
import test_dst_full as dst

from pysacd import extract
from pysacd import sbcompat as sb
from pysacd.toc import SACDError

SRC = str(Path(__file__).resolve().parent.parent / "src")

# what sacd_extract's rules leave of these: "..", ".", ".x", "", "", ". ." and ".."
TITLES = ["\x01..\x01", "\x01.\x01", "\x01.x", "..", "...", " . . ", ". .. .", " \t.. ", "\x7f..", ".", " "]


class Names(unittest.TestCase):
    def test_no_component_is_dot_dotdot_or_hidden(self):
        for t in TITLES:
            n = sb.sanitize_filename(t)
            self.assertNotIn(n, (".", ".."), repr(t))
            self.assertFalse(n.startswith((".", "-")), repr(t))
            self.assertNotIn("/", n)

    def test_ordinary_names_are_unchanged(self):
        for t, want in (("Kind of Blue", "Kind of Blue"), ("..A/B..", "A_B"), ("Vol. 2", "Vol. 2"),
                        ("R.E.M.", "R.E.M"), (" x ", "x"), ("...And Justice for All", "And Justice for All")):
            self.assertEqual(sb.sanitize_filename(t), want)

    def test_windows_names_have_no_trailing_dot_or_space(self):
        from unittest import mock
        with mock.patch.object(sb, "_WINDOWS", True):
            for t in [*TITLES, "Title.", "Title. ", "a .", "R.E.M."]:
                n = sb.sanitize_filename(t)
                self.assertFalse(n.endswith((".", " ")), repr(t))
                self.assertNotIn(n, (".", ".."))
            self.assertEqual(sb.sanitize_filename(" . . "), "_")
            self.assertEqual(sb.sanitize_filename("Kind of Blue"), "Kind of Blue")
        self.assertEqual(sb.sanitize_filename(" . . ") if not sb._WINDOWS else "_ .", "_ .")

    def test_make_filename_keeps_the_directory(self):
        self.assertEqual(sb.make_filename("../../out", "n", "dsf"), "../../out/n.dsf")
        self.assertEqual(sb.make_filename("../out", "n", "dsf"), "../out/n.dsf")
        self.assertEqual(sb.make_filename("./out", "n", "x"), "./out/n.x")
        self.assertEqual(sb.make_filename("/abs/.hidden/out.", "n", "x"), "/abs/.hidden/out./n.x")

    def test_make_filename_never_makes_a_hidden_or_empty_name(self):
        for t in TITLES:
            name = os.path.basename(sb.make_filename("out", t, "cue"))
            self.assertFalse(name.startswith("."), repr(t))
            self.assertNotEqual(name, "cue")

    def test_inside(self):
        base = Path("out/inner")
        for ok in ("out/inner", "out/inner/a", "out/inner/a/../b"):
            self.assertEqual(extract._inside(base, Path(ok)), Path(ok))
        for bad in ("out", "out/inner/..", "out/inner/../x", "/Disc 2", "out/innermost", "out/inner/a/../../b"):
            with self.assertRaises(SACDError, msg=bad):
                extract._inside(base, Path(bad))
        self.assertEqual(extract._inside(Path(), Path("a/b")), Path("a/b"))
        with self.assertRaises(SACDError):
            extract._inside(Path(), Path("../a"))


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
class Extract(unittest.TestCase):
    """End to end: every file of a run is under -o, whatever the disc says."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.frames = [sacdgen._dsd_frame(2, s) for s in range(6)]

    def _iso(self, name, title, set_size=1, sequence=1):
        path = self.root / name
        sacdgen.build_iso(path, [sacdgen.AreaSpec(2, self.frames, [3, 3], dst=False, titles=["T0", "T1"])],
                          info=sacdgen.DiscInfo(title=title, set_size=set_size, sequence=sequence))
        return path

    def _run(self, iso, out, cwd, *extra):
        env = dict(os.environ, PYTHONPATH=SRC)
        return subprocess.run([sys.executable, "-m", "pysacd.extract", "--no-cfg", "-s", "-C", "-o", str(out),
                               "-i", str(iso), *extra], cwd=cwd, env=env, capture_output=True, text=True, check=False)

    def _files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())

    def test_hostile_titles_stay_under_the_output_folder(self):
        for n, title in enumerate(TITLES):
            for set_size, seq in ((1, 1), (2, 2)):
                with self.subTest(title=title, set_size=set_size):
                    work = self.root / f"w{n}-{set_size}"
                    out = work / "out" / "inner"
                    out.mkdir(parents=True)
                    iso = self._iso(f"d{n}-{set_size}.iso", title, set_size, seq)
                    r = self._run(iso, out, work)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    written = [p for p in work.rglob("*") if p.is_file()]
                    self.assertTrue(any(p.suffix == ".dsf" for p in written))
                    for p in written:
                        self.assertTrue(p.is_relative_to(out), p)
                        self.assertFalse(any(part.startswith(".") for part in p.relative_to(out).parts), p)

    def test_relative_output_folder_with_dotdot(self):
        work = self.root / "work" / "sub"
        work.mkdir(parents=True)
        iso = self._iso("a.iso", "Album")
        for o in ("../out", "../../out2", "./out3"):
            with self.subTest(o=o):
                (work / o).mkdir()
                r = self._run(iso, o, work)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertTrue((work / o / "Album" / "01 - T0.dsf").is_file(), self._files())


if __name__ == "__main__":
    unittest.main()
