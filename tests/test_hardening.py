"""#14: the engine ABI handshake, pysacd.safeio, and the HTTP limits."""

import ctypes
import http.client
import http.server
import io
import os
import ssl
import stat
import tempfile
import threading
import time
import typing
import unittest
import urllib.request
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


class ConfigDir(unittest.TestCase):
    """An empty or relative XDG_CONFIG_HOME is ignored (XDG spec), so no config is read from the cwd."""

    def test_helper(self):
        home = Path.home() / ".config" / "pysacd"
        for value, want in (("", home), ("rel", home), ("./x", home), ("/abs/cfg", Path("/abs/cfg/pysacd"))):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": value}):
                self.assertEqual(safeio.config_dir(), want)
        with mock.patch.dict(os.environ):
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertEqual(safeio.config_dir(), home)

    def test_modules_use_it(self):
        import subprocess
        import sys
        code = "from pysacd import convert, isrcsubmit; print(convert.CONFIG_PATH); print(isrcsubmit.TOKEN_FILE)"
        for value in ("", "relative"):
            with self.subTest(value=value):
                env = {**os.environ, "XDG_CONFIG_HOME": value,
                       "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}
                out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                                     check=True).stdout.split("\n")
                self.assertTrue(Path(out[0]).is_absolute(), out[0])
                self.assertTrue(Path(out[1]).is_absolute(), out[1])


class HTTPLimits(unittest.TestCase):
    def response(self, body: bytes, ctype: str = "application/json"):
        r = mock.MagicMock()
        stream = io.BytesIO(body)
        r.read.side_effect = stream.read
        r.read1.side_effect = stream.read1
        r.headers.get_content_type.return_value = ctype
        r.__enter__.return_value = r
        return r

    def test_oversized_json_refused(self):
        with mock.patch.object(musicbrainz, "_open", return_value=self.response(b"x" * 101)), \
                mock.patch.object(musicbrainz, "_last_request", 0.0), \
                self.assertRaisesRegex(musicbrainz.MBError, "larger than 100"):
            musicbrainz._get("https://example.invalid/x", None, limit=100)

    def test_at_limit_is_fine(self):
        with mock.patch.object(musicbrainz, "_open", return_value=self.response(b"x" * 100)):
            self.assertEqual(musicbrainz._get("https://example.invalid/x", None, limit=100)[0], b"x" * 100)

    def test_cover_magic_bytes(self):
        release = {"id": "00000000-0000-4000-8000-000000000001", "release-group": {"id": "00000000-0000-4000-8000-000000000002"}}
        jpeg, png = b"\xff\xd8\xff\xe0" + bytes(20), b"\x89PNG\r\n\x1a\n" + bytes(20)
        for body, want in ((jpeg, "image/jpeg"), (png, "image/png"), (b"<html>", None), (b"MZ\x90\x00", None)):
            with self.subTest(body=body[:4]), \
                    mock.patch.object(musicbrainz, "_open", side_effect=lambda *a, b=body, **k: self.response(b, "image/jpeg")):
                got = musicbrainz.fetch_cover(release, None)
                self.assertEqual(got and got[1], want)
                if want:
                    self.assertEqual(got[0], body)


