#!/usr/bin/env python3
"""The PCM goldens: the SHA-256 of the engine's 24-bit PCM for generated inputs, per filter,
cutoff, rate, channel count and gain (tests/fixtures/pcm-goldens.json).

    PYTHONPATH=src python3 tools/pcm_goldens.py write [--with-cli | --dsd2pcm-cli PATH]
    PYTHONPATH=src python3 tools/pcm_goldens.py check

`write` converts every case with the engine (one thread) and writes the file. With --with-cli,
it first builds dsd2pcm's own command-line tool at the commit pinned in dsd2pcm.lock: the C
library source archive that pins it ("capi-src") has no command-line tool, so the tool comes from
the repository at that commit (tools/dsd2pcm.py repo_tree: GitHub's archive of the commit, or a
copy in $PYSACD_DSD2PCM_CACHE named dsd2pcm-rs-<commit>.tar.gz, refused unless its library
sources are byte-identical to the pinned, digest-checked archive's). `cargo build --release
--locked -p dsd2pcm-cli` needs crates.io for the tool's own dependencies; it runs with a CARGO_HOME
of its own. Then it converts each case's area again with the tool, from a DSF of the same area,
and refuses to write unless the bytes are the same: the goldens are then dsd2pcm's whole-file
conversion, not only what the engine's batches made. The tool's version and commit go into the
goldens file (tests/test_pcm_dsd2pcm.py compares them with dsd2pcm.lock). --dsd2pcm-cli uses a
tool already built, at the pinned commit. Run it with that check whenever the pin in
dsd2pcm.lock moves (docs/development.md, "The dsd2pcm library").

`check` converts every case again and compares; tests/test_pcm_dsd2pcm.py does the same, and
also at every SIMD level, thread count and range start.

Inputs, generated here the same on every platform (integer and IEEE double arithmetic only, no
libm): DSD silence (0x69), all ones, synthetic music (20 frames, stereo and 6 channels: a 1 kHz plus
a 7 kHz sine through a first-order 1-bit modulator, as tools/pcm_vectors/make_inputs.py makes it,
but with the sines from a rotation whose constants are written out, since libm's sin differs between
platforms), and 97 frames of seeded noise (three batches of 32 and one frame more). The goldens file
records each input's SHA-256, so a changed input is reported as such.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDENS = ROOT / "tests" / "fixtures" / "pcm-goldens.json"
FRAME = 4704
LONG_FRAMES = 97

# Every filter and cutoff pysacd offers, at each rate it writes
COMBOS = [(88200, "precise", "standard"), (88200, "precise", "low"), (88200, "pysacd", "standard"),
          (176400, "precise", "standard"), (176400, "precise", "low"), (176400, "precise", "wide"),
          (176400, "pysacd", "standard")]


@dataclass(frozen=True)
class Case:
    source: str          # silence, ff, music (20 frames) or noise (97 frames)
    channels: int
    rate: int
    filter: str
    cutoff: str
    gain: float

    @property
    def name(self) -> str:
        return f"{self.source}-{self.channels}ch-{self.rate}-{self.filter}-{self.cutoff}-{self.gain:+g}dB"


def cases() -> list[Case]:
    out = []
    for channels in (2, 6):
        for rate, filt, cut in COMBOS:
            for source in ("silence", "ff", "music"):
                for gain in (0.0, -3.0):
                    out.append(Case(source, channels, rate, filt, cut, gain))
            out.append(Case("noise", channels, rate, filt, cut, 0.0))
        # gain above 0 dB, and clipping (+24 dB on noise and all-ones), for each filter
        for filt in ("precise", "pysacd"):
            for source in ("music", "ff", "noise"):
                for gain in (6.0, 24.0):
                    out.append(Case(source, channels, 88200, filt, "standard", gain))
    return out


def noise_frames(channels: int, count: int = LONG_FRAMES) -> list[bytes]:
    rng = random.Random(9700 + channels)
    return [rng.randbytes(channels * FRAME) for _ in range(count)]


SHORT_FRAMES = 20
# cos and sin of 2 pi f / 2822400 for f = 1 kHz and 7 kHz, and of the channel phases 0.3 c rad
TONES = ((float.fromhex("0x1.ffffacda93a18p-1"), float.fromhex("0x1.23ca52113ba91p-9")),
         (float.fromhex("0x1.fff015eaeedaep-1"), float.fromhex("0x1.fe9ce0d86cd11p-7")))
PHASES = ((1.0, 0.0),
          (float.fromhex("0x1.e921dd42f09bap-1"), float.fromhex("0x1.2e9cd95baba33p-2")),
          (float.fromhex("0x1.a69263c485b15p-1"), float.fromhex("0x1.2118d17a54159p-1")),
          (float.fromhex("0x1.3e43a9692e21dp-1"), float.fromhex("0x1.91103985da840p-1")),
          (float.fromhex("0x1.730de943b79d4p-2"), float.fromhex("0x1.dd343a21a55c4p-1")),
          (float.fromhex("0x1.21bd54fc5f9a7p-4"), float.fromhex("0x1.feb7a9b2c6d8bp-1")))


def modulate(nbits: int, phase: tuple[float, float]) -> bytes:
    """0.25 sin(w1 i + p) + 0.25 sin(w2 i + p) through a first-order 1-bit modulator, MSB first."""
    out = bytearray(nbits // 8)
    osc = [list(phase), list(phase)]            # (cos, sin) of each tone's angle, rotated each bit
    integ, prev = 0.0, 1.0
    for i in range(nbits):
        x = 0.25 * osc[0][1] + 0.25 * osc[1][1]
        integ += x - prev
        bit = 1 if integ >= 0.0 else 0
        prev = 1.0 if bit else -1.0
        out[i >> 3] |= bit << (7 - (i & 7))
        for o, (c, s) in zip(osc, TONES):
            o[0], o[1] = o[0] * c - o[1] * s, o[0] * s + o[1] * c
    return bytes(out)


def music_frames(channels: int) -> list[bytes]:
    n = SHORT_FRAMES * FRAME
    chans = [modulate(n * 8, PHASES[c]) for c in range(channels)]
    frames = []
    for f in range(SHORT_FRAMES):
        frame = bytearray(FRAME * channels)
        for c in range(channels):
            frame[c::channels] = chans[c][f * FRAME:(f + 1) * FRAME]
        frames.append(bytes(frame))
    return frames


def make_inputs() -> dict[tuple[str, int], list[bytes]]:
    """Each source's frames, by (source, channels)."""
    out = {}
    for channels in (2, 6):
        out[("silence", channels)] = [b"\x69" * (FRAME * channels)] * SHORT_FRAMES
        out[("ff", channels)] = [b"\xff" * (FRAME * channels)] * SHORT_FRAMES
        out[("music", channels)] = music_frames(channels)
        out[("noise", channels)] = noise_frames(channels)
    return out


