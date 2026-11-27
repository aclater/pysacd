"""tools/lock_dev.py's PINS must equal what the lock files pin, or running it downgrades them."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "tools" / "lock_dev.py"
pytestmark = pytest.mark.skipif(not LOCK.exists(), reason="not a source checkout")

SPEC = re.compile(r"^([A-Za-z0-9_.-]+)==(\S+?)(?: ; (.*?))?(?: \\)?$", re.MULTILINE)


def _pins():
    spec = importlib.util.spec_from_file_location("lock_dev", LOCK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PINS


def _locked(name):
    return {(n, v, m or "") for n, v, m in SPEC.findall((ROOT / name).read_text())}


@pytest.mark.parametrize(
    "name,groups", [("requirements-test.txt", {"test"}), ("requirements-dev.txt", {"test", "dev"})]
)
def test_pins_match_lock(name, groups):
    want = {(n, v, m) for n, v, m, g in _pins() if g in groups}
    assert want == _locked(name)


def test_pins_match_pyproject_dev_extra():
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["dev"]
    exact = dict(d.split("==") for d in dev if "==" in d)
    pins = {n: v for n, v, m, g in _pins() if not m}
    assert {n: pins.get(n) for n in exact} == exact
