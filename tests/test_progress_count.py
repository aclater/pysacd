"""The progress count ends at the total (#205): frames processed == total frames, for whole areas,
track selections and every output format, on generated discs."""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import sacdgen
import test_dst_full as dst
import test_extract_tree as tree

from pysacd import _native
from pysacd.toc import SACDImage, read_toc

FRAME = 4704


def _frames(n: int, channels: int) -> list[bytes]:
    return [bytes((i * 13 + k * 5 + channels) & 0xFF for i in range(channels * FRAME)) for k in range(n)]


def plain_hybrid(path: Path) -> Path:
    """Plain DSD stereo + plain DSD 6 channels: a hybrid that needs no DST encoder."""
    sacdgen.build_iso(path, [sacdgen.AreaSpec(2, _frames(12, 2), tree.tracks(12), dst=False),
                             sacdgen.AreaSpec(6, _frames(9, 6), tree.tracks(9), dst=False)])
    return path


def selections(n: int) -> dict[str, list[int]]:
    """Track selections (indexes) for an area of n tracks."""
    return {"all": list(range(n)), "first": [0], "middle": [n // 2], "last": [n - 1],
            "first and last": [0, n - 1]}


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
class EngineCount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def check_area(self, iso: Path, area, formats: tuple[str, ...]) -> None:
        for fmt in formats:
            for name, picked in selections(len(area.tracks)).items():
                with self.subTest(iso=iso.name, area=area.channel_count, fmt=fmt, tracks=name):
                    out = Path(tempfile.mkdtemp(dir=self.dir))
                    specs = []
                    for i in picked:
                        t = area.tracks[i]
                        start = t.start.total_frames
                        specs.append(_native.OutputSpec(fmt, out / f"{i}.{fmt}", start,
                                                        start + t.duration.total_frames))
                    reports: list[_native.Progress] = []
                    with SACDImage(iso) as img:
                        run = _native.run_area(img, iso, area, specs, threads=2,
                                               progress=reports.append, interval=0.001)
                    lo = min(s.tc_start for s in specs)
                    hi = max(s.tc_end for s in specs)
                    self.assertEqual(run.frames_total, hi - lo)
                    self.assertEqual(run.frames_done, run.frames_total)
                    for p in reports:
                        self.assertLessEqual(p.frames_done, p.frames_total)

    def test_stereo_only_disc(self):
        iso = self.dir / "stereo.iso"
        sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, _frames(12, 2), tree.tracks(12), dst=False)])
        area = read_toc(iso).stereo
        assert area is not None
        self.check_area(iso, area, ("dsf", "dff", "wav", "flac") if _native.has_flac() else ("dsf", "dff", "wav"))

    def test_hybrid_multichannel(self):
        iso = plain_hybrid(self.dir / "hybrid.iso")
        disc = read_toc(iso)
        assert disc.stereo is not None and disc.multichannel is not None
        self.check_area(iso, disc.stereo, ("dsf", "wav"))
        self.check_area(iso, disc.multichannel, ("dsf", "dff", "wav"))

    @unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
    def test_generated_tree_discs(self):
        # the discs the sacd_extract comparison uses: plain, DST stereo + 6 ch, plain + DST 5 ch
        for iso in tree.discs(self.dir).values():
            disc = read_toc(iso)
            for area in (disc.stereo, disc.multichannel):
                if area is not None:
                    fmts = ("dsf", "dff-dst") if area.is_dst else ("dsf",)
                    self.check_area(iso, area, fmts)


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
class ExtractCount(unittest.TestCase):
    """pysacd-extract, as the issue saw it: the printed count never passes the total, and the
    run ends with processed == total."""

    def run_extract(self, iso: Path, out: Path, *args: str) -> tuple[list, str]:
        runs: list[_native.RunInfo] = []
        real = _native.run_area

        def recorded(*a, **kw):
            kw["interval"] = 0.001
            runs.append(real(*a, **kw))
            return runs[-1]

        from pysacd import extract
        printed = io.StringIO()
        with mock.patch.object(_native, "run_area", recorded), contextlib.redirect_stdout(printed):
            rc = extract.main(["--no-cfg", "--threads", "2", *args, "-o", str(out), "-i", str(iso)])
        self.assertEqual(rc, 0)
        return runs, printed.getvalue()

    def test_counts(self):
        with tempfile.TemporaryDirectory(prefix="pysacd-progress") as tmp:
            root = Path(tmp)
            iso = plain_hybrid(root / "hybrid.iso")
            cases = {"stereo": ["-2", "-s"], "multichannel": ["-m", "-s"], "both": ["-2", "-m", "-p"],
                     "track 2": ["-2", "-s", "-t", "2"], "tracks 1,3": ["-2", "-s", "-t", "1,3"],
                     "edit master": ["-m", "-e"], "wav track 3": ["-2", "--wav", "-t", "3"]}
            for i, (name, args) in enumerate(cases.items()):
                with self.subTest(name):
                    out = root / str(i)
                    out.mkdir()
                    runs, printed = self.run_extract(iso, out, *args)
                    self.assertTrue(runs)
                    for run in runs:
                        self.assertGreater(run.frames_total, 0)
                        self.assertEqual(run.frames_done, run.frames_total)
                    for done, total in re.findall(r"frames processed: (\d+) / total frames: (\d+)", printed):
                        self.assertLessEqual(int(done), int(total))


if __name__ == "__main__":
    unittest.main()
