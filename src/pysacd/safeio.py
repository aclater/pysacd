"""Creating output files safely (#14).

Every file pysacd writes is created new, with O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC and mode
0644. An existing file or symlink is never opened for writing, and output never has execute
bits, whatever the umask. Files are written under an unpredictable temporary name in the target
directory, then moved into place with os.replace(), which replaces a symlink at the final name
rather than following it. The C engine opens its outputs the same way (create_output() in
csrc/sacdconv.c).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
         | getattr(os, "O_BINARY", 0))
MODE = 0o644


def temp_name(path: str | os.PathLike) -> Path:
    """An unpredictable name for a temporary file next to `path`. It's short, so a final name
    near the 255-byte limit still leaves room for it."""
    return Path(path).with_name(f".pysacd-{secrets.token_hex(8)}.partial")


def create(path: str | os.PathLike) -> int:
    """A new file at `path`, opened for writing; FileExistsError if anything is there."""
    return os.open(path, FLAGS, MODE)


def write_file(path: str | os.PathLike, data: bytes) -> None:
    """Write `data` to `path` atomically, through a new temporary file, replacing any old file."""
    tmp = temp_name(path)
    fd = create(tmp)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
