"""Test-only DST encoder, written from docs/dst-bitstream.md as of v0.2.2 (section 6; see docs/clean-room.md).

The frame header (segmentation, mapping, half-probability flags, filter
coefficient sets, probability tables) is written here; the per-bit model
and the arithmetic coder run in tests/native/dstenc.c (build/dstenc.so,
`make test-tools`), loaded through ctypes. If that library is missing it is
compiled into a temporary directory when a C compiler is available.

The encoder does not aim for good compression. It aims to produce valid
frames and to force every optional feature of the syntax on request.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

NB = 588 * 64           # bits per channel per frame (DSD64)
FL = NB // 8            # bytes per channel per frame
MAX_SEG = 9             # segment table stride shared with dstenc.c
FILTER_LMIN, PTABLE_LMIN = 128, 4           # bytes
FILTER_SMAX, PTABLE_SMAX = 4, 8

FILTER_PRED = ((-8,), (-16, 8), (-9, -5, 6))
PTABLE_PRED = ((-8,), (-16, 8), (-24, 24, -8))

ROOT = Path(__file__).resolve().parent.parent
_SRC = ROOT / "tests" / "native" / "dstenc.c"
_lib = None
_tmpdir = None
last_layout = (0, 0)    # (header bits, code bits) of the last coded frame, for diagnostics


class EncoderUnavailable(RuntimeError):
    pass


def load() -> ctypes.CDLL:
    """The native encoder core (build/dstenc.so, else compiled on the fly)."""
    global _lib, _tmpdir
    if _lib is not None:
        return _lib
    path = ROOT / "build" / "dstenc.so"
    if not path.exists():
        cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
        if not cc:
            raise EncoderUnavailable("build/dstenc.so not built (make test-tools) and no C compiler")
        _tmpdir = tempfile.TemporaryDirectory(prefix="dstenc")
        path = Path(_tmpdir.name) / "dstenc.so"
        r = subprocess.run([cc, "-O2", "-fPIC", "-shared", "-std=gnu17", "-o", str(path), str(_SRC), "-lm"],
                           capture_output=True, text=True, check=False)
        if r.returncode:
            raise EncoderUnavailable(f"cannot compile {_SRC}: {r.stderr[-500:]}")
    lib = ctypes.CDLL(str(path))
    p8, p32 = np.ctypeslib.ndpointer(np.uint8, flags="C"), np.ctypeslib.ndpointer(np.int32, flags="C")
    lib.dstenc_sdm.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint32, p8]
    lib.dstenc_sdm.restype = None
    lib.dstenc_analyze.argtypes = [ctypes.c_int, p8, p32, p32, p32, p32, p8, p8]
    lib.dstenc_analyze.restype = ctypes.c_int
    lib.dstenc_code.argtypes = [ctypes.c_int, p8, p32, p32, p32, p32, p32, p32, p32, p32, p32, p8, ctypes.c_size_t]
    lib.dstenc_code.restype = ctypes.c_long
    _lib = lib
    return lib


def synth_dsd(channels: int, frames: int, seed: int = 1) -> bytes:
    """Deterministic DSD64 test signal (sigma-delta modulated tones), decoder layout."""
    out = np.zeros(frames * FL * channels, np.uint8)
    load().dstenc_sdm(channels, frames, seed, out)
    return out.tobytes()


# ---------------------------------------------------------------- frame description

@dataclass
class Segmentation:
    """Explicit segment lengths (units of `res` bytes) per channel; the last
    segment of each channel is implicit. `shared`: one list for all channels."""
    lengths: list[list[int]] = field(default_factory=lambda: [[]])
    res: int = 1
    shared: bool = True
    quirk: bool = False     # allow Smax explicit lengths (Smax + 1 segments: accepted only in
                            # shared mode, 2.4) and segments shorter than Lmin (invalid)

    def per_channel(self, channels: int) -> list[list[int]]:
        return [list(self.lengths[0])] * channels if self.shared else [list(x) for x in self.lengths]

    def starts(self, channels: int) -> np.ndarray:
        st = np.full((6, MAX_SEG + 1), NB, np.int32)
        for c, lens in enumerate(self.per_channel(channels)):
            pos = 0
            st[c, 0] = 0
            for j, ln in enumerate(lens):
                pos += 8 * self.res * ln
                st[c, j + 1] = pos
        return st


@dataclass
class Coding:
    """How a filter or ptable is transmitted: method None = verbatim, else 0..2 (Rice)."""
    method: int | None = None
    rice: int | None = None         # None: the cheapest


@dataclass
class Frame:
    """Every choice the DST frame syntax offers (defaults: the simplest coded frame)."""
    plain: bool = False
    fseg: Segmentation = field(default_factory=Segmentation)
    pseg: Segmentation | None = None            # None: same_segmentation = 1
    fmap: list[list[int]] | None = None         # table number per channel/segment; None: all 0
    fmap_shared: bool = True                    # same_map_all_ch for filters
    pmap: list[list[int]] | None = None         # None: same_mapping = 1
    pmap_shared: bool = True
    half_prob: list[int] | None = None          # per channel; None: all 0
    orders: list[int] | None = None             # per filter; default 32
    fcoding: list[Coding] | None = None         # per filter; default method 1
    coefs: list[list[int]] | None = None        # explicit filter coefficients
    plens: list[int] | None = None              # per ptable; default 64
    pcoding: list[Coding] | None = None         # per ptable; default method 1
    pentries: list[list[int]] | None = None     # explicit ptable entries (1..128)
    unchecked_tables: bool = False              # allow more than 2 x Nch tables (invalid)


# ---------------------------------------------------------------- bit writer

class BitWriter:
    def __init__(self):
        self.bits: list[int] = []

    def put(self, value: int, width: int) -> None:
        if width == 0:
            assert value == 0, "a zero-width field must be 0"
            return
        assert 0 <= value < (1 << width), (value, width)
        self.bits += [(value >> (width - 1 - i)) & 1 for i in range(width)]

    def sput(self, value: int, width: int) -> None:
        assert -(1 << (width - 1)) <= value < (1 << (width - 1)), (value, width)
        self.put(value & ((1 << width) - 1), width)

    def bit(self, b: int) -> None:
        self.bits.append(1 if b else 0)

    def flags(self, *bs: int) -> None:
        for b in bs:
            self.bit(b)

    def rice(self, r: int, m: int) -> None:
        """6.5"""
        u = abs(r)
        self.bits += [0] * (u >> m) + [1]
        self.put(u & ((1 << m) - 1), m)
        if r:
            self.bit(r < 0)


def rice_len(r: int, m: int) -> int:
    u = abs(r)
    return (u >> m) + 1 + m + (1 if r else 0)


# ---------------------------------------------------------------- header syntax

def _write_segmentation(bw: BitWriter, seg: Segmentation, channels: int, lmin: int, smax: int) -> None:
    """table_segmentation() (2.4, 6.2)"""
    rem0 = FL - lmin
    bw.bit(seg.shared)
    res_sent = False
    for lens in ([seg.lengths[0]] if seg.shared else seg.lengths[:channels]):
        assert len(lens) < smax + seg.quirk, "keep each channel at Smax segments or fewer"
        rem = rem0
        for ln in lens:
            bw.bit(0)
            if not res_sent:
                assert 1 <= seg.res <= rem0
                bw.put(seg.res, rem0.bit_length())
                res_sent = True
            assert seg.res * ln >= lmin or (seg.quirk and ln >= 1), "segment shorter than Lmin"
            assert seg.res * ln <= rem, "segment leaves less than Lmin"
            bw.put(ln, (rem // seg.res).bit_length())
            rem -= seg.res * ln
        bw.bit(1)


def _write_mapping(bw: BitWriter, maps: list[list[int]], shared: bool) -> int:
    """table_mapping() (2.5, 6.3); returns the table count."""
    t = 1
    assert maps[0][0] == 0
    if shared:
        assert all(m == maps[0] for m in maps), "same_map_all_ch needs identical lists"
    bw.bit(shared)
    for c, m in enumerate(maps[:1] if shared else maps):
        for j, v in enumerate(m):
            if c == 0 and j == 0:
                continue
            assert v <= t, "tables must be numbered in order of first use"
            bw.put(v, t.bit_length())
            if v == t:
                t += 1
    return t


def _coded_values(vals: list[int], pred, method: int) -> list[int]:
    """Residuals r_i = v_i + floor((x_i + 4) / 8) for i >= method + 1 (2.7, 2.8)."""
    k = method + 1
    out = []
    for i in range(k, len(vals)):
        x = sum(pred[method][t] * vals[i - 1 - t] for t in range(k))
        out.append(vals[i] + ((x + 4) >> 3))
    return out


def _write_values(bw: BitWriter, vals: list[int], coding: Coding, pred, width: int, signed: bool,
                  max_rice: int) -> None:
    """coef_coded / ptable_coded bit and the values (offset already removed for ptables)."""
    put = bw.sput if signed else bw.put
    if coding.method is None:
        bw.bit(0)
        for v in vals:
            put(v, width)
        return
    k = coding.method + 1
    assert k < len(vals), "coding method order must be below the table length"
    stored = vals if signed else [v + 1 for v in vals]       # ptables predict on P = value + 1
    res = _coded_values(stored, pred, coding.method)
    m = coding.rice
    if m is None:
        m = min(range(max_rice + 1), key=lambda m: sum(rice_len(r, m) for r in res))
    bw.bit(1)
    bw.put(coding.method, 2)
    for v in vals[:k]:
        put(v, width)
    bw.put(m, 3)
    for r in res:
        bw.rice(r, m)


# ---------------------------------------------------------------- model design

def _pm1(dsd: np.ndarray, channels: int) -> np.ndarray:
    """(channels, 128 + NB) of +-1, with the initial history (3.3) in front."""
    bits = np.unpackbits(dsd.reshape(FL, channels).T, axis=1)   # MSB first = earliest
    x = np.empty((channels, 128 + NB), np.float64)
    x[:, :128] = [1 if (127 - k) & 1 else -1 for k in range(128)]   # x[127 - i] = h[i]
    x[:, 128:] = bits * 2.0 - 1
    return x


def _design_filter(x: np.ndarray, regions: list[tuple[int, int, int]], order: int) -> list[int]:
    """Least-squares predictor of the next +-1 sample, scaled to 9-bit integers."""
    rows, ys = [], []
    for c, a, b in regions:
        idx = np.arange(a, b, max(1, (b - a) // 4000))
        lag = 128 + idx[:, None] - 1 - np.arange(order)[None, :]
        rows.append(x[c][lag])
        ys.append(x[c][128 + idx])
    X, y = np.concatenate(rows), np.concatenate(ys)
    w = np.linalg.solve(X.T @ X + 1e-3 * len(y) * np.eye(order), X.T @ y)
    s = X @ w
    scale = 160.0 / max(np.std(s), 1e-9)
    if np.max(np.abs(w)) * scale > 255:
        scale = 255 / np.max(np.abs(w))
    a = np.clip(np.round(w * scale), -256, 255).astype(int).tolist()
    if not any(a):
        a[0] = 1
    return a


def _design_ptable(bucket: np.ndarray, sym: np.ndarray, length: int) -> list[int]:
    """p_j approximating 256 * P(symbol 0) per clamped index (6.4)."""
    if length == 1:
        return [128]
    j = np.minimum(bucket, length - 1)
    n = np.bincount(j, minlength=length)
    z = np.bincount(j[sym == 0], minlength=length)
    p = np.round(256 * (z + 0.5) / (n + 1)).astype(int)
    return np.clip(p, 1, 128).tolist()


# ---------------------------------------------------------------- frame encoder

def encode_frame(dsd: bytes, channels: int, fr: Frame | None = None, fallback: bool = True) -> bytes:
    """Encode one frame of DSD (decoder layout, FL * channels bytes).

    The reference decoder keeps the arithmetic code in an array of NB * Nch
    entries and overflows it on longer code (docs/dst-bitstream.md at v0.2.2, section 8).
    Such a frame is returned as a plain frame unless `fallback` is False.
    """
    fr = fr or Frame()
    assert len(dsd) == FL * channels
    if fr.plain:
        return b"\x00" + dsd                    # DST_coded = 0, DST_X_bit = 0, stuffing

    lib = load()
    raw = np.frombuffer(dsd, np.uint8).copy()
    fseg = fr.fseg
    pseg = fr.pseg or fseg
    fcount = [len(x) + 1 for x in fseg.per_channel(channels)]
    pcount = [len(x) + 1 for x in pseg.per_channel(channels)]
    fmap = fr.fmap or [[0] * n for n in fcount]
    pmap = fr.pmap if fr.pmap is not None else fmap
    assert [len(m) for m in fmap] == fcount and [len(m) for m in pmap] == pcount
    nf = 1 + max(max(m) for m in fmap)
    np_ = 1 + max(max(m) for m in pmap)
    half = fr.half_prob or [0] * channels

    # filters: explicit or least squares over the bits each one covers
    orders = fr.orders or [32] * nf
    fst, pst = fseg.starts(channels), pseg.starts(channels)
    x = None
    coefs = []
    for f in range(nf):
        if fr.coefs and f < len(fr.coefs) and fr.coefs[f] is not None:
            coefs.append(list(fr.coefs[f]))
            continue
        if x is None:
            x = _pm1(raw, channels)
        regions = [(c, int(fst[c, j]), int(fst[c, j + 1])) for c in range(channels)
                   for j in range(fcount[c]) if fmap[c][j] == f]
        coefs.append(_design_filter(x, regions, orders[f]))
    order_a = np.array(orders, np.int32)
    coef_a = np.zeros((len(orders), 128), np.int32)
    for f, a in enumerate(coefs):
        coef_a[f, :len(a)] = a
    fmap_a = np.zeros((6, MAX_SEG), np.int32)
    pmap_a = np.zeros((6, MAX_SEG), np.int32)
    for c in range(channels):
        fmap_a[c, :fcount[c]] = fmap[c]
        pmap_a[c, :pcount[c]] = pmap[c]
    half_len = np.array([orders[fmap[c][0]] if half[c] else 0 for c in range(channels)] + [0] * (6 - channels),
                        np.int32)

    # ptables: explicit or from the symbol statistics under the chosen filters
    plens = fr.plens or [64] * np_
    entries = []
    stats = None
    for t in range(np_):
        if fr.pentries and t < len(fr.pentries) and fr.pentries[t] is not None:
            entries.append(list(fr.pentries[t]))
            continue
        if stats is None:
            bucket = np.zeros(channels * NB, np.uint8)
            sym = np.zeros(channels * NB, np.uint8)
            if lib.dstenc_analyze(channels, raw, fst, fmap_a, order_a, coef_a, bucket, sym):
                raise MemoryError
            stats = bucket.reshape(channels, NB), sym.reshape(channels, NB)
        mask = np.zeros((channels, NB), bool)
        for c in range(channels):
            for j in range(pcount[c]):
                if pmap[c][j] == t:
                    mask[c, max(int(pst[c, j]), half_len[c]):int(pst[c, j + 1])] = True
        entries.append(_design_ptable(stats[0][mask], stats[1][mask], plens[t]))
    assert all(len(e) == n and all(1 <= v <= 128 for v in e) for e, n in zip(entries, plens))
    assert all(e == [128] for e, n in zip(entries, plens) if n == 1), "one-entry ptables are 128"
    plen_a = np.array(plens, np.int32)
    ptab_a = np.zeros((len(plens), 64), np.int32)
    for t, e in enumerate(entries):
        ptab_a[t, :len(e)] = e

    # header (2.2 - 2.8)
    bw = BitWriter()
    bw.bit(1)                                           # DST_coded
    bw.bit(fr.pseg is None)                             # same_segmentation
    _write_segmentation(bw, fseg, channels, FILTER_LMIN, FILTER_SMAX)
    if fr.pseg is not None:
        _write_segmentation(bw, pseg, channels, PTABLE_LMIN, PTABLE_SMAX)
    bw.bit(fr.pmap is None)                             # same_mapping
    nf_written = _write_mapping(bw, fmap, fr.fmap_shared)
    assert nf_written == nf and (nf <= 2 * channels or fr.unchecked_tables), "at most 2 x Nch filters"
    if fr.pmap is not None:
        np_written = _write_mapping(bw, pmap, fr.pmap_shared)
        assert np_written == np_ and (np_ <= 2 * channels or fr.unchecked_tables), "at most 2 x Nch ptables"
    else:
        assert fcount == pcount, "same_mapping needs equal segment counts"
    for c in range(channels):
        bw.bit(half[c])
    fcoding = fr.fcoding or [Coding(1)] * nf
    for f in range(nf):
        n = orders[f]
        assert 1 <= n <= 128 and len(coefs[f]) == n
        bw.put(n - 1, 7)
        cd = fcoding[f]
        if cd.method is not None and cd.method + 1 >= n:
            cd = Coding()
        _write_values(bw, coefs[f], cd, FILTER_PRED, 9, True, 6)
    pcoding = fr.pcoding or [Coding(1)] * np_
    for t in range(np_):
        bw.put(plens[t] - 1, 6)
        if plens[t] == 1:
            continue                                    # nothing more for a one-entry table
        cd = pcoding[t]
        if cd.method is not None and cd.method + 1 >= plens[t]:
            cd = Coding()
        _write_values(bw, [v - 1 for v in entries[t]], cd, PTABLE_PRED, 7, False, 4)

    # arithmetic-coded data (2.9, 6.6, 6.7)
    cap = channels * NB + 4096
    code = np.zeros(cap, np.uint8)
    n = lib.dstenc_code(channels, raw, fst, fmap_a, pst, pmap_a, order_a, coef_a, plen_a, ptab_a,
                        half_len, code, cap)
    if n < 0:
        raise RuntimeError("arithmetic coder overflow")
    global last_layout
    last_layout = (len(bw.bits), n)
    fs = (len(bw.bits) + n + 7) // 8 * 8 - len(bw.bits)
    if fallback and fs > NB * channels:
        return encode_frame(dsd, channels, Frame(plain=True))
    bits = np.concatenate([np.array(bw.bits, np.uint8), code[:n]])
    return np.packbits(bits).tobytes()                  # zero-padded to a whole byte
