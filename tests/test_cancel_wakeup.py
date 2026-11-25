"""A cancel from another thread wakes the engine's waiting threads.

sacdconv_cancel() used to set the cancel flag that every wait in the engine checks, without
signalling the condition variables those waits sleep on. If the workers then stopped with
batches still queued, the writer (waiting for the next batch) and the reader (waiting for a free
one) were never woken, and the run hung; Ctrl-C during a conversion could hang pysacd the same
way. tests/native/cancel_wakeup.c reaches that state on purpose, through the engine's test hooks
(SC_TEST_HOOKS), and fails if the cancelled run doesn't return within its time limit.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import sacdgen
from buildenv import build_env

ROOT = Path(__file__).resolve().parents[1]
CC = os.environ.get("CC", "cc")


@unittest.skipUnless(shutil.which(CC) and shutil.which("make") and sys.platform.startswith("linux"),
                     "Linux, with a C compiler and make")
class CancelWakeup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        # the sanitizer CI jobs' flags, as for the fuzz harness's replay
        flags = os.environ.get("PYSACD_FUZZ_CFLAGS", "-O1 -g")
        subprocess.run(["make", "-s", "-C", str(ROOT), f"REPLAY_FLAGS={flags}", "build/cancel_wakeup"], check=True,
                       env=build_env())
        cls.driver = cls.dir / "cancel_wakeup"
        shutil.copy(ROOT / "build" / "cancel_wakeup", cls.driver)
        # 300 frames: ten batches, more than the pool of a one-worker run holds
        frames = [bytes((i * 31 + k * 7) & 0xFF for i in range(2 * 4704)) for k in range(300)]
        cls.iso = cls.dir / "d.iso"
        sacdgen.build_iso(cls.iso, [sacdgen.AreaSpec(2, frames, [100, 100, 100], dst=False)])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_cancel_wakes_writer_and_reader(self):
        for _ in range(3):
            r = subprocess.run([str(self.driver), str(self.iso), str(self.dir)], capture_output=True, text=True,
                               timeout=120, check=False)
            self.assertEqual(r.returncode, 0, f"cancel_wakeup exited {r.returncode}:\n{r.stderr[-4000:]}")


if __name__ == "__main__":
    unittest.main()
