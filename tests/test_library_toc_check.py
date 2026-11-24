"""tools/library_toc_check.py: the library TOC check (#213), on generated discs."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sacdgen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import library_toc_check as ltc

TOOL = ROOT / "tools" / "library_toc_check.py"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *args], check=False, capture_output=True, text=True, timeout=300)


def rows_of(text: str) -> dict[str, dict]:
    head, *lines = text.splitlines()
    cols = head.split("\t")
    return {r["id"]: r for r in (dict(zip(cols, ln.split("\t"))) for ln in lines)}


class LibraryTocCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        (t / "lib" / "a").mkdir(parents=True)
        (t / "lib" / "b").mkdir()
        good = t / "lib" / "a" / "Secret Artist - Secret Title.iso"
        sacdgen.build_iso(good, sacdgen.base_areas(), title="Secret Title")
        plain = t / "lib" / "b" / "plain.ISO"
        sacdgen.build_iso(plain, sacdgen.base_areas()[1:])
        img = bytes(good.read_bytes())
        (t / "lib" / "truncated.iso").write_bytes(img[:505 * 2048])
        (t / "lib" / "garbage.iso").write_bytes(bytes(range(256)) * 10000)
        (t / "lib" / "empty.iso").write_bytes(b"")
        (t / "lib" / "notes.txt").write_text("not an image")
        (t / "lib" / "a" / "good.iso.bak").write_bytes(img)
        cls.lib = t / "lib"
        cls.t = t
        out = t / "run.tsv"
        cls.proc = run(str(cls.lib), "--out", str(out), "--index", str(t / "index.tsv"))
        cls.rows = rows_of(out.read_text())
        cls.index = dict(ln.split("\t") for ln in (t / "index.tsv").read_text().splitlines())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def by_name(self, rel: str) -> dict:
        (i,) = [i for i, p in self.index.items() if p == rel]
        return self.rows[i]

    def test_finds_only_isos(self):
        self.assertEqual(sorted(self.index.values()),
                         ["a/Secret Artist - Secret Title.iso", "b/plain.ISO", "empty.iso", "garbage.iso",
                          "truncated.iso"])

    def test_good_images(self):
        r = self.by_name("a/Secret Artist - Secret Title.iso")
        self.assertEqual((r["status"], r["areas"], r["tracks"], r["dst"], r["text"]),
                         ("ok", "2ch+mc", "3/2", "mixed", "yes"))
        r = self.by_name("b/plain.ISO")
        self.assertEqual((r["status"], r["areas"], r["tracks"], r["dst"]), ("ok", "mc", "2", "plain"))

    def test_broken_images(self):
        for name in ("empty.iso", "garbage.iso", "truncated.iso"):
            r = self.by_name(name)
            self.assertNotEqual(r["status"], "ok", name)
            self.assertTrue(r["error"], name)

    def test_summary_and_exit(self):
        self.assertIn("images checked: 5", self.proc.stderr)
        self.assertIn("  ok: 2", self.proc.stderr)

    def test_no_names_in_tsv(self):
        out = (self.t / "run.tsv").read_text()
        for word in ("Secret", "garbage", "plain.ISO", "truncated", str(self.t)):
            self.assertNotIn(word, out)

    def test_ids_stable(self):
        again = run(str(self.lib))
        self.assertEqual(set(rows_of(again.stdout)), set(self.rows))

    def test_compare(self):
        out = self.t / "run.tsv"
        same = run(str(self.lib), "--compare", str(out), "--out", str(self.t / "same.tsv"))
        self.assertEqual(same.returncode, 0, same.stderr)
        self.assertIn("new errors: 0", same.stderr)
        # break the good image: a new error and a changed row against the old run
        lib2 = self.t / "lib2"
        lib2.mkdir()
        for p in self.lib.rglob("*.iso"):
            (lib2 / p.relative_to(self.lib).parent).mkdir(exist_ok=True)
        (lib2 / "b").mkdir(exist_ok=True)
        (lib2 / "b" / "plain.ISO").write_bytes(b"\0" * 4096)
        new = run(str(lib2), "--compare", str(out), "--out", str(self.t / "new.tsv"))
        self.assertEqual(new.returncode, 1)
        self.assertIn("new errors: 1", new.stderr)
        self.assertIn("no longer present: 4", new.stderr)

    def test_bad_arguments(self):
        self.assertEqual(run(str(self.t / "missing")).returncode, 2)
        self.assertEqual(run(str(self.lib), "--compare", str(self.t / "missing.tsv")).returncode, 2)

    def test_timeout_and_crash(self):
        w = ltc.Worker()
        try:
            r = w.read(str(self.lib / "garbage.iso"), 0)
            self.assertEqual(r["status"], "timeout")
            w.start = lambda: setattr(w, "proc", subprocess.Popen(  # type: ignore[method-assign]
                [sys.executable, "-c", "import os; os._exit(7)"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, text=True))
            r = w.read("x", 30)
            self.assertEqual(r["status"], "crash")
            self.assertIn("7", r["error"])
        finally:
            w.stop()

    def test_error_text_has_no_path(self):
        self.assertEqual(ltc.clean("cannot open /a/b/c.iso:\n bad", "/a/b/c.iso"), "cannot open <image>: bad")


if __name__ == "__main__":
    unittest.main()
