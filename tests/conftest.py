"""With PYSACD_TEST_REPORT=path, pytest writes which tests ran and which skipped, and why, for
tools/check_skips.py (#131)."""

import json
import os
from pathlib import Path

_tests: set[str] = set()
_skips: dict[str, str] = {}


def _name(nodeid: str) -> str:
    """tests/test_x.py::Class::test -> tests.test_x.Class.test"""
    path, _, rest = nodeid.partition("::")
    return f"{path.removesuffix('.py').replace('/', '.')}.{rest.replace('::', '.')}"


def pytest_runtest_logreport(report):
    if report.when == "call" or report.skipped:
        _tests.add(_name(report.nodeid))
    if report.skipped:
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        _skips[_name(report.nodeid)] = reason.removeprefix("Skipped: ")


def pytest_sessionfinish(session):
    path = os.environ.get("PYSACD_TEST_REPORT")
    if path:
        Path(path).write_text(json.dumps({"tests": len(_tests), "skips": _skips}, indent=1), encoding="utf-8")
