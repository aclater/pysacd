"""pysacd convert: different discs rendering to the same directory (#4)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sacdgen
import test_dst_full as dst

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from extract_compare import documented_only

SRC = Path(__file__).resolve().parents[1] / "src"


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class DiscCollisions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "out"
        self.frames = [f for _, _, f in dst.encoded(2)][:6]

    def tearDown(self):
        self.tmp.cleanup()

    def iso(self, name: str, titles: list[str] | None = None, **info) -> Path:
        path = self.dir / f"{name}.iso"
        sacdgen.build_iso(path, [sacdgen.AreaSpec(2, self.frames, [3, 3], titles=titles or [])],
                          info=sacdgen.DiscInfo(title="Live-Evil", artist="Miles Davis", **info))
        return path

    def convert(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "pysacd", "convert", "--config", os.devnull,
                               "--no-musicbrainz", "-o", str(self.out), *args],
                              env=dict(os.environ, PYTHONPATH=str(SRC)), capture_output=True, text=True, check=False)

    def files(self) -> list[str]:
        return sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*.dsf"))

    def test_mastered_as_one_disc_sets_are_separated_with_hint(self):
        a = self.iso("d1", disc_catalog="CS065232")
        b = self.iso("d2", disc_catalog="CS065233")
        self.assertEqual(self.convert(str(a)).returncode, 0)
        r = self.convert(str(b))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already holds a different disc (CS065232)", r.stderr)
        self.assertIn("--disc 1/2 for CS065232, --disc 2/2 for CS065233", r.stderr)
        dirs = sorted({Path(f).parent.as_posix() for f in self.files()})
        self.assertEqual(dirs, ["Miles Davis/Live-Evil [SACD]", "Miles Davis/Live-Evil [SACD] [CS065233]"])

    @unittest.skipUnless(dst._native_has_flac(), "engine built without libFLAC")
    def test_collision_detected_in_flac_tags(self):
        a = self.iso("d1", disc_catalog="CS065232")
        b = self.iso("d2", disc_catalog="CS065233")
        self.assertEqual(self.convert("-f", "flac", str(a)).returncode, 0)
        r = self.convert("-f", "flac", str(b))
        self.assertIn("already holds a different disc (CS065232)", r.stderr)
        self.assertEqual(len(list(self.out.rglob("*.flac"))), 4)

    def test_disc_override_joins_the_set(self):
        a = self.iso("d1", disc_catalog="CS065232")
        b = self.iso("d2", disc_catalog="CS065233")
        self.assertEqual(self.convert("--disc", "1/2", str(a)).returncode, 0)
        r = self.convert("--disc", "2/2", str(b))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("warning", r.stderr)
        self.assertEqual(self.files(), [
            "Miles Davis/Live-Evil [SACD]/1-01 - Track 1.dsf",
            "Miles Davis/Live-Evil [SACD]/1-02 - Track 2.dsf",
            "Miles Davis/Live-Evil [SACD]/2-01 - Track 1.dsf",
            "Miles Davis/Live-Evil [SACD]/2-02 - Track 2.dsf",
        ])

    def test_properly_mastered_set_shares_directory(self):
        a = self.iso("d1", disc_catalog="X-CD01", set_size=2, sequence=1)
        b = self.iso("d2", disc_catalog="X-CD02", set_size=2, sequence=2)
        self.assertEqual(self.convert(str(a)).returncode, 0)
        r = self.convert(str(b))
        self.assertNotIn("warning", r.stderr)
        self.assertEqual(len(self.files()), 4)
        self.assertEqual({Path(f).parent.as_posix() for f in self.files()}, {"Miles Davis/Live-Evil [SACD]"})

    def test_unrelated_discs_same_title_get_no_hint(self):
        a = self.iso("d1", disc_catalog="ABC-1")
        b = self.iso("d2", disc_catalog="XYZ")
        self.convert(str(a))
        r = self.convert(str(b))
        self.assertIn("already holds a different disc", r.stderr)
        self.assertNotIn("hint:", r.stderr)

    def test_rerun_reports_skips(self):
        a = self.iso("d1", disc_catalog="CS065232")
        self.convert(str(a))
        r = self.convert(str(a))
        self.assertNotIn("different disc", r.stderr)
        self.assertIn("2 tracks skipped (already exist; --overwrite replaces them)", r.stderr)

    def test_bad_disc_value(self):
        r = self.convert("--disc", "3/2", str(self.iso("d1")))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--disc must be N/M", r.stderr)


SACD_EXTRACT = os.environ.get("SACD_EXTRACT")


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class ExtractDiscCollisions(DiscCollisions):
    """pysacd-extract: same policy, a documented exception to byte-identity with sacd_extract."""

    def extract(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "pysacd.extract", "--no-cfg", *args], cwd=cwd or self.out,
                              env=dict(os.environ, PYTHONPATH=str(SRC)), capture_output=True, text=True, check=False)

    def setUp(self):
        super().setUp()
        self.out.mkdir()

    def tree(self, root: Path) -> dict[str, bytes]:
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    def test_mastered_as_one_disc_sets_are_separated_with_hint(self):
        # As on the real Live-Evil: same album title, different track titles
        a = self.iso("d1", disc_catalog="CS065232", titles=["Sivad", "Little Church"])
        b = self.iso("d2", disc_catalog="CS065233", titles=["Selim", "Funky Tonk"])
        self.assertEqual(self.extract("-s", "-i", str(a)).returncode, 0)
        r = self.extract("-s", "-i", str(b))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already holds tracks of another disc", r.stderr)
        self.assertIn("--disc N/M", r.stderr)
        self.assertEqual(sorted({Path(f).parent.as_posix() for f in self.files()}), ["Live-Evil", "Live-Evil [CS065233]"])

    def test_disc_override_joins_the_set(self):
        a = self.iso("d1", disc_catalog="CS065232")
        b = self.iso("d2", disc_catalog="CS065233")
        self.assertEqual(self.extract("--disc", "1/2", "-s", "-i", str(a)).returncode, 0)
        r = self.extract("--disc", "2/2", "-s", "-i", str(b))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("WARNING", r.stderr)
        self.assertEqual(sorted({Path(f).parent.as_posix() for f in self.files()}), ["Live-Evil/Disc 1", "Live-Evil/Disc 2"])

    def test_properly_mastered_set_shares_directory(self):
        a = self.iso("d1", disc_catalog="X-CD01", set_size=2, sequence=1)
        b = self.iso("d2", disc_catalog="X-CD02", set_size=2, sequence=2)
        self.extract("-s", "-i", str(a))
        r = self.extract("-s", "-i", str(b))
        self.assertNotIn("WARNING", r.stderr)

    def test_same_disc_rerun_overwrites_like_sacd_extract(self):
        a = self.iso("d1", disc_catalog="CS065232")
        self.extract("-s", "-i", str(a))
        r = self.extract("-s", "-i", str(a))
        self.assertNotIn("WARNING", r.stderr)
        self.assertEqual(sorted({Path(f).parent.as_posix() for f in self.files()}), ["Live-Evil"])

    def test_same_names_different_disc_not_overwritten(self):
        # Same title and track names, different ISRC-free identity: track numbers differ
        a = self.iso("d1", disc_catalog="AAA")
        self.extract("-s", "-i", str(a))
        frames = [f for _, _, f in dst.encoded(2)][:6]
        b = self.dir / "d2.iso"
        sacdgen.build_iso(b, [sacdgen.AreaSpec(2, frames, [2, 2, 2])],
                          info=sacdgen.DiscInfo(title="Live-Evil", artist="Miles Davis", disc_catalog="BBB"))
        r = self.extract("-s", "-i", str(b))
        self.assertIn("already holds tracks of another disc", r.stderr)
        self.assertIn("Live-Evil [BBB]", {Path(f).parent.as_posix() for f in self.files()})

    def test_bad_disc_value(self):
        r = self.extract("--disc", "0/2", "-s", "-i", str(self.iso("d1")))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--disc must be N/M", r.stderr)

    @unittest.skip("convert-only")
    def test_collision_detected_in_flac_tags(self):
        pass

    @unittest.skip("convert-only")
    def test_rerun_reports_skips(self):
        pass

    @unittest.skip("convert-only")
    def test_unrelated_discs_same_title_get_no_hint(self):
        pass

    @unittest.skipUnless(SACD_EXTRACT, "set SACD_EXTRACT to compare with the real sacd_extract")
    def test_disc_override_matches_sacd_extract_set_layout(self):
        # A 1/1 disc with --disc 2/2 must produce exactly what sacd_extract
        # produces for the same disc mastered as disc 2 of a 2-disc set.
        one = self.iso("d1", disc_catalog="CS065233")
        two = self.iso("d2", disc_catalog="CS065233", set_size=2, sequence=2)
        ours, theirs = self.dir / "ours", self.dir / "theirs"
        ours.mkdir(), theirs.mkdir()
        boot = ("import sys;from pysacd import sbcompat as s, extract;"
                "s.PYSACD_VERSION_INFO=s.SACD_RIPPER_VERSION_INFO;"
                "sys.exit(extract.main())")
        for args in (["-s"], ["-p"]):
            r = subprocess.run([sys.executable, "-c", boot, "--no-cfg", "--disc", "2/2", *args, "-i", str(one)],
                               cwd=ours, env=dict(os.environ, PYTHONPATH=str(SRC)), capture_output=True, text=True,
                               check=False)
            self.assertEqual(r.returncode, 0, r.stderr)
            subprocess.run([SACD_EXTRACT, *args, f"-i{two}"], cwd=theirs, capture_output=True, check=False)
        a, b = self.tree(ours), self.tree(theirs)
        self.assertEqual(sorted(a), sorted(b))
        for k in a:
            if k.endswith(".dff"):
                continue    # creating-machine COMT timestamp differs
            self.assertEqual(a[k], b[k], k)


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
class NoSilentReplacement(unittest.TestCase):
    """A second disc never replaces the first disc's files without saying so, even when its
    album and track names are the same (docs/security.md, "Hostile images")."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "out"
        self.out.mkdir()
        self.env = dict(os.environ, PYTHONPATH=str(SRC))

    def iso(self, name: str, seed: int, title: str = "Same", **info) -> Path:
        path = self.dir / f"{name}.iso"
        frames = [sacdgen._dsd_frame(2, seed + i) for i in range(6)]
        sacdgen.build_iso(path, [sacdgen.AreaSpec(2, frames, [3, 3], dst=False, titles=["One", "Two"])],
                          info=sacdgen.DiscInfo(title=title, artist="A", **info))
        return path

    def hashes(self) -> dict[str, str]:
        import hashlib
        return {p.relative_to(self.out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.out.rglob("*.dsf"))}

    def convert(self, iso: Path) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "pysacd", "convert", "--config", os.devnull, "--no-musicbrainz",
                               "-o", str(self.out), str(iso)], env=self.env, capture_output=True, text=True, check=False)

    def extract(self, iso: Path, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "pysacd.extract", "--no-cfg", "--templates", "--config", os.devnull,
                               "-s", "-o", str(self.out), "-i", str(iso), *extra],
                              env=self.env, capture_output=True, text=True, check=False)

    def test_convert_long_title_keeps_the_label(self):
        # 250 bytes of title: the folder name is cut, and the cut must not take the label
        for title in ("A" * 250, "\u00e9" * 120):
            with self.subTest(title=title[:3]):
                for p in self.out.iterdir():
                    __import__("shutil").rmtree(p)
                a = self.iso("a", 0, title=title, album_catalog="CAT-A", disc_catalog="DISC-A")
                b = self.iso("b", 50, title=title, album_catalog="CAT-B", disc_catalog="DISC-B")
                r = self.convert(a)
                self.assertEqual(r.returncode, 0, r.stderr)
                first = self.hashes()
                self.assertEqual(len(first), 2)
                r = self.convert(b)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("already holds a different disc", r.stderr)
                after = self.hashes()
                self.assertEqual({k: after[k] for k in first}, first)       # the first disc is untouched
                self.assertEqual(len(after), 4)
                folders = {Path(k).parent.name for k in after}
                self.assertEqual(len(folders), 2)
                self.assertTrue(any(f.endswith("]") and "DISC-B" in f or "CAT-B" in f for f in folders), folders)
                for f in folders:
                    self.assertLessEqual(len(f.encode()), 255)

    def test_labelled(self):
        from pysacd.convert import NAME_MAX_BYTES, _labelled
        self.assertEqual(_labelled("Album", "CAT 1", False), "Album [CAT 1]")
        n = _labelled("A" * 400, "CAT-B", False)
        self.assertTrue(n.endswith(" [CAT-B]"))
        self.assertLessEqual(len(n.encode()), NAME_MAX_BYTES)
        n = _labelled("\u00e9" * 400, "L" * 300, False)
        self.assertTrue(n.endswith("]"))
        self.assertLessEqual(len(n.encode()), NAME_MAX_BYTES)
        self.assertNotEqual(n, "\u00e9" * 120)

    def test_extract_templates_keeps_existing_files(self):
        a = self.iso("a", 0, album_catalog="CAT-A", disc_catalog="DISC-A")
        b = self.iso("b", 50, album_catalog="CAT-B", disc_catalog="DISC-B")
        r = self.extract(a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        first = self.hashes()
        self.assertEqual(len(first), 2)
        r = self.extract(b)
        self.assertEqual(self.hashes(), first)
        self.assertIn("2 files skipped (already exist; --overwrite replaces them)", r.stdout)
        self.assertEqual(r.returncode, 1)          # nothing written: not a finished extraction
        r = self.extract(b, "--overwrite")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(self.hashes()), sorted(first))
        self.assertNotEqual(self.hashes(), first)

    def test_extract_templates_warns_about_a_mixed_folder(self):
        a = self.iso("a", 0)
        self.extract(a)
        c = self.dir / "c.iso"
        frames = [sacdgen._dsd_frame(2, 90 + i) for i in range(6)]
        sacdgen.build_iso(c, [sacdgen.AreaSpec(2, frames, [2, 2, 2], dst=False, titles=["Uno", "Dos", "Tres"])],
                          info=sacdgen.DiscInfo(title="Same", artist="A"))
        r = self.extract(c)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already holds tracks of another disc", r.stderr)
        self.assertEqual(len(self.hashes()), 5)

    def test_replacing_files_of_another_size_is_said(self):
        # sacd_extract's naming: a re-run replaces, silently when the sizes are the same
        run = lambda iso: subprocess.run([sys.executable, "-m", "pysacd.extract", "--no-cfg", "-s", "-o",
                                          str(self.out), "-i", str(iso)], env=self.env, capture_output=True,
                                         text=True, check=False)
        a = self.iso("a", 0)
        self.assertNotIn("WARNING", run(a).stderr)
        self.assertNotIn("WARNING", run(a).stderr)
        # the same names and tags, longer tracks
        d = self.dir / "d.iso"
        frames = [sacdgen._dsd_frame(2, 70 + i) for i in range(16)]
        sacdgen.build_iso(d, [sacdgen.AreaSpec(2, frames, [8, 8], dst=False, titles=["One", "Two"])],
                          info=sacdgen.DiscInfo(title="Same", artist="A"))
        r = run(d)
        self.assertIn("replacing 2 existing files of a different size", r.stderr)

    def test_convert_refuses_a_path_outside_the_output_folder(self):
        from unittest import mock

        from pysacd import convert as cv
        a = self.iso("a", 0)
        with mock.patch.object(cv, "render", return_value="../../escape"), self.assertRaises(SystemExit) as e:
            cv.convert(str(a), cv.Options(output_dir=str(self.out), format="dsf", musicbrainz=False, cover=False,
                                          cover_file=""))
        self.assertIn("outside the output folder", str(e.exception))
        self.assertEqual(list(self.dir.rglob("*.dsf")), [])

    def test_check_own_file(self):
        from pysacd import safeio
        f = self.dir / "f"
        f.write_bytes(b"x")
        safeio.check_own_file(f)
        if os.name == "posix":
            link = self.dir / "l"
            link.symlink_to(f)
            with self.assertRaises(OSError):
                safeio.check_own_file(link)
            os.link(f, self.dir / "hard")
            with self.assertRaises(OSError):
                safeio.check_own_file(f)
        self.assertTrue(safeio.inside("out", "out/a/../b"))
        self.assertFalse(safeio.inside("out", "out/../b"))


