"""Output paths longer than Windows' 260 characters, and temporaries a failed move leaves (#217).

On Windows, pysacd turns each output folder into an extended-length path (safeio.long_path()),
so a long album title in a deep folder can still be written without the system-wide
LongPathsEnabled setting; users never see the prefix. Elsewhere the same tests check that the
paths are written and that nothing changes. A temporary that can't be moved into place is
removed on every platform.
"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sacdgen
import test_dst_full as dst

from pysacd import extract, safeio, safetext

LONG = "\\\\?\\"


class Helpers(unittest.TestCase):
    def test_long_path(self):
        if not safeio.WINDOWS:
            for p in ("out", "/abs/out", Path("rel/out")):
                self.assertEqual(safeio.long_path(p), os.fspath(p))
            return
        cwd = os.getcwd()
        self.assertEqual(safeio.long_path("out"), LONG + os.path.join(cwd, "out"))
        self.assertEqual(safeio.long_path("C:/a/b/../c"), LONG + "C:\\a\\c")
        self.assertEqual(safeio.long_path("\\\\server\\share\\x"), LONG + "UNC\\server\\share\\x")
        self.assertEqual(safeio.long_path(LONG + "C:\\x"), LONG + "C:\\x")      # once only
        self.assertEqual(safeio.long_path("\\\\.\\pipe\\x"), "\\\\.\\pipe\\x")  # device paths stay

    def test_display_path(self):
        with mock.patch.object(safeio, "WINDOWS", True):
            self.assertEqual(safeio.display_path(LONG + "C:\\x"), "C:\\x")
            self.assertEqual(safeio.display_path(LONG + "UNC\\server\\share\\x"), "\\\\server\\share\\x")
            self.assertEqual(safeio.display_path("C:\\x"), "C:\\x")
        with mock.patch.object(safeio, "WINDOWS", False):     # an odd but valid POSIX name
            self.assertEqual(safeio.display_path(LONG + "x"), LONG + "x")

    def test_messages_never_show_the_prefix(self):
        long = LONG + "C:\\out\\x.dsf"
        unc = LONG + "UNC\\server\\share\\x.dsf"
        err = str(FileNotFoundError(2, "No such file", long))
        with mock.patch.object(safetext, "_WINDOWS", True):
            self.assertEqual(safetext.for_terminal(f"-> {long}"), "-> C:\\out\\x.dsf")
            self.assertEqual(safetext.for_terminal(unc), "\\\\server\\share\\x.dsf")
            self.assertEqual(safetext.for_terminal(err), "[Errno 2] No such file: 'C:\\\\out\\\\x.dsf'")
            self.assertEqual(safetext.for_terminal(repr(unc)), "'\\\\\\\\server\\\\share\\\\x.dsf'")
        with mock.patch.object(safetext, "_WINDOWS", False):
            self.assertEqual(safetext.for_terminal(long), long)

    def test_inside_with_the_prefix(self):
        base = safeio.long_path(os.path.abspath("out"))
        self.assertTrue(safeio.inside(base, os.path.join(base, "a", "b.dsf")))
        self.assertFalse(safeio.inside(base, os.path.join(base, "..", "b.dsf")))
        self.assertFalse(safeio.inside(base, os.path.join(base + "x", "b.dsf")))


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
class Extract(unittest.TestCase):
    """pysacd-extract writes past 260 characters, and removes what it can't move into place."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Windows: rmtree by the plain name can't reach paths past 260 characters
        self.addCleanup(shutil.rmtree, safeio.long_path(self.tmp.name), ignore_errors=True)
        self.dir = Path(self.tmp.name)
        self.iso = self.dir / "in.iso"
        frames = [sacdgen._dsd_frame(2, i) for i in range(6)]
        # a 200-character title: the album folder and the ISO's name are each about 200 long
        sacdgen.build_iso(self.iso, [sacdgen.AreaSpec(2, frames, [3, 3], dst=False, titles=["One", "Two"])],
                          info=sacdgen.DiscInfo(title="T" * 200, artist="A"))
        # deep enough on every platform: about 330 characters to the ISO
        self.out = self.dir / ("d" * 100) / ("e" * 100)
        os.makedirs(safeio.long_path(self.out))

    def extract(self, *args: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = extract.main([*args, "--no-cfg", "--threads", "2", "-o", str(self.out), "-i", str(self.iso)])
        return rc, buf.getvalue()

    def files(self) -> list[Path]:
        return sorted(p for p in Path(safeio.long_path(self.out)).rglob("*") if p.is_file())

    def test_paths_past_260_characters(self):
        rc, text = self.extract("-s", "-I")
        self.assertEqual(rc, 0, text)
        files = self.files()
        self.assertEqual(sorted(p.suffix for p in files), [".dsf", ".dsf", ".iso"], files)
        self.assertTrue(all(len(safeio.display_path(p)) > 260 for p in files), files)
        self.assertEqual(next(p for p in files if p.suffix == ".iso").read_bytes(), self.iso.read_bytes())
        self.assertIn(str(self.out), text)  # the output folder as the user gave it
        self.assertNotIn(LONG, text)        # Windows: users see their own paths
        self.assertNotIn("\\\\\\\\?\\\\", text)

    def failing_move(self, suffix: str):
        real = os.replace

        def replace(src, dst):
            if os.fspath(dst).endswith(suffix):
                raise OSError(13, "refused by the test", os.fspath(dst))
            return real(src, dst)
        return mock.patch.object(extract.os, "replace", side_effect=replace)

    def test_a_copy_that_cant_be_moved_into_place_is_removed(self):
        # On Windows without long paths this was the ISO's move to a path past 260 characters
        with self.failing_move(".iso"):
            rc, text = self.extract("-I")
        self.assertEqual(rc, 255, text)
        self.assertIn("refused by the test", text)
        self.assertEqual(self.files(), [])

    def test_tracks_that_cant_be_moved_into_place_are_removed(self):
        with self.failing_move(".dsf"):
            rc, text = self.extract("-s")
        self.assertEqual(rc, 255, text)
        self.assertEqual(self.files(), [])


if __name__ == "__main__":
    unittest.main()
