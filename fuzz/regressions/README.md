Inputs that once crashed or tripped a sanitizer in a fuzz harness, one
directory per harness. `tests/test_fuzz_engine.py` replays every file here on
each test run. Add a minimised input (`build/fuzz_<harness> -minimize_crash=1
-runs=10000 <crash-file>`) together with its fix.
