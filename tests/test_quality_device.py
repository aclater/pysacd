"""tools/quality_device.py: DSF in, SACD frames out, channels picked back (release quality comparison)."""

import random
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import quality_device as qd

BLOCK = 4096


def write_dsf(path: Path, chans: list[list[int]], rate: int = qd.DSD64) -> None:
    """A DSF file from per-channel bit lists (0/1), LSB first, padded with zero bytes."""
    n = len(chans[0])
    packed = []
    for bits in chans:
        b = bytearray((n + 7) // 8)
        for i, v in enumerate(bits):
            b[i // 8] |= v << (i % 8)
        packed.append(bytes(b).ljust(-(-len(b) // BLOCK) * BLOCK, b"\0"))
    data = b"".join(ch[o:o + BLOCK] for o in range(0, len(packed[0]), BLOCK) for ch in packed)
    fmt = struct.pack("<4sQIIIIIIQI4x", b"fmt ", 52, 1, 0, 2, len(chans), rate, 1, n, BLOCK)
    path.write_bytes(struct.pack("<4sQQQ", b"DSD ", 28, 0, 0) + fmt + struct.pack("<4sQ", b"data", 12 + len(data)) + data)


class QualityDevice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        rng = random.Random(7)
        self.n = 3 * BLOCK * 8 + 13                 # more than one block, not a whole byte
        self.bits = [[rng.getrandbits(1) for _ in range(self.n)] for _ in range(2)]
        self.dsf = self.dir / "in.dsf"
        write_dsf(self.dsf, self.bits)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_dsf_is_msb_first_per_channel(self):
        chans, samples = qd.read_dsf(self.dsf)
        self.assertEqual(samples, self.n)
        for c in range(2):
            want = bytearray((self.n + 7) // 8)
            for i, v in enumerate(self.bits[c]):
                want[i // 8] |= v << (7 - i % 8)
            self.assertEqual(chans[c], bytes(want))

    def test_refuses_other_rates(self):
        write_dsf(self.dir / "d128.dsf", self.bits, 2 * qd.DSD64)
        with self.assertRaises(ValueError):
            qd.read_dsf(self.dir / "d128.dsf")

    def test_frames_interleave_and_pad(self):
        chans, _ = qd.read_dsf(self.dsf)
        frames = qd.frames_for(chans, 6, lead_frames=1)
        self.assertEqual(len(frames), 1 + -(-len(chans[0]) // qd.FRAME_BYTES))
        self.assertTrue(all(len(f) == 6 * qd.FRAME_BYTES for f in frames))
        self.assertEqual(frames[0], bytes([qd.IDLE]) * len(frames[0]))     # the lead frame is idle
        for k in range(6):                                                  # channel k carries input k % 2
            self.assertEqual(frames[1][k::6], chans[k % 2][:qd.FRAME_BYTES])
        tail = len(chans[0]) - (len(frames) - 2) * qd.FRAME_BYTES
        self.assertEqual(frames[-1][tail * 6:], bytes([qd.IDLE]) * ((qd.FRAME_BYTES - tail) * 6))

    def test_pick_channels_and_skip(self):
        body = bytes(range(18)) * 4                 # 4 sample frames of 6 channels x 3 bytes
        self.assertEqual(qd.pick(body, 6, 3, [4, 5], 1), bytes(range(12, 18)) * 3)
        self.assertEqual(qd.pick(body, 6, 3, list(range(6)), 0), body)

    def test_wav_round_trip(self):
        out = self.dir / "o.wav"
        qd.write_wav(out, bytes(range(6)) * 10, 2, 88200, 24)
        self.assertEqual(qd.wav_pcm(out), (bytes(range(6)) * 10, 2, 88200, 24))


if __name__ == "__main__":
    unittest.main()
