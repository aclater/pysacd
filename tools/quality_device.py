#!/usr/bin/env python3
"""Run `pysacd convert` as a DSD-to-PCM device for the release quality comparison.

The measurement tool hands a device a DSF file and expects a WAV or FLAC file back. pysacd reads
SACD images, so this wraps the DSF's DSD into a one-track plain-DSD image with tests/sacdgen.py,
converts it with `pysacd convert`, and writes the chosen channels to the output path:

    python3 -I tools/quality_device.py --format wav --rate 88200 --area stereo IN.dsf OUT.wav

Only the standard library is used, so any Python 3.10+ runs it, and the pysacd under test is the
`pysacd` on PATH (or --pysacd). See docs/development.md, "Release quality comparison".
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import sacdgen

FRAME_BYTES = sacdgen.FRAME_BYTES          # DSD bytes per channel per 1/75 s frame at DSD64
DSD64 = 2822400
IDLE = 0x69                                # the DSD idle pattern, MSB first as on a disc
AREA_CHANNELS = {"stereo": 2, "multichannel": 6}
# DSF stores bits LSB first; a disc stores them MSB first
REVERSE = bytes(int(f"{b:08b}"[::-1], 2) for b in range(256))


def read_dsf(path: Path) -> tuple[list[bytes], int]:
    """A DSF file's channels as MSB-first byte strings (one per channel), and the sample count."""
    data = path.read_bytes()
    if data[:4] != b"DSD " or data[28:32] != b"fmt ":
        raise ValueError(f"{path}: not a DSF file")
    (_, _, _, fmt_id, _, channels, rate, bits, samples, block) = struct.unpack_from("<4sQIIIIIIQI", data, 28)
    if fmt_id != 0 or bits != 1 or not 1 <= channels <= 6 or block <= 0:
        raise ValueError(f"{path}: unsupported DSF (format {fmt_id}, {bits} bits per sample, {channels} channels)")
    if rate != DSD64:
        raise ValueError(f"{path}: {rate} Hz; an SACD carries DSD64 ({DSD64} Hz) only")
    pos = 28 + struct.unpack_from("<Q", data, 32)[0]
    if data[pos:pos + 4] != b"data":
        raise ValueError(f"{path}: no data chunk")
    size = struct.unpack_from("<Q", data, pos + 4)[0] - 12
    raw = data[pos + 12:pos + 12 + size]
    nbytes = (samples + 7) // 8
    stride = block * channels
    chans = []
    for c in range(channels):
        parts = [raw[off + c * block:off + (c + 1) * block] for off in range(0, len(raw) - stride + 1, stride)]
        chans.append(b"".join(parts)[:nbytes].translate(REVERSE))
    if any(len(ch) < nbytes for ch in chans):
        raise ValueError(f"{path}: data chunk shorter than its sample count")
    return chans, samples


def frames_for(chans: list[bytes], area_channels: int, lead_frames: int) -> list[bytes]:
    """Plain DSD frames (bytes interleaved by channel) for an area of ``area_channels``.

    Area channel k carries input channel k modulo the input's channel count. ``lead_frames``
    frames of idle pattern come first; the end is padded with it to a whole frame.
    """
    lead = bytes([IDLE]) * (lead_frames * FRAME_BYTES)
    nframes = lead_frames + -(-len(chans[0]) // FRAME_BYTES)
    total = nframes * FRAME_BYTES
    streams = [(lead + ch).ljust(total, bytes([IDLE])) for ch in chans]
    frames = []
    for f in range(nframes):
        buf = bytearray(area_channels * FRAME_BYTES)
        for k in range(area_channels):
            buf[k::area_channels] = streams[k % len(streams)][f * FRAME_BYTES:(f + 1) * FRAME_BYTES]
        frames.append(bytes(buf))
    return frames


def wav_pcm(path: Path) -> tuple[bytes, int, int, int]:
    """A WAV file's data chunk, channel count, rate and bits per sample."""
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"{path}: not a WAV file")
    pos, fmt, body = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"fmt ":
            fmt = struct.unpack_from("<HHIIHH", data, pos + 8)
        elif cid == b"data":
            body = data[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    if fmt is None or body is None:
        raise ValueError(f"{path}: no fmt or data chunk")
    return body, fmt[1], fmt[2], fmt[5]


def write_wav(path: Path, body: bytes, channels: int, rate: int, bits: int) -> None:
    width = bits // 8
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * width, channels * width, bits)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(body) + (len(body) & 1)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        f.write(b"data" + struct.pack("<I", len(body)) + body + b"\0" * (len(body) & 1))


