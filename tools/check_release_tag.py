#!/usr/bin/env python3
"""Fail a release whose tag doesn't match the package, or isn't on a release branch (#154).

    python3 -I tools/check_release_tag.py --ref REF --sha COMMIT --default-branch NAME

The release workflow runs this before anything is built. For a `v*` tag it checks that

- the tag is `v` plus the version in pyproject.toml, and `__version__` says the same;
- the tagged commit is on the default branch or a `release/*` branch of `origin`, so a tag
  pushed onto an unmerged or unreviewed commit publishes nothing.

For any other ref (a pull request, a manual run) only the two version strings are compared.
`v*` tags can't be moved or deleted afterwards (a repository ruleset), so a wrong tag has to
fail here. It needs a full clone with origin's branches (`fetch-depth: 0`).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = "src/pysacd/__init__.py"  # where __version__ lives; None when the package has none
TAG = "refs/tags/"


def project_version(root: Path) -> str:
    """`version` of pyproject.toml's [project] table."""
    table = ""
    for line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            table = line.strip()
        elif table == "[project]" and (m := re.fullmatch(r'version\s*=\s*"([^"]+)"\s*(#.*)?', line.strip())):
            return m.group(1)
    raise SystemExit("pyproject.toml: no version in [project]")


def init_version(root: Path, init: str) -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', (root / init).read_text(encoding="utf-8"), re.MULTILINE)
    if not m:
        raise SystemExit(f"{init}: no __version__")
    return m.group(1)


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)


def release_branches(root: Path, default: str) -> list[str]:
    """origin's default branch and its release/* branches, as remote-tracking refs that exist."""
    out = git(root, "for-each-ref", "--format=%(refname)",
              f"refs/remotes/origin/{default}", "refs/remotes/origin/release/")
    return out.stdout.split()


def problems(root: Path, ref: str, sha: str, default: str, init: str | None = INIT) -> list[str]:
    out = []
    version = project_version(root)
    if init and (v := init_version(root, init)) != version:
        out.append(f"pyproject.toml says {version}, {init} says {v}")
    if not ref.startswith(TAG + "v"):
        return out
    tag = ref[len(TAG):]
    if tag != "v" + version:
        out.append(f"the tag is {tag}, the package version is {version} (expected v{version})")
    branches = release_branches(root, default)
    if not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", sha):  # it goes to git: never an option or a name
        out.append(f"{sha!r} isn't a commit hash")
    elif f"refs/remotes/origin/{default}" not in branches:
        out.append(f"origin/{default} isn't here: the check needs a full clone (fetch-depth: 0)")
    elif not any(git(root, "merge-base", "--is-ancestor", sha, b).returncode == 0 for b in branches):
        names = ", ".join(b.removeprefix("refs/remotes/origin/") for b in branches)
        out.append(f"commit {sha[:12]} isn't on a release branch of origin ({names})")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", required=True, help="the ref the workflow runs for (GITHUB_REF)")
    ap.add_argument("--sha", required=True, help="its commit (GITHUB_SHA)")
    ap.add_argument("--default-branch", required=True)
    args = ap.parse_args(argv)
    found = problems(ROOT, args.ref, args.sha, args.default_branch)
    for p in found:
        print(f"::error::{p}")
    if not found:
        what = "tag and version agree, and the commit is on a release branch" \
            if args.ref.startswith(TAG + "v") else "not a release tag: the version strings agree"
        print(f"{args.ref}: {what}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
