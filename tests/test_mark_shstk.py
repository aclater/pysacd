"""tools/mark_shstk.py: only a GNU_PROPERTY_X86_FEATURE_1_AND with SHSTK counts as the mark.

An object assembled with `-Wa,-mx86-used-note=yes` carries "x86 feature used:" and "x86 ISA
used:" properties but no FEATURE_1_AND: it must be marked, not left alone.
"""

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("_mark_shstk", ROOT / "tools" / "mark_shstk.py")
mark_shstk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mark_shstk)

CC = os.environ.get("CC") or shutil.which("gcc") or shutil.which("cc")
X86_LINUX = sys.platform.startswith("linux") and platform.machine() in ("x86_64", "AMD64")


@unittest.skipUnless(X86_LINUX and CC and shutil.which("objcopy"), "x86-64 Linux with a C compiler and objcopy")
class MarkShstk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "f.c").write_text("int f(int x) { return x + 1; }\n")

    def tearDown(self):
        self.tmp.cleanup()

    def obj(self, name: str, *flags: str) -> Path:
        out = self.dir / f"{name}.o"
        subprocess.run([CC, "-O2", "-c", *flags, "-o", str(out), str(self.dir / "f.c")], check=True)
        return out

    def mark(self, obj: Path) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "tools" / "mark_shstk.py"), str(obj)], capture_output=True,
                              text=True, check=False)

    def test_used_note_is_not_a_mark(self):
        o = self.obj("used", "-fcf-protection=none", "-Wa,-mx86-used-note=yes")
        notes = subprocess.run(["readelf", "-n", "-W", str(o)], capture_output=True, text=True, check=True).stdout
        self.assertIn("x86 feature used", notes)        # what the old text match took for a mark
        self.assertEqual(mark_shstk.features_of(str(o)), [])
        done = self.mark(o)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("marked SHSTK", done.stdout)
        self.assertTrue(all(b & 2 for b in mark_shstk.features_of(str(o))) and mark_shstk.features_of(str(o)))
        # the merged note keeps the other properties and survives a link: the linker marks the
        # output only when every input is marked
        props = [t for t, _ in mark_shstk.property_section(o.read_bytes())]
        self.assertEqual(props, sorted(props))
        self.assertGreater(len(props), 1)
        so = self.dir / "used.so"
        subprocess.run([CC, "-shared", "-nostdlib", "-o", str(so), str(o)], check=True)
        self.assertEqual(mark_shstk.features_of(str(so)), [2])

    def test_unmarked_object_is_marked(self):
        o = self.obj("plain", "-fcf-protection=none")
        self.assertEqual(self.mark(o).returncode, 0)
        self.assertEqual(mark_shstk.features_of(str(o)), [2])

    def test_marked_object_is_left_alone(self):
        o = self.obj("cet", "-fcf-protection")
        before = o.read_bytes()
        done = self.mark(o)
        self.assertEqual(done.returncode, 0)
        self.assertIn("left alone", done.stdout)
        self.assertEqual(o.read_bytes(), before)

    def test_feature_property_without_shstk_is_refused(self):
        o = self.obj("ibt", "-fcf-protection=branch")
        self.assertEqual(mark_shstk.features_of(str(o)), [1])
        done = self.mark(o)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("without SHSTK", done.stderr)


class Parser(unittest.TestCase):
    def test_not_elf(self):
        with self.assertRaises(ValueError):
            mark_shstk.feature_1_and(b"not an ELF file at all, just text....." * 4)


if __name__ == "__main__":
    unittest.main()
