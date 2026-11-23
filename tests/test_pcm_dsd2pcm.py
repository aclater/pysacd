"""PCM output through dsd2pcm's pysacd filter.

- Golden: the engine's WAV output of a generated disc equals, byte for byte, the reference that
  dsd2pcm's pysacd filter is checked against: pysacd's filter (tools/pcm_vectors/dsp.c) built as
  the `ref` variant, with the dither seeded per frame (tools/pcm_vectors, `frame`).
- Determinism: the same bytes at any thread count, for any range start, and for a track alone
  or inside a whole-area run.
- Bounded change: against the filter as pysacd built it before (wheel flags, dither seeded per
  batch), no sample moves by more than 7 LSB (the build's own spread) plus 2 (the dither).
- The build links nothing under a licence pysacd can't take (the FLAC reader of dsd2pcm's
  default features, claxon, is Apache-2.0 only).
"""

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pysacd import SACDImage
from pysacd.toc import read_toc

sys.path.insert(0, os.path.dirname(__file__))
import sacdgen

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "tools" / "pcm_vectors"
FRAME = 4704
OPF = {88200: 1176, 176400: 2352}

try:
    from pysacd import _native
    _native._load()
    HAVE_NATIVE = True
except Exception:  # noqa: BLE001 - any failure to load means "not built"; tests skip
    HAVE_NATIVE = False


def build_vectors(out: Path) -> dict[str, Path]:
    """pcm_vectors' ref and wheel builds (tools/pcm_vectors/build.sh, into out)."""
    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc") or "cc"
    tools = {}
    for name, flags in (("ref", ["-O2", "-ffp-contract=off"]),
                        ("wheel", ["-O3", "-ffast-math", "-fno-finite-math-only", "-DSC_DISPATCH"])):
        tools[name] = out / f"pcm_vectors_{name}"
        subprocess.run([cc, *flags, "-std=gnu17", "-Wall", f"-I{ROOT / 'csrc'}", "-o", str(tools[name]),
                        str(VECTORS / "pcm_vectors.c"), "-lm"], check=True)
    return tools


def make_inputs(out: Path) -> None:
    subprocess.run([sys.executable, "-I", str(VECTORS / "make_inputs.py"), str(out)], check=True)


def frames_of(raw: bytes, channels: int) -> list[bytes]:
    fb = channels * FRAME
    return [raw[i:i + fb] for i in range(0, len(raw), fb)]


def wav_samples(path: Path) -> list[int]:
    """The 24-bit samples of a WAV file pysacd wrote, interleaved."""
    data = path.read_bytes()
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"data":
            body = data[pos + 8:pos + 8 + size]
            return [int.from_bytes(body[i:i + 3], "little", signed=True) for i in range(0, len(body), 3)]
        pos += 8 + size + (size & 1)
    raise AssertionError(f"{path}: no data chunk")


def wav_data(path: Path) -> bytes:
    data = path.read_bytes()
    i = data.index(b"data")
    return data[i + 8:i + 8 + struct.unpack_from("<I", data, i + 4)[0]]


