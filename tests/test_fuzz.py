"""Seeded mutation fuzzing of the code that parses untrusted bytes (#12).

Targets: the protocol decoder (`decode_message`), the PS3 client against a hostile server,
`read_toc` on corrupted images, and the `ps3sim` request handler. Every case derives from a
fixed seed, so a failure names the seed that reproduces it. Hostile input may only produce
the module's own errors (RemoteError, SACDError, EOFError), never another exception or a hang.

PYSACD_FUZZ=N runs N cases per target (default 300).
"""

import io
import os
import random
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import sacdgen

from pysacd.remote import (
    MAX_FIELD,
    MAX_FIELDS,
    REQ_CLOSE,
    REQ_OPEN,
    REQ_READ,
    REQ_SIZE,
    RESP_CLOSED,
    RESP_OPENED,
    RESP_READ,
    RESP_SIZE,
    PS3Client,
    PS3Simulator,
    RemoteError,
    _Reader,
    decode_message,
    encode_message,
    needed_ranges,
)
from pysacd.toc import SECTOR_SIZE as S
from pysacd.toc import SACDError, read_toc

N = int(os.environ.get("PYSACD_FUZZ", "300"))


def mutate(rng: random.Random, data: bytes, region: tuple[int, int] | None = None) -> bytes:
    """A few random byte flips, overwrites with boundary values, truncations or insertions."""
    b = bytearray(data)
    lo, hi = region or (0, len(b))
    for _ in range(rng.randint(1, 8)):
        if hi <= lo:
            break
        op, i = rng.random(), rng.randrange(lo, hi)
        if op < 0.4:
            b[i] ^= 1 << rng.randrange(8)
        elif op < 0.7:
            b[i] = rng.choice((0x00, 0x01, 0x7F, 0x80, 0xFF))
        elif op < 0.8:      # a big-endian u16/u32 field set to an extreme
            w = rng.choice((2, 4))
            b[i:i + w] = rng.choice((b"\xff" * w, b"\x00" * w, b"\x7f" + b"\xff" * (w - 1)))
        elif op < 0.9 and region is None:
            del b[i:i + rng.randint(1, 16)]
            hi = min(hi, len(b))
        elif region is None:
            b[i:i] = rng.randbytes(rng.randint(1, 16))
            hi = len(b)
    return bytes(b)


class FakeSocket:
    """Feeds canned bytes to a _Reader in random-sized chunks; swallows what is sent."""

    def __init__(self, data: bytes, rng: random.Random):
        self.data, self.rng = data, rng

    def recv(self, n: int) -> bytes:
        k = min(n, self.rng.randint(1, 64))
        out, self.data = self.data[:k], self.data[k:]
        return out

    def sendall(self, data: bytes) -> None:
        pass

    def close(self) -> None:
        pass


def _seed_messages() -> list[bytes]:
    return [
        encode_message([(1, REQ_READ), (2, 1234), (3, 512)]),
        encode_message([(1, REQ_OPEN), (2, 0), (3, 0)]),
        encode_message([(1, RESP_READ), (2, 2), (3, bytes(2 * S))]),
        encode_message([(1, RESP_SIZE), (2, 900)]),
        encode_message([(1, RESP_OPENED), (2, -1)]),
    ]


class DecodeMessage(unittest.TestCase):
    def check(self, data: bytes, seed: int) -> None:
        r = _Reader(FakeSocket(data, random.Random(seed)))  # type: ignore[arg-type]
        try:
            fields = decode_message(r)
        except (RemoteError, EOFError):
            return
        self.assertLessEqual(len(fields), MAX_FIELDS)
        for v in fields.values():
            if isinstance(v, int):
                self.assertTrue(0 <= v < 1 << 64, v)
            else:
                self.assertLessEqual(len(v), MAX_FIELD)

    def test_mutated_messages(self):
        seeds = _seed_messages()
        for seed in range(N):
            rng = random.Random(seed)
            with self.subTest(seed=seed):
                self.check(mutate(rng, rng.choice(seeds)), seed)

    def test_random_bytes(self):
        for seed in range(N):
            rng = random.Random(seed)
            with self.subTest(seed=seed):
                self.check(rng.randbytes(rng.randint(0, 64)), seed)

    def test_overlong_varint(self):
        # Ten continuation bytes then a high final byte: more than 64 bits.
        data = b"\x08" + b"\xff" * 9 + b"\x7f" + b"\x00"
        self.check(data, 0)


def _client(data: bytes, rng: random.Random) -> PS3Client:
    c = PS3Client.__new__(PS3Client)
    c.sock = FakeSocket(data, rng)  # type: ignore[assignment]
    c.reader = _Reader(c.sock)
    c.address, c.timeout = "fuzz", 1.0
    c._open = False
    c.requests = c.bytes_read = 0
    c.busy, c._started = 0.0, 0.0
    return c


