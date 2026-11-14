"""Hostile disc images through every extraction path, in-process (issue #17).

tests/sacdgen.py builds the corpus: malformed and overlapping master and area TOCs, absurd
track counts, timecodes and LSN ranges, broken text, garbage, truncated and oversized audio
frames, and confused sector layouts. Each image goes through pysacd-extract (-s, -p, -p -c,
-e, -I, -P) and pysacd convert (dsf, flac, wav). Each run must end in a clean error or sane
output:

- no exception other than the CLI's own error handling, and no hang (a timeout);
- nothing created outside the output directory, no partial file left behind;
- no output bigger than the area could hold: at most 7 frames (the most a sector's header can
  start) per sector of the image; and no run's outputs bigger than twice that in all.

RunBound checks the bound per run (GHSA-q45f-7qmf-37x3): overlapping tracks, and sparse images
whose frames would mostly become silence.

CI runs this under ASan and UBSan, so memory errors in the engine surface there. Path
confinement and file modes are checked by tests/test_confinement.py (#14).
PYSACD_HOSTILE=name1,name2 runs only those images.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import signal
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import sacdgen

from pysacd import extract
from pysacd.toc import SACDError, read_toc

TIMEOUT = float(os.environ.get("PYSACD_HOSTILE_TIMEOUT", "60"))   # seconds per run; generous for ASan
MAX_FRAMES_PER_SECTOR = 7
MAX_FRAME_BYTES = 6 * 4704              # the biggest frame any output writes, per frame of audio
SLACK = 1 << 20                         # headers, tags, cue and XML
RUN_COVERS = 2                          # how often one run's outputs of a format may cover the area

EXTRACT_MODES = {
    "dsf": ["-2", "-m", "-s"],
    "dsdiff": ["-2", "-m", "-p"],
    "dsdiff-c": ["-2", "-m", "-p", "-c"],
    "edit-master": ["-2", "-m", "-e"],
    "iso": ["-I"],
    "print": ["-P"],
}
CONVERT_FORMATS = ("dsf", "flac", "wav")


def _native_ok() -> bool:
    try:
        from pysacd import _native
        _native._load()
        return True
    except (ImportError, OSError, RuntimeError):
        return False


def _has_flac() -> bool:
    from pysacd import _native
    return _native.has_flac()


class _Timeout(Exception):
    pass


@contextlib.contextmanager
def _deadline(seconds: float):
    """Raise _Timeout in the main thread after `seconds`. The engine runs in its own thread
    and run_area() cancels it when the exception arrives. Windows has no SIGALRM: there only
    the CI job's own timeout catches a hang."""
    if not hasattr(signal, "setitimer"):
        yield
        return

    def fire(_sig, _frame):
        raise _Timeout(f"no result after {seconds:.0f} s")
    old = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _corpus() -> list[sacdgen.Hostile]:
    only = {n for n in os.environ.get("PYSACD_HOSTILE", "").split(",") if n}
    return [h for h in sacdgen.hostile_corpus() if not only or h.name in only]


