"""tools/quality_compare.py: reports in --work are reused only for the run that made them."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import quality_compare as qc

WANT = {"pysacd": {"kind": "wheel", "file": "pysacd-0.5.5.whl", "sha256": "a" * 64, "version": "0.5.5"},
        "dsd_measure": {"version": "0.1.1", "commit": "d" * 40}, "suite_sha256": "s" * 64}


def suite_report(folder: Path, inputs: list[str]) -> Path:
    folder.mkdir(parents=True)
    (folder / "provenance.json").write_text(json.dumps({"adapter_calls": [{"input_sha256": h} for h in inputs]}))
    return folder


class Reuse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "report"
        self.reports = [suite_report(self.out / "dev-a", ["1", "2"]), suite_report(self.out / "dev-b", ["3"])]

    def tearDown(self):
        self.tmp.cleanup()

    def test_match(self):
        qc.record(self.out, WANT, self.reports)
        self.assertTrue(qc.reusable(self.out, json.loads(json.dumps(WANT)), self.reports))
        self.assertTrue(qc.fresh(self.out, WANT, self.reports))
        self.assertTrue(self.out.exists())

    def test_missing_metadata(self):
        self.assertFalse(qc.reusable(self.out, WANT, self.reports))
        (self.out / qc.META).write_text("not json")
        self.assertFalse(qc.reusable(self.out, WANT, self.reports))
        (self.out / qc.META).write_text(json.dumps({"run": WANT}))          # no input hashes
        self.assertFalse(qc.reusable(self.out, WANT, self.reports))

    def test_mismatch(self):
        qc.record(self.out, WANT, self.reports)
        for key, value in (("pysacd", dict(WANT["pysacd"], sha256="b" * 64)),            # another wheel
                           ("pysacd", {"kind": "tree", "commit": "c" * 40, "tree_sha256": "t" * 64, "version": "0.6.0"}),
                           ("dsd_measure", {"version": "0.1.2", "commit": "e" * 40}),
                           ("suite_sha256", "x" * 64)):
            with self.subTest(key=key, value=value):
                self.assertFalse(qc.reusable(self.out, dict(WANT, **{key: value}), self.reports))
        self.assertFalse(qc.reusable(self.out, dict(WANT, source_sha256="f" * 64), self.reports))  # extra key

    def test_changed_inputs(self):
        qc.record(self.out, WANT, self.reports)
        (self.reports[0] / "provenance.json").write_text(json.dumps({"adapter_calls": [{"input_sha256": "9"}]}))
        self.assertFalse(qc.reusable(self.out, WANT, self.reports))

    def test_report_missing_or_added(self):
        qc.record(self.out, WANT, self.reports)
        self.assertFalse(qc.reusable(self.out, WANT, self.reports[:1]))
        self.assertFalse(qc.reusable(self.out, WANT, self.reports + [self.out / "dev-c"]))

    def test_stale_folder_is_cleared(self):
        qc.record(self.out, WANT, self.reports)
        self.assertFalse(qc.fresh(self.out, dict(WANT, suite_sha256="x" * 64), self.reports))
        self.assertFalse(self.out.exists())

    def test_null_report_inputs(self):
        null = Path(self.tmp.name) / "null"
        null.mkdir()
        (null / "provenance.json").write_text(json.dumps({"source": {"sha256": "5" * 64}}))
        want = dict(WANT, source_sha256="5" * 64, devices=[["previous", "cmd a"], ["candidate", "cmd b"]])
        qc.record(null, want, [null])
        self.assertTrue(qc.reusable(null, want, [null]))
        self.assertFalse(qc.reusable(null, dict(want, devices=[["previous", "cmd a"], ["candidate", "cmd c"]]), [null]))


class Arguments(unittest.TestCase):
    def test_python_older_than_3_11_is_refused_with_a_reason(self):
        # dsd-measure and tomllib need 3.11; on 3.10 the module still imports, for these tests
        saved, qc.tomllib = qc.tomllib, None
        try:
            with self.assertRaises(SystemExit) as e:
                qc.main(["--previous", "0.5.5", "--candidate", ".", "--work", "unused"])
        finally:
            qc.tomllib = saved
        self.assertIn("Python 3.11 or later", str(e.exception.code))

    def test_previous_is_a_version_unless_it_looks_like_a_path(self):
        self.assertFalse(qc.is_path("0.5.5"))
        self.assertTrue(qc.is_path("dist/pysacd-0.5.5-py3-none-any.whl"))
        self.assertTrue(qc.is_path("pysacd-0.5.5-py3-none-any.whl"))
        self.assertTrue(qc.is_path("./0.5.5"))

    def test_tree_hash_ignores_build_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("x = 1\n")
            h = qc.tree_hash(root)
            (root / "build").mkdir()
            (root / "build" / "junk").write_text("y")
            (root / "src" / "engine.so").write_bytes(b"\0")
            self.assertEqual(qc.tree_hash(root), h)
            (root / "src" / "a.py").write_text("x = 2\n")
            self.assertNotEqual(qc.tree_hash(root), h)


if __name__ == "__main__":
    unittest.main()
