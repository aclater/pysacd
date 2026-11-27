"""Heap-flat gate (lab memory-testing standard, test 1 and 2) with its positive control.

One child process (tests/heapflat_child.py) runs the same conversion N times under the counting
malloc shim (tests/native/countmalloc.c, LD_PRELOAD, PYTHONMALLOC=malloc) and reports the exact live
bytes after each. The positive control `leak` leaks 4 KiB per conversion on purpose and this test
FAILS if the referee does not see it. The other cases (success, truncated input, unwritable output,
cancel, corrupt input) must stay flat within MARGIN and must not grow at every step.
"""

import itertools
import os
import random
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import sacdgen

ROOT = Path(__file__).resolve().parents[1]
CHILD = ROOT / "tests" / "heapflat_child.py"
SHIM_C = ROOT / "tests" / "native" / "countmalloc.c"
FRAME = 4704 * 2            # one stereo DSD64 frame

N = 100                     # conversions measured (after the warm-up), the calibrated count
WARMUP = 3                  # conversions before the first reading: the level settles over the first 3 (measured)
# Margins from the calibration of 2026-11-27 (an idle host, 3 runs of N = 100 per case, head 36570ce;
# the raw LEVELS lines are kept with the calibration record). Per case the first-to-last change
# over the 3 runs was: ok -2688/-3784/-3656, truncated -1864/-4072/-2696, badout -1056/-968/-848,
# cancel -3368/-4056/-4152, corrupt -3448/-2944/-3512, dstbad -3856/-3248/-4568. The widest spread between runs
# of one case is 2208 B (truncated); margin = 3 x spread = 6624 B on the level change. The fitted slope per
# conversion ranged -8 to -42 B (widest spread 21 B), so the slope margin is 3 x 21 = 63 B per conversion.
# The honest cases FALL (the warm-up does not fully settle); only growth counts as a leak. Smallest leak this
# condition can detect: about 6624 / 99 = 67 B per conversion by the level change, 63 B by the slope; the
# planted 4096 B leak measured 4115-4133 B per conversion. Fixed before the gate run; changing them goes
# back to the reviewer of the gate (lab memory-testing standard, "Conduct").
MARGIN = 6624               # bytes, |level change| allowed from the first to the last of N
SLOPE_MARGIN = 63           # bytes per conversion, fitted growth allowed
# the outcome each case has, measured: random bytes in a plain-DSD audio area are still valid DSD, and the
# truncated image is read to its end, so both convert (exit 0); the unwritable output raises ValueError;
# garbage DST frames are reported as DST errors, filled with 0x55, and convert (exit 0, warnings)
EXPECTED_OUTCOME: dict[str, str | None] = {"ok": "ok", "cancel": "KeyboardInterrupt", "truncated": "ok",
                                           "corrupt": "ok", "badout": "ValueError", "dstbad": "ok"}
NEAR_LEAK = 256              # the second control: a leak near the detection threshold, per conversion
LEAK_PER_CONVERSION = 4096  # what the positive control leaks
LEAK_MUST_SHOW = 3 * 1024   # the control must show at least this much growth per conversion


def _skip_reason() -> str | None:
    if not sys.platform.startswith("linux"):
        return "the counting shim needs glibc on Linux (LD_PRELOAD, __libc_malloc)"
    if sysconfig.get_config_var("HOST_GNU_TYPE") and "gnu" not in sysconfig.get_config_var("HOST_GNU_TYPE"):
        return "the counting shim needs glibc (this interpreter is not built for a gnu libc)"
    if shutil.which("cc") is None:
        return "no C compiler (cc) to build the counting shim"
    return None


def _child_python() -> str | None:
    """HEAPFLAT_PYTHON if set, else the running interpreter, when it has mutagen (the success case must
    really convert)."""
    for exe in (os.environ.get("HEAPFLAT_PYTHON"), sys.executable):
        if exe and Path(exe).exists() and subprocess.run([exe, "-I", "-c", "import mutagen"], capture_output=True,
                                                         check=False).returncode == 0:
            return exe
    return None


def _engine_built() -> bool:
    return any((ROOT / "src" / "pysacd").glob("_sacdconv.*"))


