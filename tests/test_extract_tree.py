"""pysacd-extract against the real sacd_extract, whole output trees: `diff -r` as a test.

On generated discs (tests/sacdgen.py), every sacd_extract-compatible mode of
tools/extract_compare.py: the same files, names and bytes, apart from the documented exceptions
(docs/sacd_extract.md). This needs SACD_EXTRACT=/path/to/sacd_extract and runs in CI's "compare
with sacd_extract" job. Real discs exercise more of the format: the same tool runs on them by hand
before a release (docs/development.md, "Packaging and releases").

The HarnessDetects tests need no sacd_extract: they check that the comparison can fail.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))     # also what test_sbcompat imports from

import extract_compare as cmp
import sacdgen
import test_dst_full as dst

SACD_EXTRACT = os.environ.get("SACD_EXTRACT") or shutil.which("sacd_extract")
SRC = str(ROOT / "src")


def tracks(n: int) -> list[int]:
    return [n // 3, n // 3, n - 2 * (n // 3)]


def discs(root: Path) -> dict[str, Path]:
    """Plain DSD stereo; DST stereo + 6 channels; plain DSD stereo + DST 5 channels."""
    titles = ["First", "Second", "Third"]
    plain = [bytes([i]) * (2 * 4704) for i in range(12)]
    two, five, six = ([f for _, _, f in dst.encoded(ch)] for ch in (2, 5, 6))
    info = sacdgen.DiscInfo(title="Tree Album", artist="Tree Artist")
    specs = {
        "plain": [sacdgen.AreaSpec(2, plain, tracks(len(plain)), dst=False, titles=titles)],
        "dst": [sacdgen.AreaSpec(2, two, tracks(len(two)), titles=titles),
                sacdgen.AreaSpec(6, six, tracks(len(six)), titles=titles)],
        "mixed": [sacdgen.AreaSpec(2, plain, tracks(len(plain)), dst=False, titles=titles),
                  sacdgen.AreaSpec(5, five, tracks(len(five)), titles=titles)],
    }
    out = {}
    for name, areas in specs.items():
        out[name] = root / f"{name}.iso"
        sacdgen.build_iso(out[name], areas, info=info)
    return out


@unittest.skipUnless(SACD_EXTRACT, "set SACD_EXTRACT to compare with the real sacd_extract")
@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class Trees(unittest.TestCase):
    @staticmethod
    def compare(iso: Path, mode: str) -> dict:
        """One mode, again if sacd_extract left out a file pysacd wrote: under load, 3 runs of
        450 of sacd_extract, and none of pysacd, came out different with exit status 0 (6-channel
        DST, all files it wrote matching). pysacd is the same tree every time, so a real difference
        fails all three."""
        for _ in range(3):
            r = cmp.compare(iso, mode, {"old": SRC, "new": SRC}, SACD_EXTRACT, 2, None)
            if not cmp.sacd_extract_problems(r):
                break
        return r

    def test_every_mode_on_every_disc(self):
        modes = [m for m, (kind, _) in cmp.MODES.items() if kind == "extract"]
        self.assertGreaterEqual(len(modes), 20)
        compared = {}
        with tempfile.TemporaryDirectory(prefix="pysacd-tree") as tmp:
            for disc, iso in discs(Path(tmp)).items():
                for mode in modes:
                    has_mch = disc != "plain"
                    if "-m" in cmp.MODES[mode][1] and not has_mch:
                        continue
                    with self.subTest(disc=disc, mode=mode):
                        r = self.compare(iso, mode)
                        self.assertEqual(cmp.sacd_extract_problems(r), [])
                        for fmt, e in r["vs_sacd_extract"]["new"].items():
                            compared[fmt] = compared.get(fmt, 0) + e["identical"] + e["documented"]
        # the formats the docs promise were really compared
        self.assertLessEqual({"dsf", "dff", "cue", "xml", "iso"}, compared.keys())
        self.assertTrue(all(compared.values()), compared)


@unittest.skipUnless(SACD_EXTRACT, "set SACD_EXTRACT to compare with the real sacd_extract")
@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
class BlankIsrcs(unittest.TestCase):
    """Blank and partly filled ISRC fields (sacdgen.ISRC_FORMS) in every output that carries an
    ISRC: DSF and DSDIFF in each ID3 mode, -k concatenations, the edit master with cue and XML,
    and -P (#175)."""

    RUNS = [(fmt, mode) for mode in range(6) for fmt in (["-2", "-s"], ["-2", "-p"])] + [
        (["-2", "-s", "-k", "-t", "2,3"], None), (["-2", "-p", "-k", "-t", "2,3"], None),
        (["-2", "-e", "-C"], None), (["-P"], None)]

    @staticmethod
    def run_both(iso: Path, root: Path, args: list[str], mode: int | None) -> tuple:
        se, py = root / "sacd_extract", root / "pysacd"
        for d in (se, py):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir()
        if mode is not None:
            (se / "sacd_extract.cfg").write_text(f"id3tag={mode}\n")
        a = subprocess.run([SACD_EXTRACT, *args, "-i", str(iso)], cwd=se, capture_output=True, text=True,
                           errors="replace", check=False)
        (se / "sacd_extract.cfg").unlink(missing_ok=True)
        b = subprocess.run([sys.executable, "-m", "pysacd.extract", "--no-cfg", "--threads", "2",
                            *(["--id3tag", str(mode)] if mode is not None else []), *args, "-i", str(iso)],
                           cwd=py, capture_output=True, text=True, errors="replace", check=False,
                           env={**os.environ, "PYTHONPATH": SRC})
        return se, py, a, b

    def test_isrc_forms(self):
        with tempfile.TemporaryDirectory(prefix="pysacd-isrc") as tmp:
            root = Path(tmp)
            iso = sacdgen.isrc_forms_iso(root / "isrcs.iso")
            compared = 0
            for args, mode in self.RUNS:
                with self.subTest(args=args, id3tag=mode):
                    for _ in range(3):      # sacd_extract now and then leaves out a file (see Trees)
                        se, py, a, b = self.run_both(iso, root, args, mode)
                        theirs, ours = cmp.tree(se), cmp.tree(py)
                        if args == ["-P"] or theirs.keys() >= ours.keys():
                            break
                    self.assertEqual((a.returncode, b.returncode), (0, 0), b.stderr)
                    if args == ["-P"]:
                        body = lambda t: t[t.find("Disc Information:"):]
                        self.assertIn("ISRC Track [1]:", a.stdout)
                        self.assertEqual(body(b.stdout), body(a.stdout))
                        continue
                    self.assertTrue(theirs)
                    vs = cmp.against_sacd_extract(py, se, ours, theirs, False)
                    self.assertEqual({fmt: e["different"] for fmt, e in vs.items() if e["different"]}, {})
                    compared += sum(e["identical"] + e["documented"] for e in vs.values())
            self.assertGreaterEqual(compared, 6 * 2 * 9)


class HarnessDetects(unittest.TestCase):
    """A comparison that can't fail compares nothing (#131)."""

    def result(self, mode="dsf", **over) -> dict:
        r = {"mode": mode, "rc": (0, 0), "rc_sacd_extract": 0, "sacd_extract_files": 1,
             "print_matches_sacd_extract": True,
             "vs_sacd_extract": {"new": {"dsf": {"theirs": 1, "identical": 1, "documented": 0, "different": []}}}}
        return {**r, **over}

    def test_a_clean_result_passes(self):
        self.assertEqual(cmp.sacd_extract_problems(self.result()), [])

    def test_sacd_extract_that_did_not_run_fails(self):
        r = self.result()
        del r["rc_sacd_extract"]
        self.assertTrue(cmp.sacd_extract_problems(r))

    def test_sacd_extract_that_failed_or_wrote_nothing_fails(self):
        self.assertTrue(cmp.sacd_extract_problems(self.result(rc_sacd_extract=255)))
        self.assertTrue(cmp.sacd_extract_problems(self.result(sacd_extract_files=0)))

    def test_print_mismatch_fails(self):
        self.assertTrue(cmp.sacd_extract_problems(self.result("print", print_matches_sacd_extract=None)))

    def test_different_file_fails(self):
        r = self.result(vs_sacd_extract={"new": {"dsf": {"theirs": 1, "identical": 0, "documented": 0,
                                                         "different": [{"file": "x", "first": 5, "bytes": 1}]}}})
        self.assertTrue(cmp.sacd_extract_problems(r))

    def test_documented_exceptions_are_narrow(self):
        def dff(machine: bytes, audio: bytes) -> bytes:
            def comment(text: bytes) -> bytes:
                pad = b"\0" if len(text) & 1 else b""
                return b"\x07\xea\x01\x02\x03\x04" + bytes(4) + len(text).to_bytes(4, "big") + text + pad
            comt = (2).to_bytes(2, "big") + comment(b"track") + comment(machine)
            chunks = b"DSD " + len(audio).to_bytes(8, "big") + audio + b"COMT" + len(comt).to_bytes(8, "big") + comt
            return b"FRM8" + (len(chunks) + 4).to_bytes(8, "big") + b"DSD " + chunks

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / "a.dff", root / "b.dff"
            a.write_bytes(dff(b"sacd-ripper", b"\1\2"))
            b.write_bytes(dff(b"pysacd 9.9", b"\1\2"))
            self.assertTrue(cmp.documented_only(a, b))              # the machine comment may differ
            b.write_bytes(dff(b"pysacd 9.9", b"\1\3"))
            self.assertFalse(cmp.documented_only(a, b))             # the audio may not
            def diin(text: bytes, pad: bytes) -> bytes:
                body = len(text).to_bytes(4, "big") + text + pad
                sub = b"DIAR" + len(body).to_bytes(8, "big") + body
                chunks = b"DIIN" + len(sub).to_bytes(8, "big") + sub
                return b"FRM8" + (len(chunks) + 4).to_bytes(8, "big") + b"DSD " + chunks
            a.write_bytes(diin(b"abc", b"\0"))
            b.write_bytes(diin(b"abc", b"7"))
            self.assertTrue(cmp.documented_only(a, b))              # sacd_extract's pad byte may differ
            b.write_bytes(diin(b"abd", b"\0"))
            self.assertFalse(cmp.documented_only(a, b))             # the text may not
            a, b = root / "a.cue", root / "b.cue"
            a.write_bytes(b"REM File created by SACD Extract, version: NA\nTITLE \"x\"\n")
            b.write_bytes(b"REM File created by SACD Extract, version: pysacd 9.9\nTITLE \"x\"\n")
            self.assertTrue(cmp.documented_only(a, b))              # the version line may differ
            b.write_bytes(b"REM File created by SACD Extract, version: pysacd 9.9\nTITLE \"y\"\n")
            self.assertFalse(cmp.documented_only(a, b))             # the rest may not
            a, b = root / "a.dsf", root / "b.dsf"
            a.write_bytes(b"x")
            b.write_bytes(b"y")
            self.assertFalse(cmp.documented_only(a, b))             # a DSF has no exceptions


if __name__ == "__main__":
    unittest.main()
