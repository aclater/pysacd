"""sacd_extract compatibility: naming, ID3, DSDIFF, CUE and XML.

The comparison tests run the real sacd_extract, and need
  SACD_EXTRACT=/path/to/sacd_extract   (or sacd_extract on PATH).
They use two generated discs (tests/sacdgen.py), unless you name real ones:
  PYSACD_COMPAT_DSD=<plain-DSD stereo ISO>  PYSACD_COMPAT_DST=<DST stereo+5.1 ISO>
With real discs, PYSACD_SLOW=1 also runs the ~1 GB DSDIFF edit-master comparison.
Output goes to a temporary directory (set TMPDIR to put it on disk).
"""

import glob
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import sacdgen
import test_dst_full as dst
from extract_compare import _dff_sans_machine as dff_sans_machine
from extract_compare import documented_only
from test_toc import make_image

from pysacd import SACDImage, read_toc, sbwriter
from pysacd import sbcompat as sb

SACD_EXTRACT = os.environ.get("SACD_EXTRACT") or shutil.which("sacd_extract")
REAL_DSD = os.environ.get("PYSACD_COMPAT_DSD")
REAL_DST = os.environ.get("PYSACD_COMPAT_DST")
HAVE_DISCS = bool(REAL_DSD and REAL_DST) or dst.HAVE_ENCODER
SRC = Path(__file__).resolve().parents[1] / "src"


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


