#!/usr/bin/env python3
"""Fail when a CI job's skipped tests aren't exactly the ones expected (#131).

    python3 -I tools/check_skips.py --job NAME JUNIT.xml

The engine check of tests/test_dst_full.py once looked for a function the engine stopped
exporting, and 44 tests, the comparison with sacd_extract among them, skipped for days in a green
job. So every skip a CI job may have is listed in tests/expected-skips.json, with its job, its
reason as pytest reports it, and why it's legitimate there. This fails on a new skip, on a skip
that has stopped (remove it from the list), on a changed reason, and on a report with no tests.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

EXPECTED = Path(__file__).resolve().parents[1] / "tests" / "expected-skips.json"


def read_report(path: str | Path) -> tuple[int, dict[str, str]]:
    """(number of tests, {test: skip reason}) of a JUnit XML report."""
    tests, skips = 0, {}
    for case in ET.parse(path).getroot().iter("testcase"):
        tests += 1
        skipped = case.find("skipped")
        if skipped is not None:
            skips[f"{case.get('classname')}.{case.get('name')}"] = skipped.get("message") or ""
    return tests, skips


def expected_for(job: str, expected: list[dict]) -> dict[str, str]:
    """{test: reason} of the entries that apply to a job."""
    return {t: e["reason"] for e in expected if job in e["jobs"] for t in e["tests"]}


def problems(tests: int, skips: dict[str, str], want: dict[str, str]) -> list[str]:
    out = []
    if not tests:
        out.append("the report holds no tests")
    out += [f"unexpected skip: {t}: {skips[t]}" for t in sorted(skips.keys() - want.keys())]
    out += [f"no longer skips (remove it from {EXPECTED.name}): {t}" for t in sorted(want.keys() - skips.keys())]
    out += [f"skip reason changed: {t}: expected {want[t]!r}, got {skips[t]!r}"
            for t in sorted(skips.keys() & want.keys()) if skips[t] != want[t]]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--job", required=True)
    ap.add_argument("report")
    args = ap.parse_args(argv)
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    if not any(args.job in e["jobs"] for e in expected):
        print(f"unknown job {args.job!r}: not in {EXPECTED.name}", file=sys.stderr)
        return 2
    tests, skips = read_report(args.report)
    bad = problems(tests, skips, expected_for(args.job, expected))
    for line in bad:
        print(f"::error::{line}")
    print(f"{args.job}: {tests} tests, {len(skips)} skipped, {len(bad)} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
