"""tools/quality_compare.py: reports in --work are reused only for the run that made them, and only
like-for-like outputs are compared."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import quality_compare as qc

try:
    import tomllib
except ModuleNotFoundError:     # Python 3.10: tomli, a pysacd dependency there, reads the suite the same way
    import tomli as tomllib  # type: ignore[no-redef]

WANT = {"pysacd": {"kind": "wheel", "file": "pysacd-0.5.5.whl", "sha256": "a" * 64, "version": "0.5.5"},
        "dsd_measure": {"version": "0.1.2", "commit": "d" * 40}, "suite_sha256": "s" * 64}


def suite_report(folder: Path, inputs: list[str], dsd_measure: str = "0.1.2", pysacd: str = "pysacd 0.5.5") -> Path:
    folder.mkdir(parents=True)
    (folder / "provenance.json").write_text(json.dumps({
        "adapter_calls": [{"input_sha256": h} for h in inputs], "versions": {"dsd_measure": dsd_measure},
        "device_versions": {"adapter": "cli", "program": "python3", "version": pysacd}}))
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
        (null / "provenance.json").write_text(json.dumps({"source": {"sha256": "5" * 64}, "versions": {"dsd_measure": "0.1.2"}}))
        want = dict(WANT, source_sha256="5" * 64, devices=[["previous", "cmd a"], ["candidate", "cmd b"]])
        qc.record(null, want, [null])
        self.assertTrue(qc.reusable(null, want, [null]))
        self.assertFalse(qc.reusable(null, dict(want, devices=[["previous", "cmd a"], ["candidate", "cmd c"]]), [null]))


    def test_report_from_another_tool_version(self):
        # the metadata matches, but the report itself says another dsd-measure or pysacd made it
        for n, kw in enumerate(({"dsd_measure": "0.1.1"}, {"pysacd": "pysacd 0.6.0"}, {"pysacd": ""})):
            with self.subTest(**kw):
                out = Path(self.tmp.name) / f"other-{n}"
                reports = [suite_report(out / "dev-a", ["1"], **kw)]
                qc.record(out, WANT, reports)
                self.assertFalse(qc.reusable(out, WANT, reports))


def analysed(kind="int", bits=24, rate=88200, dsd_rate=2822400):
    c = {"class": "A", "dsd_rate": dsd_rate, "out_rate": rate, "kind": kind, "bits": bits if kind == "int" else None}
    word = f"{bits}-bit integer" if kind == "int" else "floating-point"
    return {"conditions": c, "condition_text": f"DSD64 in, {word} out at {rate / 1000:g} kHz"}


WANT_OUTPUT = {"kind": "int", "bits": 24}


class LikeForLike(unittest.TestCase):

    def test_24_bit_against_24_bit_at_one_rate(self):
        found = [(lab, analysed()) for lab in ("previous", "candidate", "candidate-lead1")]
        self.assertEqual(qc.format_problems("stereo", WANT_OUTPUT, found), [])

    def test_another_word_length_is_reported(self):
        got = qc.format_problems("stereo", WANT_OUTPUT, [("previous", analysed()), ("candidate", analysed(bits=16))])
        self.assertEqual(len(got), 2)                 # not the suite's format, and not like for like
        self.assertIn("16-bit integer", got[0])
        got = qc.format_problems("stereo", WANT_OUTPUT, [("previous", analysed(kind="float", bits=32)),
                                                       ("candidate", analysed(kind="float", bits=32))])
        self.assertTrue(got)                          # like for like, but not what the suite measures

    def test_another_rate_is_not_like_for_like(self):
        got = qc.format_problems("stereo", WANT_OUTPUT, [("previous", analysed()), ("candidate", analysed(rate=176400))])
        self.assertEqual(len(got), 1)
        self.assertIn("not like for like", got[0])


class Suite(unittest.TestCase):
    def setUp(self):
        self.text = qc.SUITE.read_text()
        self.suite = tomllib.loads(self.text)
        self.cfg = self.suite["pysacd_quality"]

    def test_pin_and_draft(self):
        self.assertEqual(self.cfg["dsd_measure_version"], "0.1.2")
        self.assertEqual(len(self.cfg["dsd_measure_commit"]), 40)
        self.assertTrue(self.cfg["dsd_measure_commit"].startswith("3eaec2d"))
        self.assertIs(self.cfg["final"], False)       # final once the first published comparison is made
        self.assertEqual(self.cfg["output"], {"kind": "int", "bits": 24})
        self.assertEqual(len(self.cfg["null"]["source_sha256"]), 64)

    def test_figures(self):
        keys = [m["key"] for m in self.cfg["metric"]]
        self.assertEqual(keys, ["sinad", "thd", "noise", "alias"])   # THD apart from SINAD (= -THD+N)
        for m in self.cfg["metric"]:
            self.assertIn(m["better"], ("higher", "lower"))
            self.assertGreater(m["bound"], 0)

    def test_every_device_records_the_pysacd_it_ran(self):
        for dev in self.suite["device"]:
            self.assertEqual(dev["version_command"], "pysacd --version")
            self.assertIn("{input} {output}", dev["command"])

    def test_measured_suite(self):
        base = qc.measured_suite(self.text, 0)
        self.assertNotIn("pysacd_quality", tomllib.loads(base))
        self.assertEqual(tomllib.loads(base)["device"], self.suite["device"])
        self.assertEqual(qc.measured_suite(self.text.replace("bound = 1.0", "bound = 2.0"), 0), base)  # a bound is not measured
        lead = tomllib.loads(qc.measured_suite(self.text, 2))["device"]
        self.assertTrue(all("--lead-frames 2 {input} {output}" in d["command"] for d in lead))


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
