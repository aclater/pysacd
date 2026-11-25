"""tools/dsd2pcm.py, the build's fetcher of the dsd2pcm C library: what it refuses.

- obtain(): a file whose SHA-256 isn't the pinned one, wherever it comes from; the sdist's vendored
  copy is taken before the cache and the network; only HTTPS URLs are fetched.
- unpack_source(): members outside the archive's top folder, absolute paths, "..", hard links,
  devices and FIFOs, and cargo configuration; symbolic links are left out, not followed.
- repo_tree() (the command-line tool's source for tools/pcm_goldens.py): refused unless its
  library sources equal the pinned archive's.
- cargo runs with a CARGO_HOME of its own and without the caller's rustc wrappers or flags.
Standard library only; nothing is downloaded.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    spec = importlib.util.spec_from_file_location("_test_dsd2pcm_tool", ROOT / "tools" / "dsd2pcm.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


t = load_tool()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_tar(path: Path, members: list[tuple[str, str, bytes]]) -> Path:
    """members: (name, kind, data); kind file, dir, sym (data = target), link (hard; data = target), fifo."""
    with tarfile.open(path, "w:gz") as tar:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE, "link": tarfile.LNKTYPE,
                         "fifo": tarfile.FIFOTYPE, "chr": tarfile.CHRTYPE}[kind]
            if kind in ("sym", "link"):
                info.linkname = data.decode()
            tar.addfile(info)
    return path


class Obtain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("PYSACD_DSD2PCM_CACHE", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_a_wrong_digest_is_refused_from_every_source(self):
        f = self.d / "a.tar.gz"
        f.write_bytes(b"not the pinned bytes")
        with self.assertRaises(t.FetchError) as cm:
            t.obtain("a.tar.gz", "https://example.invalid/a", "0" * 64, f, self.d)
        self.assertIn("refusing", str(cm.exception))
        cache = self.d / "cache"
        cache.mkdir()
        (cache / "a.tar.gz").write_bytes(b"also wrong")
        os.environ["PYSACD_DSD2PCM_CACHE"] = str(cache)
        with mock.patch.object(t, "VENDORED", self.d / "none"), mock.patch.object(t, "download") as dl, \
                self.assertRaises(t.FetchError):
            t.obtain("a.tar.gz", "https://example.invalid/a", "0" * 64, None, self.d)
        dl.assert_not_called()

    def test_the_vendored_copy_comes_first(self):
        vend, cache = self.d / "vendor", self.d / "cache"
        vend.mkdir()
        cache.mkdir()
        (vend / "a.tar.gz").write_bytes(b"vendored")
        (cache / "a.tar.gz").write_bytes(b"cached")
        os.environ["PYSACD_DSD2PCM_CACHE"] = str(cache)
        with mock.patch.object(t, "VENDORED", vend), mock.patch.object(t, "download") as dl:
            got = t.obtain("a.tar.gz", "https://example.invalid/a", sha(b"vendored"), None, self.d)
        self.assertEqual(got, vend / "a.tar.gz")
        dl.assert_not_called()
        # and the cached copy, with another digest, is never taken instead
        with mock.patch.object(t, "VENDORED", vend), self.assertRaises(t.FetchError):
            t.obtain("a.tar.gz", "https://example.invalid/a", sha(b"cached"), None, self.d)

    def test_only_https_is_fetched(self):
        with mock.patch("urllib.request.urlopen") as op:
            for url in ("http://github.com/x.tar.gz", "file:///etc/passwd", "ftp://x/y", "HTTPS-ish://x"):
                with self.subTest(url=url), self.assertRaises(t.FetchError) as cm, \
                        mock.patch.object(t, "VENDORED", self.d / "none"):
                    t.obtain("b.tar.gz", url, "0" * 64, None, self.d)
                self.assertIn("non-HTTPS", str(cm.exception))
            op.assert_not_called()


class UnpackSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def unpack(self, members):
        tar = make_tar(self.d / "s.tar.gz", [("top", "dir", b""), ("top/Cargo.toml", "file", b"x"), *members])
        dest = self.d / "out" / "src"
        dest.parent.mkdir(exist_ok=True)
        t.unpack_source(tar, "top", dest)
        return dest

    def test_bad_members_are_refused(self):
        for bad in [("top/../evil", "file", b"x"), ("/abs/evil", "file", b"x"), ("other/x", "file", b"x"),
                    ("top/a/../../evil", "file", b"x"), ("top/hard", "link", b"top/Cargo.toml"),
                    ("top/fifo", "fifo", b""), ("top/dev", "chr", b""), ("top/.cargo/config.toml", "file", b"x"),
                    ("top/crates/x/.cargo/config.toml", "file", b"x")]:
            with self.subTest(member=bad[0], kind=bad[1]):
                with self.assertRaises(t.FetchError):
                    self.unpack([bad])
                self.assertFalse((self.d / "evil").exists())
                for p in self.d.rglob("*"):
                    if p.is_dir() and p.name == "out":
                        import shutil
                        shutil.rmtree(p)

    def test_symbolic_links_are_left_out(self):
        dest = self.unpack([("top/LICENSE", "sym", b"../../../../etc/passwd"), ("top/ok", "file", b"y")])
        self.assertEqual((dest / "ok").read_bytes(), b"y")
        self.assertFalse(os.path.lexists(dest / "LICENSE"))


class RepoTree(unittest.TestCase):
    """The repository archive the CLI is built from must carry the pinned library sources."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cache = self.d / "cache"
        self.cache.mkdir()
        self.env = mock.patch.dict(os.environ, {"PYSACD_DSD2PCM_CACHE": str(self.cache)})
        self.env.start()
        self.lock = {"repository": "o/dsd2pcm-rs", "version": "0.2.0", "min_version": "0.2.0", "commit": "c" * 40,
                     "source": {"ref": "c" * 40, "archive": "capi-src", "sha256": ""}}
        ws = b'[workspace]\nmembers = []\n\n[workspace.package]\nversion = "0.2.0"\n'
        lib = [("crates/dsd2pcm/src/lib.rs", b"pub fn f() {}\n"), ("crates/dsd2pcm-capi/src/ffi.rs", b"// ffi\n")]
        capi = make_tar(self.cache / "libdsd2pcm-0.2.0-src.tar.gz",
                        [("libdsd2pcm-0.2.0/Cargo.toml", "file", ws),
                         ("libdsd2pcm-0.2.0/include/dsd2pcm.h", "file", b"#define H 1\n"),
                         ("libdsd2pcm-0.2.0/crates/dsd2pcm/Cargo.toml", "file", b"rewritten"),
                         *[(f"libdsd2pcm-0.2.0/{n}", "file", b) for n, b in lib]])
        self.lock["source"]["sha256"] = sha(capi.read_bytes())
        self.repo_members = [("Cargo.toml", ws), ("crates/dsd2pcm-capi/include/dsd2pcm.h", b"#define H 1\n"),
                             ("crates/dsd2pcm/Cargo.toml", b"original"), ("crates/dsd2pcm-cli/src/main.rs", b"fn main(){}"),
                             *lib]

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def repo(self, change: dict[str, bytes] | None = None):
        top = f"dsd2pcm-rs-{'c' * 40}"
        files = dict(self.repo_members, **(change or {}))
        make_tar(self.cache / f"{top}.tar.gz", [(f"{top}/{n}", "file", b) for n, b in files.items()])

    def test_matching_sources_are_taken(self):
        self.repo()
        with mock.patch.object(t, "download") as dl:
            repo = t.repo_tree(self.lock, self.d / "w1")
        dl.assert_not_called()
        self.assertTrue((repo / "crates" / "dsd2pcm-cli" / "src" / "main.rs").is_file())

    def test_a_changed_library_source_is_refused(self):
        for name in ("crates/dsd2pcm/src/lib.rs", "crates/dsd2pcm-capi/src/ffi.rs",
                     "crates/dsd2pcm-capi/include/dsd2pcm.h"):
            with self.subTest(name=name):
                self.repo({name: b"// changed\n"})
                with self.assertRaises(t.FetchError) as cm:
                    t.repo_tree(self.lock, self.d / f"w-{name.replace('/', '_')}")
                self.assertIn("differs from the pinned", str(cm.exception))

    def test_another_version_is_refused(self):
        self.repo({"Cargo.toml": b'[workspace]\nmembers = []\n\n[workspace.package]\nversion = "0.2.1"\n'})
        with self.assertRaises(t.FetchError):
            t.repo_tree(self.lock, self.d / "w2")


class CargoIsolation(unittest.TestCase):
    def test_cargo_home_and_flags(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "RUSTC_WRAPPER": "evil", "RUSTFLAGS": "-C x", "CARGO_BUILD_RUSTC_WRAPPER": "evil",
                "CARGO_SOURCE_CRATES_IO_REPLACE_WITH": "m", "CARGO_HOME": str(Path.home() / ".cargo")}):
            os.environ.pop("PYSACD_DSD2PCM_CACHE", None)
            env = t.cargo_env("x86_64-unknown-linux-gnu", Path(tmp) / "src", Path(tmp))
            self.assertEqual(env["CARGO_HOME"], str(Path(tmp) / "cargo-home"))
            for k in ("RUSTC_WRAPPER", "RUSTFLAGS", "CARGO_BUILD_RUSTC_WRAPPER", "CARGO_SOURCE_CRATES_IO_REPLACE_WITH"):
                self.assertNotIn(k, env)
            self.assertIn("target-cpu=x86-64", env["CARGO_ENCODED_RUSTFLAGS"])


if __name__ == "__main__":
    unittest.main()
