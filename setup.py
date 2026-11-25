"""Build the native engine as part of building the package (#9).

The engine is a plain shared library loaded through ctypes, not a CPython
extension module, so one wheel per platform serves every Python version
(tag py3-none-<platform>). It is built with `make PORTABLE=1`: generic
x86-64/aarch64 code (the PCM conversion picks its SIMD level at run time).
Set PYSACD_NATIVE=1 to build for this machine's CPU instead (-march=native).

The scarletbook-rs C library is linked into it statically (#85): tools/scarletbook.py
downloads the release pinned in scarletbook.lock for the compiler's target and checks its
SHA-256 first. SCARLETBOOK_DIR=DIR uses a library unpacked there instead, and
PYSACD_SCARLETBOOK_CACHE=DIR keeps the downloaded archives for the next build.

The dsd2pcm C library (PCM conversion) is linked in the same way: tools/dsd2pcm.py builds it
from the source pinned in dsd2pcm.lock (that needs Rust's cargo). The sdist carries that source,
checked against its SHA-256 when the sdist is made (vendor/dsd2pcm/), so a build from the sdist
downloads no dsd2pcm source; with the C library source archive pinned (dsd2pcm 0.2.0 on) cargo
builds it offline, with no crates.io index. DSD2PCM_DIR=DIR uses a built library there instead, and
PYSACD_DSD2PCM_CACHE=DIR keeps the downloads.

On Windows, the download also trusts certifi's root certificates (#222): Python sees only the
roots already in the Windows certificate store, and a fresh Windows adds a missing root only
when its own TLS client needs it, so the first download from GitHub could fail.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

try:
    from setuptools.command.bdist_wheel import bdist_wheel
except ImportError:     # setuptools < 70
    from wheel.bdist_wheel import bdist_wheel

ROOT = Path(__file__).resolve().parent
# tools/scarletbook.py's make_path(): paths for make, with forward slashes (#225)
_spec = importlib.util.spec_from_file_location("_pysacd_sbtool", ROOT / "tools" / "scarletbook.py")
_sbtool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sbtool)
make_path = _sbtool.make_path
LIB = {"win32": "_sacdconv.dll", "darwin": "_sacdconv.dylib"}.get(sys.platform, "_sacdconv.so")


def compiler_first_env():
    """The environment for make: on Windows, with the C compiler's directory first on PATH.

    Windows finds a program's DLLs on PATH, and the compiler's own programs (cc1.exe, as.exe) live
    outside its bin directory, so they load the first libwinpthread-1.dll on PATH. Rust's
    windows-gnu toolchain carries an older one in its bin directory, which lacks functions MSYS2's
    gcc needs (clock_gettime64): with Rust's bin directory first, as CI's PATH has it, cc1.exe can't
    start and gcc fails without a message."""
    if sys.platform != "win32":
        return None
    cc = os.environ.get("CC") or "cc"     # make's default; a path, or a command with flags
    cc = shutil.which(cc if Path(cc).is_file() else cc.split()[0])
    if not cc:
        return None
    return dict(os.environ, PATH=os.pathsep.join([str(Path(cc).parent), os.environ.get("PATH", "")]))


def fetch_env() -> dict | None:
    """The environment for the scarletbook fetch: on Windows, SSL_CERT_FILE names certifi's
    roots, unless the user has set it (#222). Python's default context still loads the Windows
    store first, so roots an organisation installs there keep working. None: inherit."""
    if sys.platform != "win32" or os.environ.get("SSL_CERT_FILE"):
        return None
    try:
        import certifi  # a build requirement on Windows (pyproject.toml)
    except ImportError:     # --no-build-isolation without it: as before
        return None
    return dict(os.environ, SSL_CERT_FILE=certifi.where())


class BuildEngine(build_py):
    def run(self):
        super().run()
        native = os.environ.get("PYSACD_NATIVE") == "1"
        dest = Path(self.build_lib).resolve() / "pysacd"
        dest.mkdir(parents=True, exist_ok=True)
        # A private build directory: never touch the developer's build/ or src/pysacd/ library
        with tempfile.TemporaryDirectory() as tmp:
            sb_dir = os.environ.get("SCARLETBOOK_DIR")
            if sb_dir:
                sb_dir = make_path(sb_dir)
            else:
                # Fetched here rather than by make: on Windows, make runs under MSYS, which may
                # have no Python of its own
                sb_dir = (Path(tmp) / "scarletbook").as_posix()
                # (the tool asks $CC, else the platform's compiler, for the target to fetch)
                fetched = subprocess.run([sys.executable, "-I", (ROOT / "tools" / "scarletbook.py").as_posix(),
                                          "fetch", "--dest", sb_dir], check=False, env=fetch_env())
                if fetched.returncode:
                    sys.exit("pysacd: fetching the scarletbook library failed (see above). Offline, set "
                             "SCARLETBOOK_DIR to an unpacked copy; see docs/development.md.")
            d2p_dir = os.environ.get("DSD2PCM_DIR")
            if d2p_dir:
                d2p_dir = make_path(d2p_dir)
            else:
                d2p_dir = (Path(tmp) / "dsd2pcm").as_posix()
                fetched = subprocess.run([sys.executable, "-I", (ROOT / "tools" / "dsd2pcm.py").as_posix(),
                                          "fetch", "--dest", d2p_dir], check=False, env=fetch_env())
                if fetched.returncode:
                    sys.exit("pysacd: fetching or building the dsd2pcm library failed (see above). Offline, set "
                             "DSD2PCM_DIR to a built copy; see docs/development.md.")
            try:
                # forward slashes: MSYS make on Windows mangles backslashes
                subprocess.run(["make", "-C", make_path(ROOT), f"BUILD={make_path(tmp)}",
                                f"LIB={make_path(dest / LIB)}", f"SCARLETBOOK_DIR={sb_dir}", f"DSD2PCM_DIR={d2p_dir}",
                                f"PYTHON={make_path(sys.executable)}",
                                *([] if native else ["PORTABLE=1"])], check=True, env=compiler_first_env())
            except FileNotFoundError:
                sys.exit("pysacd: building from source needs `make` and a C compiler (gcc or clang; MinGW-w64 "
                         "on Windows). Install them, or use a prebuilt wheel. See docs/install/README.md.")
            except subprocess.CalledProcessError as e:
                sys.exit(f"pysacd: building the native engine failed (make exited {e.returncode}). It needs a "
                         "C compiler, the scarletbook library, and libFLAC (flac-devel) for FLAC output. See "
                         "the error above and docs/install/README.md.")


class SdistWithDsd2pcm(sdist):
    """The sdist with dsd2pcm's pinned source archive and licence files in vendor/dsd2pcm/."""

    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        dest = Path(base_dir) / "vendor" / "dsd2pcm"
        done = subprocess.run([sys.executable, "-I", (ROOT / "tools" / "dsd2pcm.py").as_posix(), "vendor",
                               "--dest", dest.as_posix()], check=False)
        if done.returncode:
            sys.exit("pysacd: vendoring the dsd2pcm source into the sdist failed (see above)")


class PlatformWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self):
        _, _, plat = super().get_tag()
        return "py3", "none", plat


class BinaryDistribution(Distribution):
    def has_ext_modules(self):
        return True


setup(cmdclass={"build_py": BuildEngine, "bdist_wheel": PlatformWheel, "sdist": SdistWithDsd2pcm},
      distclass=BinaryDistribution)
