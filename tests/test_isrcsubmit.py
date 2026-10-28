"""ISRC submission to MusicBrainz (#34): planning, validation, credentials and the POST.

No test talks to MusicBrainz. Lookups are patched, and the submission goes to a
local HTTP server that records the request.
"""

import http.server
import os
import stat
import tempfile
import threading
import types
import typing
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from pysacd import isrcsubmit, musicbrainz

R = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(10)]     # recording ids
NS = "{http://musicbrainz.org/ns/mmd-2.0#}"


def area(*isrcs):
    return types.SimpleNamespace(tracks=[types.SimpleNamespace(isrc=i) for i in isrcs])


def match(tracks, method="barcode"):
    medium = {"format": "Hybrid SACD (SACD layer, 2 channels)",
              "tracks": [{"title": t, "recording": {"id": rid, "isrcs": have}} for t, rid, have in tracks]}
    return musicbrainz.Match({"id": "11111111-2222-4333-8444-555555555555"}, medium, 0.1, 0, method)


class Normalise(unittest.TestCase):
    def test_valid_forms(self):
        self.assertEqual(isrcsubmit.normalise_isrc("usabc0412345"), "USABC0412345")
        self.assertEqual(isrcsubmit.normalise_isrc(" US-AB1-04-12345 "), "USAB10412345")

    def test_rejected(self):
        for bad in (None, "", "000000000000", "USABC0000000", "USABC04123", "USABC041234567",
                    "1SABC0412345", "US\x1b[2JC04123", "USABC04I2345", 'US"/><x0412345'):
            with self.subTest(bad=bad):
                self.assertIsNone(isrcsubmit.normalise_isrc(bad))


@mock.patch.object(isrcsubmit, "_isrc_recordings", return_value=[])
class Planning(unittest.TestCase):
    def test_actions(self, _):
        m = match([("a", R[0], []), ("b", R[1], ["GBAAA0400002"]), ("c", R[2], []), ("d", R[3], []),
                   ("e", R[4], []), ("f", "not-an-id", [])])
        p = isrcsubmit.plan(m, area("GBAAA0400001", "GBAAA0400002", None, "000000000000", "GBAAA0400001",
                                    "GBAAA0400006"), None)
        self.assertEqual([i.action for i in p.items], ["submit", "present", "none", "invalid", "duplicate", "invalid"])
        self.assertEqual([(i.recording, i.isrc) for i in p.to_submit], [(R[0], "GBAAA0400001")])

    def test_conflict_with_another_recording(self, lookup):
        lookup.side_effect = lambda isrc, contact: [R[7]] if isrc == "GBAAA0400001" else [R[1]]
        p = isrcsubmit.plan(match([("a", R[0], []), ("b", R[1], [])]), area("GBAAA0400001", "GBAAA0400002"), None)
        self.assertEqual([i.action for i in p.items], ["conflict", "submit"])
        self.assertIn(R[7], p.items[0].note)

    def test_text_search_match_refused(self, _):
        with self.assertRaisesRegex(musicbrainz.MBError, "text"):
            isrcsubmit.plan(match([("a", R[0], [])], method="text"), area("GBAAA0400001"), None)

    def test_track_count_mismatch_refused(self, _):
        with self.assertRaisesRegex(musicbrainz.MBError, "tracks"):
            isrcsubmit.plan(match([("a", R[0], [])]), area("GBAAA0400001", "GBAAA0400002"), None)

    def test_body(self, _):
        p = isrcsubmit.plan(match([("a", R[0], []), ("b", R[1], [])]), area("GBAAA0400001", "GBAAA0400002"), None)
        root = ET.fromstring(isrcsubmit.body(p))
        recs = root.findall(f"{NS}recording-list/{NS}recording")
        self.assertEqual([(r.get("id"), r.find(f"{NS}isrc-list/{NS}isrc").get("id")) for r in recs],
                         [(R[0], "GBAAA0400001"), (R[1], "GBAAA0400002")])

    def test_offer_dry_run_and_terminal_safety(self, _):
        out = []
        m = match([("evil\x1b]52;c;AAAA\x07title", R[0], [])])
        with mock.patch.object(isrcsubmit, "submit") as sub:
            n = isrcsubmit.offer(m, area("GBAAA0400001"), None, dry_run=True, out=out.append)
        self.assertEqual(n, 0)
        sub.assert_not_called()
        self.assertNotIn("\x1b", "\n".join(out))
        self.assertIn("would be submitted", out[-1])

    def test_offer_asks(self, _):
        m = match([("a", R[0], [])])
        with mock.patch.object(isrcsubmit, "submit") as sub, mock.patch("os.isatty", return_value=True):
            self.assertEqual(isrcsubmit.offer(m, area("GBAAA0400001"), None, out=lambda s: None, ask=lambda q: "n"), 0)
            sub.assert_not_called()
            self.assertEqual(isrcsubmit.offer(m, area("GBAAA0400001"), None, out=lambda s: None, ask=lambda q: "y"), 1)
            sub.assert_called_once()

    def test_offer_without_terminal_needs_yes(self, _):
        m = match([("a", R[0], [])])
        with mock.patch.object(isrcsubmit, "submit") as sub, mock.patch("os.isatty", return_value=False):
            with self.assertRaisesRegex(musicbrainz.MBError, "--yes"):
                isrcsubmit.offer(m, area("GBAAA0400001"), None, out=lambda s: None)
            sub.assert_not_called()