_real_sleep = time.sleep         # the tests patch time.sleep to skip the client's retry back-off


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves what the test sets in `routes`: path -> (status, headers, body chunks, delay per chunk)."""
    routes: typing.ClassVar[dict] = {}
    seen: typing.ClassVar[list] = []

    def _serve(self):
        type(self).seen.append((self.command, self.path, dict(self.headers)))
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        status, headers, chunks, delay = type(self).routes.get(self.path.split("?")[0], (404, {}, [b""], 0))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(sum(map(len, chunks))))
        self.end_headers()
        try:
            for c in chunks:
                self.wfile.write(c)
                self.wfile.flush()
                _real_sleep(delay)
        except OSError:
            pass

    do_GET = do_POST = _serve

    def log_message(self, *a):
        pass


class LocalServer:
    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class HTTPDeadlinesAndRedirects(unittest.TestCase):
    """L3/L4: a whole-request deadline, network errors as MBError, https-only redirects."""

    def setUp(self):
        self.srv = LocalServer()
        _Handler.routes, _Handler.seen = {}, []
        self.api = mock.patch.object(musicbrainz, "API", self.srv.base + "/ws/2")   # loopback http allowed
        self.api.start()
        self.sleep = mock.patch.object(musicbrainz.time, "sleep")                    # no retry back-off
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.api.stop()
        self.srv.close()

    def test_trickled_response_hits_deadline(self):
        # 1 byte every 50 ms: every socket read succeeds well inside the 30 s timeout
        _Handler.routes["/slow"] = (200, {"Content-Length": "1000"}, [b"x"] * 1000, 0.05)
        t0 = time.monotonic()
        with self.assertRaisesRegex(musicbrainz.MBError, "not complete in time"):
            musicbrainz._get(self.srv.base + "/slow", None, deadline=0.5)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(len(_Handler.seen), 1)          # not retried

    def test_truncated_body_is_mberror(self):
        _Handler.routes["/short"] = (200, {"Content-Length": "100"}, [b"x" * 10], 0)
        with self.assertRaises(musicbrainz.MBError):
            musicbrainz._get(self.srv.base + "/short", None, retries=2)
        self.assertEqual(len(_Handler.seen), 2)          # retried like any other network error

    def test_read_errors_are_mberror(self):
        for exc in (TimeoutError("timed out"), ConnectionResetError(104, "reset"), ssl.SSLError("bad record"),
                    http.client.IncompleteRead(b"x", 5)):
            r = HTTPLimits.response(None, b"")           # type: ignore[arg-type]
            r.read1.side_effect = exc
            with self.subTest(exc=type(exc).__name__), mock.patch.object(musicbrainz, "_open", return_value=r) as op, \
                    self.assertRaises(musicbrainz.MBError):
                musicbrainz._get("https://example.invalid/x", None, retries=3)
            self.assertEqual(op.call_count, 3)

    def test_redirect_to_plain_http_refused(self):
        # Refused before any connection: example.invalid is never contacted
        _Handler.routes["/r"] = (302, {"Location": "http://example.invalid/x"}, [b""], 0)
        with self.assertRaisesRegex(musicbrainz.MBError, "non-https"):
            musicbrainz._get(self.srv.base + "/r", None)

    def test_redirect_rules(self):
        h = musicbrainz._Redirects()
        req = urllib.request.Request("https://coverartarchive.org/release/x/front")
        with mock.patch.object(musicbrainz, "API", "https://musicbrainz.org/ws/2"):
            for bad in ("http://archive.org/x", "ftp://archive.org/x", "file:///etc/passwd",
                        "http://127.0.0.1/x"):       # loopback http only when API itself is loopback http
                with self.subTest(bad=bad), self.assertRaisesRegex(musicbrainz.MBError, "refused"):
                    h.redirect_request(req, io.BytesIO(), 302, "Found", {}, bad)
            self.assertIsNotNone(h.redirect_request(req, io.BytesIO(), 302, "Found", {}, "https://archive.org/x"))
            with self.assertRaisesRegex(musicbrainz.MBError, "authenticated"):
                musicbrainz._Redirects(follow=False).redirect_request(req, io.BytesIO(), 302, "Found", {},
                                                                      "https://musicbrainz.org/x")
            with self.assertRaisesRegex(musicbrainz.MBError, "only https"):
                musicbrainz._get("http://musicbrainz.org/ws/2/x", None)

    def test_loopback_redirect_followed_and_bounded(self):
        _Handler.routes["/a"] = (302, {"Location": "/b"}, [b""], 0)
        _Handler.routes["/b"] = (200, {}, [b"{}"], 0)
        self.assertEqual(musicbrainz._get(self.srv.base + "/a", None)[0], b"{}")
        for i in range(10):                           # a chain longer than MAX_REDIRECTS
            _Handler.routes[f"/c{i}"] = (302, {"Location": f"/c{i + 1}"}, [b""], 0)
        _Handler.seen = []
        with self.assertRaisesRegex(musicbrainz.MBError, "HTTP 302"):
            musicbrainz._get(self.srv.base + "/c0", None)
        self.assertEqual(len(_Handler.seen), musicbrainz.MAX_REDIRECTS + 1)


if __name__ == "__main__":
    unittest.main()
