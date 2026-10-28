"""sacd_extract compatibility: naming, ID3, DSDIFF, CUE and XML.

The comparison tests run the real sacd_extract and need:
  SACD_EXTRACT=/path/to/sacd_extract   (or sacd_extract on PATH)
  PYSACD_COMPAT_DSD=<plain-DSD stereo ISO>  PYSACD_COMPAT_DST=<DST stereo+5.1 ISO>
    (default to two discs in the author's library)
  PYSACD_SLOW=1 also runs the ~1 GB DSDIFF edit-master comparison.
Output goes to a temporary directory (set TMPDIR to put it on disk).
"""

import glob
import io
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from datetime import datetime

from test_toc import make_image

from pysacd import SACDImage, read_toc
from pysacd import sbcompat as sb

SACD_EXTRACT = os.environ.get("SACD_EXTRACT") or shutil.which("sacd_extract")
DSD_ISO = os.environ.get("PYSACD_COMPAT_DSD", "/path/to/album.iso")
DST_ISO = os.environ.get("PYSACD_COMPAT_DST",
                         "/path/to/album.iso")
HAVE_REAL = bool(SACD_EXTRACT) and os.path.exists(DSD_ISO) and os.path.exists(DST_ISO)


def dff_split(path):
    """(header, footer, dst, audio_size, frame_count, [(offset, length)]) of a DSDIFF file."""
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        f.seek(0)
        head = f.read(4096)
        pos = 16
        while True:
            cid = head[pos:pos + 4]
            n, = struct.unpack_from(">Q", head, pos + 4)
            if cid in (b"DSD ", b"DST "):
                break
            pos += 12 + n + (n & 1)
        start, end = pos + 12, pos + 12 + n
        index, frames, dst = [], 0, cid == b"DST "
        if dst:
            frames, = struct.unpack_from(">I", head, start + 12)
            p = start + 18
            while p < end:
                f.seek(p + 4)
                ln, = struct.unpack(">Q", f.read(8))
                index.append((p + 12, ln))
                p += 12 + ln + (ln & 1)
            header, audio = head[:start + 18], n - 18
        else:
            header, audio = head[:start], n
        f.seek(end)
        return header, f.read(size - end), dst, audio, frames, index


def mask_creating_machine(footer: bytes) -> bytes:
    """Blank COMT's second comment (machine name + timestamp differ by design)."""
    b = bytearray(footer)
    p = b.find(b"COMT")
    q = p + 14
    ln, = struct.unpack_from(">I", b, q + 10)
    q += 14 + ln + (ln & 1)
    end = p + 12 + struct.unpack_from(">Q", b, p + 4)[0]
    b[q:end] = bytes(end - q)
    return bytes(b)


class Helpers(unittest.TestCase):
    def test_parse_format(self):
        self.assertEqual(sb.parse_format("%N - %A - %T %%", 7, "", "a", "", "t"), "07 - a - t %")
        self.assertEqual(sb.parse_format("%N - %T", 100, title="t"), " - t")

    def test_sanitize(self):
        # dots are trimmed before whitespace, as in the C code
        self.assertEqual(sb.sanitize_filename(' ..A/B: [x] "y"?. '), "..A_B_ _x_ _y__.")
        self.assertEqual(sb.sanitize_filename("..A/B.."), "A_B")
        self.assertEqual(sb.sanitize_filepath('a/b:c?'), "a/b:c_")

    def test_make_and_unique_filename(self):
        self.assertEqual(sb.make_filename("out", "a/b", "dsf"), "out/a_b.dsf")
        self.assertEqual(sb.make_filename(None, "x", "dff"), "x.dff")
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "n.iso"), "w").close()
            open(os.path.join(d, "n (1).iso"), "w").close()
            self.assertEqual(sb.unique_filename(d, "n", "iso"), os.path.join(d, "n (2).iso"))

    def test_byte_truncation(self):
        # The C truncates on bytes: a 2-byte character split at the limit survives
        # as a lone byte (surrogate-escaped here, written back as the same byte).
        disc = read_toc(io.BytesIO(make_image()))
        disc.text["disc_title"] = "a" * 99 + "é"
        self.assertEqual(sb.album_path(disc).encode("utf-8", "surrogateescape"), b"a" * 99 + b"\xc3")

    def test_synthetic_disc(self):
        disc = read_toc(io.BytesIO(make_image()))
        area = disc.stereo
        self.assertEqual(sb.album_dir(disc, True), "Café Artist - Album Title")
        self.assertEqual(sb.music_filename(disc, area, 0, performer_flag=True), "01 - Bob - First")
        self.assertEqual(sb.music_filename(disc, area, 1), "02 - Second")
        self.assertEqual(sb.output_dir(disc, area, "/x/"), "/x/Album Title")
        tag = sb.id3_tag(disc, area, 0, 4)
        self.assertTrue(tag.startswith(b"ID3\x04\x00\x00") and b"TIT2" in tag and b"First" in tag)
        self.assertEqual(sb.id3_tag(disc, area, 0, 0), b"")
        foot = sb.dsdiff_footer(disc, area, 0, dst=False, now=datetime(2026, 1, 2, 3, 4))
        self.assertEqual(foot[:4], b"DIIN")
        self.assertIn(b"ID3 ", foot)
        hdr = sb.dsdiff_header(disc, area, dst=True, audio_data_size=100, frame_count=3, footer_size=len(foot))
        self.assertEqual(len(hdr), sb.dsdiff_header_size(area, True))


