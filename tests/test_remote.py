"""SACD Ripper network protocol: PS3Simulator + PS3Client/RemoteDisc against a synthetic image.

Run: python -m unittest discover -s tests   (with src/ on PYTHONPATH)
Set SACD_EXTRACT=/path/to/sacd_extract (with PYSACD_TEST_ISO) to also check that the real
sacd_extract copies an image byte-identically through the simulator.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_toc import make_image

from pysacd import read_toc
from pysacd.remote import (
    MAX_BLOCK, REQ_READ, RESP_READ, PS3Client, PS3Simulator, RemoteDisc, RemoteError,
    decode_message, encode_message, fetch, needed_ranges, parse_address, read_remote_toc,
    _Reader, _signed64,
)

S = 2048
TOTAL = 2600            # sectors; the synthetic stereo area spans LSN 600..1000


def _image() -> bytes:
    img = bytearray(make_image())
    img += bytes((TOTAL * S) - len(img))
    for lsn in range(550, TOTAL):          # distinct content per sector
        img[lsn * S:lsn * S + 4] = lsn.to_bytes(4, "big")
    return bytes(img)


class Codec(unittest.TestCase):
    def test_roundtrip_and_negative_int64(self):
        msg = encode_message([(1, 3), (2, -1), (3, b"abc")])
        self.assertEqual(msg[-1], 0)
        a, b = __import__("socket").socketpair()
        a.sendall(msg)
        f = decode_message(_Reader(b))
        a.close(), b.close()
        self.assertEqual(f[1], 3)
        self.assertEqual(_signed64(f[2]), -1)
        self.assertEqual(f[3], b"abc")

    def test_hostile_lengths_rejected(self):
        import socket

        from pysacd.remote import MAX_FIELD, _varint
        a, b = socket.socketpair()
        a.sendall(_varint(3 << 3 | 2) + _varint(MAX_FIELD + 1))      # huge bytes field
        with self.assertRaises(RemoteError):
            decode_message(_Reader(b))
        a.close(), b.close()
        a, b = socket.socketpair()
        a.sendall(encode_message([(1, 1)] * 40)[:-1] + b"\0")       # too many fields
        with self.assertRaises(RemoteError):
            decode_message(_Reader(b))
        a.close(), b.close()

    def test_parse_address(self):
        self.assertEqual(parse_address("192.168.1.10:2002"), ("192.168.1.10", 2002))
        self.assertEqual(parse_address("[::1]:2002"), ("::1", 2002))
        self.assertIsNone(parse_address("disc.iso"))
        self.assertIsNone(parse_address("/mnt/a:b/disc.iso"))


class _SimBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.iso = os.path.join(cls.tmp, "disc.iso")
        cls.data = _image()
        with open(cls.iso, "wb") as f:
            f.write(cls.data)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def sim(self, **kw):
        return PS3Simulator(self.iso, "127.0.0.1", 0, **kw)


class Simulator(_SimBase):

    def test_remote_disc_reads_whole_image_and_toc(self):
        with self.sim() as s:
            disc = RemoteDisc(*s.address)
            self.assertEqual(disc.sectors, TOTAL)
            self.assertEqual(read_toc(disc).title, read_toc(self.iso).title)
            disc.seek(0)
            self.assertEqual(disc.read(), self.data)
            disc.close()

    def test_reads_capped_and_cut_at_area_boundaries(self):
        with self.sim() as s:
            c = PS3Client(*s.address)
            c.open()
            def raw(lsn, n):
                return c._call(REQ_READ, lsn, n, want=RESP_READ)[2]
            self.assertEqual(raw(0, 2000), MAX_BLOCK)        # capped at 512
            self.assertEqual(raw(590, 100), 10)              # stops at area start (600)
            self.assertEqual(raw(600, 1000), 401)            # area 600..1000 inclusive
            self.assertEqual(raw(990, 100), 11)              # stops after area end
            self.assertEqual(c.read(590, 500), self.data[590 * S:1090 * S])   # client loops
            c.close()

    def test_failures(self):
        with self.sim(fail_lsn=700) as s:
            c = PS3Client(*s.address)
            c.open()
            with self.assertRaises(RemoteError):
                c.read(650, 100)
            c.close()
        with self.sim(no_disc=True) as s, self.assertRaises(RemoteError):
            RemoteDisc(*s.address)
        with self.sim(drop_after=1) as s:
            c = PS3Client(*s.address)
            c.open()
            c.read(0, 10)
            with self.assertRaises(RemoteError):
                c.read(10, 10)
            c.close()

    def test_over_returning_server_rejected(self):
        with self.sim(over_return=5) as s:
            c = PS3Client(*s.address)
            c.open()
            with self.assertRaises(RemoteError):
                c.read(0, 10)
            c.close()

    def test_send_failure_is_remote_error(self):
        with self.sim() as s:
            c = PS3Client(*s.address)
            c.open()
            c.sock.close()
            with self.assertRaises(RemoteError):
                c.size()

    def test_close_ends_session(self):
        with self.sim() as s:
            c = PS3Client(*s.address)
            c.open()
            c.close()
            c2 = PS3Client(*s.address)        # the next client gets served
            c2.open()
            self.assertEqual(c2.size(), TOTAL)
            c2.close()


class Fetch(_SimBase):
    def test_whole_disc_copy_is_identical(self):
        with self.sim() as s, tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "copy.iso")
            self.assertEqual(fetch(*s.address, out), TOTAL)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), self.data)

    def test_resumes_after_dropped_connections(self):
        with self.sim(drop_after=2) as s, tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "copy.iso")
            fetch(*s.address, out, chunk=300, retries=1, backoff=0)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), self.data)

    def test_needed_ranges_skip_unwanted_area_audio(self):
        with self.sim() as s:
            disc, total = read_remote_toc(*s.address)
        self.assertEqual(needed_ranges(disc, total, None), [(0, TOTAL)])
        self.assertEqual(needed_ranges(disc, total, disc.areas), [(0, TOTAL)])
        self.assertEqual(needed_ranges(disc, total, []), [(0, 600), (1001, TOTAL)])
        with self.sim() as s, tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "toc-only.iso")
            fetch(*s.address, out, needed_ranges(disc, total, []))
            with open(out, "rb") as f:
                got = f.read()
            self.assertEqual(got[:600 * S], self.data[:600 * S])
            self.assertEqual(got[600 * S:1001 * S], bytes(401 * S))      # hole
            self.assertEqual(read_toc(out).title, read_toc(self.iso).title)


@unittest.skipUnless(os.environ.get("PYSACD_TEST_ISO"), "set PYSACD_TEST_ISO to test the CLI against a real disc")
class RemoteCLI(unittest.TestCase):
    """pysacd-extract -i host:port through the simulator matches a local run."""

    def _extract(self, args, cwd):
        from pysacd import extract
        old = os.getcwd()
        os.chdir(cwd)
        try:
            return extract.main(args + ["--no-cfg"])
        finally:
            os.chdir(old)

    def test_iso_copy_and_dsf_match_local(self):
        iso = os.environ["PYSACD_TEST_ISO"]
        with PS3Simulator(iso, "127.0.0.1", 0) as s, tempfile.TemporaryDirectory() as d:
            addr = "%s:%d" % s.address
            net, loc, cp = (os.path.join(d, x) for x in ("net", "local", "copy"))
            for p in (net, loc, cp):
                os.mkdir(p)
            self.assertEqual(self._extract(["-I", "-i", addr], cp), 0)
            copies = [os.path.join(r, f) for r, _, fs in os.walk(cp) for f in fs if f.endswith(".iso")]
            with open(copies[0], "rb") as a, open(iso, "rb") as b:
                self.assertEqual(a.read(), b.read())
            self.assertEqual(self._extract(["-s", "-i", addr], net), 0)
            self.assertEqual(self._extract(["-s", "-i", iso], loc), 0)
            def files(root):
                return sorted(os.path.relpath(os.path.join(r, f), root) for r, _, fs in os.walk(root) for f in fs)
            self.assertEqual(files(net), files(loc))
            self.assertFalse([f for f in files(net) if ".pysacd-remote-" in f])   # temp removed
            for rel in files(loc):
                with open(os.path.join(net, rel), "rb") as a, open(os.path.join(loc, rel), "rb") as b:
                    self.assertEqual(a.read(), b.read(), rel)


@unittest.skipUnless(os.environ.get("SACD_EXTRACT") and os.environ.get("PYSACD_TEST_ISO"),
                     "set SACD_EXTRACT and PYSACD_TEST_ISO to test against the real client")
class RealClient(unittest.TestCase):
    def test_sacd_extract_iso_copy_is_identical(self):
        iso = os.environ["PYSACD_TEST_ISO"]
        with PS3Simulator(iso, "127.0.0.1", 0) as s, tempfile.TemporaryDirectory() as out:
            host, port = s.address
            subprocess.run([os.environ["SACD_EXTRACT"], "-I", "-i", f"{host}:{port}"], cwd=out,
                           check=True, capture_output=True, timeout=1800)
            copies = [os.path.join(d, f) for d, _, fs in os.walk(out) for f in fs if f.endswith(".iso")]
            self.assertEqual(len(copies), 1)
            with open(copies[0], "rb") as a, open(iso, "rb") as b:
                while True:
                    x, y = a.read(1 << 22), b.read(1 << 22)
                    self.assertEqual(x, y)
                    if not x:
                        break


if __name__ == "__main__":
    unittest.main()
