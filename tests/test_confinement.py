"""Every writer stays inside its output directory and creates files safely (#14).

A disc with hostile titles goes through every output mode of pysacd-extract and pysacd convert:
- every path created must resolve under the output directory;
- every file must be a regular file with mode 0644, even under umask 0 (no execute bits);
- no temporary file may be left behind.

A second pass pre-creates each output name as a symlink to a file outside the output directory.
Those outside files must stay untouched: writers create new files and replace names, and never
write through a symlink.
"""

import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import sacdgen
import test_safetext as hs

from pysacd import _native

EXTRACT_MODES = [["-s", "-C"], ["-s", "-z"], ["-s", "-k"], ["-p"], ["-e"], ["-I"], ["-p", "-c"], ["--wav"]]
CONVERT_FORMATS = ["dsf", "wav"]


def _native_ok() -> bool:
    try:
        _native._load()
        return True
    except (ImportError, OSError, RuntimeError):
        return False


@contextlib.contextmanager
def umask(mask: int):
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


@unittest.skipUnless(_native_ok(), "native engine not built")
class Confinement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        frame = bytes([0x69]) * (4704 * 2)
        n = len(hs.DISC_TITLES)
        cls.iso = cls.root / "hostile.iso"
        sacdgen.build_iso(cls.iso, [sacdgen.AreaSpec(2, [frame] * (10 * n), [10] * n, dst=False,
                                                     titles=hs.DISC_TITLES)], title=hs.ALBUM)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_mode(self, out: Path, mode: list[str]) -> None:
        from pysacd import __main__ as cli
        from pysacd import extract
        if _native.has_flac() is False and "--flac" in mode:
            self.skipTest("no libFLAC")
        old = os.getcwd()
        os.chdir(out)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), umask(0):
                if mode[0] == "convert":
                    cli.main(["convert", "--no-musicbrainz", "--no-cover", "-f", mode[1], "-o", str(out),
                              str(self.iso)])
                else:
                    extract.main(["--no-cfg", "-2", *mode, "-i", str(self.iso)])
        finally:
            os.chdir(old)

    def modes(self):
        return [m for m in EXTRACT_MODES] + [["convert", f] for f in CONVERT_FORMATS]

    def check_tree(self, out: Path) -> list[Path]:
        made = sorted(out.rglob("*"))
        self.assertTrue(made)
        for p in made:
            rel = p.relative_to(out)
            self.assertTrue(p.resolve().is_relative_to(out.resolve()), rel)
            self.assertFalse(p.name.endswith(".partial"), f"temporary file left: {rel}")
            st = p.lstat()
            if stat.S_ISDIR(st.st_mode):
                continue
            self.assertTrue(stat.S_ISREG(st.st_mode), f"not a regular file: {rel}")
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o644, f"{rel}: mode {oct(st.st_mode)}")
        return made

    def test_every_writer(self):
        for mode in self.modes():
            with self.subTest(mode=mode):
                name = "-".join(m.strip("-") for m in mode)
                out = self.root / f"out-{name}"
                out.mkdir()
                self.run_mode(out, mode)
                files = [p for p in self.check_tree(out) if p.is_file()]

                # Second pass: every output name is a symlink to a file outside out2.
                out2 = self.root / f"trap-{name}"
                outside = self.root / f"outside-{name}"
                outside.mkdir()
                out2.mkdir()
                canaries = {}
                for i, f in enumerate(files):
                    target = outside / f"canary{i}"
                    target.write_bytes(b"canary")
                    canaries[target] = b"canary"
                    link = out2 / f.relative_to(out)
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(target)
                self.run_mode(out2, mode)
                for target, data in canaries.items():
                    self.assertEqual(target.read_bytes(), data, f"{mode}: wrote through a symlink to {target.name}")
                for p in out2.rglob("*"):
                    if not p.is_symlink():
                        self.assertTrue(p.resolve().is_relative_to(out2.resolve()))


if __name__ == "__main__":
    unittest.main()