def i32_as_s24(raw: bytes) -> bytes:
    n = len(raw) // 4
    return b"".join(v.to_bytes(3, "little", signed=True) for v in struct.unpack(f"<{n}i", raw))


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class PcmGolden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.tools = build_vectors(cls.dir)
        make_inputs(cls.dir / "inputs")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def engine_wav(self, raw: bytes, channels: int, rate: int, gain: float, name: str) -> Path:
        frames = frames_of(raw, channels)
        iso = self.dir / f"{name}.iso"
        sacdgen.build_iso(iso, [sacdgen.AreaSpec(channels, frames, [len(frames)], dst=False)])
        toc = read_toc(iso)
        area = toc.stereo if channels == 2 else toc.multichannel
        out = self.dir / f"{name}.wav"
        with SACDImage(iso) as img:
            _native.run_area(img, iso, area, [_native.OutputSpec("wav", out, 0, len(frames))], rate, gain)
        return out

    def reference(self, tool: str, src: Path, channels: int, rate: int, gain: float, seeding: str) -> bytes:
        f32, i32 = self.dir / "o.f32", self.dir / "o.i32"
        subprocess.run([str(self.tools[tool]), str(src), str(channels), str(rate), str(gain), "0", str(f32),
                        str(i32), seeding], check=True)
        return i32.read_bytes()

    def test_golden(self):
        # Every input, both channel counts and rates, 0 and -3 dB: byte-identical to the reference
        for channels in (2, 6):
            for kind in ("silence", "ff", "music"):
                src = self.dir / "inputs" / f"{kind}_{channels}ch.dsd"
                for rate in (88200, 176400):
                    for gain in (0.0, -3.0):
                        with self.subTest(kind=kind, channels=channels, rate=rate, gain=gain):
                            wav = self.engine_wav(src.read_bytes(), channels, rate, gain, f"{kind}{channels}_{rate}_{gain}")
                            want = i32_as_s24(self.reference("ref", src, channels, rate, gain, "frame"))
                            self.assertEqual(len(wav_data(wav)), len(want))
                            self.assertTrue(wav_data(wav) == want, "PCM differs from the reference")

    def test_bounded_change_from_the_old_filter(self):
        # The old engine on a 20-frame area: one batch from frame 0, the filter built with the wheel
        # flags, the dither seeded once from frame 0 (pcm_vectors' wheel build, `batch`)
        for channels in (2, 6):
            src = self.dir / "inputs" / f"music_{channels}ch.dsd"
            for rate in (88200, 176400):
                with self.subTest(channels=channels, rate=rate):
                    new = wav_samples(self.engine_wav(src.read_bytes(), channels, rate, 0.0, f"m{channels}_{rate}"))
                    raw = self.reference("wheel", src, channels, rate, 0.0, "batch")
                    old = list(struct.unpack(f"<{len(raw) // 4}i", raw))
                    self.assertEqual(len(new), len(old))
                    diff = [abs(a - b) for a, b in zip(new, old)]
                    self.assertLessEqual(max(diff), 9)
                    # frame 0 has the same dither seed both ways: only the build's spread
                    self.assertLessEqual(max(diff[:OPF[rate] * channels]), 7)


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class PcmDeterminism(unittest.TestCase):
    """Bytes depend only on the disc and the position: not on threads, batches or range start."""

    FRAMES = 75          # more than two batches of 32
    TRACKS = (20, 31, 24)

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

    def run_range(self, channels: int, rate: int, lo: int, hi: int, threads: int = 0) -> bytes:
        iso, area = self.areas[channels]
        out = self.dir / "r.wav"
        out.unlink(missing_ok=True)
        with SACDImage(iso) as img:
            _native.run_area(img, iso, area, [_native.OutputSpec("wav", out, lo, hi)], rate, 0.0, threads)
        return wav_data(out)

    def test_threads_ranges_and_tracks(self):
        for channels in (2, 6):
            for rate in (88200, 176400):
                per_frame = OPF[rate] * channels * 3
                whole = self.run_range(channels, rate, 0, self.FRAMES, threads=1)
                with self.subTest(channels=channels, rate=rate, what="threads"):
                    for threads in (2, 3, 8):
                        self.assertTrue(self.run_range(channels, rate, 0, self.FRAMES, threads) == whole)
                with self.subTest(channels=channels, rate=rate, what="range starts"):
                    # starts around the batch edges (32 frames) and the context (2 frames)
                    for lo in (1, 2, 3, 31, 32, 33, 34, 63, 64, 74):
                        got = self.run_range(channels, rate, lo, self.FRAMES)
                        self.assertTrue(got == whole[lo * per_frame:], f"from frame {lo}")
                    for hi in (6, 33, 65):
                        got = self.run_range(channels, rate, 5, hi)
                        self.assertTrue(got == whole[5 * per_frame:hi * per_frame], f"frames 5-{hi}")
                with self.subTest(channels=channels, rate=rate, what="tracks"):
                    iso, area = self.areas[channels]
                    specs = []
                    for t in area.tracks:
                        start = t.start.total_frames
                        specs.append(_native.OutputSpec("wav", self.dir / f"t{t.number}.wav", start,
                                                        start + t.duration.total_frames))
                    for s in specs:
                        Path(s.path).unlink(missing_ok=True)
                    with SACDImage(iso) as img:
                        _native.run_area(img, iso, area, specs, rate, 0.0)
                    alone = []
                    for s in specs:
                        inside = wav_data(Path(s.path))
                        alone.append(self.run_range(channels, rate, s.tc_start, s.tc_end))
                        self.assertTrue(inside == whole[s.tc_start * per_frame:s.tc_end * per_frame])
                        self.assertTrue(alone[-1] == inside)
                    self.assertTrue(b"".join(alone) == whole)


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class PcmSettings(unittest.TestCase):
    def test_gain_out_of_range_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso = Path(tmp) / "g.iso"
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [bytes(2 * FRAME)] * 3, [3], dst=False)])
            area = read_toc(iso).stereo
            for gain in (-121.0, 24.5):
                with SACDImage(iso) as img, self.assertRaises(_native.NativeError) as cm:
                    _native.run_area(img, iso, area, [_native.OutputSpec("wav", Path(tmp) / "g.wav", 0, 3)],
                                     88200, gain)
                self.assertIn("gain", str(cm.exception).lower())


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


if __name__ == "__main__":
    unittest.main()
