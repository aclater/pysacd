"""tools/check_release_tag.py: a wrong or unmerged release tag publishes nothing (#154)."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_release_tag as crt

INIT = "pkg/__init__.py"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
         "-c", "init.defaultBranch=master", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class CheckReleaseTag(unittest.TestCase):
    """An "origin" with master and release/1.0, and a clone of it with one unmerged commit."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        origin, self.clone = Path(tmp.name, "origin"), Path(tmp.name, "clone")
        origin.mkdir()
        git(origin, "init", "-q")
        self.write(origin, "1.2.3", "1.2.3")
        git(origin, "add", "-A")
        git(origin, "commit", "-q", "-m", "one")
        self.merged = git(origin, "rev-parse", "HEAD")
        git(origin, "checkout", "-q", "-b", "release/1.0")
        git(origin, "commit", "-q", "--allow-empty", "-m", "backport")
        self.backport = git(origin, "rev-parse", "HEAD")
        git(origin, "checkout", "-q", "-b", "feature")
        git(origin, "commit", "-q", "--allow-empty", "-m", "unmerged")
        self.unmerged = git(origin, "rev-parse", "HEAD")
        git(origin, "checkout", "-q", "master")
        git(Path(tmp.name), "clone", "-q", str(origin), str(self.clone))

    @staticmethod
    def write(root: Path, project: str, init: str) -> None:
        (root / "pkg").mkdir(exist_ok=True)
        (root / "pyproject.toml").write_text(
            f'[build-system]\nversion = "9"\n[project]\nname = "x"\nversion = "{project}"\n', encoding="utf-8")
        (root / INIT).write_text(f'__version__ = "{init}"\n', encoding="utf-8")

    def problems(self, ref: str, sha: str | None = None, init: str | None = INIT) -> list[str]:
        return crt.problems(self.clone, ref, sha or self.merged, "master", init)

    def test_a_matching_tag_on_the_default_branch_passes(self):
        self.assertEqual(self.problems("refs/tags/v1.2.3"), [])

    def test_a_release_branch_counts(self):
        self.assertEqual(self.problems("refs/tags/v1.2.3", self.backport), [])

    def test_an_unmerged_commit_fails(self):
        (p,) = self.problems("refs/tags/v1.2.3", self.unmerged)
        self.assertIn("isn't on a release branch", p)
        self.assertIn("master, release/1.0", p)

    def test_an_unknown_commit_fails(self):
        (p,) = self.problems("refs/tags/v1.2.3", "0" * 40)
        self.assertIn("isn't on a release branch", p)

    def test_a_tag_that_is_not_the_version_fails(self):
        for tag in ("v1.2.4", "v1.2.3rc1", "v1.2", "v01.2.3"):
            with self.subTest(tag):
                (p,) = self.problems("refs/tags/" + tag)
                self.assertIn("expected v1.2.3", p)

    def test_versions_that_disagree_fail_for_any_ref(self):
        self.write(self.clone, "1.2.3", "1.2.2")
        for ref in ("refs/tags/v1.2.3", "refs/pull/7/merge", "refs/heads/master"):
            with self.subTest(ref):
                (p,) = self.problems(ref)
                self.assertIn("pkg/__init__.py says 1.2.2", p)

    def test_a_package_without_dunder_version(self):
        self.write(self.clone, "1.2.3", "0")
        self.assertEqual(self.problems("refs/tags/v1.2.3", init=None), [])

    def test_other_refs_skip_the_tag_checks(self):
        self.assertEqual(self.problems("refs/pull/7/merge", self.unmerged), [])
        self.assertEqual(self.problems("refs/heads/feature", self.unmerged), [])

    def test_a_clone_without_the_default_branch_fails(self):
        git(self.clone, "update-ref", "-d", "refs/remotes/origin/master")
        (p,) = self.problems("refs/tags/v1.2.3")
        self.assertIn("full clone", p)

    def test_this_repository_agrees_with_itself(self):
        self.assertEqual(crt.init_version(ROOT, crt.INIT), crt.project_version(ROOT))


if __name__ == "__main__":
    unittest.main()
