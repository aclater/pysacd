"""tests/buildenv.py: the child build gets no sanitizer runtime and keeps the rest."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from buildenv import build_env


class BuildEnv(unittest.TestCase):
    def test_sanitizer_variables_are_dropped_and_the_rest_kept(self):
        env = build_env({"LD_PRELOAD": "libtsan.so", "TSAN_OPTIONS": "x", "ASAN_OPTIONS": "y", "UBSAN_OPTIONS": "z",
                         "PATH": "/bin", "PYSACD_FUZZ_CFLAGS": "-fsanitize=thread"})
        self.assertEqual(env, {"PATH": "/bin", "PYSACD_FUZZ_CFLAGS": "-fsanitize=thread"})

    def test_defaults_to_the_process_environment(self):
        self.assertEqual(build_env().get("PATH"), os.environ.get("PATH"))


if __name__ == "__main__":
    unittest.main()
