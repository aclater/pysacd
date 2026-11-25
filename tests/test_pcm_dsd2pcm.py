"""PCM output through dsd2pcm's precise and pysacd filters.

- Goldens: the engine's PCM of generated discs, for every filter, cutoff, rate, channel count
  and gain pysacd offers, equals tests/fixtures/pcm-goldens.json (tools/pcm_goldens.py, written
  from dsd2pcm's own command-line tool's output for the pinned source).
- Determinism within a release: the same bytes at any thread count, for any range start, for a
  track alone or inside a whole-area run, at every SIMD level dsd2pcm offers (DSD2PCM_SIMD),
  and with CET shadow stacks enforced where the machine has them.
- Defaults and settings: precise with the standard cutoff at both rates (wide is an option at
  176.4 kHz); a filter, cutoff and rate that don't go together are refused before a run starts,
  by the CLIs, by run_area and by the engine. Precise is undithered (digital silence stays
  exact zero, gain scales the samples); the pysacd filter dithers.
- The DSD the reader hands over (decoded DST, missing frames as silence, undecodable frames as
  filler) is what gets converted, and FLAC holds the WAV's samples.
- The build links nothing under a licence pysacd can't take (the FLAC reader of dsd2pcm's
  default features, claxon, is Apache-2.0 only), and the licence check refuses what it can't read.
- The engine's build info names the linked dsd2pcm and the default filters, and both version
  gates (the build's, and the engine's at run time) refuse a dsd2pcm older than 0.2.0 or a
  pre-release.
- The build works from a temporary directory whose path has a space.
"""

import ctypes
import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pysacd import SACDImage
from pysacd.toc import read_toc

sys.path.insert(0, os.path.dirname(__file__))
import sacdgen

ROOT = Path(__file__).resolve().parents[1]
FRAME = 4704
OPF = {88200: 1176, 176400: 2352}

try:
    from pysacd import _native
    _native._load()
    HAVE_NATIVE = True
except Exception:  # noqa: BLE001 - any failure to load means "not built"; tests skip
    HAVE_NATIVE = False