def _case_insensitive(folder: Path) -> bool:
    probe = folder / "case-probe"
    probe.touch()
    try:
        return (folder / "CASE-PROBE").exists()
    finally:
        probe.unlink()


class DeviceNames(unittest.TestCase):
    """sacd_extract's names on Windows: device names get a leading '_' there only (#156)."""

    def test_device_names(self):
        from unittest import mock

        from pysacd import sbcompat as sb
        for name in ("CON", "con", "NUL.dsf", "aux.tar.gz", "COM1", "LPT9", "COM\u00b9", "LPT\u00b3", "PRN .x"):
            with mock.patch.object(sb, "_WINDOWS", True):
                self.assertEqual(sb._not_device(name.encode()), b"_" + name.encode(), name)
            with mock.patch.object(sb, "_WINDOWS", False):
                self.assertEqual(sb._not_device(name.encode()), name.encode(), name)
        for name in ("COM10", "CONSOLE", "COM1 - x", "_CON", "01 - CON"):
            with mock.patch.object(sb, "_WINDOWS", True):
                self.assertEqual(sb._not_device(name.encode()), name.encode(), name)

    def test_names_on_this_platform(self):
        from pysacd import sbcompat as sb
        win = sys.platform == "win32"
        self.assertEqual(sb.make_filename("out", "CON", "cue"), "out/_CON.cue" if win else "out/CON.cue")
        self.assertEqual(sb.make_filename(None, "nul", "dff"), "_nul.dff" if win else "nul.dff")
        self.assertEqual(sb.make_filename(None, "01 - CON", "dsf"), "01 - CON.dsf")


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
class PortableExtractNames(unittest.TestCase):
    """pysacd-extract, end to end, on the filesystem it runs on (#156): run on the Windows and
    macOS runners too, where the device names and the case folding are real."""

    BOOT = ("import sys;from pysacd import sbcompat as s, extract;"
            "s.PYSACD_VERSION_INFO=s.SACD_RIPPER_VERSION_INFO;"
            "sys.exit(extract.main())")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def extract(self, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        cwd.mkdir(exist_ok=True)
        r = subprocess.run([sys.executable, "-c", self.BOOT, "--no-cfg", *args], cwd=cwd,
                           env=dict(os.environ, PYTHONPATH=str(SRC)), capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    @staticmethod
    def tree(root: Path) -> dict[str, bytes]:
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    def device_iso(self) -> Path:
        path = self.dir / "con.iso"
        frames = [sacdgen._dsd_frame(2, i) for i in range(4)]
        sacdgen.build_iso(path, [sacdgen.AreaSpec(2, frames, [2, 2], dst=False, titles=["NUL", "aux"])],
                          info=sacdgen.DiscInfo(title="CON", artist="PRN"))
        return path

    def case_iso(self) -> Path:
        # Tracks past 99 get no number in sacd_extract's names: 100, 101 and 102 are all "Intro"
        path = self.dir / "case.iso"
        frames = [sacdgen._dsd_frame(2, i) for i in range(102)]
        sacdgen.build_iso(path, [sacdgen.AreaSpec(2, frames, [1] * 102, dst=False,
                                                  titles=["t"] * 99 + ["Intro", "intro", "Intro"])],
                          info=sacdgen.DiscInfo(title="Case"))
        return path

    def test_device_names(self):
        iso = self.device_iso()
        album = "_CON" if sys.platform == "win32" else "CON"
        for i, (args, names) in enumerate(((["-s"], ["01 - NUL.dsf", "02 - aux.dsf"]),
                                           (["-e", "-C"], [f"{album}.dff", f"{album}.cue", f"{album}.xml"]),
                                           (["-I"], [f"{album}.iso"]))):
            ours = self.dir / f"ours{i}"
            self.extract(ours, *args, "-i", str(iso))
            self.assertEqual(sorted(self.tree(ours)), sorted(f"{album}/{n}" for n in names))
        self.assertIn(f'FILE "{album}.dff"', (self.dir / "ours1" / album / f"{album}.cue").read_text(encoding="utf-8"))

    def test_names_differing_in_case_keep_every_track(self):
        ours = self.dir / "ours"
        ours.mkdir()
        folded = _case_insensitive(ours)
        if sys.platform in ("win32", "darwin"):
            self.assertTrue(folded, "expected a case-insensitive filesystem on this runner")
        self.extract(ours, "-s", "-i", str(self.case_iso()))
        files = self.tree(ours / "Case")
        self.assertEqual(len(files), 102)
        self.assertEqual(len(set(files.values())), 102)         # each track's own audio
        late = sorted(n for n in files if not n[:2].isdigit())
        if folded:
            self.assertEqual(late, ["_ Intro (3).dsf", "_ Intro.dsf", "_ intro (2).dsf"])
        else:
            # case-sensitive: sacd_extract's names, and only the exact duplicate is renamed
            self.assertEqual(late, ["_ Intro (2).dsf", "_ Intro.dsf", "_ intro.dsf"])
        # Re-run: the renamed files are this disc's own, not another disc's (#4)
        r = self.extract(ours, "-s", "-i", str(self.case_iso()))
        self.assertNotIn("already holds tracks of another disc", r.stderr)
        self.assertEqual(sorted(self.tree(ours)), sorted(f"Case/{n}" for n in files))

    @unittest.skipUnless(SACD_EXTRACT, "set SACD_EXTRACT to compare with the real sacd_extract")
    def test_device_names_match_sacd_extract(self):
        # Off Windows, a device name is an ordinary name: byte-identical to sacd_extract
        iso = self.device_iso()
        for i, args in enumerate((["-s"], ["-e", "-C"])):
            ours, theirs = self.dir / f"ours{i}", self.dir / f"theirs{i}"
            self.extract(ours, *args, "-i", str(iso))
            theirs.mkdir()
            subprocess.run([SACD_EXTRACT, *args, f"-i{iso}"], cwd=theirs, capture_output=True, check=False)
            a, b = self.tree(ours), self.tree(theirs)
            self.assertEqual(sorted(a), sorted(b))
            self.assertIn("CON/01 - NUL.dsf" if i == 0 else "CON/CON.cue", a)
            for k in a:
                if k.endswith(".dff"):          # the creating-machine comment differs
                    continue
                # .cue/.xml: only the version line differs (sacd_extract's names its own version)
                x, y = a[k].splitlines(), b[k].splitlines()
                self.assertEqual(len(x), len(y), k)
                self.assertEqual([(p, q) for p, q in zip(x, y) if p != q and b"pysacd" not in p], [], k)

    @unittest.skipUnless(SACD_EXTRACT, "set SACD_EXTRACT to compare with the real sacd_extract")
    def test_case_collisions_against_sacd_extract(self):
        # Linux (case-sensitive): every file sacd_extract keeps is ours byte for byte, under its
        # name ('-' leading a name becomes '_', docs/security.md); the track sacd_extract writes
        # over (track 100, replaced by 102) is ours as "_ Intro.dsf", and 102 is "_ Intro (2).dsf".
        iso = self.case_iso()
        ours, theirs = self.dir / "ours", self.dir / "theirs"
        ours.mkdir()
        if _case_insensitive(ours):
            self.skipTest("needs a case-sensitive filesystem")
        self.extract(ours, "-s", "-i", str(iso))
        theirs.mkdir()
        subprocess.run([SACD_EXTRACT, "-s", f"-i{iso}"], cwd=theirs, capture_output=True, check=False)
        a, b = self.tree(ours), self.tree(theirs)
        self.assertEqual(len(b), 101)                            # sacd_extract lost a track
        self.assertEqual(len(a), 102)
        rename = {"Case/- Intro.dsf": "Case/_ Intro (2).dsf", "Case/- intro.dsf": "Case/_ intro.dsf"}
        for k, v in b.items():
            self.assertEqual(a[rename.get(k, k)], v, k)
        self.assertNotIn(a["Case/_ Intro.dsf"], b.values())


def _plain_disc(path: Path, titles: list[str], catalog: str, seed: int = 0, tracks: tuple[int, ...] = (3, 3),
                mch: bool = False, isrcs: list[str] | None = None) -> Path:
    """A plain DSD stereo disc; with `mch`, DST stereo and 6 channels (as in test_extract_tree).
    `isrcs`: as sacdgen.AreaSpec's (default: every track has one)."""
    frames = sum(tracks)
    if mch:
        areas = [sacdgen.AreaSpec(ch, [f for _, _, f in dst.encoded(ch)][:frames], list(tracks), titles=titles,
                                  isrcs=isrcs) for ch in (2, 6)]
    else:
        areas = [sacdgen.AreaSpec(2, [sacdgen._dsd_frame(2, seed + i) for i in range(frames)], list(tracks),
                                  dst=False, titles=titles, isrcs=isrcs)]
    sacdgen.build_iso(path, areas, info=sacdgen.DiscInfo(title="Album", artist="Artist", disc_catalog=catalog))
    return path


# One disc's runs into one folder, in other modes (#165): -s, -p, -e (edit master), -C (cue and
# xml), track selections and a concatenation. The second run must find the first run's files to
# be this disc's, as sacd_extract writes them all into the album folder.
OWN_SEQUENCES = [
    (["-s"], ["-e"]), (["-s"], ["-C"]), (["-e"], ["-s"]), (["-p"], ["-e"]), (["-e"], ["-p"]), (["-e"], ["-e"]),
    (["-e"], ["-C"]), (["-s", "-t", "1"], ["-s", "-t", "2"]), (["-s"], ["-s", "-k", "-t", "1,2"]),
    (["-s", "-k", "-t", "1,2"], ["-s"]), (["-s"], ["-e"], ["-p"]),
]
OWN_MCH_SEQUENCES = [(["-2", "-m", "-s"], ["-2", "-m", "-e"]), (["-2", "-m", "-e"], ["-2", "-m", "-s"]),
                     (["-s"], ["-e"]), (["-2", "-m", "-s"], ["-2", "-m", "-C"])]


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class OwnFilesAcrossModes(unittest.TestCase):
    """This disc's own files, written in another mode or with another track selection, are this
    disc's (#165); another disc's files in the same mode combinations still keep it out (#4)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.a = _plain_disc(self.dir / "a.iso", ["One", "Two"], "CAT-A")
        self.env = dict(os.environ, PYTHONPATH=str(SRC))

    def out(self, name: str) -> Path:
        d = self.dir / name
        d.mkdir()
        return d

    def pysacd(self, cwd: Path, iso: Path, args: list[str]) -> subprocess.CompletedProcess:
        r = subprocess.run([sys.executable, "-m", "pysacd.extract", "--no-cfg", *args, "-i", str(iso)], cwd=cwd, env=self.env,
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    @staticmethod
    def tree(root: Path) -> dict[str, bytes]:
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    def test_files_without_readable_tags_are_matched_by_name(self):
        # A damaged file of this disc is replaced in place, with a warning; files written
        # without tags (--id3tag 0) are this disc's too, and say nothing
        out = self.out("damaged")
        self.pysacd(out, self.a, ["-s"])
        dsf = sorted(out.rglob("*.dsf"))
        self.assertEqual(len(dsf), 2)
        good = dsf[0].read_bytes()
        dsf[0].write_bytes(b"not a DSF file")
        r = self.pysacd(out, self.a, ["-s"])
        self.assertIn("its tags could not be read", r.stdout + r.stderr)
        self.assertIn("matched by name only", r.stdout + r.stderr)
        self.assertEqual(sorted(out.rglob("*.dsf")), dsf)        # same folder, nothing diverted
        self.assertEqual(dsf[0].read_bytes(), good)

        out = self.out("untagged")
        self.pysacd(out, self.a, ["-s", "--id3tag", "0"])
        before = self.tree(out)
        r = self.pysacd(out, self.a, ["-s", "--id3tag", "0"])
        self.assertNotIn("could not be read", r.stdout + r.stderr)
        self.assertEqual(self.tree(out), before)

    def test_own_files_stay_in_the_album_folder(self):
        m = _plain_disc(self.dir / "m.iso", ["One", "Two"], "CAT-M", mch=True)
        n = 0
        for iso, seqs in ((self.a, OWN_SEQUENCES), (m, OWN_MCH_SEQUENCES)):
            for seq in seqs:
                with self.subTest(disc=iso.stem, seq=seq):
                    n += 1
                    out = self.out(f"own{n}")
                    for args in seq:
                        r = self.pysacd(out, iso, args)
                        self.assertNotIn("WARNING", r.stderr)
                    self.assertEqual({k.split("/")[0] for k in self.tree(out)}, {"Album"})

    def test_another_disc_is_still_kept_out(self):
        # same album title, other track titles; same titles, another track count (so another edit
        # master with the same name); and a stereo + multichannel pair
        b = _plain_disc(self.dir / "b.iso", ["Uno", "Dos"], "CAT-B", seed=40)
        b3 = _plain_disc(self.dir / "b3.iso", ["One", "Two", "Three"], "CAT-B3", seed=60, tracks=(2, 2, 2))
        m = _plain_disc(self.dir / "m.iso", ["One", "Two"], "CAT-M", mch=True)
        mb = _plain_disc(self.dir / "mb.iso", ["Uno", "Dos"], "CAT-MB", seed=40, mch=True)
        cases = [(self.a, b, first, second) for first, second in (
            (["-s"], ["-e"]), (["-s"], ["-C"]), (["-e"], ["-s"]), (["-e"], ["-e"]), (["-p"], ["-e"]),
            (["-e"], ["-p"]), (["-s"], ["-s"]), (["-s", "-t", "1"], ["-s", "-t", "2"]),
            (["-s"], ["-s", "-k", "-t", "1,2"]), (["-s", "-k", "-t", "1,2"], ["-s"]))]
        cases += [(self.a, b3, first, second) for first, second in (
            (["-s"], ["-e"]), (["-e"], ["-s"]), (["-e"], ["-e"]), (["-s"], ["-s"]), (["-s", "-t", "1"], ["-s", "-t", "2"]))]
        cases += [(m, mb, first, second) for first, second in (
            (["-2", "-m", "-s"], ["-2", "-m", "-e"]), (["-2", "-m", "-e"], ["-2", "-m", "-s"]))]
        for n, (one, other, first, second) in enumerate(cases):
            with self.subTest(first=(one.stem, first), second=(other.stem, second)):
                out = self.out(f"other{n}")
                self.pysacd(out, one, first)
                before = self.tree(out)
                r = self.pysacd(out, other, second)
                self.assertIn("already holds tracks of another disc", r.stderr)
                after = self.tree(out)
                self.assertEqual({k: after[k] for k in before}, before)      # the first disc's files untouched
                label = "CAT-" + other.stem.upper()
                self.assertEqual({k.split("/")[0] for k in after if k not in before}, {f"Album [{label}]"})

    def test_check_in_process(self):
        # the same check called directly (the runs above are subprocesses): an edit master, a
        # concatenation and one track are this disc's for a run of the other track; another
        # disc's files are not
        from pysacd import extract
        from pysacd.toc import SACDImage
        b = _plain_disc(self.dir / "b.iso", ["Uno", "Dos"], "CAT-B", seed=40)
        out = self.out("inproc")
        for args in (["-e"], ["-s", "-k", "-t", "1,2"], ["-s", "-t", "1"]):
            self.pysacd(out, self.a, args)
        album = out / "Album"
        for iso, want in ((self.a, album), (b, out / "Album [CAT-B]")):
            with self.subTest(disc=iso.stem), SACDImage(str(iso)) as img:
                disc = img.read_toc()
                o = extract.parse_args(["-2", "-s", "-t", "2", "-o", str(out)])
                with self.assertLogs("pysacd.extract", "DEBUG") as logs:
                    extract.log.debug("start")
                    got = extract._avoid_other_discs(disc, extract._wanted_areas(disc, o), o, album)
                self.assertEqual(got, want)
                self.assertEqual(any("another disc" in m or "doesn't produce" in m for m in logs.output),
                                 iso == b)

    @unittest.skipUnless(SACD_EXTRACT, "set SACD_EXTRACT to compare with the real sacd_extract")
    def test_mixed_modes_match_sacd_extract(self):
        m = _plain_disc(self.dir / "m.iso", ["One", "Two"], "CAT-M", mch=True)
        n = 0
        for iso, seqs in ((self.a, OWN_SEQUENCES), (m, OWN_MCH_SEQUENCES)):
            for seq in seqs:
                with self.subTest(disc=iso.stem, seq=seq):
                    n += 1
                    ours, theirs = self.out(f"ours{n}"), self.out(f"theirs{n}")
                    for args in seq:
                        self.pysacd(ours, iso, args)
                        subprocess.run([SACD_EXTRACT, *args, f"-i{iso}"], cwd=theirs, capture_output=True, check=False)
                    a, b = self.tree(ours), self.tree(theirs)
                    self.assertEqual(sorted(a), sorted(b))
                    for k in a:
                        # apart from the documented version lines and DSDIFF creating-machine comment
                        self.assertTrue(a[k] == b[k] or documented_only(ours / k, theirs / k), k)


# Two editions of one album with the same titles and numbering, one with ISRCs and one without
# (#171): every mode pair of the other-disc test above, and pysacd's -p pair
EDITION_PAIRS = [
    (["-s"], ["-e"]), (["-s"], ["-C"]), (["-e"], ["-s"]), (["-e"], ["-e"]), (["-p"], ["-e"]), (["-e"], ["-p"]),
    (["-s"], ["-s"]), (["-p"], ["-p"]), (["-s", "-t", "1"], ["-s", "-t", "2"]), (["-s"], ["-s", "-k", "-t", "1,2"]),
    (["-s", "-k", "-t", "1,2"], ["-s"]), (["--id3tag", "1", "-s"], ["-s"]), (["--id3tag", "3", "-p"], ["-e"]),
]
# one disc's runs with other ID3 modes: the minimal modes (2, 5) and 0 write no ISRC
OWN_ID3_SEQUENCES = [
    (["--id3tag", "5", "-s"], ["-s"]), (["-s"], ["--id3tag", "5", "-s"]), (["--id3tag", "2", "-e"], ["-s"]),
    (["--id3tag", "0", "-s"], ["-e"]), (["--id3tag", "0", "-p"], ["-p"]), (["--id3tag", "1", "-s"], ["-e"]),
    (["--id3tag", "3", "-e"], ["-p"]),
]


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class IsrcEditions(unittest.TestCase):
    """An ISRC on one side only tells two discs apart where the file always carries the track's
    ISRC (#171); this disc's own files, with or without ISRCs, are still this disc's (#165)."""

    pysacd = OwnFilesAcrossModes.pysacd
    out = OwnFilesAcrossModes.out
    tree = staticmethod(OwnFilesAcrossModes.tree)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.env = dict(os.environ, PYTHONPATH=str(SRC))
        titles = ["One", "Two"]
        self.a = _plain_disc(self.dir / "a.iso", titles, "CAT-A")
        self.n = _plain_disc(self.dir / "n.iso", titles, "CAT-N", isrcs=["", ""])
        self.p = _plain_disc(self.dir / "p.iso", titles, "CAT-P", isrcs=["XXPYS2600001", ""])
        self.s = _plain_disc(self.dir / "s.iso", titles, "CAT-S", isrcs=[" " * 12, " " * 12])

    def assert_kept_apart(self, one: Path, other: Path, first: list[str], second: list[str], name: str) -> None:
        out = self.out(name)
        self.pysacd(out, one, first)
        before = self.tree(out)
        r = self.pysacd(out, other, second)
        self.assertIn("already holds tracks of another disc", r.stderr)
        after = self.tree(out)
        self.assertEqual({k: after[k] for k in before}, before)      # the first disc's files untouched
        label = "CAT-" + other.stem.upper()
        self.assertEqual({k.split("/")[0] for k in after if k not in before}, {f"Album [{label}]"})

    def assert_own(self, iso: Path, seq, name: str) -> None:
        out = self.out(name)
        for args in seq:
            r = self.pysacd(out, iso, args)
            # files rewritten in another ID3 mode change size, which is said
            self.assertNotIn("already holds tracks of another disc", r.stderr)
        self.assertEqual({k.split("/")[0] for k in self.tree(out)}, {"Album"})

    def test_editions_with_and_without_isrcs_are_kept_apart(self):
        ma = _plain_disc(self.dir / "ma.iso", ["One", "Two"], "CAT-MA", mch=True)
        mn = _plain_disc(self.dir / "mn.iso", ["One", "Two"], "CAT-MN", mch=True, isrcs=["", ""])
        cases = [(one, other, first, second) for one, other in ((self.a, self.n), (self.n, self.a), (self.s, self.a))
                 for first, second in EDITION_PAIRS]
        # ISRCs differing on track 2 only: the runs that leave track 2's file in the folder
        cases += [(self.a, self.p, first, second) for first, second in EDITION_PAIRS
                  if first[-1] in ("-s", "-p")]
        cases += [(one, other, ["-2", "-m", "-s"], ["-2", "-m", "-e"]) for one, other in ((ma, mn), (mn, ma))]
        for n, (one, other, first, second) in enumerate(cases):
            with self.subTest(first=(one.stem, first), second=(other.stem, second)):
                self.assert_kept_apart(one, other, first, second, f"ed{n}")

    @unittest.skipUnless(dst._native_has_flac(), "engine built without libFLAC")
    def test_flac_editions(self):
        # pysacd's own tags carry an ISRC only when the track has one
        n = 0
        for one, other in ((self.a, self.n), (self.n, self.a)):
            for first, second in ((["--flac"], ["--flac"]), (["--flac"], ["-s"]), (["-s"], ["--flac"])):
                with self.subTest(first=(one.stem, first), second=(other.stem, second)):
                    n += 1
                    self.assert_kept_apart(one, other, first, second, f"flac{n}")
        for iso in (self.a, self.n, self.p):
            for seq in ((["--flac"], ["-s"]), (["-s"], ["--wav"]), (["--flac"], ["-e"])):
                with self.subTest(disc=iso.stem, seq=seq):
                    n += 1
                    self.assert_own(iso, seq, f"flacown{n}")

    def test_own_files_with_and_without_isrcs(self):
        mn = _plain_disc(self.dir / "mn.iso", ["One", "Two"], "CAT-MN", mch=True, isrcs=["", ""])
        runs = [(iso, seq) for iso in (self.n, self.p, self.s) for seq in OWN_SEQUENCES]
        runs += [(mn, seq) for seq in OWN_MCH_SEQUENCES]
        runs += [(iso, seq) for iso in (self.a, self.n, self.p) for seq in OWN_ID3_SEQUENCES]
        for n, (iso, seq) in enumerate(runs):
            with self.subTest(disc=iso.stem, seq=seq):
                self.assert_own(iso, seq, f"own{n}")

    def test_minimal_tags_carry_no_isrc(self):
        # id3tag=5 writes no ISRC: the editions can't be told apart, as documented
        out = self.out("minimal")
        self.pysacd(out, self.a, ["--id3tag", "5", "-s"])
        r = self.pysacd(out, self.n, ["-s"])
        self.assertNotIn("already holds tracks of another disc", r.stderr)
        self.assertEqual({k.split("/")[0] for k in self.tree(out)}, {"Album"})

    def test_same_tags(self):
        from pysacd.extract import _same_tags
        disc = {"ALBUM": ["Album"], "TITLE": ["One"], "TRACKNUMBER": ["1"], "TRACKTOTAL": ["2"]}
        full = {**disc, "DISCNUMBER": ["1"]}            # sacd_extract's ID3 modes 1, 3, 4; pysacd's tags
        isrc = {"ISRC": ["XXPYS2600001"]}
        mb = {"MUSICBRAINZ_ALBUMID": ["x"]}
        for have, want, same in (
            ({**full, **isrc}, {**disc, **isrc}, True),
            ({**full, "ISRC": [" XXPYS2600001 "]}, {**disc, **isrc}, True),
            ({**full, "ISRC": ["XXPYS2600002"]}, {**disc, **isrc}, False),
            ({**full, "ISRC": [""]}, {**disc, **isrc}, False),        # TSRC written empty
            (full, {**disc, **isrc}, False),                          # pysacd's tags, no ISRC
            ({**disc, "ISRC": [""]}, {**disc, **isrc}, False),
            (disc, {**disc, **isrc}, True),                           # minimal modes: no ISRC, no TPOS
            ({}, {**disc, **isrc}, True),                             # untagged
            ({**full, **isrc}, disc, False),
            ({**full, "ISRC": ["  "]}, disc, True),
            ({**full, **isrc, **mb}, disc, True),                     # the ISRC may be MusicBrainz's
            ({**full, **mb}, {**disc, **isrc}, False),
            (full, disc, True),
            ({**full, "TITLE": ["Uno"]}, disc, False),
            ({k: v for k, v in full.items() if k != "TITLE"}, disc, True),
        ):
            with self.subTest(have=have, want=want):
                self.assertEqual(_same_tags(have, want), same)

    @unittest.skipUnless(SACD_EXTRACT, "set SACD_EXTRACT to compare with the real sacd_extract")
    def test_sacd_extract_files(self):
        # sacd_extract's files of this disc, in every ID3 mode, are this disc's; those of the other
        # edition keep it out and stay as they were
        def theirs(cwd: Path, iso: Path, args: list[str], mode: int | None) -> None:
            if mode is not None:
                (cwd / "sacd_extract.cfg").write_text(f"id3tag={mode}\n")
            subprocess.run([SACD_EXTRACT, *args, f"-i{iso}"], cwd=cwd, capture_output=True, check=False)
            (cwd / "sacd_extract.cfg").unlink(missing_ok=True)

        n = 0
        for iso in (self.a, self.n, self.p, self.s):
            for mode in (None, 0, 1, 2, 3, 5):
                for first in (["-s"], ["-p"], ["-e"], ["-s", "-k", "-t", "1,2"]):
                    with self.subTest(disc=iso.stem, id3tag=mode, first=first):
                        n += 1
                        out = self.out(f"sx{n}")
                        theirs(out, iso, first, mode)
                        self.assertTrue(self.tree(out))
                        r = self.pysacd(out, iso, ["-e"] if first != ["-e"] else ["-s"])
                        self.assertNotIn("WARNING", r.stderr)
                        self.assertEqual({k.split("/")[0] for k in self.tree(out)}, {"Album"})
        for one, other in ((self.a, self.n), (self.n, self.a), (self.s, self.a)):
            for first, second in ((["-s"], ["-s"]), (["-p"], ["-e"]), (["-e"], ["-e"]), (["-s", "-k", "-t", "1,2"], ["-s"])):
                with self.subTest(first=(one.stem, first), second=(other.stem, second)):
                    n += 1
                    out = self.out(f"sx{n}")
                    theirs(out, one, first, None)
                    before = self.tree(out)
                    self.assertTrue(before)
                    r = self.pysacd(out, other, second)
                    self.assertIn("already holds tracks of another disc", r.stderr)
                    after = self.tree(out)
                    self.assertEqual({k: after[k] for k in before}, before)
                    self.assertEqual({k.split("/")[0] for k in after if k not in before}, {f"Album [CAT-{other.stem.upper()}]"})


if __name__ == "__main__":
    unittest.main()
