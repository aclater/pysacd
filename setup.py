"""Build the native engine as part of building the package (#9).

The engine is a plain shared library loaded through ctypes, not a CPython
extension module, so one wheel per platform serves every Python version
(tag py3-none-<platform>). It is built with `make PORTABLE=1`: generic
x86-64/aarch64 code, with the PCM filters' SIMD picked at load time.
Set PYSACD_NATIVE=1 to build for this machine's CPU instead (-march=native).
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
        make = os.environ.get("MAKE", "make")
        dest = Path(self.build_lib).resolve() / "pysacd"
        dest.mkdir(parents=True, exist_ok=True)
        # A private build directory: never touch the developer's build/ or src/pysacd/ library
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([make, "-C", str(ROOT), f"BUILD={tmp}", f"LIB={dest / LIB}",
                            *([] if native else ["PORTABLE=1"])], check=True)


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
