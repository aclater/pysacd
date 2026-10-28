"""Tests for the native extractor (synthetic audio sectors), naming and MB helpers."""

import os
import random
import struct
import tempfile
import unittest
from pathlib import Path

from pysacd import SACDImage
from pysacd.convert import Options, naming_values, render
from pysacd.musicbrainz import physical_discs
from pysacd.toc import Area, Timecode, Track

S = 2048
AUDIO_LSN = 600
FRAME = 4704  # bytes per channel per frame

try:
    from pysacd import _native
    _native._load()
    HAVE_NATIVE = True
except Exception:
    HAVE_NATIVE = False


def mux(frames: list[bytes], first_tc: int = 0) -> list[bytes]:
    """Pack plain-DSD frames into Scarletbook audio sectors."""
    sectors, fi, off = [], 0, 0
    while fi < len(frames):
        packets, infos, payload, space = [], [], b"", S - 1
        while fi < len(frames) and len(packets) < 7:
            start = off == 0
            overhead = 2 + (3 if start else 0)
            n = min(2045, space - overhead, len(frames[fi]) - off)
            if n <= 0 or (start and len(infos) == 7):
                break
            packets.append((start, n))
            if start:
                tc = first_tc + fi
                infos.append(bytes([tc // 4500, tc // 75 % 60, tc % 75]))
            payload += frames[fi][off:off + n]
            off += n
            space -= overhead + n
            if off == len(frames[fi]):
                fi, off = fi + 1, 0
        sec = bytes([len(infos) << 2 | len(packets) << 5])
        sec += b"".join(bytes([s << 7 | 2 << 3 | n >> 8, n & 0xFF]) for s, n in packets)
        sec += b"".join(infos) + payload
        sectors.append(sec.ljust(S, b"\0"))
    return sectors


def build(path: Path, frames: list[bytes], split: int):
    """Image with two tracks: frames [0, split) and [split, len)."""
    sectors = mux(frames)
    # LSN where the frame `split` starts (first sector carrying its frame info)
    starts = []
    for i, sec in enumerate(sectors):
        n = sec[0] >> 2 & 7
        starts += [AUDIO_LSN + i] * n
    img = bytearray(AUDIO_LSN * S)
    img[510 * S:510 * S + 8] = b"SACDMTOC"
    img += b"".join(sectors)
    path.write_bytes(img)
    end = AUDIO_LSN + len(sectors) - 1
    tc = lambda f: Timecode(f // 4500, f // 75 % 60, f % 75)
    tracks = [
        Track(1, AUDIO_LSN, starts[split] - AUDIO_LSN, tc(0), tc(split), 0),
        Track(2, starts[split], end - starts[split] + 1, tc(split), tc(len(frames) - split), 0),
    ]
    area = Area("TWOCHTOC", (1, 20), 0, 0, 0, 4, 2, 2, 0, 2, 0, 0, tc(len(frames)), 0,
                AUDIO_LSN, end, [], tracks=tracks)
    return area


def rev(b: int) -> int:
    return int(f"{b:08b}"[::-1], 2)


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
class NativeExtract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        rnd = random.Random(1)
        self.frames = [bytes(rnd.getrandbits(8) for _ in range(2 * FRAME)) for _ in range(12)]
        self.iso = self.dir / "t.iso"
        self.area = build(self.iso, self.frames, 5)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def spec(t, path, fmt):
        start = t.start.total_frames
        return _native.OutputSpec(fmt, path, start, start + t.duration.total_frames)

    def test_dsf_nopad_carry(self):
        # Track 1 leaves 5*4704 % 4096 bytes/channel for track 2 to start with
        t1, t2 = self.area.tracks
        carry = 5 * FRAME % 4096
        s1 = self.spec(t1, self.dir / "1.dsf", "dsf")
        s1.tail_bytes = carry
        s2 = self.spec(t2, self.dir / "2.dsf", "dsf")
        s2.head_frame, s2.head_bytes = 4, carry
        with SACDImage(self.iso) as img:
            _native.run_area(img, self.iso, self.area, [s1, s2])
        ch0 = lambda d: b"".join(d[o:o + 4096] for o in range(92, len(d), 8192))
        a, b = (self.dir / "1.dsf").read_bytes(), (self.dir / "2.dsf").read_bytes()
        self.assertEqual((len(a) - 92) % 8192, 0)
        src = bytes(rev(x) for x in b"".join(f[0::2] for f in self.frames))
        joined = ch0(a) + ch0(b)[:struct.unpack_from("<Q", b, 64)[0] // 8]
        self.assertEqual(joined, src)

    def test_threads_deterministic(self):
        outs = []
        with SACDImage(self.iso) as img:
            for n in (1, 7):
                spec = self.spec(self.area.tracks[1], self.dir / f"t{n}.wav", "wav")
                _native.run_area(img, self.iso, self.area, [spec], threads=n)
                outs.append((self.dir / f"t{n}.wav").read_bytes())
        self.assertEqual(outs[0], outs[1])

    def test_iso_copy(self):
        out = self.dir / "copy.iso"
        with SACDImage(self.iso) as img:
            _native.run_area(img, self.iso, None, [], iso_out=out)
        self.assertEqual(out.read_bytes(), self.iso.read_bytes())

    def test_dsf_payload(self):
        out = self.dir / "2.dsf"
        with SACDImage(self.iso) as img:
            res = self.spec(self.area.tracks[1], out, "dsf")
            _native.run_area(img, self.iso, self.area, [res])
        self.assertEqual((res.frames_written, res.frames_missing), (7, 0))
        d = out.read_bytes()
        self.assertEqual(d[:4], b"DSD ")
        samples, = struct.unpack_from("<Q", d, 64)
        self.assertEqual(samples, 7 * FRAME * 8)
        data = d[92:]
        # de-block channel 0 and compare with bit-reversed interleaved source
        ch0 = b"".join(data[o:o + 4096] for o in range(0, len(data), 8192))[:7 * FRAME]
        src = b"".join(f[0::2] for f in self.frames[5:])
        self.assertEqual(ch0, bytes(rev(b) for b in src))
        self.assertEqual(struct.unpack_from("<Q", d, 12)[0], len(d))

    def test_wav_length_and_gapless(self):
        with SACDImage(self.iso) as img:
            _native.run_area(img, self.iso, self.area,
                             [self.spec(t, self.dir / f"{t.number}.wav", "wav") for t in self.area.tracks])
        n1 = len((self.dir / "1.wav").read_bytes()) - 68
        n2 = len((self.dir / "2.wav").read_bytes()) - 68
        self.assertEqual(n1, 5 * 1176 * 2 * 3)
        self.assertEqual(n2, 7 * 1176 * 2 * 3)

    def test_missing_frames_counted(self):
        # A track claiming 3 more frames than exist on disc
        t = self.area.tracks[1]
        t.duration = Timecode(0, 0, t.duration.total_frames + 3)
        with SACDImage(self.iso) as img:
            res = self.spec(t, self.dir / "x.dsf", "dsf")
            _native.run_area(img, self.iso, self.area, [res])
        # written includes the 3 silence frames substituted for the missing ones
        self.assertEqual((res.frames_written, res.frames_missing), (10, 3))


class Naming(unittest.TestCase):
    def test_empty_fields_and_brackets(self):
        self.assertEqual(render("{albumartist}/{album} [{edition}] ({year})",
                                {"albumartist": "A/B", "album": "X", "edition": ""}, False),
                         os.path.join("A-B", "X"))

    def test_portable(self):
        self.assertEqual(render("{title}", {"title": 'What? "Now": <x>'}, True), "What_ _Now__ _x_")

    def test_values(self):
        area = Area("MULCHTOC", (1, 20), 0, 0, 0, 4, 0, 6, 4, 6, 0, 0, Timecode(0, 0, 0), 0, 0, 0, [])
        tags = {"ALBUM": ["Al"], "ALBUMARTIST": ["Ar"], "DATE": ["2004-08-21"], "TRACKNUMBER": ["3"],
                "DISCNUMBER": ["2"], "DISCTOTAL": ["2"], "TITLE": ["T"]}
        v = naming_values(tags, area, Options())
        self.assertEqual((v["year"], v["edition"], v["disc_prefix"]), ("2004", "SACD 5.1", "2-"))
        self.assertEqual(render(Options().file_template, v, False), "2-03 - T")


class Layers(unittest.TestCase):
    def test_physical_discs(self):
        f = lambda *fs: physical_discs([{"format": x} for x in fs])
        self.assertEqual(f("Hybrid SACD (CD layer)", "Hybrid SACD (SACD layer, 2 channels)",
                           "Hybrid SACD (SACD layer, multichannel)"), [1, 1, 1])
        self.assertEqual(f("SACD (2 channels)", "SACD (multichannel)", "SACD (2 channels)"), [1, 1, 2])
        self.assertEqual(f("SHM-SACD", "SHM-SACD"), [1, 2])


if __name__ == "__main__":
    unittest.main()
