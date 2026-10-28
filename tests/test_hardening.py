"""#14: the engine ABI handshake, pysacd.safeio, and the HTTP limits."""

import ctypes
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pysacd import _native, musicbrainz, safeio


def _native_ok() -> bool:
    try:
        _native._load()
        return True
    except (ImportError, OSError, RuntimeError):
        return False


@unittest.skipUnless(_native_ok(), "native engine not built")
class ABI(unittest.TestCase):
    def test_matches(self):
        lib = _native._load()
        self.assertEqual(lib.sacdconv_abi(), _native.ABI)
        _native._check_abi(lib, "lib")         # no exception

    def test_version_mismatch_refused(self):
        lib = mock.Mock()
        lib.sacdconv_abi.return_value = _native.ABI + 1
        with self.assertRaisesRegex(_native.NativeError, "ABI"):
            _native._check_abi(lib, "lib")

    def test_old_library_without_handshake_refused(self):
        lib = mock.Mock(spec=[])            # no sacdconv_abi at all
        with self.assertRaisesRegex(_native.NativeError, "ABI 0"):
            _native._check_abi(lib, "lib")

    def test_layout_mismatch_refused(self):
        # A mirror with one field's type changed: same ABI number, different offsets.
        fields = list(_native._Area._fields_)
        i = next(k for k, (n, _) in enumerate(fields) if n == "gain_db")
        fields[i] = ("gain_db", ctypes.c_float)
        Wrong = type("_Area", (ctypes.Structure,), {"_fields_": fields})
        with mock.patch.object(_native, "_Area", Wrong), self.assertRaisesRegex(_native.NativeError, "layout"):
            _native._check_abi(_native._load(), "lib")


class SafeIO(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_temp_names_unpredictable_and_short(self):
        long = self.dir / ("x" * 250 + ".dsf")
        names = {safeio.temp_name(long).name for _ in range(100)}
        self.assertEqual(len(names), 100)
        self.assertTrue(all(len(n) < 40 and n.startswith(".") for n in names))

    def test_create_refuses_existing_and_symlinks(self):
        target = self.dir / "target"
        target.write_bytes(b"keep")
        link = self.dir / "link"
        link.symlink_to(target)
        for p in (target, link):
            with self.subTest(p=p.name), self.assertRaises(FileExistsError):
                safeio.create(p)
        self.assertEqual(target.read_bytes(), b"keep")

    def test_open_new_closes_fd_if_wrapping_fails(self):
        target = self.dir / "x"
        closed = []
        real_close = os.close
        with mock.patch.object(os, "fdopen", side_effect=MemoryError), \
                mock.patch.object(os, "close", side_effect=lambda fd: (closed.append(fd), real_close(fd))), \
                self.assertRaises(MemoryError):
            safeio.open_new(target)
        self.assertEqual(len(closed), 1)
        self.assertFalse(target.exists())

    def test_write_file_mode_and_symlink_replace(self):
        outside = self.dir / "outside"
        outside.write_bytes(b"canary")
        final = self.dir / "out.cue"
        final.symlink_to(outside)
        old = os.umask(0)
        try:
            safeio.write_file(final, b"data")
        finally:
            os.umask(old)
        self.assertEqual(outside.read_bytes(), b"canary")      # the symlink was replaced, not followed
        self.assertFalse(final.is_symlink())
        self.assertEqual(final.read_bytes(), b"data")
        if os.name != "nt":             # Windows has no POSIX mode bits
            self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o666)     # umask 0: nothing executable
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.endswith(".partial")], [])


class HTTPLimits(unittest.TestCase):
    def response(self, body: bytes, ctype: str = "application/json"):
        r = mock.MagicMock()
        stream = io.BytesIO(body)
        r.read.side_effect = stream.read
        r.headers.get_content_type.return_value = ctype
        r.__enter__.return_value = r
        return r

    def test_oversized_json_refused(self):
        with mock.patch("urllib.request.urlopen", return_value=self.response(b"x" * 101)), \
                mock.patch.object(musicbrainz, "_last_request", 0.0), \
                self.assertRaisesRegex(musicbrainz.MBError, "larger than 100"):
            musicbrainz._get("https://example.invalid/x", None, limit=100)

    def test_at_limit_is_fine(self):
        with mock.patch("urllib.request.urlopen", return_value=self.response(b"x" * 100)):
            self.assertEqual(musicbrainz._get("https://example.invalid/x", None, limit=100)[0], b"x" * 100)

    def test_cover_magic_bytes(self):
        release = {"id": "r", "release-group": {"id": "g"}}
        jpeg, png = b"\xff\xd8\xff\xe0" + bytes(20), b"\x89PNG\r\n\x1a\n" + bytes(20)
        for body, want in ((jpeg, "image/jpeg"), (png, "image/png"), (b"<html>", None), (b"MZ\x90\x00", None)):
            with self.subTest(body=body[:4]), \
                    mock.patch("urllib.request.urlopen", side_effect=lambda *a, b=body, **k: self.response(b, "image/jpeg")):
                got = musicbrainz.fetch_cover(release, None)
                self.assertEqual(got and got[1], want)
                if want:
                    self.assertEqual(got[0], body)


if __name__ == "__main__":
    unittest.main()
