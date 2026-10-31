Inputs that once crashed or tripped a sanitizer in the fuzz harness
(`fuzz/fuzz_engine.c`), in `fuzz_engine/`. `tests/test_fuzz_engine.py` replays every
file here on each test run. Add a minimised input (`build/fuzz_engine -minimize_crash=1
-runs=10000 <crash-file>`) together with its fix.

The regressions of pysacd's former demuxer and DST decoder harnesses (up to 0.2) are in
the repository's history; those parsers are now scarletbook-rs's, fuzzed in its own
repository (#85).
