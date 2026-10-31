"""The two TOC engines agree (#85, phase 2): scarletbook-rs, the default, and pysacd's own parser.

Every field of the Disc, Area and Track objects, `pysacd toc --json` and the beets-pysacd
fingerprint must be the same from both, on the hostile corpus, on synthetic discs in every
character set, and on seeded mutations of a valid TOC. Only the warnings' wording differs
(scarletbook gives the specification's diagnostic codes). tools/toc_compare.py runs the same
comparison on any images, for the conformance corpus and real discs.
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import sacdgen
import toc_compare

from pysacd import _sbtoc, toc
from pysacd.toc import SACDError, SACDImage, Timecode, read_toc

MUTATIONS = int(os.environ.get("PYSACD_TOC_MUTATIONS", "150"))


def _native_ok() -> bool:
    try:
        from pysacd import _native
        _native._load()
        return True
    except (ImportError, OSError, RuntimeError):
        return False


@unittest.skipUnless(_native_ok(), "native engine not built")
class Agreement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def same(self, name: str, image: bytes) -> dict:
        path = self.dir / f"{name}.iso"
        path.write_bytes(image)
        r = toc_compare.compare(path)
        self.assertIn(r["outcome"], ("identical", "both rejected"), (name, r["fields"][:5], r["errors"]))
        return r

    def test_hostile_corpus(self):
        outcomes = [self.same(h.name, h.image)["outcome"] for h in sacdgen.hostile_corpus()]
        self.assertIn("identical", outcomes)

    def test_every_charset(self):
        frames = [bytes([0x69]) * (2 * 4704)] * 150
        for charset, titles in ((2, ["Café", "Señor"]), (3, ["ソフト表示", "ダメ\\テスト"]),
                                (4, ["한국어", "노래"]), (5, ["你好", "世界"]), (6, ["許功蓋", "成功"])):
            with self.subTest(charset=charset):
                path = self.dir / f"cs{charset}.iso"
                sacdgen.build_iso(path, [sacdgen.AreaSpec(2, frames, [75, 75], dst=False, titles=titles,
                                                                   charset=charset)],
                                  info=sacdgen.DiscInfo(title=titles[0], artist=titles[1], charset=charset))
                r = toc_compare.compare(path)
                self.assertEqual(r["outcome"], "identical", r["fields"][:5])
                self.assertEqual(read_toc(path).stereo.tracks[0].title, titles[0].replace("\\", "¥"))

    def test_mutated_tocs(self):
        base = next(h.image for h in sacdgen.hostile_corpus() if h.name == "valid")
        for seed in range(MUTATIONS):
            rng = random.Random(seed)
            img = bytearray(base)
            for _ in range(rng.randint(1, 24)):
                lsn = rng.choice([510, 511, 519, 520, *range(540, 560)])
                img[lsn * 2048 + (rng.randrange(256) if rng.random() < 0.8 else rng.randrange(2048))] = rng.randrange(256)
            self.same(f"m{seed}", bytes(img))


@unittest.skipUnless(_native_ok(), "native engine not built")
class Switch(unittest.TestCase):
    def image(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = next(h for h in sacdgen.hostile_corpus() if h.name == "area-toc1-toc2-differ")
        p = Path(tmp.name) / "x.iso"
        p.write_bytes(h.image)
        return p

    def warnings(self, disc):
        return disc.warnings + [w for a in disc.areas for w in a.warnings]

    def test_default_is_scarletbook(self):
        self.assertEqual(toc.DEFAULT_TOC_ENGINE, "scarletbook")
        with mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("PYSACD_TOC_ENGINE", None)
            self.assertIn("area-toc-copies-differ (2ch area)", self.warnings(read_toc(self.image())))

    def test_environment_and_argument_choose(self):
        path = self.image()
        with mock.patch.dict(os.environ, {"PYSACD_TOC_ENGINE": "python"}):
            self.assertIn("2ch area TOC-1 and TOC-2 differ; using TOC-1", self.warnings(read_toc(path)))
            self.assertIn("area-toc-copies-differ (2ch area)", self.warnings(read_toc(path, "scarletbook")))

    def test_unknown_engine(self):
        with self.assertRaisesRegex(ValueError, "unknown TOC engine"):
            read_toc(self.image(), "libsacd")

    def test_not_sacd(self):
        with tempfile.TemporaryFile() as f:
            f.write(bytes(600 * 2048))
            f.seek(0)
            with self.assertRaises(SACDError):
                SACDImage(f).read_toc("scarletbook")

    def test_read_failure_is_an_os_error(self):
        # a source that fails after the layout check: the failure surfaces, not a parse error
        path = self.image()

        class Flaky:
            def __init__(self):
                self.f = open(path, "rb")  # noqa: SIM115
                self.reads = 0

            def seek(self, *a):
                return self.f.seek(*a)

            def tell(self):
                return self.f.tell()

            def read(self, n=-1):
                self.reads += 1
                if self.reads > 3:
                    raise OSError("connection reset")
                return self.f.read(n)

        src = Flaky()
        self.addCleanup(src.f.close)
        img = SACDImage(src)
        with self.assertRaisesRegex(OSError, "connection reset"):
            img.read_toc("scarletbook")


class Helpers(unittest.TestCase):
    def test_warning_text(self):
        w = _sbtoc.SbWarning(lsn=-1, slot=1, track=3, code=b"track-past-playtime")
        self.assertEqual(_sbtoc._warning_text(w), "track-past-playtime (multichannel area, track 3)")
        w = _sbtoc.SbWarning(lsn=600, slot=-1, track=0, code=b"audio-sector-malformed")
        self.assertEqual(_sbtoc._warning_text(w), "audio-sector-malformed (LSN 600)")
        self.assertEqual(_sbtoc._warning_text(_sbtoc.SbWarning(lsn=-1, slot=-1, track=0, code=b"x")), "x")

    def test_struct_sizes_match_the_header(self):
        import ctypes
        for struct, size in _sbtoc._SIZES.items():
            self.assertEqual(ctypes.sizeof(struct), size, struct.__name__)

    def test_stored_timecodes_only_where_the_count_agrees(self):
        # 255:255:255 is 1166880 frames: written as stored, as sacd_extract and pysacd 0.2 do
        self.assertEqual(_sbtoc._as_stored(1166880, (255, 255, 255)), Timecode(255, 255, 255))
        # a count scarletbook clamped, or no stored bytes: the count, normalised
        self.assertEqual(_sbtoc._as_stored(1166880 - 75, (255, 255, 255)), Timecode(259, 17, 30))
        self.assertEqual(_sbtoc._as_stored(75, None), Timecode(0, 1, 0))
        self.assertEqual(_sbtoc._as_stored(75, (0, 1)), Timecode(0, 1, 0))


if __name__ == "__main__":
    unittest.main()
