"""tools/check_skips.py: a skip nobody expected fails CI (#131)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_skips

REPORT = {"tests": 2, "skips": {"tests.t.A.off": "set X"}}


class CheckSkips(unittest.TestCase):
    def report(self, report: dict = REPORT) -> tuple[int, dict[str, str]]:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "r.json")
            p.write_text(json.dumps(report))
            return check_skips.read_report(p)

    def test_reads_tests_and_skips(self):
        self.assertEqual(self.report(), (2, {"tests.t.A.off": "set X"}))

    def test_exactly_the_expected_skips_pass(self):
        self.assertEqual(check_skips.problems(*self.report(), {"tests.t.A.off": "set X"}), [])

    def test_a_new_skip_fails(self):
        self.assertIn("unexpected skip", check_skips.problems(*self.report(), {})[0])

    def test_a_stopped_skip_fails(self):
        want = {"tests.t.A.off": "set X", "tests.t.A.gone": "set Y"}
        self.assertIn("no longer skips", check_skips.problems(*self.report(), want)[0])

    def test_a_changed_reason_fails(self):
        self.assertIn("reason changed", check_skips.problems(*self.report(), {"tests.t.A.off": "set Z"})[0])

    def test_no_tests_fails(self):
        self.assertIn("no tests", check_skips.problems(*self.report({"tests": 0, "skips": {}}), {})[0])

    def test_every_expected_skip_says_why(self):
        for e in json.loads(check_skips.EXPECTED.read_text(encoding="utf-8")):
            self.assertTrue(e["why"].strip() and e["reason"].strip() and e["jobs"] and e["tests"], e)


if __name__ == "__main__":
    unittest.main()
