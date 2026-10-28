"""Full DST syntax: encode -> decode round trips for every optional feature,
validity checks, and a synthetic SACD ISO decoded end to end.

Frames come from the test encoder (tests/dstenc.py + tests/native/dstenc.c,
`make test-tools`); the signal is a synthetic sigma-delta source.
Set SACD_EXTRACT=/path/to/sacd_extract to also decode the ISO with the
reference decoder and require bit-identical DSD.
"""

import ctypes
import functools
import glob
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

import dstenc
import sacdgen
from dstenc import FL, NB, BitWriter, Coding, Frame, Segmentation

try:
    from pysacd import _native
    _lib = _native._load()
    _lib.dst_decoder_new.argtypes = [ctypes.c_int]
    _lib.dst_decoder_new.restype = ctypes.c_void_p
    _lib.dst_decoder_free.argtypes = [ctypes.c_void_p]
    _lib.dst_decode_frame.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p]
    _lib.dst_decode_frame.restype = ctypes.c_int
    HAVE_NATIVE = True
except Exception:
    HAVE_NATIVE = False

try:
    dstenc.load()
    HAVE_ENCODER = True
except Exception:
    HAVE_ENCODER = False

SACD_EXTRACT = os.environ.get("SACD_EXTRACT")
DST_ERR_INVALID = -1
CHANNELS = (2, 5, 6)


def decode(frame: bytes, channels: int) -> tuple[int, bytes]:
    dec = _lib.dst_decoder_new(channels)
    try:
        out = ctypes.create_string_buffer(FL * channels)
        rc = _lib.dst_decode_frame(dec, frame, len(frame), out)
        return rc, out.raw
    finally:
        _lib.dst_decoder_free(dec)


def number(maps: list[list[int]]) -> list[list[int]]:
    """Renumber table ids in order of first use (6.3)."""
    ids: dict[int, int] = {}
    return [[ids.setdefault(v, len(ids)) for v in m] for m in maps]


