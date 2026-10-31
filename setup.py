"""Build the native engine as part of building the package (#9).

The engine is a plain shared library loaded through ctypes, not a CPython
extension module, so one wheel per platform serves every Python version
(tag py3-none-<platform>). It is built with `make PORTABLE=1`: generic
x86-64/aarch64 code, with the PCM filters' SIMD picked at load time.
Set PYSACD_NATIVE=1 to build for this machine's CPU instead (-march=native).

The scarletbook-rs C library is linked into it statically (#85): tools/scarletbook.py
downloads the release pinned in scarletbook.lock for the compiler's target and checks its
SHA-256 first. SCARLETBOOK_DIR=DIR uses a library unpacked there instead, and
PYSACD_SCARLETBOOK_CACHE=DIR keeps the downloaded archives for the next build.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py

try:
    from setuptools.command.bdist_wheel import bdist_wheel
except ImportError:     # setuptools < 70
    from wheel.bdist_wheel import bdist_wheel

ROOT = Path(__file__).resolve().parent
LIB = {"win32": "_sacdconv.dll", "darwin": "_sacdconv.dylib"}.get(sys.platform, "_sacdconv.so")


class BuildEngine(build_py):
    def run(self):
        super().run()
        native = os.environ.get("PYSACD_NATIVE") == "1"
        dest = Path(self.build_lib).resolve() / "pysacd"
        dest.mkdir(parents=True, exist_ok=True)
        # A private build directory: never touch the developer's build/ or src/pysacd/ library
        with tempfile.TemporaryDirectory() as tmp:
            sb_dir = os.environ.get("SCARLETBOOK_DIR")
            if not sb_dir:
                # Fetched here rather than by make: on Windows, make runs under MSYS, which may
                # have no Python of its own
                sb_dir = (Path(tmp) / "scarletbook").as_posix()
                # (the tool asks $CC, else the platform's compiler, for the target to fetch)
                fetched = subprocess.run([sys.executable, "-I", (ROOT / "tools" / "scarletbook.py").as_posix(),
                                          "fetch", "--dest", sb_dir], check=False)
                if fetched.returncode:
                    sys.exit("pysacd: fetching the scarletbook library failed (see above). Offline, set "
                             "SCARLETBOOK_DIR to an unpacked copy; see docs/development.md.")
            try:
                # forward slashes: MSYS make on Windows mangles backslashes
                subprocess.run(["make", "-C", ROOT.as_posix(), f"BUILD={Path(tmp).as_posix()}",
                                f"LIB={(dest / LIB).as_posix()}", f"SCARLETBOOK_DIR={sb_dir}",
                                *([] if native else ["PORTABLE=1"])], check=True)
            except FileNotFoundError:
                sys.exit("pysacd: building from source needs `make` and a C compiler (gcc or clang; MinGW-w64 "
                         "on Windows). Install them, or use a prebuilt wheel. See docs/quickstart.md.")
            except subprocess.CalledProcessError as e:
                sys.exit(f"pysacd: building the native engine failed (make exited {e.returncode}). It needs a "
                         "C compiler, and libFLAC (flac-devel) for FLAC output. See docs/quickstart.md.")


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


setup(cmdclass={"build_py": BuildEngine, "bdist_wheel": PlatformWheel}, distclass=BinaryDistribution)
