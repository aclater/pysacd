"""Replay the C fuzz harnesses' inputs without libFuzzer (#36).

Each harness in fuzz/ is compiled with the system C compiler and fuzz/replay.c,
then run on the checked-in regression inputs (fuzz/regressions/<harness>/)
and on freshly generated seeds. In the sanitizer CI job, the same compiler
flags turn any memory error into a failure here.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_dst_full as dst

ROOT = Path(__file__).resolve().parents[1]
HARNESSES = ("fuzz_dst_frame", "fuzz_demux")
CC = os.environ.get("CC", "cc")


@unittest.skipUnless(shutil.which(CC), f"no C compiler ({CC})")
class FuzzReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        flags = os.environ.get("PYSACD_FUZZ_CFLAGS", "-O1 -g").split()
        for h in HARNESSES:
            subprocess.run([CC, *flags, "-std=gnu17", "-o", str(cls.dir / h), str(ROOT / "fuzz" / f"{h}.c"),
                            str(ROOT / "fuzz" / "replay.c"), str(ROOT / "csrc" / "demux.c"),
                            str(ROOT / "csrc" / "dstdec.c"), "-lm"], check=True)
        cls.seeds = None
        if dst.HAVE_ENCODER:
            cls.seeds = cls.dir / "seeds"
            subprocess.run([sys.executable, str(ROOT / "fuzz" / "make_seeds.py"), str(cls.seeds)], check=True,
                           capture_output=True, env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / 'src'), str(ROOT / 'tests')])))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def replay(self, harness: str, files: list[Path]) -> None:
        if not files:
            return
        r = subprocess.run([str(self.dir / harness), *map(str, files)], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, f"{harness} failed on {len(files)} inputs:\n{r.stderr[-4000:]}")

    def test_regressions(self):
        for h in HARNESSES:
            with self.subTest(h):
                d = ROOT / "fuzz" / "regressions" / h
                self.replay(h, sorted(p for p in d.glob("*") if p.is_file() and not p.name.startswith(".")))

    @unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
    def test_seeds(self):
        assert self.seeds is not None
        for h, sub in (("fuzz_dst_frame", "dst_frame"), ("fuzz_demux", "demux")):
            with self.subTest(h):
                files = sorted((self.seeds / sub).iterdir())
                self.assertTrue(files)
                self.replay(h, files)


if __name__ == "__main__":
    unittest.main()
