"""scripts/check-private-names.sh: finds private details and fails when it cannot look.

Each test builds a throwaway git repository with a copy of the script and runs it with a
fake PRIVATE_NAMES. Exit status: 0 clean, 1 found, 2 the check could not run. Private-looking
strings are assembled at run time so this file itself passes the check.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("CHECK_PRIVATE_NAMES_SCRIPT", ROOT / "scripts" / "check-private-names.sh"))
FAKE = "zq" + "fakeprivname"  # the fake PRIVATE_NAMES regex
HIT = "a line naming " + FAKE + " here\n"
PRIVATE_IP = "192." + "168.77.7"


# CI runs the check on Linux only (the "no private" job, UBI 10 with GNU grep).
@unittest.skipUnless(
    sys.platform.startswith("linux") and shutil.which("git") and shutil.which("bash"),
    "the check runs on Linux with git and bash",
)
class CheckPrivateNames(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.repo = self.base / "repo"
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.base),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        self.init(self.repo)
        (self.repo / "scripts").mkdir()
        shutil.copy(SCRIPT, self.repo / "scripts" / "check-private-names.sh")
        self.write("README", "nothing private\n")
        self.commit()

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd or self.repo, env=self.env, check=True, capture_output=True, text=True
        ).stdout

    def init(self, path: Path) -> None:
        path.mkdir()
        self.git("init", "-q", cwd=path)

    def write(self, name: str, text: str, repo: Path | None = None) -> Path:
        p = (repo or self.repo) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def commit(self, repo: Path | None = None, **identity: str) -> str:
        env = dict(self.env, **identity)
        cwd = repo or self.repo
        subprocess.run(["git", "add", "-A"], cwd=cwd, env=env, check=True)
        subprocess.run(["git", "commit", "-qm", "c"], cwd=cwd, env=env, check=True)
        return self.git("rev-parse", "HEAD", cwd=cwd).strip()

    def check(self, names: str | None = FAKE, **extra: str) -> subprocess.CompletedProcess:
        env = dict(self.env, **extra)
        if names is not None:
            env["PRIVATE_NAMES"] = names
        return subprocess.run(
            ["bash", "scripts/check-private-names.sh"],
            check=False,
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def assert_status(self, r: subprocess.CompletedProcess, status: int) -> None:
        self.assertEqual(r.returncode, status, r.stdout + r.stderr)

    # Clean trees pass

    def test_clean_tree_passes(self):
        r = self.check()
        self.assert_status(r, 0)
        self.assertIn("no private details found", r.stdout)

    def test_clean_tree_passes_without_private_names(self):
        self.assert_status(self.check(names=None), 0)

    def test_the_script_itself_is_not_scanned(self):
        self.assert_status(self.check(names=None), 0)  # it spells out every built-in pattern

    # Hits are caught

    def test_hit_in_a_file_fails(self):
        self.write("docs/notes.md", HIT.upper())  # case-insensitive
        self.commit()
        r = self.check()
        self.assert_status(r, 1)
        self.assertIn("docs/notes.md", r.stdout)

    def test_builtin_patterns_are_caught(self):
        for text in (PRIVATE_IP, "nas" + ".home" + ".arpa", "/home/" + "someone/x", "/mnt/" + "media"):
            with self.subTest(text=text):
                self.write("f.txt", text + "\n")
                self.commit()
                self.assert_status(self.check(names=None), 1)
                self.write("f.txt", "clean\n")
                self.commit()

    def test_uncommitted_edit_to_a_tracked_file_is_caught(self):
        self.write("README", HIT)
        self.assert_status(self.check(), 1)

    def test_file_name_with_space_and_leading_dash(self):
        self.write("-odd name.txt", HIT)
        self.commit()
        r = self.check()
        self.assert_status(r, 1)
        self.assertIn("-odd name.txt", r.stdout)

    def test_hit_among_many_files(self):
        # Enough long paths that an xargs pipeline splits them into several batches;
        # the hit is in the first batch, the clean files in later ones.
        d = "d" * 100
        self.write(f"{d}/000-hit.txt", HIT)
        for i in range(1, 3000):
            self.write(f"{d}/{i:04d}-{'x' * 60}.txt", "clean\n")
        self.commit()
        self.assert_status(self.check(), 1)

    def test_hit_next_to_a_directory_symlink(self):
        self.write("real/a.txt", "clean\n")
        os.symlink("real", self.repo / "link")
        self.write("b.txt", HIT)
        self.commit()
        self.assert_status(self.check(), 1)

    def test_hit_next_to_a_submodule(self):
        sub = self.base / "sub"
        self.init(sub)
        self.write("s.txt", "clean\n", repo=sub)
        self.commit(repo=sub)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
        self.write("b.txt", HIT)
        self.commit()
        self.assert_status(self.check(), 1)

    def test_hit_in_a_commit_identity(self):
        self.write("c.txt", "clean\n")
        self.commit(GIT_AUTHOR_EMAIL=f"me@{FAKE}.example")
        r = self.check()
        self.assert_status(r, 1)
        self.assertIn("commit identities", r.stdout)

    def test_base_sha_limits_the_identities_checked(self):
        self.write("c.txt", "clean\n")
        self.commit(GIT_COMMITTER_NAME=FAKE)
        base = self.git("rev-parse", "HEAD").strip()
        self.write("d.txt", "clean\n")
        self.commit()
        self.assert_status(self.check(), 1)
        self.assert_status(self.check(BASE_SHA=base), 0)

    # The check fails when it cannot run, and never prints PRIVATE_NAMES

    def assert_cannot_run(self, names: str | None) -> None:
        r = self.check(names=names)
        self.assert_status(r, 2)
        self.assertIn("could not run", r.stdout)
        if names and names.strip():
            self.assertNotIn(names.strip(), r.stdout + r.stderr)

    def test_invalid_regex_fails(self):
        self.write("b.txt", HIT)
        self.commit()
        self.assert_cannot_run("(" + FAKE)

    def test_invalid_regex_fails_on_a_clean_tree(self):
        self.assert_cannot_run(FAKE + "[")

    def test_empty_alternative_fails(self):
        for names in (FAKE + "|", "|" + FAKE, FAKE + "||other", "(|" + FAKE + ")", FAKE + "?|.*"):
            with self.subTest(names=names):
                self.assert_cannot_run(names)

    def test_blank_value_fails(self):
        self.assert_cannot_run("   ")

    def test_unreadable_history_fails(self):
        first = self.git("rev-parse", "HEAD").strip()
        self.write("c.txt", "clean\n")
        self.commit()
        (self.repo / ".git" / "objects" / first[:2] / first[2:]).unlink()
        self.assert_cannot_run(FAKE)

    def test_outside_a_git_repository_fails(self):
        outside = self.base / "outside"
        (outside / "scripts").mkdir(parents=True)
        shutil.copy(SCRIPT, outside / "scripts" / "check-private-names.sh")
        env = dict(self.env, PRIVATE_NAMES=FAKE, GIT_CEILING_DIRECTORIES=str(self.base))
        r = subprocess.run(
            ["bash", "scripts/check-private-names.sh"],
            check=False,
            cwd=outside,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assert_status(r, 2)


if __name__ == "__main__":
    unittest.main()
