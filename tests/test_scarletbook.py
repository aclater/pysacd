"""The scarletbook-rs C library linked into the engine (#85): fetching and checking it
(tools/scarletbook.py), and what the built engine says about it."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import scarletbook as sbtool

from pysacd import _native


def _tarball(path: Path, top: str, extra: list[tarfile.TarInfo] | None = None) -> str:
    """A small archive shaped like a release asset; returns its SHA-256."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data in ((f"{top}/include/scarletbook.h", b"#define SB_ABI_VERSION 2\n"),
                           (f"{top}/lib/libscarletbook_ffi.a", b"!<arch>\n"),
                           (f"{top}/native-static-libs.txt", b"-lm\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isfile() else None)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Lock(unittest.TestCase):
    def test_pin_is_complete(self):
        lock = sbtool.load_lock()
        self.assertEqual(lock["abi"], _native.SB_ABI)
        self.assertGreaterEqual(_native._version_tuple(lock["version"]), _native.SB_MIN_VERSION)
        self.assertRegex(lock["commit"], r"^[0-9a-f]{40}$")
        # every platform pysacd ships a wheel for (docs/development.md, workflow.yml)
        for target in ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-unknown-linux-musl",
                       "aarch64-apple-darwin", "x86_64-apple-darwin", "x86_64-pc-windows-gnu"):
            self.assertRegex(lock["assets"].get(target, ""), r"^[0-9a-f]{64}$", target)

    def test_header_abi_matches_engine_source(self):
        src = (ROOT / "csrc" / "sacdconv.c").read_text()
        self.assertIn(f"SB_ABI_VERSION != {_native.SB_ABI}", src)