def input_hashes(inputs: dict[tuple[str, int], list[bytes]]) -> dict[str, str]:
    return {f"{s}-{c}ch": hashlib.sha256(b"".join(fr)).hexdigest() for (s, c), fr in sorted(inputs.items())}


def wav_data(path: Path) -> bytes:
    data = path.read_bytes()
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"data":
            return data[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    raise ValueError(f"{path}: no data chunk")


class Discs:
    """One generated disc per input (built once), and conversions of its area."""

    def __init__(self, work: Path):
        sys.path.insert(0, str(ROOT / "tests"))
        import sacdgen
        self.sacdgen = sacdgen
        self.work = work
        self.inputs = make_inputs()
        self.isos: dict[tuple[str, int], Path] = {}

    def iso(self, source: str, channels: int) -> Path:
        key = (source, channels)
        if key not in self.isos:
            frames = self.inputs[key]
            path = self.work / f"{source}_{channels}.iso"
            self.sacdgen.build_iso(path, [self.sacdgen.AreaSpec(channels, frames, [len(frames)], dst=False)])
            self.isos[key] = path
        return self.isos[key]

    def area(self, source: str, channels: int):
        from pysacd.toc import read_toc
        toc = read_toc(self.iso(source, channels))
        return toc.stereo if channels == 2 else toc.multichannel

    def frames(self, source: str, channels: int) -> int:
        return len(self.inputs[(source, channels)])

    def engine(self, c: Case, threads: int = 1, fmt: str = "wav") -> Path:
        from pysacd import SACDImage, _native
        iso = self.iso(c.source, c.channels)
        out = self.work / f"engine.{fmt}"
        out.unlink(missing_ok=True)
        with SACDImage(iso) as img:
            _native.run_area(img, iso, self.area(c.source, c.channels),
                             [_native.OutputSpec(fmt, out, 0, self.frames(c.source, c.channels))],
                             c.rate, c.gain, threads, pcm_filter=c.filter, pcm_cutoff=c.cutoff)
        return out

    def engine_pcm(self, c: Case, threads: int = 1) -> bytes:
        return wav_data(self.engine(c, threads))

    def cli_pcm(self, cli: Path, c: Case) -> bytes:
        dsf = self.work / f"{c.source}_{c.channels}.dsf"
        if not dsf.exists():
            dsf = self.engine(Case(c.source, c.channels, 88200, "precise", "standard", 0.0), fmt="dsf")
            dsf = dsf.rename(self.work / f"{c.source}_{c.channels}.dsf")
        out = self.work / "cli.wav"
        out.unlink(missing_ok=True)
        cutoff = {"standard": "40", "low": "30", "wide": "wide"}[c.cutoff]
        subprocess.run([str(cli), "--rate", str(c.rate), "--cutoff", cutoff, "--filter", c.filter, "--gain",
                        str(c.gain), "--single-thread", str(dsf), str(out)], check=True, capture_output=True)
        return wav_data(out)


def d2p_tool():
    d2p = importlib.util.spec_from_file_location("_pysacd_dsd2pcm_tool", ROOT / "tools" / "dsd2pcm.py")
    assert d2p and d2p.loader
    t = importlib.util.module_from_spec(d2p)
    d2p.loader.exec_module(t)
    return t


def build_cli(work: Path) -> tuple[Path, dict]:
    """dsd2pcm's command-line tool, built from the repository at the commit pinned in dsd2pcm.lock,
    checked against the pinned source (tools/dsd2pcm.py repo_tree), and its version and commit
    (the tool has no --version: the version is its workspace's, which repo_tree checked)."""
    import os
    import shutil
    t = d2p_tool()
    lock = t.load_lock()
    cargo = os.environ.get("CARGO") or shutil.which("cargo")
    if not cargo:
        raise SystemExit("pcm_goldens: --with-cli needs cargo on PATH")
    try:
        src = t.repo_tree(lock, work)
    except t.FetchError as e:
        raise SystemExit(f"pcm_goldens: {e}") from None
    env = t.cargo_env(t.host_triple(), src, work, keep_index=True)
    env.pop("CARGO_ENCODED_RUSTFLAGS", None)
    env["CARGO_TARGET_DIR"] = str(work / "cli-target")
    subprocess.run([cargo, "build", "--release", "--locked", "-p", "dsd2pcm-cli"], cwd=src, env=env, check=True)
    exe = work / "cli-target" / "release" / ("dsd2pcm.exe" if os.name == "nt" else "dsd2pcm")
    if not exe.is_file():
        raise SystemExit(f"pcm_goldens: no {exe}")
    return exe, {"version": t.workspace_version(src), "commit": lock["commit"]}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load() -> dict:
    return json.loads(GOLDENS.read_text(encoding="utf-8"))


def write(cli: Path | None, with_cli: bool = False) -> int:
    got = {}
    lock = json.loads((ROOT / "dsd2pcm.lock").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        # a tool built here names its version and commit; one given with --dsd2pcm-cli can't be
        # vouched for, and the goldens test refuses goldens checked only against such a tool
        cli_info: dict | None = None
        if with_cli:
            cli, cli_info = build_cli(Path(tmp))
        elif cli is not None:
            cli_info = {"version": None, "commit": None}
        d = Discs(Path(tmp))
        for c in cases():
            pcm = d.engine_pcm(c)
            if cli is not None and d.cli_pcm(cli, c) != pcm:
                print(f"{c.name}: the engine's PCM differs from dsd2pcm's command-line tool", file=sys.stderr)
                return 1
            got[c.name] = sha(pcm)
    doc = {"comment": "SHA-256 of the engine's 24-bit PCM per case (tools/pcm_goldens.py); regenerate with "
                      "`tools/pcm_goldens.py write --with-cli` when dsd2pcm.lock moves",
           "dsd2pcm": {"version": lock["version"], "commit": lock["commit"]},
           "checked_against_dsd2pcm_cli": cli is not None,
           "dsd2pcm_cli": cli_info,
           "inputs": input_hashes(d.inputs),
           "cases": got}
    GOLDENS.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(f"{GOLDENS.relative_to(ROOT)}: {len(got)} cases"
          f"{', each equal to dsd2pcm command-line output' if cli else ''}")
    return 0


def check() -> int:
    doc = load()
    want = doc["cases"]
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        d = Discs(Path(tmp))
        if input_hashes(d.inputs) != doc["inputs"]:
            print("the generated inputs differ from the ones the goldens were made from", file=sys.stderr)
            return 1
        for c in cases():
            if want.get(c.name) != sha(d.engine_pcm(c)):
                print(f"{c.name}: differs from the golden", file=sys.stderr)
                bad += 1
    print(f"{len(cases()) - bad} of {len(cases())} cases match")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="convert every case and write the goldens")
    how = w.add_mutually_exclusive_group()
    how.add_argument("--with-cli", action="store_true",
                     help="build dsd2pcm's command-line tool from the pinned source; every case must equal its output")
    how.add_argument("--dsd2pcm-cli", type=Path, help="the same with a tool already built")
    sub.add_parser("check", help="convert every case and compare with the goldens")
    a = ap.parse_args(argv)
    return write(a.dsd2pcm_cli, a.with_cli) if a.cmd == "write" else check()


if __name__ == "__main__":
    sys.exit(main())
