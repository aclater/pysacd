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
import stat
from pathlib import Path

FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
         | getattr(os, "O_BINARY", 0))
MODE = 0o666                # filtered by the umask; never executable


def temp_name(path: str | os.PathLike) -> Path:
    """An unpredictable name for a temporary file next to `path`. It's short, so a final name
    near the 255-byte limit still leaves room for it."""
    return Path(path).with_name(f".pysacd-{secrets.token_hex(8)}.partial")


def inside(base: str | os.PathLike, path: str | os.PathLike) -> bool:
    """True if `path` is `base` or lies inside it. Lexical (no symlinks resolved), so a user's
    own links inside the output folder keep working; it catches "..", and an absolute path that
    a join would let replace the base."""
    root = os.path.abspath(base)
    full = os.path.abspath(path)
    return full == root or os.path.commonpath([root, full]) == root


def check_own_file(path: str | os.PathLike) -> None:
    """Before reopening by name a file pysacd created: it must still be one regular file of
    ours, not a link put in its place. This narrows the window for someone else who can write
    to the folder; it can't close it (the reopen is by name), and such a folder is outside what
    pysacd defends (docs/security.md, "Files pysacd writes")."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or (hasattr(os, "geteuid") and st.st_uid != os.geteuid()):
        raise OSError(f"{os.fspath(path)!r} is no longer the file pysacd created")


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


def config_dir() -> Path:
    """pysacd's configuration folder: $XDG_CONFIG_HOME/pysacd, or ~/.config/pysacd. As the XDG
    Base Directory spec says, an empty or relative XDG_CONFIG_HOME is ignored; otherwise the
    config file would be looked for relative to the current directory, where anyone who can
    write there could plant one."""
    base = os.environ.get("XDG_CONFIG_HOME") or ""
    return (Path(base) if os.path.isabs(base) else Path.home() / ".config") / "pysacd"
