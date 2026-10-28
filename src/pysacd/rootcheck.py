"""Refuse to run as root (#17).

The engine parses attacker-controlled disc images. A bug that a crafted image can exploit
should cost an ordinary user's files, not the machine, so the CLIs refuse to run as root
unless --allow-root is given. pysacd-extract only warns: it is a drop-in for sacd_extract,
which runs as root, and people use sudo to read /dev/sr0 with it.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("pysacd")


def running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)      # absent on Windows
    return geteuid is not None and geteuid() == 0


_WHY = "an image that exploited a bug would control the whole machine"


def warn_root(prog: str) -> None:
    """One line on stderr (and in the log) when running as root."""
    if running_as_root():
        log.warning("running as root: %s", _WHY)
        print(f"{prog}: warning: running as root; disc images are untrusted and {_WHY}. "
              "Run as an ordinary user if you can.", file=sys.stderr)


def refuse_root(prog: str, allow: bool) -> bool:
    """True (after printing why) if the caller must stop: running as root without allow."""
    if not running_as_root():
        return False
    if allow:
        log.warning("running as root (--allow-root): %s", _WHY)
        print(f"{prog}: warning: running as root (--allow-root)", file=sys.stderr)
        return False
    print(f"{prog}: refusing to run as root. Disc images are untrusted input, and {_WHY}. "
          "Run as an ordinary user, or pass --allow-root.", file=sys.stderr)
    return True