class HostileServer(unittest.TestCase):
    """Client calls against mutated server responses: RemoteError or a sane result."""

    def test_responses(self):
        good = {
            "open": encode_message([(1, RESP_OPENED), (2, 0)]),
            "size": encode_message([(1, RESP_SIZE), (2, 900)]),
            "read": encode_message([(1, RESP_READ), (2, 2), (3, bytes(2 * S))]),
            "close": encode_message([(1, RESP_CLOSED), (2, 0)]),
        }
        for seed in range(N):
            rng = random.Random(seed)
            op = rng.choice(sorted(good))
            c = _client(mutate(rng, good[op]), rng)
            with self.subTest(seed=seed, op=op):
                try:
                    if op == "open":
                        c.open()
                    elif op == "size":
                        self.assertLessEqual(c.size(), 1 << 22)
                    elif op == "read":
                        self.assertEqual(len(c.read_some(0, 2)) % S, 0)
                    else:
                        c._open = True
                        c.close()
                except RemoteError:
                    pass

    def test_wrong_field_types(self):
        # Type, result and data fields sent with the wrong wire type.
        for resp, call in ((RESP_SIZE, "size"), (RESP_READ, "read_some"), (RESP_OPENED, "open")):
            for fields in ([(1, resp), (2, b"\x05")], [(1, resp), (2, 1), (3, 7)], [(1, b"x"), (2, 0)]):
                c = _client(encode_message(fields), random.Random(0))
                with self.subTest(call=call, fields=fields), self.assertRaises(RemoteError):
                    getattr(c, call)(*((0, 1) if call == "read_some" else ()))


class CorruptTOC(unittest.TestCase):
    """read_toc on corrupted images: a Disc or SACDError, and needed_ranges() stays sane."""

    @classmethod
    def setUpClass(cls):
        fr = bytes([0x69]) * (4704 * 2)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.iso")
            sacdgen.build_iso(path, [sacdgen.AreaSpec(2, [fr] * 6, [3, 3], dst=False, titles=["a", "b"]),
                                     sacdgen.AreaSpec(6, [bytes(4704 * 6)] * 4, [2, 2], dst=False)])
            with open(path, "rb") as f:
                cls.image = f.read()
        disc = read_toc(io.BytesIO(cls.image))
        # Regions worth corrupting: the master TOC (LSN 510..519) and each area's TOC.
        cls.regions = [(510 * S, 520 * S)] + [(a.toc_start * S, (a.toc_start + a.toc_size) * S)
                                              for a in disc.areas]

    def test_corrupted_tocs(self):
        total = len(self.image) // S
        for seed in range(N):
            rng = random.Random(seed)
            img = mutate(rng, self.image, rng.choice(self.regions))
            with self.subTest(seed=seed):
                try:
                    disc = read_toc(io.BytesIO(img))
                except SACDError:
                    continue
                for areas in (None, [], disc.areas[:1]):
                    try:
                        for s, e in needed_ranges(disc, total, areas):
                            self.assertTrue(0 <= s < e <= total, (s, e))
                    except RemoteError:
                        pass

    def test_track_text_offset_past_toc(self):
        # Track 1's text pointer (SACDTTxt sector of the stereo area TOC) set to 0xffff.
        disc = read_toc(io.BytesIO(self.image))
        img = bytearray(self.image)
        p = disc.stereo.toc_start * S + S + 8
        img[p:p + 2] = b"\xff\xff"
        bad = read_toc(io.BytesIO(bytes(img)))
        self.assertEqual(len(bad.stereo.tracks), len(disc.stereo.tracks))
        self.assertTrue(any("outside the area TOC" in w or "track-text-offset-invalid" in w
                            for w in bad.stereo.warnings), bad.stereo.warnings)

    def test_truncated_images(self):
        for seed in range(min(N, 100)):
            rng = random.Random(seed)
            with self.subTest(seed=seed):
                try:
                    read_toc(io.BytesIO(self.image[:rng.randrange(len(self.image))]))
                except SACDError:
                    pass


class SimulatorHandler(unittest.TestCase):
    """Mutated requests must not kill or wedge ps3sim: a well-behaved client is still served."""

    def test_mutated_requests(self):
        fr = bytes([0x69]) * (4704 * 2)
        requests = [encode_message([(1, t), (2, o), (3, n)])
                    for t, o, n in ((REQ_OPEN, 0, 0), (REQ_SIZE, 0, 0), (REQ_READ, 520, 4), (REQ_CLOSE, 0, 0))]
        errors: list[str] = []
        with tempfile.TemporaryDirectory() as d:
            iso = os.path.join(d, "s.iso")
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [fr] * 4, [4], dst=False)])
            with PS3Simulator(iso, "127.0.0.1", 0, idle_timeout=2) as s:
                # socketserver reports a handler exception through handle_error and carries on.
                s._server.handle_error = lambda req, addr: errors.append(repr(sys.exc_info()[1]))  # type: ignore[method-assign]
                for seed in range(min(N, 150)):
                    rng = random.Random(seed)
                    msgs = [requests[0]] + [mutate(rng, rng.choice(requests)) for _ in range(rng.randint(1, 4))]
                    if seed % 10 == 0:      # a field the server expects as a varint, sent as bytes
                        msgs.append(encode_message([(1, REQ_READ), (2, b"\x00" * 4), (3, 1)]))
                    with socket.create_connection(s.address, timeout=5) as sock:
                        try:
                            sock.sendall(b"".join(msgs))
                            sock.shutdown(socket.SHUT_WR)
                            while sock.recv(65536):
                                pass
                        except OSError:
                            pass
                c = PS3Client(*s.address, timeout=5)
                c.open()
                self.assertGreater(c.size(), 0)
                self.assertEqual(len(c.read(0, 1)), S)
                c.close()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
