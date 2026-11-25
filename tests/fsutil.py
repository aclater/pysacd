"""Test support: removing trees that pysacd wrote past Windows' 260-character limit (#217).

pysacd writes long output folders through extended-length paths (safeio.long_path()), so a
test can leave a tree that the plain-name rmtree of tempfile.TemporaryDirectory cannot reach
(WinError 145 on a stock Windows, where LongPathsEnabled is 0). These helpers remove it through
the extended-length name. Elsewhere they are the standard calls.
"""
import os
import shutil
import tempfile
from pathlib import Path

from pysacd import safeio


def rmtree(path) -> None:
    """shutil.rmtree through the extended-length path on Windows; errors are not ignored."""
    shutil.rmtree(safeio.long_path(path))


class TempDir(tempfile.TemporaryDirectory):
    """TemporaryDirectory whose cleanup() also removes trees deeper than 260 characters."""

    def cleanup(self):
        if safeio.WINDOWS:
            try:
                rmtree(self.name)
            except FileNotFoundError:
                pass
        super().cleanup()


NO_SYMLINK = ("creating a symlink needs a privilege this Windows user lacks "
              "(Developer Mode or an administrator); the checks that need no symlink ran")


def try_symlink(link, target) -> bool:
    """Make `link` a symlink to `target`; False only where Windows refuses a standard user
    (ERROR_PRIVILEGE_NOT_HELD, 1314). Every other failure is raised."""
    try:
        os.symlink(target, link)
    except OSError as e:
        if safeio.WINDOWS and getattr(e, "winerror", None) == 1314:
            return False
        raise
    return True


def lp(path) -> Path:
    """`path` as a Path through the extended-length form on Windows (unchanged elsewhere).

    Tests inspect pysacd's output trees through this: a plain name past 260 characters fails
    with WinError 3 on a stock Windows. Compare names shown to users in plain form
    (safeio.display_path); relative_to() against another lp() path stays valid.
    """
    return Path(safeio.long_path(path))
