"""#14: the engine ABI handshake, pysacd.safeio, and the HTTP limits."""

import ctypes
import http.client
import http.server
import io
import os
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import typing
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import fsutil

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

    def bases(self):
        # pysacd writes on Windows through extended-length paths (safeio.long_path, #217): the
        # protections must hold in that form too (elsewhere long_path() changes nothing)
        yield "plain", self.dir
        if safeio.WINDOWS:
            yield "extended-length", Path(safeio.long_path(self.dir))

    def test_create_refuses_existing_and_symlinks(self):
        linked = True
        for form, base in self.bases():
            target = base / f"target-{form}"
            target.write_bytes(b"keep")
            link = base / f"link-{form}"
            linked = fsutil.try_symlink(link, target) and linked
            for p in (target, link) if linked else (target,):
                with self.subTest(form=form, p=p.name), self.assertRaises(FileExistsError):
                    safeio.create(p)
            self.assertEqual(target.read_bytes(), b"keep")
        if not linked:
            self.skipTest(fsutil.NO_SYMLINK)

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
        linked = True
        for form, base in self.bases():
            with self.subTest(form=form):
                outside = base / f"outside-{form}"
                outside.write_bytes(b"canary")
                final = base / f"out-{form}.cue"
                this_linked = fsutil.try_symlink(final, outside)
                linked = linked and this_linked
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
        if not linked:      # the plain-file half above ran; only the symlink replacement did not
            self.skipTest(fsutil.NO_SYMLINK)


class ConfigDir(unittest.TestCase):
    """An empty or relative XDG_CONFIG_HOME is ignored (XDG spec), so no config is read from the cwd."""

    def test_helper(self):
        home = Path.home() / ".config" / "pysacd"
        absolute = Path(tempfile.gettempdir()).resolve() / "cfg"     # absolute on every platform
        for value, want in (("", home), ("rel", home), ("./x", home), (str(absolute), absolute / "pysacd")):
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


class MemoryLimit(unittest.TestCase):
    def test_a_runaway_allocation_fails_instead_of_growing(self):
        # tests/conftest.py's limit, in a child process so this one keeps its own; without a
        # sanitizer runtime preloaded, which maps more than the limit before Python starts
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import conftest\n"
                "if not conftest.limit_memory(200):\n    print('no limit here'); sys.exit()\n"
                "try:\n    held = [bytearray(64 << 20) for _ in range(16)]\n"
                "except MemoryError:\n    print('MemoryError')\n")
        env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
        out = subprocess.run([sys.executable, "-c", code, str(Path(__file__).parent)],
                             capture_output=True, text=True, timeout=60, env=env, check=False)
        want = "MemoryError" if sys.platform.startswith("linux") else "no limit here"
        self.assertEqual(out.stdout.strip(), want, out.stderr)


