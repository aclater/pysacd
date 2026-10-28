"""The opt-in isolated engine (#17): same output as in-process, and the sandbox really holds.

Uses a synthetic DST ISO (tests/sacdgen.py). The sandbox probes need Linux; the Landlock
checks are skipped on kernels without Landlock (the child reports what it applied).
"""

import json
import os
import pickle
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import sacdgen
import test_dst_full as dst
from test_extract_input import SRC, run_extract, tree

from pysacd import _native, isolate
from pysacd.toc import SACDImage


def run_convert(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONPATH=str(SRC))
    e.pop("PYSACD_LOG", None)
    return subprocess.run([sys.executable, "-m", "pysacd", "convert", "--no-musicbrainz", "--no-cover", *args],
                          cwd=cwd, env=e, capture_output=True, text=True, check=False)


@unittest.skipUnless(sys.platform == "linux", "the sandbox is Linux-only")
@unittest.skipUnless(dst.HAVE_NATIVE, "native helper not built (make)")
@unittest.skipUnless(dst.HAVE_ENCODER, "test DST encoder unavailable (make test-tools)")
class Isolated(unittest.TestCase):
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

    # --- same bytes as in-process -----------------------------------------------------------

    def test_extract_matches_in_process(self):
        for args in (["-s", "-I"], ["-e"], ["-p"], ["-s", "-z"]):
            with self.subTest(args=args):
                name = "-".join(a.strip("-") for a in args)
                a, b = self.workdir(f"in-{name}"), self.workdir(f"iso-{name}")
                r = run_extract([*args, "-i", str(self.iso)], a)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                r = run_extract([*args, "--isolate", "-i", str(self.iso), "--log-file", "x.log"], b)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                want, got = tree(a), tree(b)
                self.assertTrue(want)
                self.assertEqual(got, want)
                self.assertIn("isolated engine: landlock ABI", (b / "x.log").read_text())

    def test_convert_matches_in_process(self):
        fmts = ["dsf", "wav"] + (["flac"] if _native.has_flac() else [])
        for fmt in fmts:
            with self.subTest(fmt=fmt):
                a, b = self.workdir(f"cin-{fmt}"), self.workdir(f"ciso-{fmt}")
                r = run_convert(["-f", fmt, "-o", str(a), str(self.iso)], self.dir)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                r = run_convert(["-f", fmt, "--isolate", "-o", str(b), str(self.iso)], self.dir)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(tree(b), tree(a))
                self.assertFalse(list(b.rglob("*.partial")))

    def test_sector_source_matches_file(self):
        img = SACDImage(self.iso)
        disc = img.read_toc()
        area = disc.areas[0]
        out = self.workdir("src")

        def specs(tag):
            return [_native.OutputSpec("dsf", out / f"{tag}{t.number}.dsf", t.start.total_frames,
                                       t.start.total_frames + t.duration.total_frames) for t in area.tracks]

        with open(self.iso, "rb") as f:
            def source(lsn, count):
                f.seek(lsn * 2048)
                return f.read(count * 2048)
            _native.run_area(img, self.iso, area, specs("a"))
            _native.run_area(img, self.iso, area, specs("b"), source=source, isolate=True)
        img.close()
        for t in area.tracks:
            self.assertEqual((out / f"b{t.number}.dsf").read_bytes(), (out / f"a{t.number}.dsf").read_bytes())

    # --- cancel -----------------------------------------------------------------------------

    def test_cancel_kills_the_child(self):
        img = SACDImage(self.iso)
        area = img.read_toc().areas[0]
        out = self.workdir("cancel")
        specs = [_native.OutputSpec("dsf", out / "t.dsf", 0, area.tracks[-1].start.total_frames
                                    + area.tracks[-1].duration.total_frames)]
        children = []
        real_spawn = isolate.spawn

        def spawn():
            c = real_spawn()
            children.append(c)
            return c

        def slow_source(lsn, count):        # keeps the engine busy long enough to cancel it
            time.sleep(0.05)
            with open(self.iso, "rb") as f:
                f.seek(lsn * 2048)
                return f.read(count * 2048)

        def progress(p):
            raise KeyboardInterrupt

        isolate.spawn = spawn
        try:
            with self.assertRaises(KeyboardInterrupt):
                _native.run_area(img, self.iso, area, specs, progress=progress, interval=0.01,
                                 source=slow_source, isolate=True)
        finally:
            isolate.spawn = real_spawn
            img.close()
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll(), "the child is still running")

    # --- the sandbox itself -----------------------------------------------------------------

    def probe(self) -> tuple[dict, dict]:
        d = self.workdir(f"probe{time.monotonic_ns()}")
        outside = self.dir / f"outside{time.monotonic_ns()}.txt"
        outside.write_text("secret")
        job = {"probe": True, "read_only": [str(self.iso)], "writable": [str(d)], "fsize": 1 << 20,
               "nofile": 64, "probe_outside": str(outside), "log_level": 30}
        child = isolate.spawn()
        out, _ = child.communicate(pickle.dumps(job), timeout=60)
        msgs = {m["t"]: m for m in map(json.loads, out.splitlines())}
        return msgs["sandbox"], msgs["probe"]["results"]

    def test_sandbox_blocks(self):
        sandbox, r = self.probe()
        self.assertTrue(sandbox["nnp"])
        self.assertTrue(sandbox["seccomp"])
        self.assertEqual(r["exec"], "EPERM")
        self.assertEqual(r["socket"], "EPERM")
        self.assertEqual(r["unix_socket"], "EPERM")
        self.assertEqual(r["oversize"], "EFBIG")            # RLIMIT_FSIZE
        self.assertEqual(r["read_inside"], "ok")
        self.assertEqual(r["write_inside"], "ok")
        self.assertEqual(r["thread"], "ok")                 # the engine still gets its threads
        if sandbox["landlock"] > 0:
            self.assertEqual(r["read_outside"], "EACCES")
            self.assertEqual(r["write_outside"], "EACCES")
        else:
            self.skipTest("no Landlock on this kernel")

    def test_child_ignores_python_environment(self):
        # A PYTHONPATH pointing at a hostile "pysacd" must not be imported by the child (-E).
        evil = self.workdir("evil")
        (evil / "pysacd").mkdir()
        (evil / "pysacd" / "__init__.py").write_text("raise SystemExit('hijacked')\n")
        old = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(evil)
        try:
            sandbox, _ = self.probe()
        finally:
            if old is None:
                del os.environ["PYTHONPATH"]
            else:
                os.environ["PYTHONPATH"] = old
        self.assertTrue(sandbox["seccomp"])


