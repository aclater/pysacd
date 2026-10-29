"""One real MusicBrainz lookup, to notice when the live ws/2 API stops fitting pysacd's parser.

The rest of the suite never touches the network. This test is skipped unless
PYSACD_LIVE_MB=1; the weekly `live-musicbrainz` workflow sets it. It sends a single request
through pysacd's own client (so its throttle, User-Agent and response checks are what's tested),
naming the repository as the contact, as MusicBrainz asks.

The release is the 2003 EMI hybrid SACD of Pink Floyd's "The Dark Side of the Moon": three media
(CD layer, SACD stereo, SACD multichannel) of ten tracks each, long established in the database.
"""

import os
import unittest
from types import SimpleNamespace

from pysacd import musicbrainz

RELEASE_ID = "1a2a37a4-2ee5-3aa8-b1bf-4976dbb8007c"
CONTACT = "https://github.com/aclater/pysacd"
# The SACD stereo layer's track lengths in seconds, as MusicBrainz listed them in 2026
STEREO_SECONDS = [70, 169, 210, 429, 285, 382, 469, 205, 228, 132]


def _stereo_area():
    """Stand-ins for the Disc and Area that find_release reads (no disc image needed)."""
    tracks = [SimpleNamespace(duration=SimpleNamespace(seconds_float=float(s)), isrc=None) for s in STEREO_SECONDS]
    return SimpleNamespace(album_set_size=1, album_sequence_number=1), \
        SimpleNamespace(tracks=tracks, is_multichannel=False)


@unittest.skipUnless(os.environ.get("PYSACD_LIVE_MB") == "1", "live MusicBrainz test: set PYSACD_LIVE_MB=1")
class LiveMusicBrainz(unittest.TestCase):
    def test_release_lookup_still_parses_and_matches(self):
        disc, area = _stereo_area()
        # One request: a release-id match looks up just that release
        match = musicbrainz.find_release(disc, area, CONTACT, release_id=RELEASE_ID)  # type: ignore[arg-type]
        self.assertIsNotNone(match)
        assert match is not None
        rel = match.release
        self.assertEqual(rel["id"], RELEASE_ID)
        self.assertEqual(rel["title"], "The Dark Side of the Moon")
        self.assertEqual(musicbrainz.credit_name(rel["artist-credit"]), "Pink Floyd")
        self.assertEqual(rel["barcode"], "724358213621")
        self.assertTrue(rel["release-group"]["id"])
        self.assertTrue(any(li.get("catalog-number") for li in rel["label-info"]))

        formats = [m.get("format") for m in rel["media"]]
        self.assertEqual(formats, ["Hybrid SACD (CD layer)", "Hybrid SACD (SACD layer, 2 channels)",
                                   "Hybrid SACD (SACD layer, multichannel)"])
        self.assertEqual(musicbrainz.physical_discs(rel["media"]), [1, 1, 1])
        for m in rel["media"]:
            self.assertEqual(len(m["tracks"]), 10)
            for t in m["tracks"]:
                self.assertTrue(t["id"] and t["title"] and t["recording"]["id"])
                self.assertGreater(t["length"], 0)

        # The stereo area lands on the SACD stereo layer, not the CD layer
        self.assertEqual(match.medium.get("format"), "Hybrid SACD (SACD layer, 2 channels)")
        self.assertEqual(match.method, "release-id")
        self.assertLess(match.score, 3.0)


if __name__ == "__main__":
    unittest.main()