def tool_module(name: str):
    spec = importlib.util.spec_from_file_location(f"_pysacd_tool_{name}", ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod        # dataclasses look their module up there
    spec.loader.exec_module(mod)
    return mod


goldens = tool_module("pcm_goldens")
# (rate, filter, cutoff): every combination pysacd offers
COMBOS = goldens.COMBOS


def frames_of(raw: bytes, channels: int) -> list[bytes]:
    fb = channels * FRAME
    return [raw[i:i + fb] for i in range(0, len(raw), fb)]


def wav_samples(path: Path) -> list[int]:
    """The 24-bit samples of a WAV file pysacd wrote, interleaved."""
    body = wav_data(path)
    return [int.from_bytes(body[i:i + 3], "little", signed=True) for i in range(0, len(body), 3)]


def wav_data(path: Path) -> bytes:
    return goldens.wav_data(path)


def i32_as_s24(raw: bytes) -> bytes:
    n = len(raw) // 4
    return b"".join(v.to_bytes(3, "little", signed=True) for v in struct.unpack(f"<{n}i", raw))


def noise_frames(channels: int, count: int, seed: int) -> list[bytes]:
    rng = random.Random(seed)
    return [rng.randbytes(channels * FRAME) for _ in range(count)]


def area_of(iso: Path, channels: int):
    toc = read_toc(iso)
    return toc.stereo if channels == 2 else toc.multichannel


def convert(iso: Path, channels: int, out: Path, lo: int, hi: int, rate: int = 88200, gain: float = 0.0,
            threads: int = 0, pcm_filter: str | None = None, pcm_cutoff: str | None = None) -> bytes:
    out.unlink(missing_ok=True)
    with SACDImage(iso) as img:
        _native.run_area(img, iso, area_of(iso, channels), [_native.OutputSpec("wav", out, lo, hi)], rate, gain,
                         threads, pcm_filter=pcm_filter, pcm_cutoff=pcm_cutoff)
    return wav_data(out)


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class PcmGolden(unittest.TestCase):
    """Every case of tools/pcm_goldens.py against tests/fixtures/pcm-goldens.json."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.discs = goldens.Discs(Path(cls.tmp.name))
        cls.want = goldens.load()["cases"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_case_is_listed(self):
        self.assertEqual(sorted(self.want), sorted(c.name for c in goldens.cases()))

    def test_the_inputs_are_the_goldens_inputs(self):
        # generated without libm, so the same on every platform
        self.assertEqual(goldens.input_hashes(self.discs.inputs), goldens.load()["inputs"])

    def test_goldens(self):
        for c in goldens.cases():
            with self.subTest(case=c.name):
                pcm = self.discs.engine_pcm(c)
                self.assertEqual(len(pcm), self.discs.frames(c.source, c.channels) * OPF[c.rate] * c.channels * 3)
                self.assertEqual(goldens.sha(pcm), self.want[c.name], "PCM differs from the golden")

    def test_goldens_on_all_threads(self):
        # 97 frames: three whole batches of 32 and one frame more, on every worker
        for c in goldens.cases():
            if c.source == "noise" and c.gain == 0.0:
                with self.subTest(case=c.name):
                    self.assertEqual(goldens.sha(self.discs.engine_pcm(c, threads=0)), self.want[c.name])

    def test_defaults(self):
        # no filter or cutoff given: precise with the standard cutoff at both rates
        for rate, cutoff in ((88200, "standard"), (176400, "standard")):
            with self.subTest(rate=rate):
                self.assertEqual(_native.pcm_settings(rate), ("precise", cutoff))
                c = goldens.Case("music", 2, rate, "precise", cutoff, 0.0)
                iso = self.discs.iso("music", 2)
                got = convert(iso, 2, Path(self.tmp.name) / "d.wav", 0, 20, rate)
                self.assertEqual(goldens.sha(got), self.want[c.name])


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class PcmDeterminism(unittest.TestCase):
    """Bytes depend only on the disc, the settings and the position: not on threads, batches or
    range start, for each filter."""

    FRAMES = 75          # more than two batches of 32
    TRACKS = (20, 31, 24)
    SETTINGS = ((88200, "precise", "standard"), (176400, "precise", "wide"), (88200, "pysacd", "standard"),
                (176400, "pysacd", "standard"))

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.areas = {}
        for channels in (2, 6):
            frames = [os.urandom(channels * FRAME) for _ in range(cls.FRAMES)]
            iso = cls.dir / f"d{channels}.iso"
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(channels, frames, list(cls.TRACKS), dst=False)])
            toc = read_toc(iso)
            cls.areas[channels] = (iso, toc.stereo if channels == 2 else toc.multichannel)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_threads_ranges_and_tracks(self):
        for channels in (2, 6):
            iso, area = self.areas[channels]
            for rate, filt, cut in self.SETTINGS:
                kw = {"rate": rate, "pcm_filter": filt, "pcm_cutoff": cut}

                def run(lo, hi, threads=0, iso=iso, channels=channels, kw=kw):
                    return convert(iso, channels, self.dir / "r.wav", lo, hi, threads=threads, **kw)
                per_frame = OPF[rate] * channels * 3
                whole = run(0, self.FRAMES, threads=1)
                sub = {"channels": channels, "rate": rate, "filter": filt}
                with self.subTest(**sub, what="threads"):
                    for threads in (2, 3, 8):
                        self.assertTrue(run(0, self.FRAMES, threads) == whole)
                with self.subTest(**sub, what="range starts"):
                    # starts around the batch edges (32 frames) and the context (2 frames)
                    for lo in (1, 2, 3, 31, 32, 33, 34, 63, 64, 74):
                        self.assertTrue(run(lo, self.FRAMES) == whole[lo * per_frame:], f"from frame {lo}")
                    for hi in (6, 33, 65):
                        self.assertTrue(run(5, hi) == whole[5 * per_frame:hi * per_frame], f"frames 5-{hi}")
                with self.subTest(**sub, what="tracks"):
                    specs = []
                    for t in area.tracks:
                        start = t.start.total_frames
                        specs.append(_native.OutputSpec("wav", self.dir / f"t{t.number}.wav", start,
                                                        start + t.duration.total_frames))
                    for s in specs:
                        Path(s.path).unlink(missing_ok=True)
                    with SACDImage(iso) as img:
                        _native.run_area(img, iso, area, specs, rate, 0.0, pcm_filter=filt, pcm_cutoff=cut)
                    alone = []
                    for s in specs:
                        inside = wav_data(Path(s.path))
                        alone.append(run(s.tc_start, s.tc_end))
                        self.assertTrue(inside == whole[s.tc_start * per_frame:s.tc_end * per_frame])
                        self.assertTrue(alone[-1] == inside)
                    self.assertTrue(b"".join(alone) == whole)


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class FilterBehaviour(unittest.TestCase):
    """Precise rounds without dither; the pysacd filter dithers. Gain scales the samples."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.discs = goldens.Discs(cls.dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def samples(self, source, channels, rate, filt, cut, gain):
        out = self.discs.engine(goldens.Case(source, channels, rate, filt, cut, gain))
        return wav_samples(out)

    def test_precise_silence_is_exact_zero(self):
        for channels in (2, 6):
            for rate, filt, cut in COMBOS:
                if filt == "precise":
                    with self.subTest(channels=channels, rate=rate, cutoff=cut):
                        got = self.samples("silence", channels, rate, filt, cut, 0.0)
                        self.assertEqual(len(got), 20 * OPF[rate] * channels)
                        self.assertFalse(any(got), "precise turned DSD silence into non-zero samples")

    def test_pysacd_filter_dithers_silence(self):
        for rate in (88200, 176400):
            with self.subTest(rate=rate):
                got = self.samples("silence", 2, rate, "pysacd", "standard", 0.0)
                self.assertTrue(any(got), "no dither on silence")
                self.assertLessEqual(max(abs(v) for v in got), 4, "more than dither on silence")

    def test_precise_gain_scales_without_dither(self):
        # rounding only: the sample at -6 dB is the 0 dB sample times the gain, within 1 LSB
        for rate, filt, cut in COMBOS:
            if filt == "precise":
                with self.subTest(rate=rate, cutoff=cut):
                    ref = self.samples("music", 2, rate, filt, cut, 0.0)
                    got = self.samples("music", 2, rate, filt, cut, -6.0)
                    k = 10 ** (-6.0 / 20)
                    self.assertEqual(len(ref), len(got))
                    self.assertLessEqual(max(abs(a * k - b) for a, b in zip(ref, got)), 1.0)
                    self.assertGreater(max(abs(v) for v in ref), 1 << 20)

    def test_clipping(self):
        for filt in ("precise", "pysacd"):
            for source in ("ff", "noise"):
                with self.subTest(filter=filt, source=source):
                    got = self.samples(source, 2, 88200, filt, "standard", 24.0)
                    self.assertTrue(any(v in (8388607, -8388608) for v in got), "nothing clipped")
                    self.assertTrue(all(-8388608 <= v <= 8388607 for v in got))


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class PcmSettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.iso = cls.dir / "g.iso"
        sacdgen.build_iso(cls.iso, [sacdgen.AreaSpec(2, [bytes(2 * FRAME)] * 3, [3], dst=False)])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_wav(self, rate=88200, gain=0.0, **kw):
        out = self.dir / "g.wav"
        out.unlink(missing_ok=True)
        with SACDImage(self.iso) as img:
            _native.run_area(img, self.iso, read_toc(self.iso).stereo, [_native.OutputSpec("wav", out, 0, 3)],
                             rate, gain, **kw)
        return out

    def test_gain_out_of_range_is_refused(self):
        # out of range, not finite, or not a number at all (config.toml can hand over any TOML type)
        for gain in (-121.0, 24.5, 30, float("nan"), float("inf"), "loud", True, None, [6]):
            with self.subTest(gain=gain):
                out = self.dir / "g.wav"
                out.unlink(missing_ok=True)
                with self.assertRaises(_native.NativeError) as cm:
                    self.run_wav(gain=gain)
                self.assertIn("gain", str(cm.exception).lower())
                self.assertFalse(out.exists())
        for gain in (-120, 24, 0, 6.5):
            with self.subTest(gain=gain):
                self.assertEqual(_native.check_gain_db(gain), float(gain))

    def test_clipped_samples_are_counted_and_reported(self):
        ff = self.dir / "ff.iso"
        if not ff.exists():
            sacdgen.build_iso(ff, [sacdgen.AreaSpec(2, [b"\xff" * (2 * FRAME)] * 6, [6], dst=False)])
        # all-ones DSD is +6 dB above SACD's 0 dB reference: it clips from 0 dB gain up, not at -6.1 dB
        for gain, clipped in ((24.0, True), (-6.1, False)):
            with self.subTest(gain=gain):
                out = self.dir / "ff.wav"
                out.unlink(missing_ok=True)
                with self.assertLogs("pysacd.engine", "DEBUG") as logs, SACDImage(ff) as img:
                    info = _native.run_area(img, ff, read_toc(ff).stereo, [_native.OutputSpec("wav", out, 0, 6)],
                                            88200, gain)
                warned = [r for r in logs.records if "clipped" in r.getMessage()]
                if clipped:
                    # every sample of the area's six frames clips at +24 dB on all-ones DSD; the
                    # count covers the written frames only
                    self.assertGreater(info.pcm_clipped, 0)
                    self.assertLessEqual(info.pcm_clipped, 6 * 1176 * 2)
                    self.assertEqual(len(warned), 1)
                    self.assertEqual(warned[0].levelname, "WARNING")
                    self.assertIn(f"{info.pcm_clipped} samples clipped", warned[0].getMessage())
                else:
                    self.assertEqual(info.pcm_clipped, 0)
                    self.assertEqual(warned, [])

    def test_pcm_settings(self):
        ok = {(88200, None, None): ("precise", "standard"), (176400, None, None): ("precise", "standard"),
              (88200, "pysacd", None): ("pysacd", "standard"), (176400, "pysacd", None): ("pysacd", "standard"),
              (176400, "precise", "standard"): ("precise", "standard"), (88200, None, "low"): ("precise", "low"),
              (176400, None, "wide"): ("precise", "wide"), (176400, "pysacd", "standard"): ("pysacd", "standard")}
        for args, want in ok.items():
            self.assertEqual(_native.pcm_settings(*args), want, args)
        bad = [((88200, None, "wide"), "wide PCM cutoff"), ((88200, "precise", "wide"), "wide PCM cutoff"),
               ((176400, "pysacd", "wide"), "standard cutoff only"), ((88200, "pysacd", "low"), "standard cutoff only"),
               ((88200, "fast", None), "unknown PCM filter"), ((88200, "default", None), "unknown PCM filter"),
               ((88200, None, "40"), "unknown PCM cutoff"), ((44100, None, None), "PCM rate"),
               ((352800, None, None), "PCM rate"), ((88200, ["precise"], None), "unknown PCM filter"),
               ((88200, None, {"a": 1}), "unknown PCM cutoff"), ((88200.0, None, None), "PCM rate"),
               ((True, None, None), "PCM rate"), (("88200", None, None), "PCM rate")]
        for args, msg in bad:
            with self.assertRaises(ValueError, msg=args) as cm:
                _native.pcm_settings(*args)
            self.assertIn(msg, str(cm.exception), args)

    def test_refused_before_the_run(self):
        for rate, filt, cut in ((88200, None, "wide"), (176400, "pysacd", "wide"), (88200, "x", None)):
            with self.subTest(rate=rate, filter=filt, cutoff=cut):
                with self.assertRaises(_native.NativeError):
                    self.run_wav(rate, pcm_filter=filt, pcm_cutoff=cut)
                self.assertFalse((self.dir / "g.wav").exists())

    def test_the_engine_refuses_what_run_area_would(self):
        # the engine's own check (for the C interface): run_area's check bypassed
        for rate, filt, cut, msg in ((88200, "precise", "wide", "wide PCM cutoff"),
                                     (176400, "pysacd", "wide", "standard cutoff only"),
                                     (176400, "pysacd", "low", "standard cutoff only")):
            with self.subTest(rate=rate, filter=filt, cutoff=cut), \
                    mock.patch.object(_native, "pcm_settings", return_value=(filt, cut)):
                with self.assertRaises(_native.NativeError) as cm:
                    self.run_wav(rate)
                self.assertIn(msg, str(cm.exception))
                self.assertNotIn("dsd2pcm", str(cm.exception).lower())

    def test_dsd_outputs_ignore_the_pcm_settings(self):
        out = self.dir / "g.dff"
        out.unlink(missing_ok=True)
        with SACDImage(self.iso) as img:
            _native.run_area(img, self.iso, read_toc(self.iso).stereo, [_native.OutputSpec("dff", out, 0, 3)],
                             88200, pcm_cutoff="wide")
        self.assertTrue(out.exists())


class PcmOptions(unittest.TestCase):
    """--pcm-filter and --pcm-cutoff on both command lines, and in config.toml."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.iso = Path(cls.tmp.name) / "o.iso"
        sacdgen.build_iso(cls.iso, [sacdgen.AreaSpec(2, [bytes(2 * FRAME)] * 3, [3], dst=False)])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_config_gain_is_checked_before_anything_is_created(self):
        out = Path(self.tmp.name) / "out"
        for value, msg in (("30", "outside"), ('"loud"', "not a number"), ("nan", "outside"), ("true", "not a number"),
                           ("[6]", "not a number")):
            for fmt in ("flac", "dsf"):
                with self.subTest(gain_db=value, format=fmt):
                    done = self.run_cli("pysacd", "convert", str(self.iso), "-f", fmt, "-o", str(out),
                                        "--no-musicbrainz", "--no-cover", config=f"gain_db = {value}\n")
                    self.assertEqual(done.returncode, 2, done.stderr)
                    self.assertIn(msg, done.stderr)
                    self.assertNotIn("Traceback", done.stderr)
                    self.assertFalse(out.exists())

    def test_pcm_options_with_dsd_output(self):
        # a wrong pair is refused even when the output is DSD; a right one is ignored with a warning
        done = self.run_cli("pysacd", "convert", "missing.iso", "-f", "dsf", "--no-musicbrainz", "--pcm-cutoff", "wide")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("wide PCM cutoff", done.stderr)
        done = self.run_cli("pysacd.extract", "-i", "missing.iso", "-2", "--pcm-filter", "pysacd", "--pcm-cutoff", "low")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("standard cutoff only", done.stderr)
        done = self.run_cli("pysacd", "convert", "missing.iso", "-f", "dsf", "--no-musicbrainz", "--pcm-filter", "pysacd")
        self.assertNotEqual(done.returncode, 2, done.stderr)
        self.assertIn("--pcm-filter only apply to FLAC and WAV; ignored for DSF", done.stderr)
        done = self.run_cli("pysacd.extract", "-i", "missing.iso", "-2", "--pcm-filter", "pysacd")
        self.assertNotEqual(done.returncode, 2, done.stderr)
        self.assertIn("only apply to --flac and --wav; ignored", done.stderr)

    def run_cli(self, *args, config: str | None = None):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [sys.executable, "-m", *args]
            if config is not None:
                cfg = Path(tmp) / "config.toml"
                cfg.write_text(config)
                cmd += ["--config", str(cfg)]
            src = str(ROOT / "src")
            env = dict(os.environ, HOME=tmp, PYTHONPATH=os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")])))
            return subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=tmp, env=env)

    def test_help(self):
        for mod in (["pysacd", "convert"], ["pysacd.extract"]):
            out = " ".join(self.run_cli(*mod, "--help").stdout.split())
            self.assertIn("--pcm-filter {precise,pysacd}", out, mod)
            self.assertIn("--pcm-cutoff {standard,low,wide}", out, mod)

    def test_bad_combinations_are_refused_before_anything_runs(self):
        for args, msg in (([], "wide PCM cutoff"), (["--pcm-filter", "pysacd"], "standard cutoff only")):
            with self.subTest(args=args):
                done = self.run_cli("pysacd.extract", "-i", "missing.iso", "--flac", *args, "--pcm-cutoff", "wide")
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn(msg, done.stderr)
                done = self.run_cli("pysacd", "convert", "missing.iso", "-f", "flac", "--no-musicbrainz", *args,
                                    "--pcm-cutoff", "wide")
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn(msg, done.stderr)
        done = self.run_cli("pysacd.extract", "-i", "missing.iso", "--wav", "--pcm-filter", "fast")
        self.assertEqual(done.returncode, 2)
        self.assertIn("invalid choice", done.stderr)

    def test_config_keys(self):
        done = self.run_cli("pysacd", "convert", "missing.iso", "-f", "wav",
                            config='pcm_filter = "pysacd"\npcm-cutoff = "wide"\npcm_rate = 176400\n')
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("standard cutoff only", done.stderr)
        from pysacd.convert import Options
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "c.toml"
            cfg.write_text('pcm_filter = "pysacd"\npcm-cutoff = "standard"\n')
            o = Options.from_config(cfg)
        self.assertEqual((o.pcm_filter, o.pcm_cutoff), ("pysacd", "standard"))
        # any TOML type: refused with a message, not a traceback
        done = self.run_cli("pysacd", "convert", "missing.iso", "-f", "flac", config='pcm_filter = ["pysacd"]\n')
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("unknown PCM filter", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual((Options().pcm_filter, Options().pcm_cutoff), (None, None))


def engine_library() -> Path | None:
    for name in ("_sacdconv.so", "_sacdconv.dylib", "_sacdconv.dll"):
        p = ROOT / "src" / "pysacd" / name
        if p.is_file():
            return p
    return None


# Crates whose source paths may appear in the engine (panic locations): dsd2pcm and the
# dependencies of Rust's standard library, all with an MIT alternative.
ALLOWED_CRATES = {"dsd2pcm", "dsd2pcm-capi", "addr2line", "adler2", "cfg-if", "compiler_builtins", "gimli",
                  "hashbrown", "libc", "memchr", "miniz_oxide", "object", "rustc-demangle", "std_detect",
                  "unwind", "scarletbook", "scarletbook-ffi", "rayon", "rayon-core", "crossbeam-deque",
                  "crossbeam-epoch", "crossbeam-utils", "either", "thiserror"}


@unittest.skipUnless(engine_library(), "native helper not built (make)")
class LinkedLicences(unittest.TestCase):
    def test_no_apache_only_crate_in_the_engine(self):
        data = engine_library().read_bytes()
        self.assertNotIn(b"claxon", data.lower())
        crates = {m.decode() for m in re.findall(rb"index\.crates\.io-[0-9a-f]+[/\\]([A-Za-z0-9_-]+?)-\d+\.\d+",
                                                 data)}
        self.assertLessEqual(crates, ALLOWED_CRATES, f"unexpected crates: {sorted(crates - ALLOWED_CRATES)}")


class MinimumVersion(unittest.TestCase):
    """pysacd requires dsd2pcm 0.2.0 or later: the build refuses an older source."""

    def test_lock_and_source_versions(self):
        t = tool_module("dsd2pcm")
        lock = t.load_lock()
        self.assertGreaterEqual(t.version_tuple(lock["min_version"]), (0, 2, 0))
        t.check_version(lock, lock["version"])
        for old in ("0.1.0", "0.1.2", "0.1.10", "0.0.9"):
            with self.assertRaises(t.FetchError):
                t.check_version(lock, old)
        for new in ("0.2.0", "0.2.10", "0.3.0", "1.0.0"):
            t.check_version(lock, new)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Cargo.toml").write_text('[workspace]\nmembers = []\n\n[workspace.package]\n'
                                               'version = "0.1.2"\nedition = "2021"\n')
            self.assertEqual(t.workspace_version(Path(tmp)), "0.1.2")

    def test_goldens_name_the_pinned_dsd2pcm_and_its_cli(self):
        # the goldens were made with, and checked against the command-line tool of, the pinned release
        doc, lock = goldens.load(), tool_module("dsd2pcm").load_lock()
        self.assertEqual(doc["dsd2pcm"], {"version": lock["version"], "commit": lock["commit"]})
        self.assertTrue(doc["checked_against_dsd2pcm_cli"])
        self.assertEqual(doc["dsd2pcm_cli"], {"version": lock["version"], "commit": lock["commit"]})

    def test_the_engine_source_needs_the_0_2_header(self):
        src = (ROOT / "csrc" / "sacdconv.c").read_text()
        self.assertIn("#if !defined(DSD2PCM_FILTER_FAST)", src)
        self.assertIn('#error "pysacd needs dsd2pcm 0.2.0 or later', src)


def _child(job_file: Path) -> int:
    """Run in a subprocess (DSD2PCM_SIMD, GLIBC_TUNABLES): convert each job's area to WAV and
    report the data's SHA-256 and this process's shadow-stack state."""
    jobs = json.loads(job_file.read_text())
    out = {"sha256": [], "x86_thread_features": None}
    for j in jobs:
        iso, wav = Path(j["iso"]), Path(j["wav"])
        pcm = convert(iso, j["channels"], wav, 0, j["frames"], j["rate"], j["gain"], j.get("threads", 0),
                      j["filter"], j["cutoff"])
        out["sha256"].append(hashlib.sha256(pcm).hexdigest())
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("x86_Thread_features:"):
                out["x86_thread_features"] = line.split(":", 1)[1].split()
    except OSError:
        pass
    print(json.dumps(out))
    return 0


GETENV_SHIM = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
/* logs each read of DSD2PCM_SIMD (the value seen) to $PYSACD_TEST_GETENV_LOG */
char *getenv(const char *name)
{
    static char *(*real)(const char *);
    if (!real)
        real = (char *(*)(const char *))dlsym(RTLD_NEXT, "getenv");
    char *v = real(name);
    if (name && strcmp(name, "DSD2PCM_SIMD") == 0) {
        const char *path = real("PYSACD_TEST_GETENV_LOG");
        FILE *f = path ? fopen(path, "a") : NULL;
        if (f) {
            fprintf(f, "%s\n", v ? v : "(unset)");
            fclose(f);
        }
    }
    return v;
}
"""


def getenv_shim(work: Path) -> Path | None:
    """A preloadable getenv that logs reads of DSD2PCM_SIMD, or None where it can't be built."""
    import platform
    cc = shutil.which(os.environ.get("CC", "cc"))
    if not sys.platform.startswith("linux") or platform.libc_ver()[0] != "glibc" or not cc:
        return None
    src, lib = work / "getenv_shim.c", work / "getenv_shim.so"
    src.write_text(GETENV_SHIM)
    done = subprocess.run([cc, "-shared", "-fPIC", "-O1", "-o", str(lib), str(src), "-ldl"],
                          capture_output=True, text=True, check=False)
    return lib if done.returncode == 0 else None


def noise_jobs(discs, work: Path, threads: int = 0) -> tuple[list[dict], list[str]]:
    """The 97-frame noise cases of every filter, cutoff, rate and channel count, and their goldens."""
    want = goldens.load()["cases"]
    jobs, hashes = [], []
    for c in goldens.cases():
        if c.source == "noise" and c.gain == 0.0:
            jobs.append({"iso": str(discs.iso("noise", c.channels)), "channels": c.channels, "rate": c.rate,
                         "gain": 0.0, "frames": goldens.LONG_FRAMES, "filter": c.filter, "cutoff": c.cutoff,
                         "threads": threads, "wav": str(work / f"j{len(jobs)}.wav")})
            hashes.append(want[c.name])
    return jobs, hashes


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class PcmGoldenEverywhere(unittest.TestCase):
    """The golden bytes at every SIMD level dsd2pcm offers."""

    def test_golden_at_every_simd_level(self):
        # Each level in its own process: dsd2pcm reads DSD2PCM_SIMD once. A level above what the
        # CPU has is capped to it; generic always runs the portable code. dsd2pcm 0.2's C interface
        # doesn't report the level it runs at, so where a getenv shim can be preloaded (Linux,
        # glibc, a C compiler) each child logs what the engine's dsd2pcm read: proof that the level
        # reached the library in that run, and differed between the runs.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            jobs, want = noise_jobs(goldens.Discs(d), d)
            job_file = d / "simd.json"
            job_file.write_text(json.dumps(jobs))
            shim = getenv_shim(d)
            seen = {}
            for level in ("generic", "sse2", "avx2", "avx2fma"):
                with self.subTest(level=level):
                    env = dict(os.environ, DSD2PCM_SIMD=level)
                    log = d / f"getenv-{level}.log"
                    if shim:
                        env["LD_PRELOAD"] = " ".join(filter(None, [os.environ.get("LD_PRELOAD"), str(shim)]))
                        env["PYSACD_TEST_GETENV_LOG"] = str(log)
                    done = subprocess.run([sys.executable, __file__, "--child", str(job_file)], env=env,
                                          check=False, capture_output=True, text=True)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertEqual(json.loads(done.stdout.splitlines()[-1])["sha256"], want)
                    if shim:
                        read = set(log.read_text().split()) if log.exists() else set()
                        self.assertEqual(read, {level}, "the engine's dsd2pcm did not read DSD2PCM_SIMD")
                        seen[level] = read
            if not shim:
                self.skipTest("no getenv shim here (needs Linux, glibc and cc): the level was set, not observed")
            self.assertEqual(len({frozenset(v) for v in seen.values()}), 4)


def engine_shstk_marked() -> bool:
    lib = engine_library()
    if lib is None or lib.suffix != ".so":
        return False
    try:
        bits = tool_module("mark_shstk").feature_1_and(lib.read_bytes())
    except ValueError:
        return False
    return bool(bits) and all(b & 2 for b in bits)


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class ShadowStacks(unittest.TestCase):
    """With CET shadow stacks enforced (glibc.cpu.x86_shstk=on), the engine loads, converts and
    gives the golden bytes. glibc refuses to load an unmarked library in that mode."""

    def test_golden_with_shadow_stacks_on(self):
        if not engine_shstk_marked():
            self.skipTest("the engine is not SHSTK-marked (not x86-64 glibc, or a HARDEN=0 sanitizer build)")
        env = dict(os.environ, GLIBC_TUNABLES="glibc.cpu.x86_shstk=on")
        probe = subprocess.run([sys.executable, "-c", "print(open('/proc/self/status').read())"], env=env,
                               capture_output=True, text=True, check=False)
        feats = [ln.split(":", 1)[1].split() for ln in probe.stdout.splitlines() if ln.startswith("x86_Thread_features:")]
        if probe.returncode or not feats or "shstk" not in feats[0]:
            self.skipTest("user-space shadow stacks not available here (CPU, kernel or glibc)")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            jobs, want = noise_jobs(goldens.Discs(d), d, threads=4)
            job = d / "job.json"
            job.write_text(json.dumps(jobs))
            done = subprocess.run([sys.executable, __file__, "--child", str(job)], env=env, check=False,
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            got = json.loads(done.stdout.splitlines()[-1])
            self.assertIn("shstk", got["x86_thread_features"], "shadow stacks were not on in the converting process")
            self.assertEqual(got["sha256"], want)


def user_shstk_cpu() -> bool:
    try:
        return any(ln.startswith("flags") and " user_shstk" in ln
                   for ln in Path("/proc/cpuinfo").read_text().splitlines())
    except OSError:
        return False


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class ShadowStacksForced(unittest.TestCase):
    """CET shadow stacks forced on without glibc's policy (which leaves them off on Fedora 44 and
    RHEL 10 even with glibc.cpu.x86_shstk=on): tests/native/shstk_run.c, linked to the engine
    library pysacd loads, turns them on with a raw arch_prctl in main() and never returns from
    there, so every later call and return in the process, the engine's threads and dsd2pcm
    included, is checked by the CPU. The golden cases at every SIMD level must give the golden
    bytes, and a deliberately wrong return must fault with SEGV_CPERR (the control)."""

    def test_golden_with_shadow_stacks_forced(self):
        import platform
        lib = engine_library()
        cc = shutil.which(os.environ.get("CC", "cc"))
        if sys.platform != "linux" or platform.machine() != "x86_64" or lib is None or lib.suffix != ".so":
            self.skipTest("x86-64 Linux only")
        if not cc:
            self.skipTest("no C compiler for the harness")
        if not user_shstk_cpu():
            self.skipTest("the CPU or kernel has no user-space shadow stacks (no user_shstk in /proc/cpuinfo)")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            harness = d / "shstk_run"
            subprocess.run([cc, "-O2", "-Wall", "-pthread", "-o", str(harness), str(ROOT / "tests" / "native" / "shstk_run.c"),
                            str(lib), f"-Wl,-rpath,{lib.parent}"], check=True, capture_output=True, text=True)
            for mode, want_exit in (("on", 3), ("off", 0)):
                with self.subTest(control=mode):
                    r = subprocess.run([str(harness), mode, "--bad"], capture_output=True, text=True, check=False)
                    self.assertEqual(r.returncode, want_exit, r.stdout + r.stderr)
            jobs, want = noise_jobs(goldens.Discs(d), d, threads=4)
            lines = []
            for j in jobs:
                iso = Path(j["iso"])
                area = area_of(iso, j["channels"])
                filt, cut = _native.pcm_settings(j["rate"], j["filter"], j["cutoff"])
                with SACDImage(iso) as img:
                    ss, doff, total = img.sector_size, img.data_offset, img.total_sectors
                frames = area.total_playtime.total_frames
                lines.append(f"{iso} {ss} {doff} {area.channel_count} {area.toc_start} {total} "
                             f"{frames - 2 if frames >= 2 else 0xFFFFFFFF} {j['rate']} {_native.PCM_FILTERS[filt]} "
                             f"{_native.PCM_CUTOFFS[cut]} {j['threads']} {j['frames']} {j['wav']}")
            job_file = d / "jobs.txt"
            job_file.write_text("\n".join(lines) + "\n")
            shim = getenv_shim(d)
            for level in ("generic", "sse2", "avx2", "avx2fma"):
                with self.subTest(level=level):
                    for j in jobs:
                        Path(j["wav"]).unlink(missing_ok=True)
                    env = dict(os.environ, DSD2PCM_SIMD=level)
                    log = d / f"getenv-{level}.log"
                    if shim:
                        env["LD_PRELOAD"] = " ".join(filter(None, [os.environ.get("LD_PRELOAD"), str(shim)]))
                        env["PYSACD_TEST_GETENV_LOG"] = str(log)
                    r = subprocess.run([str(harness), "on", str(job_file)], env=env, capture_output=True, text=True,
                                       check=False)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertIn("main thread: shstk on", r.stdout)
                    self.assertIn("new thread: shstk on", r.stdout)
                    self.assertEqual([hashlib.sha256(wav_data(Path(j["wav"]))).hexdigest() for j in jobs], want)
                    if shim:
                        self.assertEqual(set(log.read_text().split()) if log.exists() else set(), {level},
                                         "the engine's dsd2pcm did not read DSD2PCM_SIMD")


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class SourcesOfDsd(unittest.TestCase):
    """PCM is the conversion of exactly the DSD the reader hands over: decoded DST, missing
    frames as 0x69 silence, undecodable DST frames as 0x55 filler. The DSDIFF output (no header:
    the area's raw interleaved DSD) shows what that was; the same DSD in a plain area gives the
    same PCM."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def convert(self, iso: Path, channels: int, frames: int, **kw) -> tuple[bytes, bytes, _native.OutputSpec]:
        area = area_of(iso, channels)
        wav, dff = self.dir / "x.wav", self.dir / "x.dff"
        for p in (wav, dff):
            p.unlink(missing_ok=True)
        specs = [_native.OutputSpec("wav", wav, 0, frames), _native.OutputSpec("dff", dff, 0, frames)]
        with SACDImage(iso) as img:
            _native.run_area(img, iso, area, specs, 88200, 0.0, allow_silence=True, **kw)
        return wav_data(wav), dff.read_bytes(), specs[1]

    def reference(self, dsd: bytes, channels: int, **kw) -> bytes:
        frames = frames_of(dsd, channels)
        iso = self.dir / "plain.iso"
        sacdgen.build_iso(iso, [sacdgen.AreaSpec(channels, frames, [len(frames)], dst=False)])
        return convert(iso, channels, self.dir / "plain.wav", 0, len(frames), 88200, 0.0, **kw)

    def test_dst_area(self):
        try:
            import dstenc
            dstenc.load()
        except Exception as e:  # noqa: BLE001 - the encoder needs numpy and build/dstenc.so
            self.skipTest(f"DST test encoder unavailable: {e}")
        for channels in (2, 6):
            dsd = [dstenc.synth_dsd(channels, 1, seed=500 + i) for i in range(40)]
            frames = [dstenc.encode_frame(f, channels) for f in dsd]
            bad = 33
            frames[bad] += b"\xff\xff\xff"      # fails the end-of-frame check: 0x55 filler
            iso = self.dir / f"dst{channels}.iso"
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(channels, frames, [20, 20])])
            for filt in ("precise", "pysacd"):
                with self.subTest(channels=channels, filter=filt):
                    pcm, raw, spec = self.convert(iso, channels, 40, pcm_filter=filt)
                    fb = channels * FRAME
                    self.assertEqual(spec.dst_errors, 1)
                    self.assertEqual(raw[bad * fb:(bad + 1) * fb], b"\x55" * fb)
                    self.assertTrue(raw[:bad * fb] == b"".join(dsd[:bad]))
                    self.assertTrue(pcm == self.reference(raw, channels, pcm_filter=filt),
                                    "PCM differs from the plain area's")

    def test_missing_frames(self):
        channels, count = 2, 60
        frames = noise_frames(channels, count, 6060)
        img, (lay,) = sacdgen.build_image([sacdgen.AreaSpec(channels, frames, [count], dst=False)],
                                          sacdgen.DiscInfo(title="gaps"))
        _, first = sacdgen.mux(frames, False)
        # the sectors of frames 35 and 36 read as zeros: no frame starts there
        lo, hi = lay.audio_start + first[35], lay.audio_start + first[37]
        img[lo * 2048:hi * 2048] = bytes((hi - lo) * 2048)
        iso = self.dir / "gaps.iso"
        iso.write_bytes(bytes(img))
        for filt in ("precise", "pysacd"):
            with self.subTest(filter=filt):
                pcm, raw, spec = self.convert(iso, channels, count, pcm_filter=filt)
                fb = channels * FRAME
                self.assertGreater(spec.frames_missing, 0)
                silent = [i for i in range(count) if raw[i * fb:(i + 1) * fb] == b"\x69" * fb]
                self.assertTrue(silent and set(silent) <= set(range(34, 38)), silent)
                self.assertTrue(pcm == self.reference(raw, channels, pcm_filter=filt),
                                "PCM differs from the plain area's")


FLAC_DUMP = r"""
#include <FLAC/stream_decoder.h>
#include <stdio.h>
static FLAC__StreamDecoderWriteStatus wr(const FLAC__StreamDecoder *d, const FLAC__Frame *f,
                                         const FLAC__int32 *const buf[], void *out)
{
    (void)d;
    for (unsigned i = 0; i < f->header.blocksize; i++)
        for (unsigned c = 0; c < f->header.channels; c++)
            fwrite(&buf[c][i], 4, 1, out);
    return FLAC__STREAM_DECODER_WRITE_STATUS_CONTINUE;
}
static void er(const FLAC__StreamDecoder *d, FLAC__StreamDecoderErrorStatus s, void *u) { (void)d; (void)s; (void)u; }
int main(int argc, char **argv)
{
    FILE *out = fopen(argv[2], "wb");
    FLAC__StreamDecoder *d = FLAC__stream_decoder_new();
    if (argc != 3 || !out || !d)
        return 2;
    FLAC__stream_decoder_set_md5_checking(d, true);
    if (FLAC__stream_decoder_init_file(d, argv[1], wr, NULL, er, out) != FLAC__STREAM_DECODER_INIT_STATUS_OK)
        return 3;
    int ok = FLAC__stream_decoder_process_until_end_of_stream(d) && FLAC__stream_decoder_finish(d);
    fclose(out);
    return ok ? 0 : 4;
}
"""


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class FlacEqualsWav(unittest.TestCase):
    def test_flac_samples_are_the_wav_samples(self):
        if not _native.has_flac():
            self.skipTest("engine built without libFLAC")
        # the tools by their resolved paths: a bare name may not be found where only a .bat or .cmd shim exists
        cc = shutil.which(os.environ.get("CC") or "cc") or shutil.which("gcc") or shutil.which("clang")
        pkg_config = shutil.which("pkg-config") or shutil.which("pkgconf")
        if not cc:
            self.skipTest("no C compiler (cc, gcc or clang) for the FLAC dump tool")
        if not pkg_config:
            self.skipTest("libFLAC development files not found (no pkg-config or pkgconf)")
        flags = subprocess.run([pkg_config, "--cflags", "--libs", "flac"], capture_output=True, text=True,
                               check=False)
        if flags.returncode:
            self.skipTest("libFLAC development files not found (pkg-config flac)")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "dump.c").write_text(FLAC_DUMP)
            dump = d / ("dump.exe" if sys.platform == "win32" else "dump")
            cmd = [cc, "-O1", "-o", str(dump), str(d / "dump.c"), *flags.stdout.split()]
            # the compiler's own directory first on PATH: on Windows its programs (cc1, as) load the first
            # libwinpthread DLL on PATH, and the pinned Rust's older one fails them silently (setup.py
            # does the same for make)
            cc_env = dict(os.environ, PATH=os.pathsep.join([str(Path(cc).parent), os.environ.get("PATH", "")]))
            if subprocess.run(cmd, check=False, capture_output=True, env=cc_env).returncode:
                # a static libFLAC (the wheel builds') needs its private libraries too
                static = subprocess.run([pkg_config, "--static", "--libs", "flac"], capture_output=True, text=True,
                                        check=True)
                subprocess.run([*cmd, *static.stdout.split(), "-lm"], check=True, env=cc_env)
            for channels in (2, 6):
                frames = noise_frames(channels, 45, 4500 + channels)
                iso = d / f"f{channels}.iso"
                sacdgen.build_iso(iso, [sacdgen.AreaSpec(channels, frames, [45], dst=False)])
                area = area_of(iso, channels)
                for rate, filt in ((88200, None), (176400, None), (88200, "pysacd")):
                    with self.subTest(channels=channels, rate=rate, filter=filt):
                        wav, flac = d / "x.wav", d / "x.flac"
                        for p in (wav, flac):
                            p.unlink(missing_ok=True)
                        specs = [_native.OutputSpec("wav", wav, 0, 45), _native.OutputSpec("flac", flac, 0, 45)]
                        with SACDImage(iso) as img:
                            _native.run_area(img, iso, area, specs, rate, 0.0, pcm_filter=filt)
                        subprocess.run([str(dump), str(flac), str(d / "x.i32")], check=True, env=cc_env)
                        raw = (d / "x.i32").read_bytes()
                        self.assertTrue(i32_as_s24(raw) == wav_data(wav), "FLAC samples differ from WAV samples")
                        self.assertEqual(len(raw) // 4, 45 * OPF[rate] * channels)


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class EngineVersionGate(unittest.TestCase):
    """The engine's run-time check of the linked dsd2pcm (sacdconv_dsd2pcm_version_ok), and its
    build info."""

    def test_versions(self):
        f = _native._load().sacdconv_dsd2pcm_version_ok
        f.argtypes, f.restype = [ctypes.c_char_p, ctypes.c_int], ctypes.c_int
        for v in ("0.2.0", "0.2.10", "0.3.0", "1.0.0", "0.2.0+build.5"):
            self.assertEqual(f(v.encode(), 0), 1, v)
        for v in ("0.1.2", "0.1.10", "0.1.1", "0.0.9", "0.2", "abc", "", "0.2.0x", "0.2.0-", "0.2.0 ", "-0.2.0",
                  " 0.2.0", "0.+2.0", "0.2.99999999999999999999"):
            self.assertEqual(f(v.encode(), 1), 0, v)
        self.assertEqual(f(None, 1), 0)
        # pre-releases: only with the testing override
        self.assertEqual(f(b"0.2.0-rc.1", 0), 0)
        self.assertEqual(f(b"0.2.0-rc.1", 1), 1)
        self.assertEqual(f(b"0.1.2-rc1", 1), 0)
        self.assertEqual(f(b"0.2.0-alpha.1+x", 0), 0)

    def test_abi(self):
        # the run-time check every PCM run makes: the linked library's dsd2pcm_abi_version() must be
        # the DSD2PCM_ABI_VERSION the engine was compiled with (1); a run that gets this far passed it
        f = _native._load().sacdconv_dsd2pcm_abi_ok
        f.argtypes, f.restype = [ctypes.c_uint32], ctypes.c_int
        self.assertEqual(f(1), 1)
        for abi in (0, 2, 0xFFFFFFFF):
            self.assertEqual(f(abi), 0, abi)
        src = (ROOT / "csrc" / "sacdconv.c").read_text()
        body = src[src.index("static int dsd2pcm_library_ok("):]
        body = body[:body.index("\n}\n")]
        self.assertIn("sacdconv_dsd2pcm_abi_ok(abi)", body)
        self.assertIn("dsd2pcm_abi_version()", body)
        # checked once per run, before the probe converter, not in each worker's converter
        conv = src[src.index("static dsd2pcm_frames *pcm_converter("):]
        conv = conv[:conv.index("\n}\n")]
        self.assertNotIn("getenv", conv)
        self.assertNotIn("dsd2pcm_library_ok", conv)

    def test_build_info_names_the_linked_dsd2pcm_and_the_default_filters(self):
        info = _native.build_info()
        m = re.search(r"PCM by dsd2pcm ([^,\s]+),", info)
        self.assertIsNotNone(m, info)
        lock = tool_module("dsd2pcm").load_lock()
        linked = m.group(1)
        if os.environ.get("PYSACD_DSD2PCM_ALLOW_PRERELEASE") == "1":   # testing a candidate
            linked = linked.split("-", 1)[0]
        self.assertEqual(linked, lock["version"], info)
        self.assertIn("PCM by default: precise with the standard cutoff (40 kHz) at 88.2 kHz; precise with the "
                      "standard cutoff (40 kHz) at 176.4 kHz", info)

    def test_version_shows_the_filters(self):
        done = subprocess.run([sys.executable, "-m", "pysacd", "--version"], capture_output=True, text=True,
                              check=True)
        self.assertIn("PCM by default: precise with the standard cutoff", done.stdout)

    def test_gain_error_names_the_problem_once(self):
        # the engine's own check (run_area refuses such a gain before it reaches the engine)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(_native, "check_gain_db", float):
            iso = Path(tmp) / "g.iso"
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [bytes(2 * FRAME)] * 3, [3], dst=False)])
            with SACDImage(iso) as img, self.assertRaises(_native.NativeError) as cm:
                _native.run_area(img, iso, read_toc(iso).stereo,
                                 [_native.OutputSpec("wav", Path(tmp) / "g.wav", 0, 3)], 88200, 30.0)
            msg = str(cm.exception)
            self.assertIn("gain", msg.lower())
            self.assertEqual(msg.lower().count("invalid argument"), 1, msg)


class GainArgument(unittest.TestCase):
    def test_cli_range(self):
        import argparse
        for ok in ("-120", "24", "0", "-3.5", "+6"):
            _native.gain_db_arg(ok)
        for bad in ("-120.5", "24.01", "nan", "inf", "loud"):
            with self.assertRaises(argparse.ArgumentTypeError):
                _native.gain_db_arg(bad)
        for cmd in ([sys.executable, "-m", "pysacd", "convert", "--help"],
                    [sys.executable, "-m", "pysacd.extract", "--help"]):
            out = subprocess.run(cmd, capture_output=True, text=True, check=False).stdout
            self.assertIn("-120 to +24", " ".join(out.split()), cmd)
        done = subprocess.run([sys.executable, "-m", "pysacd.extract", "-i", "x.iso", "--wav", "--gain-db", "30"],
                              capture_output=True, text=True, check=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("outside -120 to +24 dB", done.stderr)


FAKE_CARGO = """#!{python}
import os, sys
sys.stdout.write(os.environ["FAKE_CARGO_TREE"])
sys.exit(int(os.environ.get("FAKE_CARGO_RC", "0")))
"""


@unittest.skipIf(os.name == "nt", "the fake cargo is a script with a #! line")
class Licences(unittest.TestCase):
    """check_licences against a fake `cargo tree`."""

    def setUp(self):
        self.t = tool_module("dsd2pcm")

    def check(self, tree: str, rc: int = 0) -> None:
        t = self.t
        with tempfile.TemporaryDirectory() as tmp:
            cargo = Path(tmp) / "cargo"
            cargo.write_text(FAKE_CARGO.format(python=sys.executable))
            cargo.chmod(0o755)
            env = dict(os.environ, FAKE_CARGO_TREE=tree, FAKE_CARGO_RC=str(rc))
            t.check_licences(str(cargo), Path(tmp), "x86_64-unknown-linux-gnu", env)

    def refused(self, tree: str, rc: int = 0) -> None:
        with self.assertRaises(self.t.FetchError, msg=tree):
            self.check(tree, rc)

    def test_alternatives(self):
        self.check("dsd2pcm v0.1.2\tMIT OR Apache-2.0\ndsd2pcm-capi v0.1.2\tMIT OR Apache-2.0\n")
        self.check("a v1.0.0\tApache-2.0 OR MIT\nb v1.0.0\t(MIT OR Apache-2.0)\nc v2.0.0\tMIT/Apache-2.0\n"
                   "d v0.1.0\tZlib\ne v0.1.0\tBSD-3-Clause OR Apache-2.0\n")

    def test_refusals(self):
        self.refused("claxon v0.4.3\tApache-2.0\n")
        self.refused("x v1.0.0\t\n")
        self.refused("x v1.0.0\tGPL-3.0-only\n")
        self.refused("x v1.0.0\tMPL-2.0 OR LGPL-2.1\n")
        # AND and WITH need a review, even with an MIT alternative inside
        self.refused("x v1.0.0\t(MIT OR Apache-2.0) AND Unicode-3.0\n")
        self.refused("x v1.0.0\tMIT AND BSD-3-Clause\n")
        self.refused("x v1.0.0\tApache-2.0 WITH LLVM-exception OR MIT\n")
        self.refused("x v1.0.0\tMIT with Foo-exception\n")
        self.refused("dsd2pcm v0.1.2\tMIT OR Apache-2.0\n", rc=101)

    def test_reviewed_list(self):
        t = tool_module("dsd2pcm")
        tree = "x v1.0.0\t(MIT OR Apache-2.0) AND Unicode-3.0\n"
        t.REVIEWED_LICENCES["x (MIT OR Apache-2.0) AND Unicode-3.0"] = "MIT"
        with tempfile.TemporaryDirectory() as tmp:
            cargo = Path(tmp) / "cargo"
            cargo.write_text(FAKE_CARGO.format(python=sys.executable))
            cargo.chmod(0o755)
            t.check_licences(str(cargo), Path(tmp), "x86_64-unknown-linux-gnu", dict(os.environ, FAKE_CARGO_TREE=tree))


class Prereleases(unittest.TestCase):
    def test_build_gate(self):
        t = tool_module("dsd2pcm")
        lock = t.load_lock()
        env = os.environ.pop(t.ALLOW_PRERELEASE, None)
        try:
            for v in ("0.2.0-rc.1", "0.2.0-alpha.1", "1.0.0-0"):
                with self.assertRaises(t.FetchError):
                    t.check_version(lock, v)
            t.check_version(lock, "0.2.0+meta")
            os.environ[t.ALLOW_PRERELEASE] = "1"
            t.check_version(lock, "0.2.0-rc.1")
            with self.assertRaises(t.FetchError):
                t.check_version(lock, "0.1.2-rc1")
        finally:
            os.environ.pop(t.ALLOW_PRERELEASE, None)
            if env is not None:
                os.environ[t.ALLOW_PRERELEASE] = env


class SourceArchives(unittest.TestCase):
    """The two kinds of pinned source: GitHub's archive of the repository, and the C library's own
    source archive that dsd2pcm releases attach from 0.2.0 on (built offline)."""

    def test_names_and_urls(self):
        t = tool_module("dsd2pcm")
        base = {"repository": "dsd-toolbox/dsd2pcm-rs", "version": "0.2.0", "min_version": "0.2.0", "abi": 1,
                "commit": "a" * 40, "assets": {}, "rust": {}}
        repo = dict(base, source={"ref": "v0.2.0", "sha256": "0" * 64})
        self.assertFalse(t.capi_src(repo))
        self.assertEqual(t.source_name(repo), "dsd2pcm-rs-0.2.0.tar.gz")
        self.assertEqual(t.source_top(repo), "dsd2pcm-rs-0.2.0")
        self.assertEqual(t.source_url(repo), "https://github.com/dsd-toolbox/dsd2pcm-rs/archive/refs/tags/v0.2.0.tar.gz")
        capi = dict(base, source={"ref": "v0.2.0", "archive": "capi-src", "sha256": "0" * 64})
        self.assertTrue(t.capi_src(capi))
        self.assertEqual(t.source_name(capi), "libdsd2pcm-0.2.0-src.tar.gz")
        self.assertEqual(t.source_top(capi), "libdsd2pcm-0.2.0")
        self.assertEqual(t.source_url(capi), "https://github.com/dsd-toolbox/dsd2pcm-rs/releases/download/v0.2.0/"
                                             "libdsd2pcm-0.2.0-src.tar.gz")
        with self.assertRaises(t.FetchError):
            t.capi_src(dict(base, source={"ref": "v0.2.0", "archive": "zip", "sha256": "0" * 64}))


class CargoFlags(unittest.TestCase):
    def test_encoded_rustflags_keep_spaces(self):
        t = tool_module("dsd2pcm")
        src = Path("/tmp/with space/src")
        remap = f"--remap-path-prefix={src}=/dsd2pcm"     # the platform's own form of the path
        target_dir = Path("/tmp/with space/target")
        env = t.cargo_env("x86_64-unknown-linux-gnu", src, Path("/tmp/with space"))
        self.assertNotIn("RUSTFLAGS", env)
        self.assertEqual(env["CARGO_ENCODED_RUSTFLAGS"].split("\x1f"),
                         ["-C", "target-cpu=x86-64", f"--remap-path-prefix={target_dir}=/target", remap])
        self.assertEqual(t.rustflags("aarch64-unknown-linux-gnu", src).split("\x1f"), [remap])

    @unittest.skipUnless(shutil.which(os.environ.get("CARGO", "cargo")), "cargo not on PATH")
    def test_build_in_a_temporary_directory_with_a_space(self):
        # the whole fetch: unpack, cargo tree, cargo rustc, all under TMPDIR="... a b"
        with tempfile.TemporaryDirectory() as tmp:
            spaced = Path(tmp) / "a b"
            spaced.mkdir()
            env = dict(os.environ, TMPDIR=str(spaced), TEMP=str(spaced), TMP=str(spaced))
            env.pop("LD_PRELOAD", None)     # the sanitizer jobs' runtimes are for the engine, not cargo
            done = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "dsd2pcm.py"), "fetch", "--dest",
                                   str(Path(tmp) / "out")], env=env, capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr[-3000:])
            lib = (Path(tmp) / "out" / "lib" / "libdsd2pcm.a").read_bytes()
            self.assertNotIn(str(spaced).encode(), lib)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        sys.exit(_child(Path(sys.argv[2])))
    unittest.main()