def pick(body: bytes, channels: int, width: int, keep: list[int], skip_frames: int) -> bytes:
    """The interleaved samples of channels ``keep`` (0-based), without the first ``skip_frames``."""
    step = channels * width
    body = body[skip_frames * step:]
    if keep == list(range(channels)):
        return body
    out = bytearray(len(body) // step * len(keep) * width)
    for j, k in enumerate(keep):
        for b in range(width):
            out[j * width + b::len(keep) * width] = body[k * width + b::step]
    return bytes(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("input", type=Path, help="DSF file (DSD64)")
    ap.add_argument("output", type=Path, help="WAV or FLAC file to write")
    ap.add_argument("--format", choices=["wav", "flac"], required=True)
    ap.add_argument("--rate", type=int, choices=[88200, 176400], required=True)
    ap.add_argument("--area", choices=["stereo", "multichannel"], required=True)
    ap.add_argument("--channels", default="",
                    help="area channels to return, 1-based, e.g. 5,6 (default: as many as the input has, from 1)")
    ap.add_argument("--lead-frames", type=int, default=0,
                    help="idle frames before the signal, removed from the output (changes the dither's draw)")
    ap.add_argument("--jobs", type=int, default=1, help="pysacd worker threads (default 1)")
    ap.add_argument("--pysacd", default="pysacd", help="the pysacd command (default: pysacd on PATH)")
    a = ap.parse_args(argv)

    chans, _ = read_dsf(a.input)
    area_ch = AREA_CHANNELS[a.area]
    keep = [int(c) - 1 for c in a.channels.split(",")] if a.channels else list(range(len(chans)))
    if not keep or any(not 0 <= k < area_ch for k in keep):
        ap.error(f"--channels must name channels 1 to {area_ch}")
    if len(keep) < len(chans):
        ap.error("--channels must return at least as many channels as the input has")
    exe = shutil.which(a.pysacd)
    if exe is None:
        ap.error(f"{a.pysacd}: not found")

    with tempfile.TemporaryDirectory(prefix="pysacd-quality-") as tmp:
        work = Path(tmp)
        frames = frames_for(chans, area_ch, a.lead_frames)
        iso = work / "disc.iso"
        sacdgen.build_iso(iso, [sacdgen.AreaSpec(area_ch, frames, [len(frames)], dst=False, titles=["Signal"])],
                          title="Quality")
        out = work / "out"
        cmd = [exe, "convert", str(iso), "--config", str(work / "none.toml"), "-f", a.format, "-r", str(a.rate),
               "-a", a.area, "-o", str(out), "--dir-template", "album", "--file-template", "track",
               "--no-musicbrainz", "--no-cover", "--cover-file", "", "--jobs", str(a.jobs)]
        env = dict(os.environ, XDG_CONFIG_HOME=str(work / "config"))
        p = subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False)
        if p.returncode:
            sys.stderr.write(p.stderr)
            return p.returncode
        got = sorted(out.rglob(f"*.{a.format}"))
        if len(got) != 1:
            print(f"expected one .{a.format} file from pysacd convert, found {len(got)}", file=sys.stderr)
            return 1
        src = got[0]
        if a.format == "flac":   # decode to WAV with ffmpeg, keeping the samples exact
            wav = work / "track.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c:a", "pcm_s24le", str(wav)], check=True)
            src = wav
        body, channels, rate, bits = wav_pcm(src)
        if channels != area_ch or rate != a.rate or bits != 24:
            print(f"unexpected output: {channels} channels, {rate} Hz, {bits} bits", file=sys.stderr)
            return 1
        skip = a.lead_frames * (a.rate // 75)
        write_wav(a.output, pick(body, channels, 3, keep, skip), len(keep), rate, bits)
    return 0


if __name__ == "__main__":
    sys.exit(main())
