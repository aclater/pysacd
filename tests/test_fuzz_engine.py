"""Replay the engine fuzz harness's inputs without libFuzzer (#36, #85).

fuzz/fuzz_engine.c is compiled with the system C compiler and fuzz/replay.c (`make
fuzz-replay`, linked with the scarletbook library), then run on the checked-in regression
inputs (fuzz/regressions/fuzz_engine/) and on the seeds (fuzz/make_seeds.py): plain images, and
configured runs (cfg-*.iso, #157) that reach ranges past frame 0, DSF chains, the ISO copy,
cancellation, 176.4 kHz PCM, FLAC, other thread counts and the filler limit. In the ASan and TSan
CI jobs, PYSACD_FUZZ_CFLAGS gives it the same sanitizer flags, so a memory error or a data race
in the engine fails here.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from buildenv import build_env

ROOT = Path(__file__).resolve().parents[1]
CC = os.environ.get("CC", "cc")


@unittest.skipUnless(shutil.which(CC) and shutil.which("make") and sys.platform.startswith("linux"),
                     "Linux, with a C compiler and make")
class FuzzReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        flags = os.environ.get("PYSACD_FUZZ_CFLAGS", "-O1 -g")
        cls.harness = cls.dir / "replay_engine"
        subprocess.run(["make", "-s", "-C", str(ROOT), f"REPLAY_FLAGS={flags}", "build/replay_engine"], check=True,
                       env=build_env())
        shutil.copy(ROOT / "build" / "replay_engine", cls.harness)
        cls.seeds = cls.dir / "seeds"
        subprocess.run([sys.executable, str(ROOT / "fuzz" / "make_seeds.py"), str(cls.seeds)], check=True,
                       capture_output=True,
                       env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests")])))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def replay(self, files: list[Path]) -> None:
        if not files:
            return
        r = subprocess.run([str(self.harness), *map(str, files)], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, f"fuzz_engine failed on {len(files)} inputs:\n{r.stderr[-4000:]}")

    def test_regressions(self):
        d = ROOT / "fuzz" / "regressions" / "fuzz_engine"
        self.replay(sorted(p for p in d.glob("*") if p.is_file() and not p.name.startswith(".")))

    def test_seeds(self):
        files = sorted((self.seeds / "engine").iterdir())
        self.assertGreater(len(files), 10)
        self.replay(files)

    def test_configured_seeds(self):
        """The configured runs are there, and each starts with the harness's magic."""
        files = sorted((self.seeds / "engine").glob("cfg-*.iso"))
        self.assertGreaterEqual(len(files), 30)
        for f in files:
            with open(f, "rb") as fp:
                self.assertEqual(fp.read(8), b"PSFZcfg1", f.name)


if __name__ == "__main__":
    unittest.main()
