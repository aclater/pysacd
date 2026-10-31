"""#47 part B: name truncation, case/NFC collisions, decoding before sanitising, MusicBrainz types.

(Part A, safetext's filtering and terminal output, is tested in tests/test_safetext.py.)
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import sacdgen

from pysacd import _native, convert, musicbrainz, safetext, tagging
from pysacd.convert import NAME_MAX_BYTES, _clusters, name_key, render, unique_path
from pysacd.toc import read_toc

FRAME = bytes([0x69]) * (4704 * 2)          # DSD silence, 2 channels
SACD_EXTRACT = os.environ.get("SACD_EXTRACT")


def _native_ok() -> bool:
    try:
        _native._load()
        return True
    except (ImportError, OSError, RuntimeError):
        return False


class Truncation(unittest.TestCase):
    """The 240-byte cap cuts on a grapheme boundary, after sanitising, and tidies the cut."""

    CASES: ClassVar[dict[str, str]] = {
        "ascii": "x" * 400,
        "cjk": "表示" * 100,                                  # 3 bytes each
        "combining": "e\u0301" * 150,                          # é as e + combining acute
        "zwj family": "\U0001f468\u200d\U0001f469\u200d\U0001f467" * 30,
        "flags": "\U0001f1ef\U0001f1f5\U0001f1eb\U0001f1f7" * 40,   # JP FR
        "skin tones": "\U0001f44d\U0001f3fd" * 40,
        "hangul jamo": "\u1100\u1161\u11a8" * 60,               # conjoining jamo
        "variation selectors": "❤\ufe0f" * 80,
    }

    def test_cut_on_cluster_boundary(self):
        for name, s in self.CASES.items():
            with self.subTest(name=name):
                out = render("{t}", {"t": s}, False)
                b = out.encode("utf-8")                     # strict: valid UTF-8, no surrogates
                self.assertLessEqual(len(b), NAME_MAX_BYTES)
                self.assertTrue(out)
                norm = unicodedata.normalize("NFC", safetext.name_component(s))   # what gets cut
                # A whole-cluster prefix of the (normalised) input: nothing half-cut
                self.assertTrue(norm.startswith(out), name)
                self.assertEqual(_clusters(norm)[:len(_clusters(out))], _clusters(out))

    def test_trailing_dot_space_after_cut(self):
        s = "a" * 238 + " ." + "b" * 50
        out = render("{t}", {"t": s}, False)
        self.assertFalse(out.endswith((" ", ".")), repr(out[-5:]))

    def test_device_name_rechecked_after_cut(self):
        # The cut drops the huge last cluster, exposing "aux": still escaped with --portable-names
        s = "aux " + "x" + "\u0301" * 150
        self.assertEqual(render("{t}", {"t": s}, True), "_aux")

    def test_extension_and_suffix_fit(self):
        for name, s in self.CASES.items():
            with self.subTest(name=name):
                p = Path(render("{t}", {"t": s}, False) + ".flac")
                q = unique_path(p, {name_key(p)})               # forced collision: " (2)"
                self.assertTrue(q.name.endswith(" (2).flac"))
                self.assertLessEqual(len(q.name.encode()), 255)

    def test_nfc(self):
        self.assertEqual(render("{t}", {"t": "Cafe\u0301"}, False), "Café")


class Collisions(unittest.TestCase):
    def test_unique_path(self):
        seen: set[str] = set()
        names = ["Intro.dsf", "intro.dsf", "INTRO.dsf", "Café.dsf", "Cafe\u0301.dsf", "Straße.dsf",
                 "STRASSE.dsf", "Outro.dsf"]
        got = [unique_path(Path("d") / n, seen).name for n in names]
        self.assertEqual(got, ["Intro.dsf", "intro (2).dsf", "INTRO (3).dsf", "Café.dsf",
                               "Cafe\u0301 (2).dsf", "Straße.dsf", "STRASSE (2).dsf", "Outro.dsf"])
        self.assertEqual(len({name_key(Path("d") / g) for g in got}), len(got))

    def test_other_directories_dont_collide(self):
        seen: set[str] = set()
        self.assertEqual(unique_path(Path("a/Intro.dsf"), seen), Path("a/Intro.dsf"))
        self.assertEqual(unique_path(Path("b/intro.dsf"), seen), Path("b/intro.dsf"))

    @unittest.skipUnless(_native_ok(), "native engine not built")
    def test_convert_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            iso = root / "c.iso"
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [FRAME] * 30, [10, 10, 10], dst=False,
                                                     titles=["Intro", "intro", "Outro"])])
            out = root / "out"
            from pysacd import __main__ as cli
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = cli.main(["convert", "--no-musicbrainz", "--no-cover", "--file-template", "{title}",
                               "--dir-template", "x", "-o", str(out), str(iso)])
            self.assertEqual(rc, 0)
            self.assertEqual(sorted(p.name for p in (out / "x").iterdir()),
                             ["Intro.dsf", "Outro.dsf", "intro (2).dsf"])


class Decoding(unittest.TestCase):
    """Disc text is decoded before any check, and never yields lone surrogates."""

    def raw_title(self, charset: int, raw: bytes) -> str:
        """The album title scarletbook reads when the disc stores these bytes for it: a disc
        built with a placeholder title of the same length, then patched."""
        placeholder = "Q" * len(raw)
        with tempfile.TemporaryDirectory() as d:
            iso = Path(d) / "t.iso"
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [FRAME] * 10, [10], dst=False, titles=["t"], charset=charset)],
                              info=sacdgen.DiscInfo(title=placeholder, artist="a", charset=charset))
            img = iso.read_bytes()
            self.assertGreaterEqual(img.count(placeholder.encode()), 1)   # every copy of the master TOC
            iso.write_bytes(img.replace(placeholder.encode(), raw))
            return read_toc(iso).text["album_title"]

    def test_codecs(self):
        cases = [
            (b"\x83\x5c\x95\x5c", 3, "ソ表"),        # Shift-JIS, 0x5C trail bytes
            (b"\xb3\x5c", 6, "許"),                     # Big5, 0x5C trail byte
            (b"\xc4\xe3\xba\xc3", 5, "你好"),         # GB 2312
            (b"\xc7\xd1\xb1\xb9", 4, "한국"),         # KSC 5601
            (b"a\x01b", 2, "a b"),                      # controls become spaces
        ]
        for raw, charset, want in cases:
            with self.subTest(charset=charset):
                self.assertEqual(self.raw_title(charset, raw), want)

    def test_invalid_sequences_replace(self):
        for raw, charset in ((b"ab\x83", 3), (b"\xff\xfe\xfd", 6), (b"\xa1", 5), (b"\xc7", 4),
                             (b"\x83\x01", 3)):
            with self.subTest(charset=charset, raw=raw):
                s = self.raw_title(charset, raw)
                self.assertIn("\ufffd", s)
                s.encode("utf-8")                                   # strict: no surrogates
                self.assertFalse(any(c < " " for c in s))

    def disc(self, root: Path, charset: int, titles: list[str], album: str, artist: str = "能ソ") -> Path:
        iso = root / f"cs{charset}.iso"
        sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [FRAME] * (10 * len(titles)), [10] * len(titles), dst=False,
                                                 titles=titles, charset=charset)],
                          info=sacdgen.DiscInfo(title=album, artist=artist, charset=charset))
        return iso

    def test_toc_and_json(self):
        with tempfile.TemporaryDirectory() as d:
            iso = self.disc(Path(d), 3, ["ソフト表示", "ダメ\\テスト"], "表ソ アルバム")
            disc = read_toc(iso)
            self.assertEqual([t.title for t in disc.stereo.tracks], ["ソフト表示", "ダメ¥テスト"])
            from pysacd import __main__ as cli
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["toc", "--json", str(iso)])
            json.loads(buf.getvalue())
            buf.getvalue().encode("utf-8")

    @unittest.skipUnless(_native_ok(), "native engine not built")
    def test_convert_names(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            iso = self.disc(root, 6, ["許功蓋", "成功"], "許蓋", "歌手")         # Big5 with 0x5C trail bytes
            out = root / "out"
            from pysacd import __main__ as cli
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                # explicit templates: the result mustn't depend on the user's own config file
                self.assertEqual(cli.main(["convert", "--no-musicbrainz", "--no-cover", "--dir-template", "x",
                                           "--file-template", "{tracknumber:02d} - {title}",
                                           "-o", str(out), str(iso)]), 0)
            names = [p.name for p in out.rglob("*.dsf")]
            self.assertEqual(sorted(names), ["01 - 許功蓋.dsf", "02 - 成功.dsf"])

    def run_both(self, root: Path, iso: Path) -> list[dict[bytes, bytes]]:
        src = Path(__file__).resolve().parents[1] / "src"
        trees = []
        for tool in ("se", "py"):
            out = root / tool
            out.mkdir()
            cmd = [SACD_EXTRACT] if tool == "se" else [sys.executable, "-m", "pysacd.extract", "--no-cfg"]
            subprocess.run([*cmd, "-2", "-s", "-C", "-i", str(iso)], cwd=out, check=True,
                           env=dict(os.environ, PYTHONPATH=str(src)), capture_output=True)
            trees.append({os.fsencode(p.relative_to(out)): p.read_bytes() for p in sorted(out.rglob("*"))
                          if p.is_file() and p.suffix in (".dsf", ".cue")})
        return trees

    @staticmethod
    def audio(tree: dict[bytes, bytes]) -> list[bytes]:
        """The DSF data chunks (the ID3 tags differ: UTF-8 against the raw disc bytes)."""
        out = []
        for name, v in sorted(tree.items()):
            if name.endswith(b".dsf"):
                size = int.from_bytes(v[84:92], "little")         # "data" chunk at 80, 12-byte header
                out.append(v[92:80 + size])
        return out

    @unittest.skipUnless(SACD_EXTRACT and _native_ok(), "set SACD_EXTRACT to compare with the real sacd_extract")
    def test_sjis_big5_match_sacd_extract(self):
        """sacd_extract decodes Shift-JIS and Big5, 0x5C trail bytes included: byte-identical."""
        for charset, titles, album, artist in ((3, ["ソフト表示", "ダメ\\テスト"], "表ソ アルバム", "能ソ"),
                                               (6, ["許功蓋", "成功"], "許蓋", "歌手")):
            with self.subTest(charset=charset), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                iso = self.disc(root, charset, titles, album, artist)
                se, py = self.run_both(root, iso)
                self.assertTrue(se)
                self.assertEqual(sorted(py), sorted(se))                  # names, cue sheets included
                for name, data in se.items():
                    if name.endswith(b".dsf"):
                        self.assertEqual(py[name], data, name)            # (cue: version line differs)

    @unittest.skipUnless(SACD_EXTRACT and _native_ok(), "set SACD_EXTRACT to compare with the real sacd_extract")
    def test_ksc_gb_documented_difference(self):
        """sacd_extract doesn't convert KSC 5601 or GB 2312 text: it writes the raw disc bytes
        as names, which aren't UTF-8. pysacd decodes them (docs/sacd_extract.md). The audio is
        identical."""
        for charset, titles, album, artist in ((4, ["한국어", "노래"], "앨범", "가수"),
                                               (5, ["你好", "世界"], "专辑", "歌手")):
            with self.subTest(charset=charset), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                iso = self.disc(root, charset, titles, album, artist)
                se, py = self.run_both(root, iso)
                for name in py:
                    name.decode("utf-8")                                  # pysacd: valid UTF-8 names
                self.assertIn(f"{album}/01 - {titles[0]}.dsf".encode(), py)
                codec = sacdgen.CHARSET_CODECS[charset]
                self.assertIn(f"{album}/01 - {titles[0]}.dsf".encode(codec), se)   # raw disc bytes
                self.assertEqual(self.audio(py), self.audio(se))


class MusicBrainzTypes(unittest.TestCase):
    """Malformed MusicBrainz fields are dropped, and the disc's text is used instead."""

    GOOD: ClassVar[dict] = {
        "id": "r1", "title": "Album", "date": "2004", "barcode": "123", "country": "JP", "status": "Official",
        "artist-credit": [{"name": "A", "joinphrase": "", "artist": {"id": "a1", "name": "A"}}],
        "release-group": {"id": "g1", "first-release-date": "2003", "primary-type": "Album"},
        "label-info": [{"label": {"name": "L"}, "catalog-number": "C-1"}],
        "media": [{"format": "Hybrid SACD (SACD layer, 2 channels)", "position": 1, "tracks": [
            {"id": "t1", "title": "One", "length": 1000, "position": 1,
             "recording": {"id": "rec1", "title": "One", "isrcs": ["XX0000000001"]}}]}],
    }

    def test_integer_fields(self):
        # 32-bit range, digit strings up to 10 long; a huge digit string must not reach int()
        for v, want in [("2147483647", 2147483647), ("2147483648", None), ("12", 12), (5, 5), (True, None),
                        ("-1", None), ("\uff11\uff12", None), ("9" * 11, None), ("9" * 5000, None)]:
            self.assertEqual(musicbrainz._int(v), want, repr(v)[:20])

    def test_good_release_kept(self):
        rel = musicbrainz.clean_release(self.GOOD)
        assert rel is not None
        self.assertEqual(rel["title"], "Album")
        self.assertEqual(rel["media"][0]["tracks"][0]["position"], 1)
        self.assertEqual(musicbrainz.credit_name(rel["artist-credit"]), "A")

    def test_hostile_fields_dropped(self):
        bad = json.loads(json.dumps(self.GOOD))
        bad["title"] = ["not", "a", "string"]
        bad["date"] = None
        bad["artist-credit"] = [None, "x", {"name": 5, "artist": {"id": "a1", "name": "A"}, "joinphrase": None},
                                {"artist": None}]
        bad["release-group"] = "nope"
        bad["label-info"] = [None, {"label": "L"}, {"label": {"name": None}, "catalog-number": 7}]
        t = bad["media"][0]["tracks"][0]
        t["length"], t["position"], t["title"] = "long", "A1", {"x": 1}
        t["recording"]["isrcs"] = "XX0000000001"
        bad["media"].append("junk")
        rel = musicbrainz.clean_release(bad)
        assert rel is not None
        self.assertNotIn("title", rel)
        self.assertNotIn("date", rel)
        self.assertEqual(musicbrainz.credit_name(rel["artist-credit"]), "A")     # name fell back to the artist's
        self.assertNotIn("release-group", rel)
        self.assertEqual(rel["label-info"], [{}, {}])            # the None entry is dropped
        self.assertEqual(len(rel["media"]), 1)
        tr = rel["media"][0]["tracks"][0]
        self.assertNotIn("length", tr)
        self.assertNotIn("position", tr)
        self.assertNotIn("title", tr)
        self.assertEqual(tr["recording"]["isrcs"], [])

    def test_no_id_rejected(self):
        for v in (None, [], {"id": 5}, {"title": "x"}):
            self.assertIsNone(musicbrainz.clean_release(v))

    def test_string_track_numbers_and_tags_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            iso = Path(d) / "m.iso"
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [FRAME] * 10, [10], dst=False, titles=["Disc Title"])])
            disc = read_toc(iso)
        area, track = disc.stereo, disc.stereo.tracks[0]
        bad = json.loads(json.dumps(self.GOOD))
        t = bad["media"][0]["tracks"][0]
        t["title"], t["position"] = None, "3"                                 # string position is fine
        t["recording"]["title"] = 7                                           # and no recording title
        rel = musicbrainz.clean_release(bad)
        assert rel is not None
        match = musicbrainz.Match(rel, rel["media"][0], 0.0, 0, "release-id")
        tags = tagging.base_tags(disc, area, track)
        tagging.apply_musicbrainz(tags, match, track)
        self.assertEqual(tags["TITLE"], ["Disc Title"])                        # disc text kept
        self.assertEqual(tags["TRACKNUMBER"], ["3"])
        v = convert.naming_values(tags, area, convert.Options())
        self.assertEqual(render(convert.Options().file_template, v, False), "03 - Disc Title")

    def test_overlay_failure_keeps_disc_text(self):
        with tempfile.TemporaryDirectory() as d:
            iso = Path(d) / "m.iso"
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [FRAME] * 10, [10], dst=False, titles=["Disc Title"])])
            disc = read_toc(iso)
        track = disc.stereo.tracks[0]
        match = musicbrainz.Match({"id": "r"}, {"tracks": []}, 0.0, 0, "release-id")   # no track 1
        tags = tagging.base_tags(disc, disc.stereo, track)
        before = dict(tags)
        with self.assertLogs("pysacd.tagging", "WARNING"):
            tagging.apply_musicbrainz(tags, match, track)
        self.assertEqual(tags, before)

    def test_tag_numbers(self):
        for s, want in (("7", 7), ("3/12", 3), ("A1", 0), ("", 0), (" 12 ", 12)):
            self.assertEqual(convert._tag_int(s, 0), want, s)

    def test_find_release_survives_hostile_json(self):
        def fake(path, contact, **params):
            if path == "release":
                return {"releases": [None, {"id": 5}, {"id": "r1"}]}
            bad = json.loads(json.dumps(self.GOOD))
            bad["media"][0]["tracks"][0]["length"] = "x"
            return bad
        with tempfile.TemporaryDirectory() as d:
            iso = Path(d) / "m.iso"
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [FRAME] * 10, [10], dst=False)],
                              info=sacdgen.DiscInfo(album_catalog="4988001234567"))
            disc = read_toc(iso)
        with mock.patch.object(musicbrainz, "_json", side_effect=fake):
            musicbrainz.find_release(disc, disc.stereo, None)       # no exception, whatever it returns


if __name__ == "__main__":
    unittest.main()