def sans_machine(footer: bytes) -> str:
    """A DSDIFF footer without what differs by design (docs/sacd_extract.md)."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d, "x.dff")
        p.write_bytes(b"FRM8" + (len(footer) + 4).to_bytes(8, "big") + b"DSD " + footer)
        return dff_sans_machine(p)


class Helpers(unittest.TestCase):
    def test_parse_format(self):
        self.assertEqual(sb.parse_format("%N - %A - %T %%", 7, "", "a", "", "t"), "07 - a - t %")
        self.assertEqual(sb.parse_format("%N - %T", 100, title="t"), " - t")

    def test_sanitize(self):
        # dots are trimmed before whitespace, as in the C code
        # (a '.' left in front by that order becomes '_': pysacd never makes a hidden name)
        self.assertEqual(sb.sanitize_filename(' ..A/B: [x] "y"?. '), "_.A_B_ _x_ _y__.")
        self.assertEqual(sb.sanitize_filename("..A/B.."), "A_B")
        self.assertEqual(sb.sanitize_filepath('a/b:c?'), "a/b:c_")

    def test_make_and_unique_filename(self):
        self.assertEqual(sb.make_filename("out", "a/b", "dsf"), "out/a_b.dsf")
        self.assertEqual(sb.make_filename(None, "x", "dff"), "x.dff")
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "n.iso"), "w").close()
            open(os.path.join(d, "n (1).iso"), "w").close()
            self.assertEqual(sb.unique_filename(d, "n", "iso"), f"{d}/n (2).iso")    # "/", as sacd_extract

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

    def test_dsdiff_layout(self):
        # pysacd's DSDIFF choices through scarletbook (pysacd.sbwriter, #94) on a generated disc
        with tempfile.TemporaryDirectory() as tmp:
            iso = os.path.join(tmp, "t.iso")
            sacdgen.build_iso(iso, [sacdgen.AreaSpec(2, [bytes(2 * 4704)] * 4, [2, 2], dst=False)])
            with SACDImage(iso) as img:
                disc = img.read_toc()
                area = disc.stereo
                now = datetime(2026, 1, 2, 3, 4)
                lay = sbwriter.dsdiff(img, disc, area, 0, 0, 2, id3_tags=[sb.id3_tag(disc, area, 0, 4, True)],
                                      now=now, machine=sb.PYSACD_VERSION_INFO)
                self.assertEqual(lay.trailer[:4], b"DIIN")
                self.assertIn(b"ID3 ", lay.trailer)
                self.assertEqual(lay.size, len(lay.header) + 2 * 2 * 4704 + len(lay.trailer))
                dst = sbwriter.dsdiff(img, disc, area, 0, 0, 2, id3_tags=[], now=now,
                                      machine=sb.PYSACD_VERSION_INFO, dst_lengths=[100, 101])
                self.assertEqual(dst.size, len(dst.header) + 12 + 100 + 12 + 102 + len(dst.trailer))


def synthetic_discs(root: Path) -> tuple[str, str]:
    """A plain-DSD stereo disc and a DST stereo + 6-channel disc, each track numbered and titled."""
    def tracks(n: int) -> list[int]:
        return [n // 3, n // 3, n - 2 * (n // 3)]

    titles = ["First", "Second", "Third"]
    plain = [bytes([i]) * (2 * 4704) for i in range(12)]
    two, six = [f for _, _, f in dst.encoded(2)], [f for _, _, f in dst.encoded(6)]
    info = sacdgen.DiscInfo(title="Compat Album", artist="Compat Artist")
    dsd_iso, dst_iso = root / "dsd.iso", root / "dst.iso"
    sacdgen.build_iso(dsd_iso, [sacdgen.AreaSpec(2, plain, tracks(len(plain)), dst=False, titles=titles)], info=info)
    sacdgen.build_iso(dst_iso, [sacdgen.AreaSpec(2, two, tracks(len(two)), titles=titles),
                                sacdgen.AreaSpec(6, six, tracks(len(six)), titles=titles)], info=info)
    return str(dsd_iso), str(dst_iso)


@unittest.skipUnless(SACD_EXTRACT and HAVE_DISCS, "set SACD_EXTRACT to compare with the real sacd_extract")
class AgainstSacdExtract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real = bool(REAL_DSD and REAL_DST)
        if cls.real:
            cls.dsd_iso, cls.dst_iso = REAL_DSD, REAL_DST
        else:
            cls.tmp = tempfile.TemporaryDirectory()
            cls.addClassCleanup(cls.tmp.cleanup)
            cls.dsd_iso, cls.dst_iso = synthetic_discs(Path(cls.tmp.name))
        cls.dsd, cls.dst = read_toc(cls.dsd_iso), read_toc(cls.dst_iso)

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
                d = self.run_se(self.dsd_iso, "-s", *flags, "-t", "2", cfg=cfg)
                want = sb.make_filename(sb.output_dir(disc, area, None, aa),
                                        sb.music_filename(disc, area, 1, "", aa), "dsf")
                self.assertEqual(self.files(d, "dsf"), [want])
                self.assertEqual(sb.id3_tag(disc, area, 1, mode), self.dsf_footer(os.path.join(d, want)))

    def test_concatenate_name(self):
        disc, area = self.dsd, self.dsd.stereo
        d = self.run_se(self.dsd_iso, "-s", "-k", "-t", "2,3")
        want = sb.make_filename(sb.output_dir(disc, area, None),
                                sb.music_filename(disc, area, 1, "[02-03]"), "dsf")
        self.assertEqual(self.files(d, "dsf"), [want])

    def check_dff(self, iso, disc, area, track_idx, path):
        """scarletbook's layout with pysacd's choices (pysacd.sbwriter) against sacd_extract's file, except
        what differs by design."""
        header, footer, dst, _audio, _frames, index = dff_split(path)
        t = area.tracks[track_idx]
        first = t.start.total_frames
        with SACDImage(iso) as img:
            lay = sbwriter.dsdiff(img, disc, area, track_idx, first, first + t.duration.total_frames,
                                  id3_tags=[sb.id3_tag(disc, area, track_idx, 4, True)], now=datetime(2026, 1, 1),
                                  machine=sb.PYSACD_VERSION_INFO,
                                  dst_lengths=[n for _o, n in index] if dst else None)
        self.assertEqual(sans_machine(lay.trailer), sans_machine(footer))
        self.assertEqual(lay.header[16:], header[16:])          # (FRM8's size follows the machine comment)
        self.assertEqual(lay.size, len(lay.header) + (os.path.getsize(path) - len(header) - len(footer)) + len(lay.trailer))

    def test_dsdiff_tracks(self):
        for iso, disc, args, area, t in ((self.dsd_iso, self.dsd, ("-p", "-t", "2"), self.dsd.stereo, 1),
                                         (self.dst_iso, self.dst, ("-p", "-t", "3"), self.dst.stereo, 2),
                                         (self.dst_iso, self.dst, ("-p", "-c", "-t", "1"), self.dst.stereo, 0),
                                         (self.dst_iso, self.dst, ("-p", "-m", "-t", "1"), self.dst.multichannel, 0)):
            with self.subTest(args=args):
                d = self.run_se(iso, *args)
                want = sb.make_filename(sb.output_dir(disc, area, None),
                                        sb.music_filename(disc, area, t), "dff")
                self.assertEqual(self.files(d, "dff"), [want])
                self.check_dff(iso, disc, area, t, os.path.join(d, want))

    def test_cue_and_xml(self):
        disc = self.dsd
        d = self.run_se(self.dsd_iso, "-C")
        base = sb.album_dir(disc)
        cue = sb.make_filename(sb.output_dir(disc, disc.stereo, None), base, "cue")
        self.assertEqual(self.files(d, "cue"), [cue])
        with open(os.path.join(d, cue), "rb") as f:
            self.assertEqual(sb.cue_sheet(disc, disc.stereo, sb.make_filename(None, base, "dff"), version="NA"),
                             f.read())
        with SACDImage(self.dsd_iso) as img:
            raw = sb.raw_catalog_numbers(img)
        xml = self.files(d, "xml")
        self.assertEqual(xml, [sb.make_filename(sb.album_path(disc), base, "xml")])
        with open(os.path.join(d, xml[0]), "rb") as f:
            self.assertEqual(sb.metadata_xml(disc, version="NA", raw_catalog=raw), f.read())

    def test_edit_master(self):
        if self.real and not os.environ.get("PYSACD_SLOW"):
            self.skipTest("set PYSACD_SLOW=1 for the edit-master test on a real disc (about 1 GB)")
        disc, area = self.dst, self.dst.stereo
        d = self.run_se(self.dst_iso, "-e")
        ours = tempfile.mkdtemp(prefix="sbcompat-")
        self.addCleanup(shutil.rmtree, ours)
        r = subprocess.run([sys.executable, "-m", "pysacd.extract", "--no-cfg", "-e", "-i", self.dst_iso], cwd=ours,
                           env={**os.environ, "PYTHONPATH": str(SRC)}, capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        base = sb.album_dir(disc)
        dff = sb.make_filename(sb.output_dir(disc, area, None), base, "dff")
        self.assertEqual(self.files(d, "dff"), [dff])
        self.assertEqual(self.files(ours, "dff"), [dff])
        self.assertTrue(documented_only(Path(ours, dff), Path(d, dff)), "the edit master differs from sacd_extract's")
        with open(os.path.join(d, sb.make_filename(sb.output_dir(disc, area, None), base, "cue")), "rb") as f:
            self.assertEqual(sb.cue_sheet(disc, area, sb.make_filename(None, base, "dff"), version="NA"), f.read())


if __name__ == "__main__":
    unittest.main()
