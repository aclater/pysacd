#!/usr/bin/env python3
"""Check that the version claims in README.md and docs/ match pyproject.toml and scarletbook.lock.

    python3 -I tools/check_doc_versions.py      # Python 3.11 or later (tomllib)

A short list of known sentences, not a scan for every number: each pattern below is matched
in README.md and every docs/**/*.md, and each match must say what the source of truth says.
Patterns marked required must match somewhere, so a reworded sentence fails here instead of
going unchecked; reword the pattern with it. Exits 1 with one line per mismatch.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent


def truth() -> dict[str, str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    m = re.fullmatch(r">=\s*(3\.\d+)", project["requires-python"])
    if not m:
        sys.exit(f"pyproject.toml: requires-python {project['requires-python']!r} isn't '>=3.N'")
    minors = sorted(int(c.rsplit(".", 1)[1]) for c in project["classifiers"]
                     if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c))
    lock = json.loads((ROOT / "scarletbook.lock").read_text(encoding="utf-8"))
    return {"version": project["version"], "python_min": m.group(1),
            "classifier_min": f"3.{minors[0]}", "python_max": f"3.{minors[-1]}",
            "scarletbook": lock["version"]}


# (what, pattern, expected value from truth(), required): a pattern's groups, joined with " to ",
# are compared with the expected value.
CHECKS = [
    ("minimum Python", r"Python (3\.\d+) or later", "{python_min}", True),
    ("minimum Python (badge)", r"python-(3\.\d+)%2B", "{python_min}", True),
    ("minimum Python (badge text)", r"Python (3\.\d+)\+", "{python_min}", True),
    ("tested Python range", r"Python (3\.\d+) to (3\.\d+)", "{python_min} to {python_max}", True),
    ("scarletbook pin", r"The pin is (\d+\.\d+\.\d+)", "{scarletbook}", True),
    ("pinned pysacd release", r"pysacd==(\d+\.\d+\.\d+)", "{version}", False),
]


def docs() -> list[Path]:
    return [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]


def main() -> int:
    t = truth()
    bad = []
    if t["classifier_min"] != t["python_min"]:
        bad.append(f"pyproject.toml: requires-python is >={t['python_min']} but the oldest Python "
                   f"classifier is {t['classifier_min']}")
    texts = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in docs()}
    for what, pattern, expected, required in CHECKS:
        want = expected.format(**t)
        seen = 0
        for name, text in texts.items():
            for m in re.finditer(pattern, text):
                seen += 1
                got = " to ".join(m.groups())
                if got != want:
                    line = text.count("\n", 0, m.start()) + 1
                    bad.append(f"{name}:{line}: {what} says {got}; pyproject.toml/scarletbook.lock say {want}")
        if required and not seen:
            bad.append(f"{what}: no sentence matches {pattern!r} any more; update "
                       f"tools/check_doc_versions.py with the new wording")
    for b in bad:
        print(f"::error::{b}")
    if not bad:
        print(f"doc versions match: pysacd {t['version']}, Python {t['python_min']} to {t['python_max']}, "
              f"scarletbook {t['scarletbook']}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
