"""The engine caps its worker count (MAX_WORKERS), so a huge -j cannot ask for memory without bound.

The address space is limited to 6 GiB: a capped pool fits, the 65 GB that 100000 workers would ask for does not."""

import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sacdgen
import test_dst_full as dst

SRC = Path(__file__).resolve().parents[1] / "src"
LIMIT = 6 * 1024**3        # address space: a capped pool and 256 thread stacks fit, 65 GB of pool does not

CHILD = r"""
import resource, sys
resource.setrlimit(resource.RLIMIT_AS, (int(sys.argv[3]), int(sys.argv[3])))
from pysacd import convert
opts = convert.Options(output_dir=sys.argv[2], musicbrainz=False, cover=False, jobs=100000)
sys.exit(convert.convert(sys.argv[1], opts))
"""


try:
    import resource
except ImportError:             # Windows has no resource module
    resource = None


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(resource is not None and hasattr(resource, "RLIMIT_AS") and sys.platform.startswith("linux"),
                     "needs RLIMIT_AS (Linux)")
class JobsCap(unittest.TestCase):
    def test_a_huge_worker_count_is_capped(self):
        with tempfile.TemporaryDirectory() as d:
            rng = random.Random(7)
            frames = [bytes(rng.getrandbits(8) for _ in range(4704 * 2)) for _ in range(20)]
            iso = Path(d) / "disc.iso"
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, frames, [10, 10], dst=False, titles=["One", "Two"])],
                              info=sacdgen.DiscInfo(title="Cap", artist="pysacd"))
            out = Path(d) / "out"
            r = subprocess.run([sys.executable, "-c", CHILD, str(iso), str(out), str(LIMIT)],
                               env=dict(os.environ, PYTHONPATH=str(SRC)), capture_output=True, text=True, check=False)
            text = r.stdout + r.stderr
            self.assertIn("using the maximum, 256", text)
            # a host that cannot start 256 threads (a pid limit) fails cleanly there; what must not happen is the
            # pool allocation failing, which is what 100000 workers asked for
            self.assertNotIn("out of memory", text)
            if r.returncode == 0:
                self.assertEqual(len(list(out.rglob("*.dsf"))), 2)
            else:
                # the wording differs between hosts ("cannot start 256 threads", "cannot start threads")
                self.assertIn("cannot start", text)


if __name__ == "__main__":
    unittest.main()
