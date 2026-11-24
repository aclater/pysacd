"""find_release: matching each area of a disc to a MusicBrainz medium (#218).

MusicBrainz often gives a hybrid disc's "SACD layer, multichannel" medium the stereo track
lengths. A multichannel mix can differ from them by several seconds a track, so the
multichannel area misses the 3 s limit on its own. It then matches through the disc's stereo
area, under the rules tested here.

The fixtures in tests/fixtures/musicbrainz are ws/2 responses with synthetic, neutral data:
the JSON shape MusicBrainz sends, with made-up ids, titles and lengths. Requests are served from
them through musicbrainz._json, so candidate_ids, lookup_release and clean_release run as they
do against the real server; nothing touches the network.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import sacdgen

from pysacd import convert, isrcsubmit, musicbrainz
from pysacd.toc import read_toc

FIXTURES = Path(__file__).parent / "fixtures" / "musicbrainz"
HYBRID = "21800000-0000-4000-8000-000000000001"
SET = "21800000-0000-4000-8000-000000000002"
CD_ONLY = "21800000-0000-4000-8000-000000000003"
CATNO = {HYBRID: "PYSACD-MB-218", SET: "PYSACD-MB-219", CD_ONLY: "PYSACD-MB-220"}

# Track lengths in seconds: what MusicBrainz lists for every layer, and the disc's own areas
STEREO = [4.2, 4.4, 4.0, 4.6]                   # MusicBrainz's lengths, within 0.1 s
STEREO_AREA = [4.25, 4.35, 4.05, 4.55]
MC_AREA = [0.2, 0.3, 0.2, 0.4]                  # about 4 s shorter a track: a different mix
SET_DISC2 = [5.2, 5.4, 5.0, 5.6]


def _json_from_fixtures(calls: list):
    search = json.loads((FIXTURES / "search-catno.json").read_text())
    releases = {}
    for f in FIXTURES.glob("release-*.json"):
        rel = json.loads(f.read_text())
        releases[rel["id"]] = rel

    def fake(path, contact, **params):
        calls.append((path, params.get("query")))
        if path == "release":
            q = params["query"]
            if q.startswith("catno:"):
                return search.get(q.split(":", 1)[1].strip('"'), {"releases": []})
            return search["text"] if q.startswith("release:") else {"releases": []}
        rid = path.split("/", 1)[1]
        if rid not in releases:
            raise musicbrainz.MBError(f"not found: {path}")
        return json.loads(json.dumps(releases[rid]))
    return fake


def _area(seconds, multichannel, isrcs=None):
    tracks = [SimpleNamespace(duration=SimpleNamespace(seconds_float=float(s)),
                              isrc=(isrcs[i] if isrcs else None)) for i, s in enumerate(seconds)]
    return SimpleNamespace(tracks=tracks, is_multichannel=multichannel,
                           speaker_config="5ch" if multichannel else "Stereo")


def _disc(stereo, multichannel, catno=CATNO[HYBRID], set_size=1, sequence=1, text=None):
    """Stand-ins for the Disc and Area that find_release reads (no disc image needed)."""
    s = _area(stereo, False) if stereo is not None else None
    m = _area(multichannel, True) if multichannel is not None else None
    disc = SimpleNamespace(album_catalog_number=catno, disc_catalog_number="", text=text or {},
                           album_set_size=set_size, album_sequence_number=sequence, stereo=s, multichannel=m)
    return disc, s, m


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.calls: list = []
        p = mock.patch.object(musicbrainz, "_json", side_effect=_json_from_fixtures(self.calls))
        p.start()
        self.addCleanup(p.stop)

    def find(self, disc, area, **kw):
        return musicbrainz.find_release(disc, area, None, **kw)


class HybridDisc(MatchTest):
    def test_stereo_area_unchanged(self):
        disc, s, _ = _disc(STEREO_AREA, MC_AREA)
        m = self.find(disc, s)
        assert m is not None
        self.assertEqual((m.release_id, m.medium["format"], m.method), (HYBRID, "Hybrid SACD (SACD layer, 2 channels)", "catno"))
        self.assertAlmostEqual(m.score, 0.05)
        self.assertFalse(m.via_stereo)
        self.assertEqual(m.warnings, [])

    def test_multichannel_matches_through_stereo_with_warning(self):
        disc, _, mc = _disc(STEREO_AREA, MC_AREA)
        m = self.find(disc, mc)
        assert m is not None
        self.assertEqual((m.release_id, m.medium["format"], m.method), (HYBRID, "Hybrid SACD (SACD layer, multichannel)", "catno"))
        self.assertTrue(m.via_stereo)
        self.assertAlmostEqual(m.score, 4.025)          # the area's own score is kept, not the stereo one
        self.assertEqual(len(m.warnings), 1)
        self.assertIn("4.0 s on average (limit 3.0 s)", m.warnings[0])
        self.assertIn("stereo area", m.warnings[0])
        # The stereo pass reuses the releases already fetched: one search, one lookup
        self.assertEqual([c[0] for c in self.calls], ["release", f"release/{HYBRID}"])

    def test_multichannel_within_limit_needs_no_fallback(self):
        disc, _, mc = _disc(STEREO_AREA, [4.0, 4.2, 4.0, 4.4])     # a mix within 0.2 s of stereo
        m = self.find(disc, mc)
        assert m is not None
        self.assertEqual(m.medium["format"], "Hybrid SACD (SACD layer, multichannel)")
        self.assertFalse(m.via_stereo)
        self.assertEqual(m.warnings, [])

    def test_track_count_mismatch_rejected(self):
        # A multichannel area of five tracks: no multichannel medium has five
        disc, _, mc = _disc(STEREO_AREA, MC_AREA + [0.3])
        self.assertIsNone(self.find(disc, mc))
        # Three tracks: a medium of three exists only on the set's first disc, not on this release
        disc, _, mc = _disc(STEREO_AREA, MC_AREA[:3])
        self.assertIsNone(self.find(disc, mc))

    def test_stereo_must_pass_the_normal_limit(self):
        for off in (3.1, 5.0):
            with self.subTest(off=off):
                disc, s, mc = _disc([x + off for x in STEREO], MC_AREA)
                self.assertIsNone(self.find(disc, s))
                self.assertIsNone(self.find(disc, mc))
        # max_mean_diff applies to the stereo area too, and is never widened
        disc, s, mc = _disc([x + 1.5 for x in STEREO], MC_AREA)
        self.assertIsNotNone(self.find(disc, mc))
        self.assertIsNone(self.find(disc, mc, max_mean_diff=1.0))

    def test_stereo_isrcs_count_but_not_zero_hits(self):
        # Stereo lengths off by 5 s, but ISRCs on every track match: still confident
        rel = json.loads((FIXTURES / "release-hybrid.json").read_text())
        isrcs = [f"XXPYS2600{i:03d}" for i in range(1, 5)]
        for medium in rel["media"]:
            for t, code in zip(medium["tracks"], isrcs):
                t["recording"]["isrcs"] = [code]
        disc, _, mc = _disc([x + 5 for x in STEREO], MC_AREA)
        disc.stereo = _area([x + 5 for x in STEREO], False, isrcs)
        with mock.patch.object(musicbrainz, "lookup_release", return_value=musicbrainz.clean_release(rel)):
            m = self.find(disc, mc)
        assert m is not None
        self.assertTrue(m.via_stereo)
        # A one-track area needs its length to match: zero ISRC hits are never enough. Before
        # #218, half of one track rounded down to zero hits, so any one-track medium matched.
        one = json.loads(json.dumps(rel))
        for medium in one["media"]:
            medium["tracks"] = medium["tracks"][:1]
            medium["tracks"][0]["recording"]["isrcs"] = []
        with mock.patch.object(musicbrainz, "lookup_release", return_value=musicbrainz.clean_release(one)):
            disc, s, mc = _disc([9.0], [0.2])
            self.assertIsNone(self.find(disc, s))
            self.assertIsNone(self.find(disc, mc))
            disc, s, mc = _disc([4.3], [0.2])                       # stereo matches by length
            self.assertFalse(self.find(disc, s).via_stereo)         # type: ignore[union-attr]
            self.assertTrue(self.find(disc, mc).via_stereo)         # type: ignore[union-attr]

    def test_multichannel_only_disc_unchanged(self):
        # No stereo area to vouch for the release: the area must match on its own, as before
        disc, _, mc = _disc(None, MC_AREA)
        self.assertIsNone(self.find(disc, mc))
        disc, _, mc = _disc(None, STEREO_AREA)
        m = self.find(disc, mc)
        assert m is not None
        self.assertEqual(m.medium["format"], "Hybrid SACD (SACD layer, multichannel)")
        self.assertFalse(m.via_stereo)

    def test_release_id_path_unchanged(self):
        # A named release is accepted without the limit, as before: no fallback, no warning
        disc, _, mc = _disc(STEREO_AREA, MC_AREA, catno="")
        m = self.find(disc, mc, release_id=HYBRID)
        assert m is not None
        self.assertEqual((m.method, m.medium["format"]), ("release-id", "Hybrid SACD (SACD layer, multichannel)"))
        self.assertFalse(m.via_stereo)
        self.assertEqual(m.warnings, [])
        self.assertEqual(self.calls, [(f"release/{HYBRID}", None)])

    def test_text_search_keeps_its_warning(self):
        disc, _, mc = _disc(STEREO_AREA, MC_AREA, catno="", text={"album_title": "Synthetic Hybrid Album"})
        self.assertIsNone(self.find(disc, mc))                      # no text search unless asked
        m = self.find(disc, mc, text_search=True)
        assert m is not None
        self.assertEqual((m.method, m.via_stereo), ("text", True))
        self.assertEqual(len(m.warnings), 2)
        self.assertIn("title search", m.warnings[1])

    def test_no_multichannel_medium_on_a_cd_release(self):
        # The stereo area matches a CD (with a warning); a CD has no multichannel layer to lend
        disc, s, mc = _disc(STEREO_AREA, MC_AREA, catno=CATNO[CD_ONLY])
        m = self.find(disc, s)
        assert m is not None
        self.assertEqual(m.medium["format"], "CD")
        self.assertIsNone(self.find(disc, mc))
        # Not even when the multichannel area's lengths are close to the CD's
        disc, s, mc = _disc(STEREO_AREA, STEREO_AREA, catno=CATNO[CD_ONLY])
        self.assertEqual(self.find(disc, mc).medium["format"], "CD")         # type: ignore[union-attr]

    def test_isrc_submission_refuses_a_fallback_match(self):
        disc, _, mc = _disc(STEREO_AREA, MC_AREA)
        m = self.find(disc, mc)
        assert m is not None
        with self.assertRaisesRegex(musicbrainz.MBError, "through the stereo area; pass --mb-release"):
            isrcsubmit.plan(m, mc, None, check_global=False)        # type: ignore[arg-type]


class MultiDiscSet(MatchTest):
    def test_second_disc_takes_its_own_multichannel_layer(self):
        disc, s, mc = _disc(SET_DISC2, MC_AREA, catno=CATNO[SET], set_size=2, sequence=2)
        sm = self.find(disc, s)
        m = self.find(disc, mc)
        assert sm is not None and m is not None
        media = sm.release["media"]
        self.assertEqual(media.index(sm.medium), 4)                 # disc 2, SACD stereo layer
        self.assertEqual(media.index(m.medium), 5)                  # disc 2, multichannel layer
        self.assertTrue(m.via_stereo)

    def test_first_disc_multichannel_programme_of_its_own_length(self):
        # Disc 1's multichannel layer has three tracks, its stereo layer four
        disc, _, mc = _disc(STEREO_AREA, MC_AREA[:3], catno=CATNO[SET], set_size=2, sequence=1)
        m = self.find(disc, mc)
        assert m is not None
        self.assertEqual(m.release["media"].index(m.medium), 2)
        # The disc doesn't say it is part of a set (as some sets don't): the stereo match by
        # lengths alone still finds disc 1, and its multichannel layer
        disc, _, mc = _disc(STEREO_AREA, MC_AREA[:3], catno=CATNO[SET])
        m = self.find(disc, mc)
        assert m is not None
        self.assertEqual(m.release["media"].index(m.medium), 2)

    def test_never_borrows_another_discs_layer(self):
        # Disc 1's stereo area, a four-track multichannel area: only disc 2 has a four-track
        # multichannel layer, so there is no match
        disc, _, mc = _disc(STEREO_AREA, MC_AREA, catno=CATNO[SET], set_size=2, sequence=1)
        self.assertIsNone(self.find(disc, mc))


FRAME2 = bytes([0x69]) * (4704 * 2)     # DSD silence: one frame of 2 channels, 6 channels
FRAME6 = bytes([0x69]) * (4704 * 6)


class ConvertEndToEnd(MatchTest):
    """pysacd convert on a generated hybrid disc: both areas tagged from MusicBrainz."""

    def test_dry_run_tags_both_areas(self):
        frames = [round(s * 75) for s in STEREO_AREA]
        mframes = [round(s * 75) for s in MC_AREA]
        with tempfile.TemporaryDirectory() as d:
            iso = Path(d) / "hybrid.iso"
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [FRAME2] * sum(frames), frames, dst=False),
                                    sacdgen.AreaSpec(6, [FRAME6] * sum(mframes), mframes, dst=False)],
                              info=sacdgen.DiscInfo(album_catalog=CATNO[HYBRID], disc_catalog=""))
            disc = read_toc(iso)
            self.assertIsNotNone(disc.stereo)
            self.assertIsNotNone(disc.multichannel)
            out, err = io.StringIO(), io.StringIO()
            opts = convert.Options(output_dir=str(Path(d) / "out"), areas="both", dry_run=True, cover=False)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(convert.convert(str(iso), opts), 0)
        err_s, out_s = err.getvalue(), out.getvalue()
        self.assertIn("Hybrid SACD (SACD layer, 2 channels)", err_s)
        self.assertIn("Hybrid SACD (SACD layer, multichannel)", err_s)
        self.assertIn("warning: multichannel track lengths differ from MusicBrainz's", err_s)
        self.assertNotIn("no confident match", err_s)
        self.assertEqual(out_s.count("Fourth Movement"), 2)                 # both areas named from MusicBrainz


if __name__ == "__main__":
    unittest.main()
