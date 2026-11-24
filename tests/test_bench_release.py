"""The release timing harness (tools/bench_release.py, #212): a tiny run on a generated disc."""

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import dstenc
import sacdgen

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "bench_release.py"
spec = importlib.util.spec_from_file_location("bench_release", SCRIPT)
assert spec and spec.loader
bench = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bench  # dataclasses look the module up here (Python <= 3.14)
spec.loader.exec_module(bench)


class Logic(unittest.TestCase):
    def test_order_rotates_each_round(self):
        self.assertEqual([bench.order(["a", "b", "c"], r) for r in range(4)],
                         [["a", "b", "c"], ["b", "c", "a"], ["c", "a", "b"], ["a", "b", "c"]])

    def test_check_follows_the_release_plan(self):
        prev = [10.0, 10.1, 10.2]
        self.assertEqual(bench.check(prev, [10.1, 10.2, 10.3], [100], [100]), "ok")         # ranges overlap
        self.assertEqual(bench.check(prev, [10.3, 10.4, 10.5], [100], [100]), "slower, ranges apart")
        self.assertTrue(bench.check(prev, [10.7, 10.7, 10.8], [100], [100]).startswith("FAIL"))  # > 5 %
        self.assertIn("FAIL: peak memory", bench.check(prev, prev, [100], [111]))           # > 10 %
        self.assertEqual(bench.check(prev, prev, [100], [109]), "ok")
        self.assertEqual(bench.check(prev[:2], [20.0, 20.0], [100], [200]), "not checked: under 3 runs")

    def test_cpus_are_distinct_cores(self):
        fast, many, low, _ = bench.pick_cpus(2, None)
        self.assertIn(fast, many)
        self.assertNotIn(low, many)
        self.assertEqual(len(set(many)), len(many))


class TinyRun(unittest.TestCase):
    def test_two_trees_one_round(self):
        if not list((ROOT / "src/pysacd").glob("_sacdconv.*")):
            self.skipTest("needs the engine (make)")
        with tempfile.TemporaryDirectory() as d:
            disc = Path(d) / "tiny.iso"
            areas = []
            for ch in (2, 6):
                size = ch * dstenc.FL
                dsd = dstenc.synth_dsd(ch, 4)
                frames = [dsd[i * size:(i + 1) * size] for i in range(4)] * 2
                areas.append(sacdgen.AreaSpec(ch, frames, [4, 4], dst=False))
            sacdgen.build_iso(disc, areas, title="bench")
            out = Path(d) / "out"
            discs = [f"--disc={role}={disc}" for role in ("dst2", "dst6", "plain")]
            r = subprocess.run([sys.executable, str(SCRIPT), "run", f"--tree=old={ROOT}", f"--tree=new={ROOT}",
                                *discs, "--out", str(out), "--rounds", "1", "--threads", "2", "--no-benchlock",
                                "--tmp", d, "--rows", "dst2-1t", "dst6-nt", "dst6-1t", "plain-1t"],
                               capture_output=True, text=True, check=False)
            self.assertIn(r.returncode, (0, 2), r.stderr)
            with open(out / "runs.csv", newline="") as f:
                runs = list(csv.DictReader(f))
            self.assertEqual(len(runs), 8)
            self.assertTrue(all(x["rc"] == "0" and int(x["maxrss_kb"]) > 0 and int(x["bytes"]) > 0 for x in runs))
            # the same tree twice writes the same bytes per row
            for row in {x["row"] for x in runs}:
                self.assertEqual(len({x["bytes"] for x in runs if x["row"] == row}), 1)
            table = (out / "table.md").read_text()
            self.assertIn("| Wall s | old", table)
            self.assertIn("vs old", table)
            self.assertIn("Peak RSS MiB", table)
            self.assertEqual(json.loads((out / "meta.json").read_text())["rows"],
                             ["dst2-1t", "dst6-nt", "dst6-1t", "plain-1t"])


if __name__ == "__main__":
    unittest.main()