@unittest.skipIf(_skip_reason() is not None, _skip_reason() or "")
@unittest.skipUnless(_engine_built(), "native engine not built (make)")
class HeapFlat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.python = _child_python()
        if cls.python is None:
            raise unittest.SkipTest("no interpreter with mutagen (set HEAPFLAT_PYTHON to one that has it)")
        cls.tmp = Path(tempfile.mkdtemp(prefix="heapflat-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.shim = cls.tmp / "countmalloc.so"
        r = subprocess.run(["cc", "-O1", "-shared", "-fPIC", "-o", str(cls.shim), str(SHIM_C), "-lpthread", "-ldl"],
                           capture_output=True, text=True, check=False)
        if r.returncode:
            raise unittest.SkipTest(f"the counting shim does not build here: {r.stderr[-300:]}")
        rng = random.Random(61)
        frames = [bytes(rng.getrandbits(8) for _ in range(FRAME)) for _ in range(40)]
        spec = sacdgen.AreaSpec(2, frames, [20, 20], dst=False, titles=["One", "Two"])
        cls.iso = cls.tmp / "disc.iso"
        img, layout = sacdgen.build_image([spec], sacdgen.DiscInfo(title="Heap flat", artist="pysacd"))
        cls.iso.write_bytes(bytes(img))
        cls.audio = (layout[0].audio_start, layout[0].audio_end)
        # a DST-coded area whose frames are random bytes: DST validity fails, a real error path of the engine
        bad = [bytes(rng.getrandbits(8) for _ in range(900)) for _ in range(20)]
        img, _ = sacdgen.build_image([sacdgen.AreaSpec(2, bad, [10, 10], dst=True, titles=["One", "Two"])],
                                     sacdgen.DiscInfo(title="DST garbage", artist="pysacd"))
        cls.dst_iso = cls.tmp / "dst-garbage.iso"
        cls.dst_iso.write_bytes(bytes(img))
        # input classes for the peak observation: six channels, and the longest text the TOC can carry
        six = [sacdgen._dsd_frame(6, i) for i in range(40)]
        img, _ = sacdgen.build_image([sacdgen.AreaSpec(6, six, [20, 20], dst=False, titles=["One", "Two"])],
                                     sacdgen.DiscInfo(title="Six channels", artist="pysacd"))
        cls.mch_iso = cls.tmp / "six.iso"
        cls.mch_iso.write_bytes(bytes(img))
        big = "T" * 600           # the most the generator fits in the text sector
        img, _ = sacdgen.build_image([sacdgen.AreaSpec(2, frames, [20, 20], dst=False, titles=[big, big])],
                                     sacdgen.DiscInfo(title=big, artist=big))
        cls.tag_iso = cls.tmp / "long-text.iso"
        cls.tag_iso.write_bytes(bytes(img))

    outcomes: ClassVar[dict[str, dict[str, int]]] = {}
    peaks: ClassVar[dict[str, list[int]]] = {}

    def levels(self, case: str, n: int = N) -> list[int]:
        out = self.tmp / f"out-{case}"
        out.mkdir(exist_ok=True)
        env = dict(os.environ, LD_PRELOAD=str(self.shim), PYTHONMALLOC="malloc", PYTHONPATH=str(ROOT / "src"),
                   PYTHONDONTWRITEBYTECODE="1", HEAPFLAT_WARMUP=str(WARMUP))
        env.pop("HEAPFLAT_TRACEMALLOC", None)
        env.pop("HEAPFLAT_SITES", None)
        iso = {"dstbad": self.dst_iso, "ok6": self.mch_iso, "longtext": self.tag_iso}.get(case, self.iso)
        args = [self.python, str(CHILD), case, str(iso), str(out), str(n)]
        if case == "corrupt":
            args += [str(self.audio[0]), str(self.audio[1])]
        r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(r.returncode, 0, f"{case}: child failed:\n{r.stderr[-3000:]}")
        line = next(ln for ln in r.stdout.splitlines() if ln.startswith("LEVELS "))
        levels = [int(x) for x in line.split()[1:]]
        peaks = next(ln for ln in r.stdout.splitlines() if ln.startswith("PEAKS "))
        self.peaks[case] = [int(x) for x in peaks.split()[1:]]
        got = next(ln for ln in r.stdout.splitlines() if ln.startswith("OUTCOMES "))
        self.outcomes[case] = {k: int(v) for k, v in (kv.split("=") for kv in got.split()[1:])}
        self.assertEqual(len(levels), n)
        return levels

    def assert_flat(self, case: str) -> None:
        lv = self.levels(case)
        change = lv[-1] - lv[0]
        self.assertLessEqual(abs(change), MARGIN, f"{case}: level moved {change} B over {N} conversions: {lv}")
        steps_up = [b > a for a, b in itertools.pairwise(lv)]
        self.assertFalse(all(steps_up), f"{case}: the level grew at every conversion: {lv}")
        # a leak with some flat steps shows in the fitted trend, not in the step test
        mean_i, mean_l = (N - 1) / 2, sum(lv) / N
        slope = sum((i - mean_i) * (b - mean_l) for i, b in enumerate(lv)) / sum((i - mean_i) ** 2 for i in range(N))
        self.assertLessEqual(slope, SLOPE_MARGIN, f"{case}: fitted growth {slope:.1f} B per conversion: {lv}")
        # the case did what its name says, the same way every time (one outcome)
        got = self.outcomes[case]
        if case == "cancel":
            # a cancel can arrive after a short conversion has finished: 3,249 of 1.1 million in the lab soak
            total = sum(got.values())
            self.assertLessEqual(set(got), {"KeyboardInterrupt", "ok"}, f"{case}: unexpected outcome {got}")
            self.assertGreaterEqual(got.get("KeyboardInterrupt", 0), 0.9 * total, f"{case}: {got}")
        else:
            self.assertEqual(len(got), 1, f"{case}: the outcome changed between conversions: {got}")
            want = EXPECTED_OUTCOME.get(case)
            if want is not None:
                self.assertEqual(next(iter(got)), want, f"{case}: unexpected outcome {got}")
        print(f"\nheap-flat {case}: outcome {got}")
        print(f"heap-flat {case}: first {lv[0]} last {lv[-1]} change {change} B "
              f"(min {min(lv)} max {max(lv)}, margin {MARGIN})", file=sys.stderr)

    def test_positive_control_leak_is_seen(self):
        lv = self.levels("leak")
        per = (lv[-1] - lv[0]) / (N - 1)
        print(f"\nheap-flat leak control: {per:.0f} B per conversion (leaks {LEAK_PER_CONVERSION})", file=sys.stderr)
        self.assertGreaterEqual(per, LEAK_MUST_SHOW,
                                f"POSITIVE CONTROL NOT SEEN: the referee shows {per:.0f} B per conversion for a "
                                f"{LEAK_PER_CONVERSION} B leak, so this gate cannot fail; levels {lv}")

    def test_positive_control_fails_the_gate(self):
        """The same gate that passes the honest cases must FAIL the leaking build."""
        with self.assertRaises(AssertionError):
            self.assert_flat("leak")

    def test_second_control_near_the_threshold_fails_the_gate(self):
        """A 256 B per conversion leak, near what the gate can detect (about 67 B), must also fail it."""
        with self.assertRaises(AssertionError):
            self.assert_flat("leak256")

    def test_peak_heap_per_input_class_is_observed(self):
        """OBSERVATION, not yet a gate: the peak heap above the level before one conversion, per input class
        (stereo, six channels, the longest text, garbage DST). Bounds follow when the lab has calibrated them."""
        for case in ("ok", "ok6", "longtext", "dstbad"):
            self.levels(case, 10)
            pk = self.peaks[case]
            print(f"\nheap-flat peak {case}: min {min(pk)} max {max(pk)} B above the starting level", file=sys.stderr)
            self.assertGreater(min(pk), 0)

    def test_third_control_just_above_the_floor_fails_the_gate(self):
        """100 B per conversion, just above the floor of about 67 B, must fail the gate too."""
        with self.assertRaises(AssertionError):
            self.assert_flat("leak100")

    def test_a_short_soak_is_never_a_pass(self):
        """tests/heapflat_release.py soak under 1,000,000 conversions is an observation: exit status 3."""
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([self.python, str(ROOT / "tests" / "heapflat_release.py"), "soak", "--conversions", "30",
                            "--every", "10", "--scenario", "ok"], env=env, capture_output=True, text=True,
                           timeout=120, check=False)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("observation", r.stdout)

    def test_ok(self):
        self.assert_flat("ok")

    def test_truncated_input(self):
        self.assert_flat("truncated")

    def test_unwritable_output(self):
        self.assert_flat("badout")

    def test_cancel(self):
        self.assert_flat("cancel")

    def test_dst_garbage_frames(self):
        self.assert_flat("dstbad")

    def test_corrupt_audio(self):
        self.assert_flat("corrupt")


if __name__ == "__main__":
    unittest.main()