class HTTPLimits(unittest.TestCase):
    @staticmethod
    def response(body: bytes, ctype: str = "application/json"):
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

    def test_a_response_that_is_not_bytes_ends_the_read(self):
        # A bare MagicMock's read1() returns a truthy mock for ever; the loop must stop at once
        r = mock.MagicMock()
        with self.assertRaisesRegex(musicbrainz.MBError, "unreadable response"):
            musicbrainz._read_capped(r, 100, "https://example.invalid/x")
        self.assertEqual(r.read1.call_count, 1)

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

    def test_trickled_headers_hit_the_deadline_too(self):
        # the status line and headers at one byte per 0.2 s: every socket read succeeds, and the
        # body loop is never reached
        import socket
        srv = socket.create_server(("127.0.0.1", 0))
        stop = threading.Event()

        def serve():
            conn, _ = srv.accept()
            with conn:
                conn.recv(65536)
                for b in b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}":
                    if stop.is_set():
                        return
                    try:
                        conn.sendall(bytes([b]))
                    except OSError:
                        return
                    _real_sleep(0.2)

        threading.Thread(target=serve, daemon=True).start()
        try:
            t0 = time.monotonic()
            with self.assertRaisesRegex(musicbrainz.MBError, "no response in time"):
                musicbrainz._get(f"http://127.0.0.1:{srv.getsockname()[1]}/h", None, retries=1, deadline=1.0)
            self.assertLess(time.monotonic() - t0, 4)
        finally:
            stop.set()
            srv.close()

    def test_set_server_takes_https_only(self):
        for bad in ("http://127.0.0.1:8080", "http://localhost", "ftp://musicbrainz.org", "musicbrainz.org"):
            with self.assertRaises(musicbrainz.MBError, msg=bad):
                musicbrainz.set_server(bad)
        self.assertTrue(musicbrainz.API.startswith(self.srv.base))      # unchanged by the refusals

    def test_ps3_rate_floor_setting(self):
        from pysacd import remote
        self.assertEqual(remote._min_fetch_rate(None), 256 * 1024)
        self.assertEqual(remote._min_fetch_rate("64"), 64 * 1024)
        for bad in ("", "0", "-5", "abc", "nan", "1e99"):
            self.assertEqual(remote._min_fetch_rate(bad), 256 * 1024, bad)

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

    def test_cover_redirects_stay_on_the_archive(self):
        # #155: a cover download follows redirects only to the Cover Art Archive and archive.org
        h = musicbrainz._Redirects(hosts=musicbrainz.COVER_HOSTS)
        req = urllib.request.Request("https://coverartarchive.org/release/x/front")
        with mock.patch.object(musicbrainz, "API", "https://musicbrainz.org/ws/2"):
            for ok in ("https://archive.org/download/x.jpg", "https://ia800100.us.archive.org/1/x.jpg",
                       "https://coverartarchive.org/y", "https://ARCHIVE.org./x"):
                with self.subTest(ok=ok):
                    self.assertIsNotNone(h.redirect_request(req, io.BytesIO(), 302, "Found", {}, ok))
            for bad in ("https://example.invalid/x.jpg", "https://archive.org.example.invalid/x",
                        "https://notarchive.org/x", "https://archive.org@example.invalid/x",
                        "https://musicbrainz.org/x", "https:///x"):
                with self.subTest(bad=bad), self.assertRaisesRegex(
                        musicbrainz.RedirectHostError, "only coverartarchive.org and archive.org hosts"):
                    h.redirect_request(req, io.BytesIO(), 302, "Found", {}, bad)
            # the scheme rule still comes first
            with self.assertRaisesRegex(musicbrainz.MBError, "non-https"):
                h.redirect_request(req, io.BytesIO(), 302, "Found", {}, "http://archive.org/x")
            # other requests are not limited to those hosts
            self.assertIsNotNone(musicbrainz._Redirects().redirect_request(
                req, io.BytesIO(), 302, "Found", {}, "https://example.invalid/x"))

    def test_cover_redirect_host_must_be_a_plain_name(self):
        # #172: urllib connects to the percent-decoded netloc, not to urlsplit().hostname, so
        # only an exact host name (optionally :443) passes
        h = musicbrainz._Redirects(hosts=musicbrainz.COVER_HOSTS)
        req = urllib.request.Request("https://coverartarchive.org/release/x/front")
        with mock.patch.object(musicbrainz, "API", "https://musicbrainz.org/ws/2"):
            for ok in ("https://archive.org:443/x", "https://dn1.ca.archive.org/x", "https://Archive.Org/x"):
                with self.subTest(ok=ok):
                    self.assertIsNotNone(h.redirect_request(req, io.BytesIO(), 302, "Found", {}, ok))
            for bad in ("https://evil.example\\@archive.org/x", "https://evil.example%2f.archive.org/x",
                        "https://evil.example%23.archive.org/x", "https://evil.example%40archive.org/x",
                        "https://evil.example%5c.archive.org/x", "https://.archive.org/x",
                        "https://archive.org../x", "https://a..archive.org/x", "https://archive.org:8443/x",
                        "https://archive.org:/x", "https://archive.org\t/x", "https://arch\nive.org/x", " https://archive.org/x",
                        "https://archive.org%09/x",
                        "https://archive.org%00/x", "https://user@archive.org/x", "https://[::1]/x",
                        "https://[archive.org]/x", "https://[/x", "https://archive.org\u2100/x"):
                with self.subTest(bad=bad), self.assertRaises(musicbrainz.MBError):
                    h.redirect_request(req, io.BytesIO(), 302, "Found", {}, bad)

    def test_host_check_stands_alone(self):
        with mock.patch.object(musicbrainz, "API", "https://musicbrainz.org/ws/2"):
            for bad in ("ftp://archive.org/x", "http://archive.org/x", "http://localhost/x",
                        "http://127.0.0.1/x", "file:///archive.org", "//archive.org/x", "archive.org"):
                with self.subTest(bad=bad):
                    self.assertFalse(musicbrainz._host_allowed(bad, musicbrainz.COVER_HOSTS))
            self.assertTrue(musicbrainz._host_allowed("https://archive.org/x", musicbrainz.COVER_HOSTS))
        # the loopback http scaffolding, only while API itself is loopback http (setUp)
        self.assertTrue(musicbrainz._host_allowed("http://127.0.0.1:1/x", musicbrainz.COVER_HOSTS))

    def test_malformed_redirect_is_an_error_not_a_traceback(self):
        for i, loc in enumerate(("https://[archive.org]/x", "https://[/x", "https://archive.org\u2100/x")):
            _Handler.routes[f"/m{i}"] = (302, {"Location": loc.encode("utf-8").decode("latin-1")}, [b""], 0)
            _Handler.seen = []
            with self.subTest(loc=loc):
                with self.assertRaises(musicbrainz.MBError):
                    musicbrainz._get(self.srv.base + f"/m{i}", None)
                with self.assertRaises(musicbrainz.RedirectHostError):
                    musicbrainz._get(self.srv.base + f"/m{i}", None, redirect_hosts=musicbrainz.COVER_HOSTS)
                self.assertEqual(len(_Handler.seen), 2)      # one request each, no retry

    def test_cover_redirect_elsewhere_is_reported_not_followed(self):
        # The stub's redirect handler sees a redirect off the archive: nothing is fetched from
        # it, the release-group URL is still tried, and the caller is told why there is no cover
        release = {"id": "00000000-0000-4000-8000-000000000001",
                   "release-group": {"id": "00000000-0000-4000-8000-000000000002"}}
        seen = []

        def opened(req, **kw):
            seen.append((req.full_url, kw.get("redirect_hosts")))
            raise musicbrainz.RedirectHostError("redirect to example.invalid refused: only ... hosts")

        with mock.patch.object(musicbrainz, "_open", side_effect=opened), \
                mock.patch.object(musicbrainz, "_last_request", 0.0), \
                self.assertRaisesRegex(musicbrainz.RedirectHostError, "example.invalid.*newer pysacd"):
            musicbrainz.fetch_cover(release, None)
        self.assertEqual([h for _, h in seen], [musicbrainz.COVER_HOSTS] * 2)   # no retries
        self.assertIn("/release-group/", seen[1][0])

    def test_cover_from_the_second_url_hides_a_refused_first(self):
        release = {"id": "00000000-0000-4000-8000-000000000001",
                   "release-group": {"id": "00000000-0000-4000-8000-000000000002"}}
        jpeg = b"\xff\xd8\xff\xe0" + bytes(20)

        def opened(req, **kw):
            if "/release-group/" not in req.full_url:
                raise musicbrainz.RedirectHostError("redirect to example.invalid refused")
            return HTTPLimits.response(jpeg, "image/jpeg")   # a real stream behind read1()

        with mock.patch.object(musicbrainz, "_open", side_effect=opened), \
                mock.patch.object(musicbrainz, "_last_request", 0.0):
            self.assertEqual(musicbrainz.fetch_cover(release, None), (jpeg, "image/jpeg"))

    def test_loopback_cover_redirect_followed_off_host_refused(self):
        # End to end through urllib against the local stub: a loopback redirect (test scaffolding)
        # is followed, one to another host is refused before any connection to it
        _Handler.routes["/cover"] = (302, {"Location": "/img"}, [b""], 0)
        _Handler.routes["/img"] = (200, {}, [b"IMG"], 0)
        self.assertEqual(musicbrainz._get(self.srv.base + "/cover", None,
                                          redirect_hosts=musicbrainz.COVER_HOSTS)[0], b"IMG")
        _Handler.routes["/away"] = (302, {"Location": "https://example.invalid/x.jpg"}, [b""], 0)
        _Handler.seen = []
        with self.assertRaisesRegex(musicbrainz.RedirectHostError, "example.invalid refused"):
            musicbrainz._get(self.srv.base + "/away", None, redirect_hosts=musicbrainz.COVER_HOSTS)
        self.assertEqual(len(_Handler.seen), 1)       # asked once, not retried

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
