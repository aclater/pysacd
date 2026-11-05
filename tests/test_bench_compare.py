"""The comparison logic of the informational CI benchmark (scripts/bench_compare.py, #123)."""

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_compare.py"
spec = importlib.util.spec_from_file_location("bench_compare", SCRIPT)
assert spec and spec.loader
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class Compare(unittest.TestCase):
    def test_builds_alternate_and_swap_each_round(self):
        self.assertEqual([bench.order(r) for r in range(3)],
                         [("base", "head"), ("head", "base"), ("base", "head")])

    def test_flag_is_on_medians_over_the_threshold(self):
        base = [1.0, 1.0, 1.0, 9.0, 0.1]  # an outlier either way doesn't move the median
        self.assertFalse(bench.is_flagged(base, [1.1, 1.1, 1.1, 1.1, 1.1], 1.10))
        self.assertTrue(bench.is_flagged(base, [1.11, 1.11, 1.11, 0.1, 9.0], 1.10))

    def test_report_names_the_slower_workload(self):
        text, flagged = bench.report({"a": {"base": [1.0], "head": [1.5]}, "b": {"base": [1.0], "head": [1.0]}},
                                     1.10, 3.0)
        self.assertEqual(flagged, ["a"])
        self.assertIn("| a | 1.000 s (1.000 to 1.000) | 1.500 s (1.500 to 1.500) | 1.500 | **SLOWER** |", text)
        self.assertIn("| b |", text)

    def run_main(self, measure: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--base", "1", "--head", "2", "--rounds", "2", "--measure", measure, "w"],
            capture_output=True, text=True, check=False, env=env)

    def test_a_slower_head_only_warns(self):
        r = self.run_main(f"{sys.executable} -I -c 'import sys; print(sys.argv[1])'")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("**SLOWER**", r.stdout)

    def test_a_failed_measurement_fails(self):
        r = self.run_main("false")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("failed", r.stderr)


if __name__ == "__main__":
    unittest.main()
