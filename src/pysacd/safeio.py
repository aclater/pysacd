"""Creating output files safely (#14).

Every file pysacd writes is created new, with O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC. An existing
file or symlink is never opened for writing. The mode is 0666 filtered by the user's umask, as
cp and sacd_extract do (shared music libraries rely on umask 002), and never has execute bits.
Files are written under an unpredictable temporary name in the target directory, then moved into
place with os.replace(), which replaces a symlink at the final name rather than following it.
The C engine opens its outputs the same way (sc_fopen_write() in csrc/compat.h).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
         | getattr(os, "O_BINARY", 0))
MODE = 0o666                # filtered by the umask; never executable


def temp_name(path: str | os.PathLike) -> Path:
    """An unpredictable name for a temporary file next to `path`. It's short, so a final name
    near the 255-byte limit still leaves room for it."""
    return Path(path).with_name(f".pysacd-{secrets.token_hex(8)}.partial")


def create(path: str | os.PathLike) -> int:
    """A new file at `path`, opened for writing; FileExistsError if anything is there."""
    return os.open(path, FLAGS, MODE)


def open_new(path: str | os.PathLike):
    """create() as a binary file object. If wrapping the descriptor fails, it is closed and
    the new file removed, so nothing leaks."""
    fd = create(path)
    try:
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        Path(path).unlink(missing_ok=True)
        raise


def write_file(path: str | os.PathLike, data: bytes) -> None:
    """Write `data` to `path` atomically, through a new temporary file, replacing any old file."""
    tmp = temp_name(path)
    f = open_new(tmp)
    try:
        with f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
