"""pysacd-extract input sources (ISO, PS3 server) and diagnostic logging.

Uses a synthetic DST ISO (tests/sacdgen.py) and the PS3 simulator, so no real
disc or PS3 is needed.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sacdgen
import test_dst_full as dst

from pysacd.remote import PS3Simulator

SRC = Path(__file__).resolve().parents[1] / "src"


def run_extract(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONPATH=str(SRC), **(env or {}))
    e.pop("PYSACD_LOG", None) if env is None or "PYSACD_LOG" not in env else None
    return subprocess.run([sys.executable, "-m", "pysacd.extract", *args], cwd=cwd, env=e,
                          capture_output=True, text=True, check=False)


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and not p.name.endswith((".log", ".txt", ".cfg"))}


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class ExtractInputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        frames = [f for _, _, f in dst.encoded(2)]
        n = len(frames)
        cls.iso = cls.dir / "disc.iso"
        sacdgen.build_iso(cls.iso, [sacdgen.AreaSpec(2, frames, [n // 2, n - n // 2])])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def workdir(self, name: str) -> Path:
        d = self.dir / name
        d.mkdir()
        return d

    def test_network_output_matches_local(self):
        local = self.workdir("local")
        r = run_extract(["-s", "-i", str(self.iso)], local)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        net = self.workdir("net")
        with PS3Simulator(self.iso, "127.0.0.1", 0) as sim:
            host, port = sim.address
            r = run_extract(["-s", "-i", f"{host}:{port}", "--log-file", "x.log"], net)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        want, got = tree(local), tree(net)
        self.assertTrue(want)
        self.assertEqual(sorted(got), sorted(want))
        self.assertEqual(got, want)
        log = (net / "x.log").read_text()
        self.assertIn("PS3 SACD Ripper server", log)
        self.assertIn("DISC_READ", log)

    def test_unwritable_output_fails_cleanly(self):
        # An output that can't be created (read-only directory, full disk) used to crash the
        # engine: out_close finished it through a NULL FILE*.
        if os.name != "posix":
            self.skipTest("POSIX directory permissions")
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        for args in (["-s"], ["-s", "-z"], ["-p"], ["-e"]):
            with self.subTest(args=args):
                d = self.workdir("ro-" + "".join(args).replace("-", ""))
                album = d / "pysacd DST test"
                album.mkdir()
                album.chmod(0o555)
                try:
                    r = run_extract([*args, "-i", str(self.iso)], d)
                finally:
                    album.chmod(0o755)
                self.assertEqual(r.returncode, 255, r.stdout + r.stderr)
                self.assertIn("ERROR", r.stdout + r.stderr)

    def test_network_iso_copy(self):
        out = self.workdir("netiso")
        with PS3Simulator(self.iso, "127.0.0.1", 0) as sim:
            host, port = sim.address
            r = run_extract(["-I", "-i", f"{host}:{port}"], out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        isos = list(out.rglob("*.iso"))
        self.assertEqual(len(isos), 1)
        self.assertEqual(isos[0].read_bytes(), self.iso.read_bytes())

    def test_dropped_connection_is_logged(self):
        out = self.workdir("drop")
        with PS3Simulator(self.iso, "127.0.0.1", 0, drop_after=3) as sim:
            host, port = sim.address
            r = run_extract(["-s", "-i", f"{host}:{port}", "--log-file", "fail.log"], out)
        self.assertEqual(r.returncode, 255)
        log = (out / "fail.log").read_text()
        self.assertIn("ERROR", log)
        self.assertRegex(log, r"DISC_READ.*(failed|timed out)|cannot read")
        self.assertIn("Traceback", log)                   # the full exception is recorded
        self.assertFalse(list(out.rglob("*.dsf")))        # no partial outputs left behind

    def test_log_file_contents(self):
        out = self.workdir("log")
        r = run_extract(["-s", "-t", "1", "-i", str(self.iso), "--log-file", "run.log"], out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = (out / "run.log").read_text()
        for needle in ("pysacd ", "command line:", "engine: ", "input: file", " sectors of 2048 bytes", "disc: ",
                       "engine: finished", "wrote "):
            self.assertIn(needle, log)

    def test_cfg_logging_writes_sacd_extract_logfile(self):
        out = self.workdir("cfg")
        (out / "sacd_extract.cfg").write_text("logging=1\n")
        r = run_extract(["-s", "-t", "1", "-i", str(self.iso)], out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("\tLogging (logging = 1) yes", r.stdout)
        self.assertIn("engine: finished", (out / "logfile-sacd_extract.txt").read_text())

    def test_env_log(self):
        out = self.workdir("env")
        r = run_extract(["-P", "-i", str(self.iso)], out, env={"PYSACD_LOG": "info:env.log"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("command line:", (out / "env.log").read_text())

    def test_rejects_directory_input(self):
        out = self.workdir("dir")
        r = run_extract(["-s", "-i", str(self.dir)], out)
        self.assertEqual(r.returncode, 255)
        self.assertIn("only SACD ISO images, block devices", r.stderr)


@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class SectorSource(unittest.TestCase):
    """The engine reading through a Python sector source (#11): a network disc
    streams straight into the pipeline, and a misbehaving source fails cleanly."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        frames = [f for _, _, f in dst.encoded(2)]
        cls.iso = cls.dir / "disc.iso"
        sacdgen.build_iso(cls.iso, [sacdgen.AreaSpec(2, frames, [len(frames)])])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_source(self, source, name: str):
        from pysacd import SACDImage, _native
        with SACDImage(self.iso) as img:
            area = img.read_toc().stereo
            t = area.tracks[0]
            spec = _native.OutputSpec("dsf", self.dir / name, t.start.total_frames,
                                      t.start.total_frames + t.duration.total_frames)
            _native.run_area(img, "source", area, [spec], source=source)
        return (self.dir / name).read_bytes()

    def local(self, lsn: int, count: int) -> bytes:
        with open(self.iso, "rb") as f:
            f.seek(lsn * 2048)
            return f.read(count * 2048)

    def test_streamed_from_simulator_matches_file(self):
        from pysacd.remote import PS3Simulator, RemoteDisc
        want = self.run_source(self.local, "file.dsf")
        with PS3Simulator(self.iso, "127.0.0.1", 0) as sim:
            disc = RemoteDisc(*sim.address)
            try:
                got = self.run_source(disc.read_sectors, "net.dsf")
            finally:
                disc.close()
        self.assertEqual(got, want)

    def test_misbehaving_sources_fail_cleanly(self):
        from pysacd import _native
        cases = {
            "raises": lambda lsn, n: (_ for _ in ()).throw(OSError("connection reset")),
            "short": lambda lsn, n: self.local(lsn, n)[:-1],
            "long": lambda lsn, n: self.local(lsn, n) + b"x",
            "not bytes": lambda lsn, n: None,
        }
        for name, source in cases.items():
            with self.subTest(name), self.assertRaises(_native.NativeError):
                self.run_source(source, f"{name}.dsf")
            self.assertFalse((self.dir / f".{name}.dsf.partial").exists())


    def test_area_outside_image_is_refused_before_reading(self):
        from pysacd import SACDImage, _native
        asked = []
        with SACDImage(self.iso) as img:
            area = img.read_toc().stereo
            area.track_end = img.total_sectors + 100      # as a hostile TOC could claim
            t = area.tracks[-1]
            spec = _native.OutputSpec("dsf", self.dir / "out.dsf", t.start.total_frames,
                                      t.start.total_frames + t.duration.total_frames)
            with self.assertRaisesRegex(_native.NativeError, "outside the image"):
                _native.run_area(img, "source", area, [spec],
                                 source=lambda lsn, n: asked.append(lsn) or self.local(lsn, n))
        self.assertEqual(asked, [])


class LogSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        import logging

        from pysacd import log as pslog
        pslog.setup(None, None)     # drop the file handler before the directory goes
        logging.getLogger(pslog.LOGGER).handlers.clear()
        self.tmp.cleanup()

    def test_hostile_text_is_escaped(self):
        import logging

        from pysacd import log as pslog
        path = self.dir / "x.log"
        pslog.setup("debug", path, ["test"])
        logging.getLogger("pysacd.test").warning("title %s", "evil\x1b]52;c;AAAA\x07\nFAKE ERROR line\u202e")
        text = path.read_text()
        self.assertIn("evil\\x1b]52;c;AAAA\\x07\\x0aFAKE ERROR line\\u202e", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\nFAKE", text)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "no O_NOFOLLOW on this platform")
    def test_symlinked_log_path_refused(self):
        from pysacd import log as pslog
        target = self.dir / "target"
        target.write_text("keep")
        link = self.dir / "link.log"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            pslog.setup("debug", link, ["test"])
        self.assertEqual(target.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