class Messages(unittest.TestCase):
    """The parent's parser for what the (untrusted) child sends."""

    def parse(self, line: bytes, specs=()):
        from pysacd._native import Progress
        return isolate._message(line, None, None, None, list(specs), Progress)  # type: ignore[arg-type]

    def test_malformed(self):
        spec = _native.OutputSpec("dsf", "x", 0, 10)
        ok = {"frames_written": 1, "frames_missing": 0, "dst_errors": 0, "samples": 0, "data_bytes": 0,
              "dst_index": []}
        for line in (b"not json", b"[1]", b'{"t": "bogus"}', b'{"t": "read", "lsn": 0, "count": 1}',
                     b'{"t": "log", "level": 99, "msg": "x"}',
                     json.dumps({"t": "done", "error": "", "outputs": []}).encode(),
                     json.dumps({"t": "done", "error": "", "outputs": [dict(ok, frames_written=-1)]}).encode(),
                     json.dumps({"t": "done", "error": "", "outputs": [dict(ok, frames_written=True)]}).encode(),
                     json.dumps({"t": "done", "error": 5, "outputs": [ok]}).encode(),
                     json.dumps({"t": "done", "error": "", "outputs": [dict(ok, dst_index=[[0, 1]] * 11)]}).encode()):
            with self.subTest(line=line[:60]), self.assertRaises(_native.NativeError):
                self.parse(line, [spec])
        self.assertEqual(self.parse(json.dumps({"t": "done", "error": "", "outputs": [ok]}).encode(), [spec]), "")
        self.assertEqual(spec.frames_written, 1)

    def test_default_path_does_not_import_isolate(self):
        # isolate=False must cost nothing: the module isn't even imported.
        code = ("import sys; from pysacd import _native; "
                "assert 'pysacd.isolate' not in sys.modules, 'imported'")
        r = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, PYTHONPATH=str(SRC)),
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