class Tokens(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "pysacd" / "token.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_private_round_trip(self):
        isrcsubmit.save_token({"access_token": "t"}, self.path)
        if os.name == "posix":      # Windows has no POSIX mode bits
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(isrcsubmit.load_token(self.path), {"access_token": "t"})

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "no O_NOFOLLOW on this platform")
    def test_symlink_refused(self):
        target = Path(self.tmp.name) / "elsewhere"
        target.write_text("{}")
        self.path.parent.mkdir()
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            isrcsubmit.load_token(self.path)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_readable_by_others_refused(self):
        for mode in (0o100640, 0o100604, 0o100620):
            with self.subTest(mode=oct(mode)), self.assertRaisesRegex(musicbrainz.MBError, "chmod 600"):
                isrcsubmit.check_private(mode, self.path)
        isrcsubmit.check_private(0o100600, self.path)


class _Server(http.server.BaseHTTPRequestHandler):
    reply = (200, b"<message><text>OK</text></message>")
    seen: typing.ClassVar[list] = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        type(self).seen.append((self.path, dict(self.headers), self.rfile.read(n)))
        code, body = type(self).reply
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Submission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token = Path(self.tmp.name) / "token.json"
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _Server)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.api = mock.patch.object(musicbrainz, "API", f"http://127.0.0.1:{self.httpd.server_port}/ws/2")
        self.api.start()
        _Server.seen, _Server.reply = [], (200, b"<message><text>OK</text></message>")
        with mock.patch.object(isrcsubmit, "_isrc_recordings", return_value=[]):
            self.plan = isrcsubmit.plan(match([("a", R[0], [])]), area("GBAAA0400001"), None)

    def tearDown(self):
        self.api.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_bearer_post(self):
        isrcsubmit.save_token({"access_token": "abc", "expires_at": 4e9, "server": musicbrainz.API}, self.token)
        isrcsubmit.submit(self.plan, "me@example.org", token_path=self.token)
        path, headers, body = _Server.seen[0]
        self.assertTrue(path.startswith("/ws/2/recording/?client=pysacd-"))
        self.assertEqual(headers["Authorization"], "Bearer abc")
        self.assertIn("me@example.org", headers["User-Agent"])
        self.assertIn(R[0].encode(), body)

    def test_refusal_reported(self):
        isrcsubmit.save_token({"access_token": "abc", "expires_at": 4e9, "server": musicbrainz.API}, self.token)
        _Server.reply = (400, b"<error><text>Invalid ISRC</text></error>")
        with self.assertRaisesRegex(musicbrainz.MBError, "HTTP 400: Invalid ISRC"):
            isrcsubmit.submit(self.plan, None, token_path=self.token)

    def test_token_for_another_server_not_used(self):
        isrcsubmit.save_token({"access_token": "abc", "expires_at": 4e9, "server": "https://x/ws/2"}, self.token)
        with self.assertRaisesRegex(musicbrainz.MBError, "not signed in"):
            isrcsubmit.submit(self.plan, None, token_path=self.token)
        self.assertEqual(_Server.seen, [])

    def test_set_server_https_only(self):
        with self.assertRaises(musicbrainz.MBError):
            musicbrainz.set_server("http://test.musicbrainz.org")


class CommandLine(unittest.TestCase):
    def test_help_lists_commands(self):
        import contextlib
        import io

        from pysacd import __main__ as cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["submit-isrcs", "--help"])
        self.assertIn("--dry-run", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
