"""The environment for a test's child build (make, cc): without the sanitizer runtime.

The ASan and TSan CI jobs preload the runtime into the Python that runs the tests (Python itself
isn't instrumented). Inherited by `make`, that LD_PRELOAD makes it segfault, and the sanitizer
options only matter to the runtime. A build never needs either; CFLAGS and PYSACD_FUZZ_CFLAGS stay.
"""

import os

SANITIZER_VARS = ("LD_PRELOAD", "ASAN_OPTIONS", "UBSAN_OPTIONS", "TSAN_OPTIONS", "LSAN_OPTIONS", "MSAN_OPTIONS")


def build_env(environ=None) -> dict[str, str]:
    return {k: v for k, v in (os.environ if environ is None else environ).items() if k not in SANITIZER_VARS}