@unittest.skipUnless(_native_ok(), "native engine not built")
class HostileImages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = _corpus()
        # the engine and TOC warnings are expected; keep the test output readable
        logging.getLogger("pysacd").setLevel(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls):
        logging.getLogger("pysacd").setLevel(logging.NOTSET)

    def _run(self, h: sacdgen.Hostile, label: str, fn) -> None:
        with tempfile.TemporaryDirectory(prefix="pysacd-hostile") as tmp:
            root = Path(tmp)
            iso = root / "in.iso"
            iso.write_bytes(h.image)
            out = root / "out"
            out.mkdir()
            old = os.getcwd()
            os.chdir(out)
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                        _deadline(TIMEOUT):
                    fn(str(iso), str(out))
            except _Timeout as e:
                self.fail(f"{h.name} {label}: {e}")
            except SystemExit as e:     # convert's own clean errors ("disc has no stereo area")
                self.assertIsInstance(e.code, (str, int), f"{h.name} {label}")
            finally:
                os.chdir(old)
            self._check(h, label, root, out)

    def _check(self, h: sacdgen.Hostile, label: str, root: Path, out: Path) -> None:
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["in.iso", "out"],
                         f"{h.name} {label}: wrote outside the output directory")
        sectors = len(h.image) // 2048 + 1
        limit = sectors * MAX_FRAMES_PER_SECTOR * MAX_FRAME_BYTES + SLACK
        total = 0
        for p in out.rglob("*"):
            rel = p.relative_to(out)
            self.assertFalse(p.name.endswith(".partial"), f"{h.name} {label}: left {rel}")
            if p.is_file():
                size = p.stat().st_size
                bound = sectors * 2048 if p.suffix == ".iso" else limit
                self.assertLessEqual(size, bound, f"{h.name} {label}: {rel} is {size} bytes")
                total += size if p.suffix != ".iso" else 0
        # each mode writes one format, which may cover the area twice (GHSA-q45f-7qmf-37x3)
        self.assertLessEqual(total, RUN_COVERS * limit, f"{h.name} {label}: {total} bytes in all")

    def test_extract(self):
        for h in self.corpus:
            for label, args in EXTRACT_MODES.items():
                with self.subTest(image=h.name, mode=label):
                    def run(iso, out, args=args):
                        rc = extract.main([*args, "--no-cfg", "--threads", "2", "-o", out, "-i", iso])
                        self.assertIn(rc, (0, 1, 255))
                    self._run(h, f"extract {label}", run)

    def test_convert(self):
        from pysacd.convert import Options, convert
        formats = [f for f in CONVERT_FORMATS if f != "flac" or _has_flac()]
        for h in self.corpus:
            for fmt in formats:
                with self.subTest(image=h.name, format=fmt):
                    def run(iso, out, fmt=fmt):
                        opts = Options(output_dir=out, format=fmt, areas="both", musicbrainz=False, cover=False,
                                       cover_file="", jobs=2)
                        try:
                            convert(iso, opts)
                        except (SACDError, OSError):
                            pass        # an unreadable image: convert's caller reports it
                    self._run(h, f"convert {fmt}", run)