def features(ch: int) -> dict[str, Frame]:
    """One frame description per optional feature of the syntax, all valid."""
    C = range(ch)
    own = [[c] for c in C]
    cycle = lambda xs, n: [xs[i % len(xs)] for i in range(n)]
    f = {
        "simple": Frame(),
        "plain": Frame(plain=True),
        "half_prob": Frame(half_prob=[1] * ch),
        "half_prob_mixed": Frame(half_prob=[c % 2 for c in C], fmap=[[c % 2] for c in C], fmap_shared=False,
                                 orders=[128, 7]),
        "filter_verbatim": Frame(fcoding=[Coding()]),
        "filter_method0": Frame(fcoding=[Coding(0)]),
        "filter_method1_rice0": Frame(fcoding=[Coding(1, 0)]),
        "filter_method2": Frame(fcoding=[Coding(2)]),
        "filter_method2_rice7": Frame(fcoding=[Coding(2, 7)]),
        "filter_order1": Frame(orders=[1]),
        "filter_order128": Frame(orders=[128], fcoding=[Coding(2)]),
        "ptable_verbatim": Frame(pcoding=[Coding()]),
        "ptable_method0": Frame(pcoding=[Coding(0)]),
        "ptable_method1_rice0": Frame(pcoding=[Coding(1, 0)]),
        "ptable_method2": Frame(pcoding=[Coding(2)]),
        "ptable_method2_rice7": Frame(pcoding=[Coding(2, 7)]),
        # all channels share one-entry ptable 0 for most of the frame (a frame
        # coded with p = 128 throughout is too long for the reference, see
        # test_code_longer_than_raw)
        "ptable_one_entry": Frame(pseg=Segmentation([[3000]]), pmap=[[0, 1]] * ch, plens=[1, 64]),
        "ptable_one_entry_mixed": Frame(fmap=own, fmap_shared=False, plens=cycle([1, 64], ch)),
        "ptable_lengths_2_3_4": Frame(fmap=own, fmap_shared=False, pmap=number([[c % 3] for c in C]),
                                      pmap_shared=False, plens=[2, 3, 4][:min(3, ch)],
                                      pcoding=[Coding(0), Coding(1), Coding(2)]),
        "per_channel_filters": Frame(fmap=own, fmap_shared=False),
        "not_same_mapping": Frame(fmap=own, fmap_shared=False, pmap=[[0]] * ch),
        "tables_shared_across_channels": Frame(fmap=[[c % 2] for c in C], fmap_shared=False,
                                               pmap=[[c // 2] for c in C], pmap_shared=False),
        "max_tables": Frame(fseg=Segmentation([[2000]]), fmap=[[2 * c, 2 * c + 1] for c in C], fmap_shared=False),
        "filter_segments_shared": Frame(fseg=Segmentation([[1000, 1500, 1000]]), fmap=[[0, 1, 2, 3]] * ch),
        "filter_segments_per_channel": Frame(
            fseg=Segmentation([cycle([[], [3000], [1000, 1000], [600, 700, 800]], ch)[c] for c in C], shared=False),
            fmap=[[j % 2 for j in range(c % 4 + 1)] for c in C], fmap_shared=False),
        "ptable_segments_shared": Frame(pseg=Segmentation([[500] * 7]), pmap=[[0, 1, 0, 1, 2, 3, 2, 3]] * ch),
        "ptable_segments_per_channel": Frame(
            pseg=Segmentation([cycle([[], [1] * 7, [100, 200], [1, 2, 3]], ch)[c] for c in C], res=4, shared=False),
            pmap=[[j % 2 for j in range([1, 8, 3, 4][c % 4])] for c in C], pmap_shared=False),
        "same_mapping_different_lengths": Frame(fseg=Segmentation([[2000]]), pseg=Segmentation([[100]], res=4),
                                                fmap=[[0, 1]] * ch),
        "same_map_all_ch_segmented": Frame(fseg=Segmentation([[700, 900]]), fmap=[[0, 1, 0]] * ch,
                                           pseg=Segmentation([[30, 40, 50, 60]], res=8),
                                           pmap=[[0, 1, 2, 1, 0]] * ch),
        "resolution_max": Frame(fseg=Segmentation([[1]], res=4576), pseg=Segmentation([[1]], res=4700),
                                fmap=[[0, 1]] * ch, pmap=[[0, 1]] * ch),
        "resolution_min_lmin_segments": Frame(fseg=Segmentation([[128, 128, 4320]], res=1), fmap=[[0, 1, 0, 1]] * ch,
                                              pseg=Segmentation([[4] * 7], res=1), pmap=[[0, 1] * 4] * ch),
        "resolution_odd": Frame(fseg=Segmentation([[13, 40]], res=37), fmap=[[0, 1, 0]] * ch,
                                pseg=Segmentation([[3, 50, 7]] * ch, res=11, shared=False),
                                pmap=[[0, 1, 0, 1]] * ch, pmap_shared=False),
        "half_prob_over_ptable_segments": Frame(half_prob=[1] * ch, orders=[128],
                                                pseg=Segmentation([[1] * 7], res=4),
                                                pmap=[[0, 1, 2, 3, 0, 1, 2, 3]] * ch),
        "filter_segments_quirk_smax_shared": Frame(fseg=Segmentation([[500] * 4], quirk=True),
                                                   fmap=[[0, 1, 0, 1, 0]] * ch),
    }
    fl = [[[], [2000], [600, 1200], [300, 300, 300]][c % 4] for c in C]
    pl = [[[1] * 3, [], [50, 60], [10] * 7][c % 4] for c in C]
    fmap = number([[(c * 3 + j) % min(7, 2 * ch) for j in range(len(fl[c]) + 1)] for c in C])
    pmap = number([[(c * 5 + j) % (2 * ch) for j in range(len(pl[c]) + 1)] for c in C])
    nf, np_ = 1 + max(map(max, fmap)), 1 + max(map(max, pmap))
    f["kitchen_sink"] = Frame(
        fseg=Segmentation(fl, shared=False), pseg=Segmentation(pl, res=8, shared=False),
        fmap=fmap, fmap_shared=False, pmap=pmap, pmap_shared=False,
        half_prob=[(c + 1) % 2 for c in C],
        orders=[(37 * i) % 128 + 1 for i in range(nf)],
        fcoding=cycle([Coding(), Coding(0), Coding(1), Coding(2)], nf),
        plens=cycle([64, 1, 9, 33, 2], np_),
        pcoding=cycle([Coding(2), Coding(), Coding(1), Coding(0)], np_))
    return f


@functools.lru_cache(maxsize=None)
def source(ch: int) -> bytes:
    return dstenc.synth_dsd(ch, len(features(ch)), seed=ch)


@functools.lru_cache(maxsize=None)
def encoded(ch: int) -> tuple[tuple[str, bytes, bytes], ...]:
    """(feature, original DSD, DST frame) per feature, consecutive frames of the source."""
    src = source(ch)
    out = []
    for i, (name, fr) in enumerate(features(ch).items()):
        dsd = src[i * FL * ch:(i + 1) * FL * ch]
        out.append((name, dsd, dstenc.encode_frame(dsd, ch, fr)))
    return tuple(out)


# ---------------------------------------------------------------- invalid frames

def _hdr(bw: BitWriter, nbytes: int = 64) -> bytes:
    bits = bw.bits + [0] * (nbytes * 8 - len(bw.bits))
    return np.packbits(np.array(bits, np.uint8)).tobytes()


def _simple_head(bw: BitWriter) -> None:
    """DST_coded, one segment everywhere, same mapping, one filter/ptable."""
    bw.flags(1, 1)          # DST_coded, same_segmentation
    bw.flags(1, 1)          # filter segmentation: shared, end
    bw.flags(1, 1)          # same_mapping, same_map_all_ch


def invalid_frames(ch: int) -> dict[str, bytes]:
    """Frames violating one rule of section 5 each."""
    out = {}
    out["stuffing"] = b"\x01" + bytes(FL * ch)

    def seg_case(name, res, length, kind="filter"):
        bw = BitWriter()
        bw.bit(1)
        bw.bit(kind == "filter")
        if kind == "ptable":
            bw.flags(1, 1)    # filters: one segment
        bw.bit(1)                   # shared
        bw.bit(0)                   # end_of_channel_segm = 0
        rem0 = FL - (128 if kind == "filter" else 4)
        bw.put(res, rem0.bit_length())
        if 1 <= res <= rem0:
            bw.put(length, (rem0 // res).bit_length())
        out[name] = _hdr(bw)

    seg_case("resolution_zero", 0, 0)
    seg_case("resolution_too_big", 4577, 0)
    seg_case("ptable_resolution_too_big", 4701, 0, "ptable")
    seg_case("segment_length_zero", 1, 0)
    seg_case("segment_shorter_than_lmin", 1, 127)
    seg_case("segment_leaves_less_than_lmin", 1, 4577)
    seg_case("ptable_segment_leaves_less_than_lmin", 1, 4701, "ptable")

    # shared: Smax + 1 explicit lengths
    bw = BitWriter()
    bw.flags(1, 1, 1)
    for i in range(5):
        bw.bit(0)
        if i == 0:
            bw.put(128, 13)
        bw.put(1, ((4576 - 128 * i) // 128).bit_length())
    bw.bit(1)
    out["shared_smax_plus_one_segments"] = _hdr(bw)

    # per channel: Smax explicit lengths, even if end_of_channel_segm = 1 follows
    bw = BitWriter()
    bw.flags(1, 1, 0)
    for i in range(4):
        bw.bit(0)
        if i == 0:
            bw.put(128, 13)
        bw.put(1, ((4576 - 128 * i) // 128).bit_length())
    bw.bit(1)
    out["per_channel_smax_segments"] = _hdr(bw)

    # mapping: index > T
    bw = BitWriter()
    bw.flags(1, 1)
    bw.flags(1, 0); bw.put(128, 13); bw.put(5, 6); bw.bit(0); bw.put(5, 5); bw.bit(1)  # 3 segments
    bw.flags(1, 1)
    bw.put(1, 1)                    # segment 1: new table 1, T = 2
    bw.put(3, 2)                    # segment 2: 3 > T
    out["table_index_too_big"] = _hdr(bw)

    # too many tables: 3 segments per channel, a new filter for each up to 2 x Nch + 1
    bw = BitWriter()
    bw.flags(1, 1, 1)
    for i in range(2):
        bw.bit(0)
        if i == 0:
            bw.put(128, 13)
        bw.put(2, ((4576 - 256 * i) // 128).bit_length())
    bw.bit(1)
    bw.flags(1, 0)          # same_mapping, per-channel map
    t = 1
    for c in range(ch):
        for j in range(3):
            if c or j:
                v = min(t, 2 * ch)
                bw.put(v, t.bit_length())
                t += v == t
    assert t == 2 * ch + 1
    out["too_many_tables"] = _hdr(bw, 256)

    # same_map_all_ch with unequal segment counts
    bw = BitWriter()
    bw.flags(1, 1)
    bw.bit(0)                       # per-channel segmentation: ch0 two segments, others one
    bw.bit(0); bw.put(128, 13); bw.put(10, 6); bw.bit(1)
    bw.flags(*[1] * (ch - 1))
    bw.flags(1, 1)
    bw.put(0, 1)
    out["channel_mapping_counts_differ"] = _hdr(bw)

    # same_mapping with filter/ptable segment counts that differ
    bw = BitWriter()
    bw.flags(1, 0)
    bw.flags(1, 0); bw.put(128, 13); bw.put(10, 6); bw.bit(1)    # filters: 2 segments
    bw.flags(1, 1)                                                # ptables: 1 segment
    bw.flags(1, 1); bw.put(0, 1)
    out["segment_number_mismatch"] = _hdr(bw)

    def table_case(name, filt=None, ptab=None):
        bw = BitWriter()
        _simple_head(bw)
        bw.flags(*[0] * ch)     # half_prob
        filt(bw) if filt else (bw.put(0, 7), bw.bit(0), bw.sput(64, 9))
        if ptab:
            ptab(bw)
        out[name] = _hdr(bw, 2048)

    table_case("filter_method3", lambda bw: (bw.put(7, 7), bw.bit(1), bw.put(3, 2)))
    table_case("filter_method_order_too_high", lambda bw: (bw.put(1, 7), bw.bit(1), bw.put(1, 2)))
    # order 2, method 0: a1 = r - floor((-8 * a0 + 4) / 8) with a0 = 255 -> r - (-255) > 255 when r = 1
    table_case("filter_coef_out_of_range",
               lambda bw: (bw.put(1, 7), bw.bit(1), bw.put(0, 2), bw.sput(255, 9), bw.put(0, 3), bw.rice(1, 0)))
    table_case("ptable_method3", None, lambda bw: (bw.put(7, 6), bw.bit(1), bw.put(3, 2)))
    table_case("ptable_method_length_too_high", None, lambda bw: (bw.put(2, 6), bw.bit(1), bw.put(2, 2)))
    # length 2, method 0: P1 = r - floor((-8 * 128 + 4) / 8) = r + 128 > 128 when r = 1
    table_case("ptable_entry_out_of_range",
               None, lambda bw: (bw.put(1, 6), bw.bit(1), bw.put(0, 2), bw.put(127, 7), bw.put(0, 3), bw.rice(1, 0)))
    table_case("ptable_entry_zero",
               None, lambda bw: (bw.put(1, 6), bw.bit(1), bw.put(0, 2), bw.put(0, 7), bw.put(7, 3), bw.rice(-129, 7)))
    # Rice code running off the end of the frame
    bw = BitWriter()
    _simple_head(bw)
    bw.flags(*[0] * ch)
    bw.put(9, 7); bw.bit(1); bw.put(0, 2); bw.sput(1, 9); bw.put(0, 3)
    out["rice_overrun"] = _hdr(bw, (len(bw.bits) + 7) // 8 + 16)
    # header cut short (verbatim order-128 filter needs 1152 bits)
    bw = BitWriter()
    _simple_head(bw)
    bw.flags(*[0] * ch)
    bw.put(127, 7); bw.bit(0)
    out["header_truncated"] = _hdr(bw, 20)
    return out


# ---------------------------------------------------------------- DSF

REV = np.array([int(f"{b:08b}"[::-1], 2) for b in range(256)], np.uint8)


def dsf_audio(path) -> bytes:
    """Audio of a DSF file in decoder layout (byte-interleaved channels, MSB first)."""
    d = Path(path).read_bytes()
    assert d[:4] == b"DSD " and d[28:32] == b"fmt " and d[80:84] == b"data"
    ch, = struct.unpack_from("<I", d, 52)
    bps, = struct.unpack_from("<I", d, 60)
    n, = struct.unpack_from("<Q", d, 64)
    blk, = struct.unpack_from("<I", d, 72)
    size, = struct.unpack_from("<Q", d, 84)
    data = np.frombuffer(d, np.uint8, size - 12, 92).reshape(-1, ch, blk)
    planar = data.transpose(1, 0, 2).reshape(ch, -1)[:, :n // 8]
    if bps == 1:
        planar = REV[planar]
    return planar.T.reshape(-1).tobytes()


# ---------------------------------------------------------------- tests

@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class FrameRoundTrip(unittest.TestCase):
    def test_features(self):
        for ch in CHANNELS:
            for name, dsd, frame in encoded(ch):
                with self.subTest(channels=ch, feature=name):
                    rc, out = decode(frame, ch)
                    self.assertEqual(rc, 0)
                    self.assertTrue(out == dsd, "decoded DSD differs")

    def test_features_are_present(self):
        # spot-check that the encoder really emitted the forced syntax
        frames = {name: frame for name, _, frame in encoded(2)}
        self.assertEqual(frames["plain"][0], 0)
        self.assertEqual(frames["simple"][0] >> 4, 0b1111)         # coded, same seg, shared, end
        self.assertEqual(frames["ptable_segments_shared"][0] >> 5, 0b101)
        self.assertEqual(frames["filter_segments_per_channel"][0] >> 5, 0b110)
        # a one-entry ptable codes at one bit per sample: much larger than the simple frame
        self.assertGreater(len(frames["ptable_one_entry"]), 1.5 * len(frames["simple"]))
        self.assertLess(len(frames["simple"]), 2 * FL)

    def test_no_silent_fallback(self):
        for ch in CHANNELS:
            for name, _, frame in encoded(ch):
                with self.subTest(channels=ch, feature=name):
                    self.assertEqual(frame[0] >> 7, name != "plain")

    def test_code_longer_than_raw(self):
        # p = 128 everywhere: more code bits than NB * Nch. The reference
        # overflows on this (section 8); we decode it, the encoder avoids it.
        ch = 2
        dsd = source(ch)[:FL * ch]
        fr = Frame(plens=[1])
        frame = dstenc.encode_frame(dsd, ch, fr, fallback=False)
        self.assertGreater(len(frame) * 8 - dstenc.last_layout[0], NB * ch)
        self.assertEqual(decode(frame, ch), (0, dsd))
        self.assertEqual(dstenc.encode_frame(dsd, ch, fr), b"\0" + dsd)

    def test_ptable_nine_segments_shared_rejected(self):
        # 2.4 lets shared segmentation announce Smax lengths; for ptables (9
        # segments) the reference overflows and decodes wrongly or rejects
        # (section 8), so it is invalid. 5 filter segments stay valid
        # ("filter_segments_quirk_smax_shared").
        ch = 2
        dsd = source(ch)[:FL * ch]
        fr = Frame(pseg=Segmentation([[100] * 8], quirk=True), pmap=[[0, 1, 2, 3, 0, 1, 2, 3, 0]] * ch)
        self.assertEqual(decode(dstenc.encode_frame(dsd, ch, fr), ch)[0], DST_ERR_INVALID)
        fr.pseg.lengths = [[100] * 7]
        fr.pmap = [[0, 1, 2, 3, 0, 1, 2, 3]] * ch
        self.assertEqual(decode(dstenc.encode_frame(dsd, ch, fr), ch), (0, dsd))

    def test_one_entry_ptable_reads_no_coded_bit(self):
        # with M = 1 the next field follows the 6-bit length directly
        ch = 2
        dsd = source(ch)[:FL * ch]
        frame = dstenc.encode_frame(dsd, ch, Frame(plens=[1], orders=[3], fcoding=[Coding()]), fallback=False)
        bits = np.unpackbits(np.frombuffer(frame, np.uint8))
        hdr = 1 + 1 + 2 + 1 + 1 + ch + 7 + 1 + 3 * 9
        self.assertEqual(bits[hdr:hdr + 6].tolist(), [0] * 6)
        self.assertEqual(bits[hdr + 6], 0)          # cb[0], not a ptable_coded bit
        self.assertEqual(decode(frame, ch)[0], 0)

    def test_twelve_tables_six_channels(self):
        # FFmpeg capped the running count at 12; 2 x Nch = 12 is valid for 6 channels
        frames = {name: frame for name, _, frame in encoded(6)}
        self.assertEqual(decode(frames["max_tables"], 6)[0], 0)

    def test_invalid(self):
        for ch in CHANNELS:
            for name, frame in invalid_frames(ch).items():
                with self.subTest(channels=ch, case=name):
                    self.assertEqual(decode(frame, ch)[0], DST_ERR_INVALID)

    def test_end_of_frame_check(self):
        for ch in CHANNELS:
            _, dsd, frame = encoded(ch)[0]
            with self.subTest(channels=ch):
                # the decoder reads 11 bits past the codeword, so one extra byte is fine ...
                self.assertEqual(decode(frame + b"\0", ch), (0, dsd))
                # ... three leave more than 7 code bits unread
                self.assertEqual(decode(frame + b"\0\0\0", ch)[0], DST_ERR_INVALID)
                # trailing zero bytes of the codeword may be dropped (6.7)
                stripped = frame.rstrip(b"\0")
                self.assertEqual(decode(stripped, ch), (0, dsd))

    def test_leading_code_bit(self):
        ch = 2
        bw = BitWriter()
        _simple_head(bw)
        bw.flags(*[0] * ch)
        bw.put(0, 7); bw.bit(0); bw.sput(64, 9)
        bw.put(0, 6)                # one-entry ptable
        good, bad = BitWriter(), BitWriter()
        good.bits, bad.bits = bw.bits + [0], bw.bits + [1]
        self.assertEqual(decode(_hdr(good, (len(good.bits) + 7) // 8 + 1), ch)[0], 0)
        self.assertEqual(decode(_hdr(bad, (len(bad.bits) + 7) // 8 + 1), ch)[0], DST_ERR_INVALID)

    def test_plain_frame_short_and_long(self):
        ch = 2
        dsd = source(ch)[:FL * ch]
        self.assertEqual(decode(b"\x40" + dsd + b"xx", ch), (0, dsd))        # X bit ignored, extra ignored
        rc, out = decode(b"\x00" + dsd[:100], ch)
        self.assertEqual((rc, out), (0, dsd[:100] + bytes(FL * ch - 100)))


@unittest.skipUnless(HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class SyntheticISO(unittest.TestCase):
    """Every feature frame in one DST area per channel count, decoded by pysacd
    (and by sacd_extract when SACD_EXTRACT is set)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, chs: list[int]) -> Path:
        areas = []
        for ch in chs:
            frames = [f for _, _, f in encoded(ch)]
            n = len(frames)
            areas.append(sacdgen.AreaSpec(ch, frames, [n // 3, n // 3, n - 2 * (n // 3)]))
        iso = self.dir / f"dst{'_'.join(map(str, chs))}.iso"
        sacdgen.build_iso(iso, areas)
        return iso

    def pysacd_tracks(self, iso: Path, ch: int) -> list[tuple[int, int, bytes]]:
        from pysacd import SACDImage
        with SACDImage(iso) as img:
            area = next(a for a in img.read_toc().areas if a.channel_count == ch)
            self.assertTrue(area.is_dst)
            specs = [_native.OutputSpec("dsf", self.dir / f"py{ch}_{t.number}.dsf", t.start.total_frames,
                                        t.start.total_frames + t.duration.total_frames) for t in area.tracks]
            _native.run_area(img, iso, area, specs)
        for s in specs:
            self.assertEqual((s.frames_missing, s.dst_errors), (0, 0))
        return [(s.tc_start, s.tc_end, dsf_audio(s.path)) for s in specs]

    def oracle_tracks(self, iso: Path, ch: int) -> list[bytes]:
        out = self.dir / f"se{ch}"
        out.mkdir()
        r = subprocess.run([SACD_EXTRACT, "-2" if ch == 2 else "-m", "-s", f"-i{iso}"], cwd=out,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])
        files = sorted(glob.glob(str(out / "**" / "*.dsf"), recursive=True))
        return [dsf_audio(f) for f in files]

    def check(self, chs: list[int]):
        iso = self.build(chs)
        for ch in chs:
            frames = encoded(ch)
            src = b"".join(d for _, d, _ in frames)
            ours = self.pysacd_tracks(iso, ch)
            theirs = self.oracle_tracks(iso, ch) if SACD_EXTRACT else None
            if theirs is not None:
                self.assertEqual(len(theirs), len(ours))
            for k, (a, b, got) in enumerate(ours):
                want = src[a * FL * ch:b * FL * ch]
                for i in range(a, b):
                    name = frames[i][0]
                    sl = slice((i - a) * FL * ch, (i - a + 1) * FL * ch)
                    with self.subTest(channels=ch, feature=name):
                        self.assertTrue(got[sl] == want[sl], "pysacd differs from the original")
                        if theirs is not None:
                            self.assertTrue(theirs[k][sl] == want[sl], "sacd_extract differs from the original")
                self.assertEqual(len(got), len(want))
                if theirs is not None:
                    self.assertEqual(theirs[k], got)

    def test_stereo_and_6ch(self):
        self.check([2, 6])

    def test_5ch(self):
        self.check([5])


if __name__ == "__main__":
    unittest.main()