@unittest.skipUnless(HAVE_REAL, "sacd_extract binary or test ISOs not available")
class AgainstSacdExtract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsd, cls.dst = read_toc(DSD_ISO), read_toc(DST_ISO)

    def run_se(self, iso, *args, cfg=None):
        d = tempfile.mkdtemp(prefix="sbcompat-")
        self.addCleanup(shutil.rmtree, d)
        if cfg:
            with open(os.path.join(d, "sacd_extract.cfg"), "w") as f:
                f.write(cfg + "\n")
        subprocess.run([SACD_EXTRACT, *args, "-i", iso], cwd=d, stdout=subprocess.PIPE, check=False)
        return d

    @staticmethod
    def files(d, ext):
        return sorted(os.path.relpath(p, d) for p in glob.glob(f"{d}/**/*.{ext}", recursive=True))

    @staticmethod
    def dsf_footer(path):
        with open(path, "rb") as f:
            data = f.read()
        off, = struct.unpack_from("<Q", data, 20)
        return data[off:] if off else b""

    def test_dsf_names_and_id3(self):
        disc, area = self.dsd, self.dsd.stereo
        for flags, cfg, mode in (((), None, 4), (("-A", "-a"), None, 4), ((), "id3tag=1", 1),
                                 ((), "id3tag=2", 2), ((), "id3tag=3", 3), ((), "id3tag=5", 5)):
            with self.subTest(flags=flags, cfg=cfg):
                aa = bool(flags)
                d = self.run_se(DSD_ISO, "-s", *flags, "-t", "2", cfg=cfg)
                want = sb.make_filename(sb.output_dir(disc, area, None, aa),
                                        sb.music_filename(disc, area, 1, "", aa), "dsf")
                self.assertEqual(self.files(d, "dsf"), [want])
                self.assertEqual(sb.id3_tag(disc, area, 1, mode), self.dsf_footer(os.path.join(d, want)))

    def test_concatenate_name(self):
        disc, area = self.dsd, self.dsd.stereo
        d = self.run_se(DSD_ISO, "-s", "-k", "-t", "2,3")
        want = sb.make_filename(sb.output_dir(disc, area, None),
                                sb.music_filename(disc, area, 1, "[02-03]"), "dsf")
        self.assertEqual(self.files(d, "dsf"), [want])

    def check_dff(self, disc, area, track_idx, path):
        header, footer, dst, audio, frames, index = dff_split(path)
        mine = sb.dsdiff_footer(disc, area, track_idx, dst=dst, frame_index=index, id3_mode=4,
                                now=datetime(2026, 1, 1), machine=sb.SACD_RIPPER_VERSION_INFO)
        self.assertEqual(mask_creating_machine(mine), mask_creating_machine(footer))
        self.assertEqual(sb.dsdiff_header(disc, area, dst=dst, audio_data_size=audio, frame_count=frames,
                                          footer_size=len(mine)), header)
        self.assertEqual(sb.dsdiff_header_size(area, dst), len(header))

    def test_dsdiff_tracks(self):
        for iso, disc, args, area, t in ((DSD_ISO, self.dsd, ("-p", "-t", "2"), self.dsd.stereo, 1),
                                         (DST_ISO, self.dst, ("-p", "-t", "3"), self.dst.stereo, 2),
                                         (DST_ISO, self.dst, ("-p", "-c", "-t", "1"), self.dst.stereo, 0),
                                         (DST_ISO, self.dst, ("-p", "-m", "-t", "1"), self.dst.multichannel, 0)):
            with self.subTest(args=args):
                d = self.run_se(iso, *args)
                want = sb.make_filename(sb.output_dir(disc, area, None),
                                        sb.music_filename(disc, area, t), "dff")
                self.assertEqual(self.files(d, "dff"), [want])
                self.check_dff(disc, area, t, os.path.join(d, want))

    def test_cue_and_xml(self):
        disc = self.dsd
        d = self.run_se(DSD_ISO, "-C")
        base = sb.album_dir(disc)
        cue = sb.make_filename(sb.output_dir(disc, disc.stereo, None), base, "cue")
        self.assertEqual(self.files(d, "cue"), [cue])
        with open(os.path.join(d, cue), "rb") as f:
            self.assertEqual(sb.cue_sheet(disc, disc.stereo, sb.make_filename(None, base, "dff"), version="NA"),
                             f.read())
        with SACDImage(DSD_ISO) as img:
            raw = sb.raw_catalog_numbers(img)
        xml = self.files(d, "xml")
        self.assertEqual(xml, [sb.make_filename(sb.album_path(disc), base, "xml")])
        with open(os.path.join(d, xml[0]), "rb") as f:
            self.assertEqual(sb.metadata_xml(disc, version="NA", raw_catalog=raw), f.read())

    @unittest.skipUnless(os.environ.get("PYSACD_SLOW"), "set PYSACD_SLOW=1 for the edit-master test")
    def test_edit_master(self):
        disc, area = self.dst, self.dst.stereo
        d = self.run_se(DST_ISO, "-e")
        base = sb.album_dir(disc)
        dff = sb.make_filename(sb.output_dir(disc, area, None), base, "dff")
        self.assertEqual(self.files(d, "dff"), [dff])
        self.check_dff(disc, area, None, os.path.join(d, dff))
        with open(os.path.join(d, sb.make_filename(sb.output_dir(disc, area, None), base, "cue")), "rb") as f:
            self.assertEqual(sb.cue_sheet(disc, area, sb.make_filename(None, base, "dff"), version="NA"), f.read())


if __name__ == "__main__":
    unittest.main()
