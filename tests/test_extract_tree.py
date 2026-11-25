"""pysacd-extract against the real sacd_extract, whole output trees: `diff -r` as a test.

On generated discs (tests/sacdgen.py), every sacd_extract-compatible mode of
tools/extract_compare.py: the same files, names and bytes, apart from the documented exceptions
(docs/sacd_extract.md). This needs SACD_EXTRACT=/path/to/sacd_extract and runs in CI's "compare
with sacd_extract" job. Real discs exercise more of the format: the same tool runs on them by hand
before a release (docs/development.md, "Packaging and releases").

The HarnessDetects tests need no sacd_extract: they check that the comparison can fail.
"""

import contextlib
import io
import os
import random
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))     # also what test_sbcompat imports from

import extract_compare as cmp
import fsutil
import sacdgen
import test_dst_full as dst

SACD_EXTRACT = os.environ.get("SACD_EXTRACT") or shutil.which("sacd_extract")
SRC = str(ROOT / "src")


def tracks(n: int) -> list[int]:
    return [n // 3, n // 3, n - 2 * (n // 3)]


def discs(root: Path) -> dict[str, Path]:
    """Plain DSD stereo; DST stereo + 6 channels; plain DSD stereo + DST 5 channels."""
    titles = ["First", "Second", "Third"]
    plain = [bytes([i]) * (2 * 4704) for i in range(12)]
    two, five, six = ([f for _, _, f in dst.encoded(ch)] for ch in (2, 5, 6))
    info = sacdgen.DiscInfo(title="Tree Album", artist="Tree Artist")
    specs = {
        "plain": [sacdgen.AreaSpec(2, plain, tracks(len(plain)), dst=False, titles=titles)],
        "dst": [sacdgen.AreaSpec(2, two, tracks(len(two)), titles=titles),
                sacdgen.AreaSpec(6, six, tracks(len(six)), titles=titles)],
        "mixed": [sacdgen.AreaSpec(2, plain, tracks(len(plain)), dst=False, titles=titles),
                  sacdgen.AreaSpec(5, five, tracks(len(five)), titles=titles)],
    }
    out = {}
    for name, areas in specs.items():
        out[name] = root / f"{name}.iso"
        sacdgen.build_iso(out[name], areas, info=info)
    return out


@unittest.skipUnless(SACD_EXTRACT, "set SACD_EXTRACT to compare with the real sacd_extract")
@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class Trees(unittest.TestCase):
    @staticmethod
    def compare(iso: Path, mode: str) -> dict:
        """One mode, again if sacd_extract left out a file pysacd wrote: under load, 3 runs of
        450 of sacd_extract, and none of pysacd, came out different with exit status 0 (6-channel
        DST, all files it wrote matching). pysacd is the same tree every time, so a real difference
        fails all three."""
        for _ in range(3):
            r = cmp.compare(iso, mode, {"old": SRC, "new": SRC}, SACD_EXTRACT, 2, None)
            if not cmp.sacd_extract_problems(r):
                break
        return r

    def test_every_mode_on_every_disc(self):
        modes = [m for m, (kind, _) in cmp.MODES.items() if kind == "extract"]
        self.assertGreaterEqual(len(modes), 20)
        compared = {}
        with fsutil.TempDir(prefix="pysacd-tree") as tmp:
            for disc, iso in discs(Path(tmp)).items():
                for mode in modes:
                    has_mch = disc != "plain"
                    if "-m" in cmp.MODES[mode][1] and not has_mch:
                        continue
                    with self.subTest(disc=disc, mode=mode):
                        r = self.compare(iso, mode)
                        self.assertEqual(cmp.sacd_extract_problems(r), [])
                        for fmt, e in r["vs_sacd_extract"]["new"].items():
                            compared[fmt] = compared.get(fmt, 0) + e["identical"] + e["documented"]
        # the formats the docs promise were really compared
        self.assertLessEqual({"dsf", "dff", "cue", "xml", "iso"}, compared.keys())
        self.assertTrue(all(compared.values()), compared)


@unittest.skipUnless(SACD_EXTRACT, "set SACD_EXTRACT to compare with the real sacd_extract")
@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
class BlankIsrcs(unittest.TestCase):
    """Blank and partly filled ISRC fields (sacdgen.ISRC_FORMS) in every output that carries an
    ISRC: DSF and DSDIFF in each ID3 mode, -k concatenations, the edit master with cue and XML,
    and -P (#175)."""

    RUNS = [(fmt, mode) for mode in range(6) for fmt in (["-2", "-s"], ["-2", "-p"])] + [
        (["-2", "-s", "-k", "-t", "2,3"], None), (["-2", "-p", "-k", "-t", "2,3"], None),
        (["-2", "-e", "-C"], None), (["-P"], None)]

    @staticmethod
    def run_both(iso: Path, root: Path, args: list[str], mode: int | None) -> tuple:
        se, py = root / "sacd_extract", root / "pysacd"
        for d in (se, py):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir()
        if mode is not None:
            (se / "sacd_extract.cfg").write_text(f"id3tag={mode}\n")
        a = subprocess.run([SACD_EXTRACT, *args, "-i", str(iso)], cwd=se, capture_output=True, text=True,
                           errors="replace", check=False)
        (se / "sacd_extract.cfg").unlink(missing_ok=True)
        b = subprocess.run([sys.executable, "-m", "pysacd.extract", "--no-cfg", "--threads", "2",
                            *(["--id3tag", str(mode)] if mode is not None else []), *args, "-i", str(iso)],
                           cwd=py, capture_output=True, text=True, errors="replace", check=False,
                           env={**os.environ, "PYTHONPATH": SRC})
        return se, py, a, b

    def test_isrc_forms(self):
        with fsutil.TempDir(prefix="pysacd-isrc") as tmp:
            root = Path(tmp)
            iso = sacdgen.isrc_forms_iso(root / "isrcs.iso")
            compared = 0
            for args, mode in self.RUNS:
                with self.subTest(args=args, id3tag=mode):
                    for _ in range(3):      # sacd_extract now and then leaves out a file (see Trees)
                        se, py, a, b = self.run_both(iso, root, args, mode)
                        theirs, ours = cmp.tree(se), cmp.tree(py)
                        if args == ["-P"] or theirs.keys() >= ours.keys():
                            break
                    self.assertEqual((a.returncode, b.returncode), (0, 0), b.stderr)
                    if args == ["-P"]:
                        body = lambda t: t[t.find("Disc Information:"):]
                        self.assertIn("ISRC Track [1]:", a.stdout)
                        self.assertEqual(body(b.stdout), body(a.stdout))
                        continue
                    self.assertTrue(theirs)
                    vs = cmp.against_sacd_extract(py, se, ours, theirs, False)
                    self.assertEqual({fmt: e["different"] for fmt, e in vs.items() if e["different"]}, {})
                    compared += sum(e["identical"] + e["documented"] for e in vs.values())
            self.assertGreaterEqual(compared, 6 * 2 * 9)


# Track flags on TrackStop markers (#179): an edited master whose area has gaps between tracks gets
# a TrackStop before each gap, and sacd_extract writes the area TOC's track mute flags (the flag
# byte of each track's SACDTRL2 length entry) there. DSDIFF 1.5 (3.7.2) wants them zero on every
# marker but a TrackStart, as pysacd writes them: a documented exception (docs/sacd_extract.md).
LENGTH_FLAGS = [0x20, 0x40, 0x10]       # TMF3, TMF4 and TMF2 mute in DSDIFF's TrackFlags
STOP_FLAGS = [8, 1, 4]


def gapped_iso(path: Path, frames6: list[bytes], dst_area: bool) -> Path:
    """Stereo plain DSD, and a 6-channel area whose tracks 2 and 3 start one frame after the
    previous one ends, with LENGTH_FLAGS."""
    n = len(frames6)
    assert n >= 6, n
    plain = [bytes([i]) * (2 * 4704) for i in range(6)]
    sacdgen.build_iso(path, [
        sacdgen.AreaSpec(2, plain, [2, 2, 2], dst=False, titles=["One", "Two", "Three"]),
        sacdgen.AreaSpec(6, frames6, tracks(n), dst=dst_area, titles=["One", "Two", "Three"],
                         pauses=[0, 1, 1], length_flags=LENGTH_FLAGS)],
        info=sacdgen.DiscInfo(title="Gap Album", artist="Gap Artist"))
    return path


def markers(path: Path) -> list[tuple[int, int]]:
    """(markType, TrackFlags) of a DSDIFF file's markers, in order."""
    data = path.read_bytes()
    pos, out = 16, []
    while pos + 12 <= len(data):
        size = int.from_bytes(data[pos + 4:pos + 12], "big")
        if data[pos:pos + 4] == b"DIIN":
            q, end = pos + 12, pos + 12 + size
            while q + 12 <= end:
                n = int.from_bytes(data[q + 4:q + 12], "big")
                if data[q:q + 4] == b"MARK":
                    out.append((int.from_bytes(data[q + 24:q + 26], "big"), int.from_bytes(data[q + 28:q + 30], "big")))
                q += 12 + n + (n & 1)
        pos += 12 + size + (size & 1)
    return out


def edit_master(cmd: list[str], iso: Path, cwd: Path, env: dict | None = None) -> Path:
    cwd.mkdir()
    r = subprocess.run([*cmd, "-m", "-e", "-i", str(iso)], cwd=cwd, capture_output=True, text=True,
                       errors="replace", check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    found = sorted(fsutil.lp(cwd).rglob("*.dff"))
    assert len(found) == 1, found
    return found[0]


PYSACD = [sys.executable, "-m", "pysacd.extract", "--no-cfg", "--threads", "2"]


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
class TrackStopFlags(unittest.TestCase):
    """pysacd's edited master keeps TrackFlags zero on TrackStop markers (#179)."""

    def test_stop_markers_carry_no_flags(self):
        with fsutil.TempDir(prefix="pysacd-stops") as tmp:
            root = Path(tmp)
            iso = gapped_iso(root / "gap.iso", [bytes([i]) * (6 * 4704) for i in range(9)], dst_area=False)
            dff = edit_master(PYSACD, iso, root / "out", {**os.environ, "PYTHONPATH": SRC})
            # program start, then per track a start, a stop before each gap, and the last stop
            self.assertEqual(markers(dff), [(2, 0), (0, 0), (1, 0), (0, 0), (1, 0), (0, 0), (1, 0)])
            self.assertEqual(cmp._dff_stop_flags(dff), [0, 0, 0])


@unittest.skipUnless(SACD_EXTRACT, "set SACD_EXTRACT to compare with the real sacd_extract")
@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class TrackStopFlagsAgainstSacdExtract(unittest.TestCase):
    """A 6-channel DST edited master with gaps and track mute flags, as on a real disc (#179):
    sacd_extract's file differs only in its TrackStop markers' flags, which come from the TOC."""

    def test_mch_edit_master(self):
        with fsutil.TempDir(prefix="pysacd-stops") as tmp:
            root = Path(tmp)
            iso = gapped_iso(root / "gap.iso", [f for _, _, f in dst.encoded(6)], dst_area=True)
            for _ in range(3):      # sacd_extract now and then leaves out a file (see Trees)
                shutil.rmtree(root / "se", ignore_errors=True)
                try:
                    theirs = edit_master([SACD_EXTRACT], iso, root / "se")
                    break
                except AssertionError:
                    continue
            ours = edit_master(PYSACD, iso, root / "py", {**os.environ, "PYTHONPATH": SRC})
            self.assertEqual(cmp._dff_stop_flags(theirs), STOP_FLAGS)
            self.assertEqual(cmp._dff_stop_flags(ours), [0, 0, 0])
            self.assertEqual([t for t, _ in markers(theirs)], [t for t, _ in markers(ours)])
            self.assertTrue(cmp.documented_only(ours, theirs))
            r = cmp.compare(iso, "mch-edit-master", {"old": SRC, "new": SRC}, SACD_EXTRACT, 2, None)
            self.assertEqual(cmp.sacd_extract_problems(r), [])
            self.assertEqual(r["vs_sacd_extract"]["new"]["dff"]["documented"], 1)


# -z where a file ends exactly on a DSF block (#182). Bytes per channel of each DSF file written
# with -s and these arguments, measured with the real sacd_extract on a Fedora 44 machine (plain DSD
# and DST, 2 and 6 channels, with and without -b give the same counts): where a file's audio, with
# the bytes carried into it, ends on a whole block, sacd_extract ends it one block earlier and moves
# the block to the next file of the chain, or drops it. scarletbook 0.7.0's chain plan moves it the
# same way, so these files are sacd_extract's, byte for byte (before 0.7.0 pysacd kept the block).
CHAIN_CASES = [
    # name, frames per track, pauses, arguments, sizes (sacd_extract's and pysacd's)
    ("first track", [128, 50, 60], [], ["-z"], [598016, 237568, 286720]),
    ("two in a row", [128, 128, 60], [], ["-z"], [598016, 602112, 286720]),
    ("middle track", [50, 78, 60], [], ["-z"], [233472, 364544, 286720]),
    ("after a track not selected", [50, 50, 128, 60], [], ["-z", "-t", "1,3,4"], [233472, 598016, 286720]),
    ("before a pause left out", [128, 50, 60], [0, 10, 0], ["-z"], [598016, 188416, 286720]),
    ("with -b", [128, 50, 60], [], ["-z", "-b"], [598016, 237568, 286720]),
    ("area ends on a block", [50, 78], [], ["-z"], [233472, 368640]),
    ("first track of 256 frames", [256, 50, 60], [], ["-z"], [1200128, 237568, 286720]),
    ("a block carried across two pauses", [128, 128, 60], [0, 10, 10], ["-z"], [598016, 557056, 237568]),
    ("the area's last track alone", [50, 128], [], ["-z", "-t", "2"], [602112]),
    ("with -e", [128, 50, 60], [], ["-z", "-e"], [598016, 237568, 286720]),
    # no file on a block: unchanged by 0.7.0
    ("127 frames", [127, 50, 60], [], ["-z"], [593920, 237568, 286720]),
    ("carry past a track not selected", [50, 50, 60, 60], [], ["-z", "-t", "1,3,4"], [233472, 278528, 286720]),
    ("concatenation not on a block", [50, 50, 60], [], ["-z", "-k", "-t", "1,2"], [466944]),
]

# Where a chain stops before the area's end and its last file ends on a block, sacd_extract drops
# that block, so its audio is in no file; pysacd keeps it, by decision (#182, docs/sacd_extract.md).
# Every other byte is the same, and tools/extract_compare.py counts such a file as documented.
# name, frames per track, pauses, arguments, sacd_extract's sizes, pysacd's
KEPT_BLOCK_CASES = [
    ("last of the chain", [50, 78, 60], [], ["-z", "-t", "1,2"], [233472, 364544], [233472, 368640]),
    ("one track", [128, 50, 60], [], ["-z", "-t", "1"], [598016], [602112]),
    ("before a track not selected", [128, 50, 60], [], ["-z", "-t", "1,3"], [598016, 282624], [602112, 282624]),
    ("concatenation", [64, 64, 60], [], ["-z", "-k", "-t", "1,2"], [598016], [602112]),
    ("one track before the area's last", [50, 128, 60], [], ["-z", "-t", "2"], [598016], [602112]),
    # a -k concatenation to the area's end, on a block: sacd_extract drops the block there too
    ("concatenation to the area's end, on a block", [50, 78], [], ["-z", "-k", "-t", "1,2"], [598016], [602112]),
]

# The other known -z difference (docs/sacd_extract.md): a -k concatenation that holds the area's
# last track. sacd_extract writes it unpadded, and drops a whole block where it ends on one;
# scarletbook pads it, as it pads the area's last file, and never shortens it.
# name, frames per track, pauses, arguments, sacd_extract's sizes, pysacd's
AREA_END_CASES = [
    ("concatenation to the area's end", [50, 50, 60], [], ["-z", "-k", "-t", "2,3"], [516096], [520192]),
]


def chain_iso(path: Path, frames: list[int], pauses: list[int], channels: int = 2, dst_area: bool = False) -> Path:
    n = sum(frames)
    make = sacdgen._dst_frame if dst_area else sacdgen._dsd_frame
    sacdgen.build_iso(path, [sacdgen.AreaSpec(channels, [make(channels, i) for i in range(n)], frames,
                                              dst=dst_area, pauses=pauses)],
                      info=sacdgen.DiscInfo(title="Chain Album", artist="Chain Artist"))
    return path


def dsf_sizes(root: Path) -> list[int]:
    """Bytes per channel of the audio of each DSF file under root, in name order; also checks that
    the header's sample count covers them (an unpadded file) or less (the padded last one)."""
    out = []
    for f in sorted(fsutil.lp(root).rglob("*.dsf")):
        head = f.read_bytes()[:92]
        ch = int.from_bytes(head[52:56], "little")
        per = (int.from_bytes(head[84:92], "little") - 12) // ch
        assert int.from_bytes(head[64:72], "little") <= per * 8, f
        out.append(per)
    return out


def extract_dsf(cmd: list[str], iso: Path, cwd: Path, args: list[str], env: dict | None = None) -> Path:
    shutil.rmtree(cwd, ignore_errors=True)
    cwd.mkdir()
    area = "-m" if "-m" in args else "-2"
    r = subprocess.run([*cmd, area, "-s", *[a for a in args if a != "-m"], "-i", str(iso)], cwd=cwd,
                       capture_output=True, text=True, errors="replace", check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return cwd


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
class ChainBoundary(unittest.TestCase):
    """pysacd's -z files at a block boundary (#182), as CHAIN_CASES pins them."""

    def test_sizes(self):
        # in this process, so the coverage run sees the chain plan
        from pysacd import extract
        with fsutil.TempDir(prefix="pysacd-chain") as tmp:
            root = Path(tmp)
            for name, frames, pauses, args, *_theirs, ours in CHAIN_CASES + KEPT_BLOCK_CASES + AREA_END_CASES:
                with self.subTest(name):
                    iso = chain_iso(root / "c.iso", frames, pauses)
                    out = root / "py"
                    shutil.rmtree(out, ignore_errors=True)
                    out.mkdir()
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = extract.main(["--no-cfg", "--threads", "2", "-2", "-s", *args, "-o", str(out),
                                           "-i", str(iso)])
                    self.assertEqual(rc, 0)
                    self.assertEqual(dsf_sizes(out), ours)


@unittest.skipUnless(SACD_EXTRACT, "set SACD_EXTRACT to compare with the real sacd_extract")
@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
class ChainBoundaryAgainstSacdExtract(unittest.TestCase):
    """CHAIN_CASES against the real sacd_extract: its sizes as measured, and pysacd's files
    byte-identical to its files; AREA_END_CASES left as a difference."""

    def check(self, iso: Path, root: Path, args: list[str], theirs: list[int], ours: list[int],
              kept: bool = False) -> None:
        env = {**os.environ, "PYTHONPATH": SRC}
        py = extract_dsf(PYSACD, iso, root / "py", args, env)
        for _ in range(3):      # sacd_extract now and then leaves out a file (see Trees)
            se = extract_dsf([SACD_EXTRACT], iso, root / "se", args)
            if len(dsf_sizes(se)) == len(ours):
                break
        self.assertEqual(dsf_sizes(se), theirs)
        self.assertEqual(dsf_sizes(py), ours)
        vs = cmp.against_sacd_extract(py, se, cmp.tree(py), cmp.tree(se), False, True)["dsf"]
        moved = sum(a != b for a, b in zip(theirs, ours, strict=True))
        if kept:                # sacd_extract drops the block: documented
            self.assertEqual((vs["documented"], vs["identical"], vs["different"]), (moved, len(theirs) - moved, []))
        elif theirs != ours:    # padded at the area's end
            self.assertEqual((vs["identical"], len(vs["different"])), (0, 1))
        else:
            self.assertEqual((vs["identical"], vs["different"]), (len(theirs), []))

    def test_stereo_plain_dsd(self):
        with fsutil.TempDir(prefix="pysacd-chain") as tmp:
            root = Path(tmp)
            for name, frames, pauses, args, *sizes in CHAIN_CASES + AREA_END_CASES:
                with self.subTest(name):
                    self.check(chain_iso(root / "c.iso", frames, pauses), root, args, sizes[0], sizes[-1])
            for name, frames, pauses, args, theirs, ours in KEPT_BLOCK_CASES:
                with self.subTest(name):
                    self.check(chain_iso(root / "c.iso", frames, pauses), root, args, theirs, ours, kept=True)

    def test_multichannel_dst(self):
        with fsutil.TempDir(prefix="pysacd-chain") as tmp:
            root = Path(tmp)
            pick = ("first track", "two in a row", "middle track", "before a pause left out",
                    "a block carried across two pauses")
            for name, frames, pauses, args, sizes in [c for c in CHAIN_CASES if c[0] in pick]:
                with self.subTest(name):
                    iso = chain_iso(root / "c.iso", frames, pauses, channels=6, dst_area=True)
                    self.check(iso, root, ["-m", *args], sizes, sizes)
            for name, frames, pauses, args, theirs, ours in KEPT_BLOCK_CASES[1:2]:
                with self.subTest(name):
                    iso = chain_iso(root / "c.iso", frames, pauses, channels=6, dst_area=True)
                    self.check(iso, root, ["-m", *args], theirs, ours, kept=True)

    def test_compare_tool_modes(self):
        """The -z modes of the release comparison, and -b, on a disc with a first track of 128
        frames: every file is sacd_extract's, but for the block pysacd keeps before a track not
        selected (-t 1,3), which is documented."""
        with fsutil.TempDir(prefix="pysacd-chain") as tmp:
            iso = chain_iso(Path(tmp) / "c.iso", [128, 50, 60, 40], [])
            for mode in ("dsf-nopad", "dsf-nopad-pauses", "dsf-nopad-tracks", "dsf-nopad-gap",
                         "dsf-nopad-concat", "dsf-pauses"):
                with self.subTest(mode):
                    r = Trees.compare(iso, mode)
                    self.assertEqual(cmp.sacd_extract_problems(r), [])
                    vs = r["vs_sacd_extract"]["new"]["dsf"]
                    self.assertEqual(vs["documented"], 1 if mode == "dsf-nopad-gap" else 0)
                    self.assertEqual(vs["identical"] + vs["documented"], vs["theirs"])


class HarnessDetects(unittest.TestCase):
    """A comparison that can't fail compares nothing (#131)."""

    def result(self, mode="dsf", **over) -> dict:
        r = {"mode": mode, "rc": (0, 0), "rc_sacd_extract": 0, "sacd_extract_files": 1,
             "print_matches_sacd_extract": True,
             "vs_sacd_extract": {"new": {"dsf": {"theirs": 1, "identical": 1, "documented": 0, "different": []}}}}
        return {**r, **over}

    def test_a_clean_result_passes(self):
        self.assertEqual(cmp.sacd_extract_problems(self.result()), [])

    def test_sacd_extract_that_did_not_run_fails(self):
        r = self.result()
        del r["rc_sacd_extract"]
        self.assertTrue(cmp.sacd_extract_problems(r))

    def test_sacd_extract_that_failed_or_wrote_nothing_fails(self):
        self.assertTrue(cmp.sacd_extract_problems(self.result(rc_sacd_extract=255)))
        self.assertTrue(cmp.sacd_extract_problems(self.result(sacd_extract_files=0)))

    def test_print_mismatch_fails(self):
        self.assertTrue(cmp.sacd_extract_problems(self.result("print", print_matches_sacd_extract=None)))

    def test_different_file_fails(self):
        r = self.result(vs_sacd_extract={"new": {"dsf": {"theirs": 1, "identical": 0, "documented": 0,
                                                         "different": [{"file": "x", "first": 5, "bytes": 1}]}}})
        self.assertTrue(cmp.sacd_extract_problems(r))

    def test_documented_exceptions_are_narrow(self):
        def dff(machine: bytes, audio: bytes) -> bytes:
            def comment(text: bytes) -> bytes:
                pad = b"\0" if len(text) & 1 else b""
                return b"\x07\xea\x01\x02\x03\x04" + bytes(4) + len(text).to_bytes(4, "big") + text + pad
            comt = (2).to_bytes(2, "big") + comment(b"track") + comment(machine)
            chunks = b"DSD " + len(audio).to_bytes(8, "big") + audio + b"COMT" + len(comt).to_bytes(8, "big") + comt
            return b"FRM8" + (len(chunks) + 4).to_bytes(8, "big") + b"DSD " + chunks

        with fsutil.TempDir() as tmp:
            root = Path(tmp)
            a, b = root / "a.dff", root / "b.dff"
            a.write_bytes(dff(b"sacd-ripper", b"\1\2"))
            b.write_bytes(dff(b"pysacd 9.9", b"\1\2"))
            self.assertTrue(cmp.documented_only(a, b))              # the machine comment may differ
            b.write_bytes(dff(b"pysacd 9.9", b"\1\3"))
            self.assertFalse(cmp.documented_only(a, b))             # the audio may not
            def diin(text: bytes, pad: bytes) -> bytes:
                body = len(text).to_bytes(4, "big") + text + pad
                sub = b"DIAR" + len(body).to_bytes(8, "big") + body
                chunks = b"DIIN" + len(sub).to_bytes(8, "big") + sub
                return b"FRM8" + (len(chunks) + 4).to_bytes(8, "big") + b"DSD " + chunks
            a.write_bytes(diin(b"abc", b"\0"))
            b.write_bytes(diin(b"abc", b"7"))
            self.assertTrue(cmp.documented_only(a, b))              # sacd_extract's pad byte may differ
            b.write_bytes(diin(b"abd", b"\0"))
            self.assertFalse(cmp.documented_only(a, b))             # the text may not
            def marks(stop_flags: int, start_flags: int = 0, pad: bytes = b"") -> bytes:
                def mark(kind: int, flags: int) -> bytes:
                    body = bytes(12) + kind.to_bytes(2, "big") + bytes(2) + flags.to_bytes(2, "big") + bytes(4)
                    return b"MARK" + len(body).to_bytes(8, "big") + body
                sub = mark(2, 0) + mark(0, start_flags) + mark(1, stop_flags) + mark(4, 0) + pad
                chunks = b"DIIN" + len(sub).to_bytes(8, "big") + sub
                return b"FRM8" + (len(chunks) + 4).to_bytes(8, "big") + b"DSD " + chunks
            a.write_bytes(marks(0))
            b.write_bytes(marks(8))
            self.assertTrue(cmp.documented_only(a, b))              # sacd_extract's TrackStop flags may differ
            self.assertFalse(cmp.documented_only(b, a))             # pysacd's must be zero
            b.write_bytes(marks(0x100))
            self.assertFalse(cmp.documented_only(a, b))             # only the mute flags (bits 0 to 3)
            b.write_bytes(marks(0, start_flags=8))
            self.assertFalse(cmp.documented_only(a, b))             # a TrackStart's flags may not differ
            a.write_bytes(marks(0, pad=b"MARK" + (22).to_bytes(8, "big") + bytes(12) + b"\0\4" + bytes(8)))
            b.write_bytes(marks(0, pad=b"MARK" + (22).to_bytes(8, "big") + bytes(12) + b"\0\4" + b"\0\0\0\1" + bytes(4)))
            self.assertFalse(cmp.documented_only(a, b))             # nor an index marker's
            a.write_bytes(marks(0))
            b.write_bytes(marks(8)[:-49] + b"\1" + marks(8)[-48:])   # the TrackStop's samples
            self.assertFalse(cmp.documented_only(a, b))             # nor the marker's time
            self.assertEqual(cmp._dff_stop_flags(b), [8])
            a, b = root / "a.cue", root / "b.cue"
            a.write_bytes(b"REM File created by SACD Extract, version: NA\nTITLE \"x\"\n")
            b.write_bytes(b"REM File created by SACD Extract, version: pysacd 9.9\nTITLE \"x\"\n")
            self.assertTrue(cmp.documented_only(a, b))              # the version line may differ
            b.write_bytes(b"REM File created by SACD Extract, version: pysacd 9.9\nTITLE \"y\"\n")
            self.assertFalse(cmp.documented_only(a, b))             # the rest may not
            a, b = root / "a.dsf", root / "b.dsf"
            a.write_bytes(b"x")
            b.write_bytes(b"y")
            self.assertFalse(cmp.documented_only(a, b))             # a DSF has no exceptions

    def test_b_and_z_modes_fail_on_a_difference(self):
        diff = {"new": {"dsf": {"theirs": 1, "identical": 0, "documented": 0,
                                "different": [{"file": "x", "first": 5, "bytes": 1}]}}}
        for mode in ("dsf-pauses", "dsf-nopad-pauses", "dsdiff-pauses", "dsf-nopad"):
            self.assertTrue(cmp.sacd_extract_problems(self.result(mode, vs_sacd_extract=diff)))

    @staticmethod
    def dsf(rows: list[bytes], samples_per_ch: int, tag: bytes = b"ID3tag", ch: int = 2) -> bytes:
        audio = b"".join(rows)
        meta = 92 + len(audio) if tag else 0
        total = 92 + len(audio) + len(tag)
        return (b"DSD " + (28).to_bytes(8, "little") + total.to_bytes(8, "little") + meta.to_bytes(8, "little") +
                b"fmt " + (52).to_bytes(8, "little") + (1).to_bytes(4, "little") + bytes(4) +
                (2).to_bytes(4, "little") + ch.to_bytes(4, "little") + (2822400).to_bytes(4, "little") +
                (1).to_bytes(4, "little") + (samples_per_ch * 8).to_bytes(8, "little") +
                (4096).to_bytes(4, "little") + bytes(4) +
                b"data" + (12 + len(audio)).to_bytes(8, "little") + audio + tag)

    def test_block_carry_is_a_difference(self):
        """sacd_extract's -z block carry (#182) was once excused in -z runs; since scarletbook
        0.7.0 pysacd writes it too. The file that gives up the block looks like a dropped block
        (documented), but the next one, which starts with it, is different, so the run fails."""
        rnd = random.Random(182)
        row = lambda: rnd.randbytes(2 * 4096)
        a, b = [row() for _ in range(3)], [row() for _ in range(2)]
        ours = [self.dsf(a, 3 * 4096), self.dsf(b, 2 * 4096)]
        theirs = [self.dsf(a[:2], 2 * 4096), self.dsf([a[2], *b], 3 * 4096)]
        with fsutil.TempDir(prefix="pysacd-carry") as tmp:
            se, py = self.trees(Path(tmp), ours, theirs)
            vs = cmp.against_sacd_extract(py, se, cmp.tree(py), cmp.tree(se), False, True)["dsf"]
            self.assertEqual((vs["identical"], vs["documented"], len(vs["different"])), (0, 1, 1))

    def test_dropped_block_is_narrow(self):
        """The documented -z difference (#182): sacd_extract's file is pysacd's without its last
        row of blocks, header sizes and sample count smaller by as much, and nothing else."""
        rnd = random.Random(182)
        rows = [rnd.randbytes(2 * 4096) for _ in range(3)]
        ours = self.dsf(rows, 3 * 4096)
        with fsutil.TempDir(prefix="pysacd-drop") as tmp:
            root = Path(tmp)

            def vs(theirs: bytes, nopad: bool = True) -> dict:
                se, py = self.trees(root / str(len(list(root.iterdir()))), [ours], [theirs])
                return cmp.against_sacd_extract(py, se, cmp.tree(py), cmp.tree(se), False, nopad)["dsf"]

            self.assertEqual(vs(self.dsf(rows[:2], 2 * 4096))["documented"], 1)
            self.assertEqual(len(vs(self.dsf(rows[:2], 2 * 4096), nopad=False)["different"]), 1)  # only -z
            self.assertEqual(len(vs(self.dsf(rows[:2], 2 * 4096 - 1))["different"]), 1)           # the count
            self.assertEqual(len(vs(self.dsf(rows[:2], 2 * 4096, b"ID3taG"))["different"]), 1)     # the tag
            self.assertEqual(len(vs(self.dsf(rows[:1], 4096))["different"]), 1)                   # two rows
            self.assertEqual(len(vs(self.dsf([rows[0], rows[2]], 2 * 4096))["different"]), 1)      # a middle row
            bad = bytearray(self.dsf(rows[:2], 2 * 4096))
            bad[92 + 100] ^= 1
            self.assertEqual(len(vs(bytes(bad))["different"]), 1)                                 # a byte of audio
            padded = self.dsf(rows, 3 * 4096 - 1000)                # pysacd's file padded: never this
            se, py = self.trees(root / "padded", [padded], [self.dsf(rows[:2], 2 * 4096 - 1000)])
            vs_p = cmp.against_sacd_extract(py, se, cmp.tree(py), cmp.tree(se), False, True)["dsf"]
            self.assertEqual(len(vs_p["different"]), 1)

    @staticmethod
    def trees(root: Path, ours: list[bytes], theirs: list[bytes]) -> tuple[Path, Path]:
        se, py = root / "se", root / "py"
        for d, files in ((se, theirs), (py, ours)):
            d.mkdir(parents=True)
            for i, data in enumerate(files):
                (d / f"0{i + 1} - T.dsf").write_bytes(data)
        return se, py


if __name__ == "__main__":
    unittest.main()