class Target(unittest.TestCase):
    def test_compiler_triples(self):
        for machine, target in [
            ("x86_64-redhat-linux", "x86_64-unknown-linux-gnu"),
            ("x86_64-linux-gnu", "x86_64-unknown-linux-gnu"),
            ("aarch64-redhat-linux", "aarch64-unknown-linux-gnu"),
            ("x86_64-alpine-linux-musl", "x86_64-unknown-linux-musl"),
            ("aarch64-alpine-linux-musl", "aarch64-unknown-linux-musl"),
            ("arm64-apple-darwin24.6.0", "aarch64-apple-darwin"),
            ("x86_64-apple-darwin23.0.0", "x86_64-apple-darwin"),
            ("x86_64-w64-mingw32\n", "x86_64-pc-windows-gnu"),
        ]:
            self.assertEqual(sbtool.target_of(machine), target, machine)

    def test_unknown_system(self):
        with self.assertRaises(sbtool.FetchError):
            sbtool.target_of("x86_64-unknown-freebsd14.1")

    def test_target_without_a_build(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(sbtool.FetchError, "no build for"):
            sbtool.obtain(sbtool.load_lock(), "riscv64gc-unknown-linux-gnu", None, Path(tmp))


class Fetch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.lock = {"repository": "example/sb", "version": "9.9.9", "abi": 2, "commit": "0" * 40, "assets": {}}
        self.top = "scarletbook-ffi-9.9.9-x86_64-unknown-linux-gnu"
        self.archive = self.tmp / f"{self.top}.tar.gz"

    def _fetch(self, digest: str, archive: Path | None = None):
        self.lock["assets"]["x86_64-unknown-linux-gnu"] = digest
        lock_file = self.tmp / "scarletbook.lock"
        lock_file.write_text(json.dumps(self.lock))
        with mock.patch.object(sbtool, "LOCK", lock_file), mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("PYSACD_SCARLETBOOK_CACHE", None)
            return sbtool.fetch(self.tmp / "out", "x86_64-unknown-linux-gnu", archive or self.archive)

    def test_pinned_archive_unpacks(self):
        digest = _tarball(self.archive, self.top)
        out = self._fetch(digest)
        self.assertTrue((out / "include" / "scarletbook.h").is_file())
        self.assertTrue((out / "lib" / "libscarletbook_ffi.a").is_file())
        self.assertIn(digest, (out / sbtool.STAMP).read_text())
        # a second fetch with the same pin keeps what is there, without reading the archive
        self.archive.unlink()
        self.assertEqual(self._fetch(digest), out)

    def test_wrong_digest_is_refused(self):
        _tarball(self.archive, self.top)
        with self.assertRaisesRegex(sbtool.FetchError, "refusing it"):
            self._fetch("0" * 64)
        self.assertFalse((self.tmp / "out").exists())

    def test_member_outside_the_package_is_refused(self):
        evil = tarfile.TarInfo("../evil")
        evil.size = 1
        with self.assertRaisesRegex(sbtool.FetchError, "unexpected member"):
            self._fetch(_tarball(self.archive, self.top, [evil]))
        self.assertFalse((self.tmp / "evil").exists())

    def _link(self, name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> tarfile.TarInfo:
        link = tarfile.TarInfo(f"{self.top}/{name}")
        link.type, link.linkname = kind, target
        return link

    def test_link_member_is_refused(self):
        for target in ("/etc/passwd", "../include/scarletbook.h", "lib/libscarletbook_ffi.a", "missing.so.2",
                       "..", ".", ""):
            with self.subTest(target=target), self.assertRaisesRegex(sbtool.FetchError, "not to a file beside it"):
                self._fetch(_tarball(self.archive, self.top, [self._link("lib/link", target)]))
            self.assertFalse((self.tmp / "out").exists())
        # a link to a directory, and a hard link
        with self.assertRaisesRegex(sbtool.FetchError, "not to a file beside it"):
            self._fetch(_tarball(self.archive, self.top, [self._link("link", "lib")]))
        with self.assertRaisesRegex(sbtool.FetchError, "not a regular file"):
            self._fetch(_tarball(self.archive, self.top,
                                 [self._link("lib/link", f"{self.top}/lib/libscarletbook_ffi.a", tarfile.LNKTYPE)]))

    def test_link_to_a_file_beside_it_is_left_out(self):
        """scarletbook 0.6.0 and later: lib/libscarletbook_ffi.so -> libscarletbook_ffi.so.2 (#188)."""
        real = tarfile.TarInfo(f"{self.top}/lib/libscarletbook_ffi.so.2")
        real.size = 4
        link = self._link("lib/libscarletbook_ffi.so", "libscarletbook_ffi.so.2")
        out = self._fetch(_tarball(self.archive, self.top, [link, real]))   # the link comes first
        self.assertTrue((out / "lib" / "libscarletbook_ffi.so.2").is_file())
        self.assertTrue((out / "lib" / "libscarletbook_ffi.a").is_file())
        self.assertFalse(os.path.lexists(out / "lib" / "libscarletbook_ffi.so"))
        self.assertFalse(any(p.is_symlink() for p in out.rglob("*")))

    def test_plain_http_is_refused(self):
        with self.assertRaisesRegex(sbtool.FetchError, "non-HTTPS"):
            sbtool.download("http://example.invalid/x.tar.gz", self.tmp / "x")



class StaleEngine(unittest.TestCase):
    """An engine built before sacdconv_cancel existed (ABI 5) is refused with a message that says
    to rebuild it, not with an AttributeError for the first symbol it lacks (found 2026-11-06,
    when a stale src/pysacd/_sacdconv.so stopped a test run)."""

    def test_old_engine_is_refused_with_a_rebuild_message(self):
        import ctypes
        from unittest import mock

        class OldLib:
            def __init__(self, path):
                self.sacdconv_area_run = mock.Mock()
                self.sacdconv_has_flac = mock.Mock()
                self.sacdconv_build_info = mock.Mock()
                self.sacdconv_abi = lambda: 5      # and no sacdconv_cancel

        saved = _native._lib
        _native._lib = None
        try:
            with (
                mock.patch.object(ctypes, "CDLL", OldLib),
                mock.patch("pathlib.Path.exists", return_value=True),
                self.assertRaisesRegex(_native.NativeError, "engine ABI 5.*rebuild it"),
            ):
                _native._load()
        finally:
            _native._lib = saved


class Engine(unittest.TestCase):
    def setUp(self):
        try:
            _native._load()
        except (OSError, _native.NativeError) as e:
            self.skipTest(f"engine not built: {e}")

    def test_linked_release(self):
        version = _native.scarletbook_version()
        self.assertGreaterEqual(_native._version_tuple(version), _native.SB_MIN_VERSION)
        info = _native.scarletbook_build_info()
        self.assertIn(f"version={version}", info)
        self.assertIn(f"scarletbook {version}", _native.build_info())

    def test_c_abi_is_callable_through_the_engine(self):
        # sb_* functions are exported from the engine for ctypes (phase 2 of #85 reads the TOC
        # through them): an empty image must fail cleanly with an error message, not crash
        import ctypes
        lib = _native._load()
        lib.sb_disc_open_memory.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
        lib.sb_disc_open_memory.restype = ctypes.c_int32
        lib.sb_last_error_message.restype = ctypes.c_char_p
        disc = ctypes.c_void_p()
        status = lib.sb_disc_open_memory(b"\0" * 2048, 2048, ctypes.byref(disc))
        self.assertLess(status, 0)
        self.assertIsNone(disc.value)
        self.assertTrue(lib.sb_last_error_message())

    @unittest.skipUnless(sys.platform.startswith("linux") and __import__("shutil").which("nm"), "Linux, with nm")
    def test_exports_only_the_engine_and_the_c_abi(self):
        # csrc/exports.map: Rust's runtime stays inside the engine (nothing for another library
        # in the process to collide with), and every sb_* function of the header is there
        import subprocess
        out = subprocess.run(["nm", "-D", "--defined-only", _native.library_path()], capture_output=True,
                             text=True, check=True).stdout
        names = {line.split()[-1] for line in out.splitlines() if line.strip()}
        names = {n.split("@", 1)[0] for n in names}
        self.assertEqual({n for n in names if not n.startswith(("sb_", "sacdconv_"))}, set())
        self.assertIn("sacdconv_area_run", names)
        self.assertGreater(len([n for n in names if n.startswith("sb_")]), 90)

    def test_version_tuple(self):
        self.assertEqual(_native._version_tuple("0.5.0"), (0, 5, 0))
        self.assertEqual(_native._version_tuple("0.10.1-rc.1+abc"), (0, 10, 1))
        self.assertEqual(_native._version_tuple("garbage"), ())
        self.assertLess(_native._version_tuple("0.5.0"), _native.SB_MIN_VERSION)

    def test_old_or_foreign_library_is_refused(self):
        class Fn:
            def __init__(self, value):
                self.value = value

            def __call__(self):
                return self.value

        class Lib:
            def __init__(self, abi, version):
                self.sb_abi_version, self.sb_version, self.sb_build_info = Fn(abi), Fn(version), Fn(b"x")

        with self.assertRaisesRegex(_native.NativeError, "C ABI 1"):
            _native._check_scarletbook(Lib(1, b"0.3.0"), "lib")
        with self.assertRaisesRegex(_native.NativeError, re.escape("0.5.3, this pysacd needs 0.5.4")):
            _native._check_scarletbook(Lib(2, b"0.5.3"), "lib")
        _native._check_scarletbook(Lib(2, b"0.5.4"), "lib")


class MakePath(unittest.TestCase):
    """setup.py passes make paths with forward slashes: under MSYS's sh, a backslash
    SCARLETBOOK_DIR lost its backslashes and the engine was built without scarletbook (#225)."""

    def test_backslashes_become_slashes_on_windows(self):
        self.assertEqual(sbtool.make_path(r"C:\lib\scarletbook", windows=True), "C:/lib/scarletbook")
        self.assertEqual(sbtool.make_path(r"C:\lib/mixed\sb", windows=True), "C:/lib/mixed/sb")
        self.assertEqual(sbtool.make_path("C:/lib/scarletbook", windows=True), "C:/lib/scarletbook")
        self.assertEqual(sbtool.make_path(r"\\server\share\sb", windows=True), "//server/share/sb")
        with mock.patch.object(sbtool.sys, "platform", "win32"):
            self.assertEqual(sbtool.make_path(r"D:\a\b"), "D:/a/b")

    def test_backslash_is_kept_elsewhere(self):
        # an ordinary character in a POSIX file name
        self.assertEqual(sbtool.make_path("/tmp/a\\b", windows=False), "/tmp/a\\b")

    def test_setup_py_gives_make_forward_slashes(self):
        import runpy
        with mock.patch("setuptools.setup"):
            setup_py = runpy.run_path(str(ROOT / "setup.py"))
        engine = setup_py["BuildEngine"]
        cmd = object.__new__(engine)
        cmd.build_lib = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(cmd.build_lib, ignore_errors=True))
        with mock.patch.object(engine.__mro__[1], "run"), mock.patch.object(subprocess, "run") as run, \
                mock.patch.dict(os.environ, {"SCARLETBOOK_DIR": r"C:\lib\scarletbook"}), \
                mock.patch.object(sys, "platform", "win32"):
            engine.run(cmd)
        make = run.call_args.args[0]
        self.assertEqual(make[0], "make")
        self.assertIn("SCARLETBOOK_DIR=C:/lib/scarletbook", make)
        self.assertFalse([a for a in make if "\\" in a], make)



class WheelSmoke(unittest.TestCase):
    """tests/wheel_smoke.py fails a wheel whose engine has no scarletbook library (#225)."""

    def test_engine_without_scarletbook_fails_the_smoke_test(self):
        import importlib.util
        from contextlib import redirect_stderr, redirect_stdout
        spec = importlib.util.spec_from_file_location("wheel_smoke", ROOT / "tests" / "wheel_smoke.py")
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        missing = _native.NativeError("_sacdconv.dll has no scarletbook library linked in; rebuild it")
        for patch in (mock.patch.object(_native, "build_info", side_effect=missing),
                      mock.patch.multiple(_native, build_info=mock.Mock(return_value="gcc 15, flac"),
                                          scarletbook_version=mock.Mock(return_value="1.0.2"))):
            err = io.StringIO()
            with self.subTest(patch=patch), patch, redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertEqual(smoke.main(), 1)
            self.assertIn("scarletbook", err.getvalue())

@unittest.skipUnless(sys.platform != "win32" and shutil.which("make"), "make, outside Windows")
class BuildRefusesNoScarletbook(unittest.TestCase):
    """The build stops, with a message, when the engine would lack scarletbook's functions: the
    export list is checked before linking and the engine's export table after it (#225)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        text = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.required = re.search(r"^SB_REQUIRED := (.+)$", text, re.MULTILINE).group(1).split()

    def make(self, *args: str):
        return subprocess.run(["make", "-s", "-C", str(ROOT), *args], capture_output=True, text=True, check=False)

    def library(self, folder: str, header: str) -> Path:
        sb = self.tmp / folder
        (sb / "include").mkdir(parents=True)
        (sb / "include" / "scarletbook.h").write_text(header, encoding="utf-8")
        return sb

    def exports_def(self, sb: Path):
        out = self.tmp / "build"
        return self.make(f"BUILD={out}", f"SCARLETBOOK_DIR={sb}", f"{out}/exports.def"), out / "exports.def"

    def test_header_in_a_folder_with_a_backslash(self):
        # what MSYS's sh did to C:\...\scarletbook: before #225 sed couldn't read the header, the
        # export list had no sb_ function, and make still exited 0
        header = "".join(f"const char *{name}(void);\n" for name in self.required)
        done, exports = self.exports_def(self.library("lib\\scarletbook", header))
        self.assertEqual(done.returncode, 0, done.stderr)
        listed = exports.read_text(encoding="utf-8").split()
        for name in self.required:
            self.assertIn(name, listed)

    def test_export_list_without_sb_functions_is_refused(self):
        for header in ("", "/* not scarletbook's header */\nint other(void);\n",
                       "const char *sb_version(void);\n"):    # some, not all
            with self.subTest(header=header):
                shutil.rmtree(self.tmp / "sb", ignore_errors=True)
                done, exports = self.exports_def(self.library("sb", header))
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("without the scarletbook library", done.stderr)
                self.assertFalse(exports.exists())

    def test_unreadable_header_is_refused(self):
        # sed fails to read it (a directory, so the test holds as root too): the build stops
        sb = self.tmp / "sb"
        (sb / "include" / "scarletbook.h").mkdir(parents=True)
        done, exports = self.exports_def(sb)
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(exports.exists())

    def test_platform_export_list(self):
        self.assertEqual(self.make("check-export-list", f"SB_EXPORT_LIST={ROOT / 'csrc' / 'exports.map'}"
                                   if sys.platform != "darwin" else
                                   f"SB_EXPORT_LIST={ROOT / 'csrc' / 'exports.darwin'}").returncode, 0)
        empty = self.tmp / "empty.map"
        empty.write_text("{\n  global:\n    sacdconv_*;\n  local:\n    *;\n};\n", encoding="utf-8")
        done = self.make("check-export-list", f"SB_EXPORT_LIST={empty}")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("would be built without them", done.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("nm"), "Linux, with nm")
    def test_engine_without_sb_functions_is_refused(self):
        # a real shared library that isn't the engine: one of Python's own extension modules
        import _ctypes
        foreign = self.tmp / "_sacdconv.so"
        shutil.copyfile(_ctypes.__file__, foreign)
        done = self.make("check-engine", f"LIB={foreign}")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("does not export sb_", done.stderr)
        self.assertFalse(foreign.exists(), "the incomplete engine is removed")
        junk = self.tmp / "junk.so"
        junk.write_bytes(b"not a library")
        self.assertNotEqual(self.make("check-engine", f"LIB={junk}").returncode, 0)

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("nm"), "Linux, with nm")
    def test_built_engine_passes(self):
        try:
            _native._load()
        except (OSError, _native.NativeError) as e:
            self.skipTest(f"engine not built: {e}")
        engine = self.tmp / "_sacdconv.so"      # a copy: a failed check removes the file it checks
        shutil.copyfile(_native.library_path(), engine)
        done = self.make("check-engine", f"LIB={engine}")
        self.assertEqual(done.returncode, 0, done.stderr)

if __name__ == "__main__":
    unittest.main()