class Expectations(unittest.TestCase):
    """What specific images must produce, beyond surviving. Each image here is the regression
    case for a bound added in #17."""

    @classmethod
    def setUpClass(cls):
        cls.images = {h.name: h.image for h in sacdgen.hostile_corpus()}

    def _disc(self, name: str):
        # a directory, not NamedTemporaryFile: Windows can't reopen a file that's still open
        with tempfile.TemporaryDirectory(prefix="pysacd-hostile") as tmp:
            iso = Path(tmp) / "in.iso"
            iso.write_bytes(self.images[name])
            return read_toc(str(iso))

    def _extract(self, name: str, *args: str) -> tuple[int, dict[str, bytes]]:
        """Run pysacd-extract on an image; returns the exit status and {relative path: bytes}."""
        with tempfile.TemporaryDirectory(prefix="pysacd-hostile") as tmp:
            iso = Path(tmp) / "in.iso"
            iso.write_bytes(self.images[name])
            out = Path(tmp) / "out"
            out.mkdir()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                    _deadline(TIMEOUT):
                rc = extract.main([*args, "--no-cfg", "--threads", "2", "-o", str(out), "-i", str(iso)])
            return rc, {str(p.relative_to(out)): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}

    def test_raw_layouts_read_like_the_plain_image(self):
        plain = self._disc("valid")
        for name in ("layout-2064", "layout-2054"):
            d = self._disc(name)
            self.assertEqual([(a.kind, a.track_start, a.track_end, len(a.tracks)) for a in d.areas],
                             [(a.kind, a.track_start, a.track_end, len(a.tracks)) for a in plain.areas], name)

    def test_oversized_area_toc_is_skipped(self):
        with self.assertRaises(SACDError):         # neither area is left
            self._disc("master-area-size-max")

    def test_reversed_audio_range_drops_the_area(self):
        d = self._disc("area-audio-reversed")
        self.assertIsNone(d.stereo)
        self.assertIsNotNone(d.multichannel)
        # pysacd's parser words it, scarletbook gives the specification's code (#85)
        self.assertTrue(any("ends before it starts" in w or "area-audio-range-reversed" in w for w in d.warnings))

    def test_tracks_held_to_the_playtime(self):
        for name in ("track-duration-max", "track-start-max", "track-start-past-playtime"):
            area = self._disc(name).stereo
            assert area is not None
            for t in area.tracks:
                self.assertLessEqual(t.start.total_frames + t.duration.total_frames,
                                     area.total_playtime.total_frames, name)
            self.assertTrue(any("past the area's playtime" in w or "past-playtime" in w for w in area.warnings),
                            (name, area.warnings))

    @unittest.skipUnless(_native_ok(), "native engine not built")
    def test_tracks_cut_to_the_playtime_extract_normally(self):
        _, want = self._extract("valid", "-2", "-s")
        rc, got = self._extract("track-duration-max", "-2", "-s")
        self.assertEqual(rc, 0)
        self.assertEqual(got.keys(), want.keys())

    @unittest.skipUnless(_native_ok(), "native engine not built")
    def test_edit_master_of_an_endless_playtime_is_refused(self):
        rc, files = self._extract("area-playtime-max", "-2", "-e")
        self.assertEqual(rc, 255)
        self.assertFalse([f for f in files if f.endswith(".dff")])

    @unittest.skipUnless(_native_ok(), "native engine not built")
    def test_output_bound_uses_the_image_not_the_toc(self):
        # The TOC's own audio range is huge, so its timecodes pass; the image holds a few
        # sectors, so the engine must refuse rather than write hours of silence.
        area = self._disc("track-duration-max-area-past-end").stereo
        assert area is not None
        self.assertGreater(area.tracks[0].duration.total_frames, 1_000_000)
        rc, files = self._extract("track-duration-max-area-past-end", "-2", "-s")
        self.assertEqual(rc, 255)
        self.assertFalse([f for f in files if f.endswith(".dsf")])

    def test_track_text_scans_are_bounded(self):
        for name in ("text-items-255-zero", "text-items-255-unterminated", "text-everywhere",
                     "master-text-unterminated"):
            self._disc(name)        # returns promptly; the corpus run checks the rest

    @unittest.skipUnless(_native_ok(), "native engine not built")
    def test_valid_and_raw_layouts_extract_the_same_audio(self):
        rc, want = self._extract("valid", "-2", "-m", "-s")
        self.assertEqual(rc, 0)
        self.assertEqual(sum(k.endswith(".dsf") for k in want), 5)
        for name in ("layout-2064", "layout-2054"):
            self.assertEqual(self._extract(name, "-2", "-m", "-s"), (0, want), name)

    @unittest.skipUnless(_native_ok(), "native engine not built")
    def test_convert_skips_a_zero_length_track(self):
        from pysacd.convert import Options, convert
        with tempfile.TemporaryDirectory(prefix="pysacd-hostile") as tmp:
            iso = Path(tmp) / "in.iso"
            iso.write_bytes(self.images["track-duration-one-zero"])
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = convert(str(iso), Options(output_dir=str(Path(tmp) / "out"), format="dsf", musicbrainz=False,
                                               cover=False, cover_file="", jobs=2))
            self.assertEqual(rc, 0)
            self.assertEqual(len(list((Path(tmp) / "out").rglob("*.dsf"))), 2)
            self.assertIn("2 has no audio", err.getvalue())

    @unittest.skipUnless(_native_ok(), "native engine not built")
    def test_exit_zero_means_complete_output(self):
        # An area that claims to run past the end reads what exists. Whatever an image claims, a
        # run that exits 0 wrote every frame the TOC gives its tracks, and each file holds what
        # its header says.
        checked = 0
        for name in self.images:
            for args, which in ((("-2", "-s"), "stereo"), (("-m", "-s"), "multichannel")):
                rc, files = self._extract(name, *args)
                if rc != 0:
                    continue
                area = getattr(self._disc(name), which)
                total = 0
                for path, data in files.items():
                    if not path.endswith(".dsf"):
                        continue
                    with self.subTest(image=name, mode=args, file=Path(path).suffix):
                        self.assertEqual(struct.unpack_from("<Q", data, 12)[0], len(data), "file size field")
                        channels, samples = struct.unpack_from("<I", data, 52)[0], struct.unpack_from("<Q", data, 64)[0]
                        chunk = struct.unpack_from("<Q", data, 84)[0]
                        self.assertGreaterEqual(chunk - 12, samples // 8 * channels, "data chunk against the header")
                        self.assertLessEqual(92 + chunk - 12, len(data), "data chunk against the file")
                        total += samples // 8 * channels
                want = sum(t.duration.total_frames for t in area.tracks) * 4704 * area.channel_count if area else 0
                with self.subTest(image=name, mode=args):
                    self.assertEqual(total, want, "audio written against the TOC")
                checked += total > 0
        self.assertGreater(checked, 100)        # the check above really ran

    @unittest.skipUnless(_native_ok(), "native engine not built")
    def test_track_lsns_only_steer_the_scan(self):
        # Track LSNs past the end, zero or out of order used to give the engine a scan range
        # outside the area (or reversed); now they're clamped and the audio is found as usual.
        for tracks in ("1,2,3", "3"):
            _, want = self._extract("valid", "-2", "-s", "-t", tracks)
            for name in ("track-lsn-past-end", "track-lsn-zero", "track-lsn-reversed", "track-lsn-outside-area"):
                self.assertEqual(self._extract(name, "-2", "-s", "-t", tracks), (0, want), (name, tracks))


@unittest.skipUnless(_native_ok(), "native engine not built")
class RunBound(unittest.TestCase):
    """GHSA-q45f-7qmf-37x3: the bound holds per run, not per output. Tracks that each claim the
    whole area pass a per-output bound one by one; and frames an image lacks become silence, so
    a sparse image with little real data could make one run write gigabytes of it."""

    @classmethod
    def setUpClass(cls):
        logging.getLogger("pysacd").setLevel(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls):
        logging.getLogger("pysacd").setLevel(logging.NOTSET)

    def _extract(self, image: bytes, *args: str, sectors: int = 0) -> tuple[int, dict[str, int], str]:
        """Run pysacd-extract; the image is extended sparsely to `sectors`. Returns the exit
        status, {relative path: size} of what is left in the output directory, and what it said
        (stdout and stderr)."""
        with tempfile.TemporaryDirectory(prefix="pysacd-runbound") as tmp:
            iso = Path(tmp) / "in.iso"
            with open(iso, "wb") as f:
                f.write(image)
                if sectors * 2048 > len(image):
                    f.truncate(sectors * 2048)
            out = Path(tmp) / "out"
            out.mkdir()
            say = io.StringIO()
            with contextlib.redirect_stdout(say), contextlib.redirect_stderr(say), _deadline(TIMEOUT):
                rc = extract.main([*args, "--no-cfg", "--threads", "2", "-o", str(out), "-i", str(iso)])
            return rc, {str(p.relative_to(out)): p.stat().st_size for p in out.rglob("*") if p.is_file()}, \
                say.getvalue()

    def test_overlapping_tracks_are_bounded_per_run(self):
        # three tracks of 1000 frames over the same 1000 frames: each fits the area's 1099, the
        # three together don't fit twice
        rc, files, said = self._extract(sacdgen.overlapping_tracks(1000, 3), "-2", "-s")
        self.assertEqual(rc, 255)
        self.assertEqual([f for f in files if f.endswith(".dsf")], [])
        self.assertIn("3 dsf outputs need 3000 frames", said)

    def test_convert_refuses_overlapping_tracks(self):
        from pysacd.convert import Options, convert
        with tempfile.TemporaryDirectory(prefix="pysacd-runbound") as tmp:
            iso = Path(tmp) / "in.iso"
            iso.write_bytes(sacdgen.overlapping_tracks(1000, 3))
            out = Path(tmp) / "out"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                convert(str(iso), Options(output_dir=str(out), format="dsf", musicbrainz=False, cover=False,
                                          cover_file="", jobs=2))
            self.assertEqual(list(out.rglob("*.dsf")), [])

    def test_a_sparse_image_cannot_become_silence(self):
        # 1.4 MB of data in a 2.4 MB sparse file: room for the track's 3000 frames, of which the
        # image holds 11. Without the bound this wrote 28 MB of silence (a bigger hole: gigabytes)
        rc, files, said = self._extract(sacdgen.overlapping_tracks(3000, 1), "-2", "-s", sectors=1200)
        self.assertEqual(rc, 255)
        self.assertEqual(files, {})
        self.assertIn("the image lacks most of the audio", said)

    def test_the_engine_holds_the_bound_without_the_python_check(self):
        from unittest import mock

        from pysacd import _native
        with mock.patch.object(_native, "area_frame_capacity", return_value=1 << 40):
            # 20 x 1000 frames against 2 x 7 x 703 for the whole image
            rc, files, said = self._extract(sacdgen.overlapping_tracks(1000, 20), "-2", "-s")
            self.assertEqual(rc, 255)
            self.assertEqual(files, {})
            self.assertIn("more than 9842 frames in one format", said)
            # one output, mostly silence: stopped while writing
            rc, files, said = self._extract(sacdgen.overlapping_tracks(3000, 1), "-2", "-s", sectors=1200)
            self.assertEqual(rc, 255)
            self.assertEqual(files, {})
            self.assertIn("the image lacks most of the audio", said)

    @staticmethod
    def _tail_image(zero_from: int | None) -> bytes:
        """A stereo DST area of 1600 frames in two tracks of 800, packed 7 to a sector, so its
        capacity is about its play time: the tightest a real image gets. From frame zero_from
        on, the audio sectors read as zeros, as in a rip whose tail could not be read."""
        import dstenc
        frame = dstenc.encode_frame(bytes([0x69]) * (2 * 4704), 2)
        frames = [frame] * 1600
        area = sacdgen.AreaSpec(2, frames, [800, 800], titles=["One", "Two"])
        img, (lay,) = sacdgen.build_image([area], sacdgen.DiscInfo(title="tail"))
        if zero_from is not None:
            _, first = sacdgen.mux(frames, True)
            cut = lay.audio_start + first[zero_from]
            img[cut * 2048:(lay.audio_end + 1) * 2048] = bytes((lay.audio_end + 1 - cut) * 2048)
        return bytes(img)

    def test_edit_master_and_tracks_in_one_run(self):
        # An edit master and the tracks in the same format cover the area twice: allowed, on an
        # image whose capacity is barely its play time
        rc, files, said = self._extract(self._tail_image(None), "-2", "-p", "-e")
        self.assertEqual(rc, 0, said)
        self.assertEqual(len([f for f in files if f.endswith(".dff")]), 3)

    def test_a_zero_filled_tail_is_still_filled(self):
        # The last 780 of 1600 frames read as zeros: the run has more audio than silence, so the
        # tail is filled with silence as sacd_extract does. Asking for the second track alone
        # (20 frames of audio, 780 of silence) is refused.
        img = self._tail_image(820)
        rc, files, said = self._extract(img, "-2", "-s")
        self.assertEqual(rc, 1, said)       # complete, with warnings for the missing frames
        sizes = sorted(v for k, v in files.items() if k.endswith(".dsf"))
        self.assertEqual(len(sizes), 2)
        self.assertGreaterEqual(sizes[0], 800 * 2 * 4704)
        self.assertIn("missing frames", said)
        rc, files, said = self._extract(img, "-2", "-p", "-e")
        self.assertEqual(rc, 1, said)
        self.assertEqual(len([f for f in files if f.endswith(".dff")]), 3)
        rc, files, said = self._extract(img, "-2", "-s", "-t", "2")
        self.assertEqual(rc, 255)
        self.assertEqual(files, {})
        self.assertIn("the image lacks most of the audio", said)

if __name__ == "__main__":
    unittest.main()
