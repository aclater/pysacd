"""The scarletbook-rs C library linked into the engine (#85): fetching and checking it
(tools/scarletbook.py), and what the built engine says about it."""

from __future__ import annotations

import hashlib
import io
import json
import re
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

    def test_link_member_is_refused(self):
        link = tarfile.TarInfo(f"{self.top}/lib/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        with self.assertRaisesRegex(sbtool.FetchError, "not a regular file"):
            self._fetch(_tarball(self.archive, self.top, [link]))

    def test_plain_http_is_refused(self):
        with self.assertRaisesRegex(sbtool.FetchError, "non-HTTPS"):
            sbtool.download("http://example.invalid/x.tar.gz", self.tmp / "x")


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
        self.assertLess(_native._version_tuple("0.4.1"), _native.SB_MIN_VERSION)

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
        with self.assertRaisesRegex(_native.NativeError, re.escape("0.4.1, this pysacd needs 0.5.0")):
            _native._check_scarletbook(Lib(2, b"0.4.1"), "lib")
        _native._check_scarletbook(Lib(2, b"0.5.0"), "lib")


if __name__ == "__main__":
    unittest.main()
