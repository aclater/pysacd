"""Diagnostic logging for the pysacd tools.

Everything logs under the "pysacd" logger (pysacd.engine, pysacd.remote,
pysacd.extract, ...). As a library pysacd stays silent; the command-line tools
call setup(), which writes a detailed log file when asked to:

    --log-file PATH / --log-level debug|info|warning|error
    PYSACD_LOG=debug              (level only; file pysacd.log in the cwd)
    PYSACD_LOG=debug:/tmp/x.log   (level and file)

The file starts with the environment (versions, platform, CPU, engine build,
arguments) because most field failures turn out to be environment problems.
Warnings also go to stderr; errors are reported once by the tool itself (with
--log-level but no --log-file, everything goes to stderr instead). A native crash still leaves a Python
and C stack trace in the log file (faulthandler).
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import platform
import sys
from pathlib import Path
from typing import TextIO

from . import __version__, safetext

LOGGER = "pysacd"
FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(threadName)s %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}

logging.getLogger(LOGGER).addHandler(logging.NullHandler())

_crash_file: TextIO | None = None
MAX_LOG_BYTES = 64 * 1024 * 1024    # rotate beyond this; two old files are kept


def _open_nofollow(path: str, flags: int) -> int:
    """Log files never follow a symlink planted at their path, and are private."""
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)    # no O_NOFOLLOW on Windows


class _LogFile(logging.handlers.RotatingFileHandler):
    def _open(self):
        return open(self.baseFilename, self.mode, encoding=self.encoding, opener=_open_nofollow)


def _escape_message(record: logging.LogRecord) -> bool:
    """Disc and network text can reach log messages: make control characters
    (newlines included) visible so nothing can forge log lines or drive a
    terminal. Tracebacks keep their line breaks."""
    record.msg = safetext.for_terminal(record.getMessage())
    record.args = None
    return True


class _Brief(logging.Formatter):
    """Console format: one line per message; tracebacks belong in the log file."""

    def format(self, record: logging.LogRecord) -> str:
        saved = record.exc_info, record.exc_text
        record.exc_info = record.exc_text = None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text = saved


def from_env(default_file: str = "pysacd.log") -> tuple[str | None, str | None]:
    """(level, file) from PYSACD_LOG, or (None, None)."""
    spec = os.environ.get("PYSACD_LOG", "").strip()
    if not spec:
        return None, None
    level, _, path = spec.partition(":")
    return level.lower() or "debug", path or default_file


def setup(level: str | None = None, path: str | os.PathLike | None = None, argv: list[str] | None = None) -> None:
    """Configure the "pysacd" logger for a command-line tool.

    With neither level nor path, only warnings and errors are shown (stderr).
    With a path, the file gets everything at `level` (default debug).
    """
    global _crash_file
    root = logging.getLogger(LOGGER)
    for h in list(root.handlers):
        if not isinstance(h, logging.NullHandler):
            root.removeHandler(h)
            h.close()
    file_level = LEVELS.get((level or "debug").lower())
    if file_level is None:
        raise ValueError(f"unknown log level {level!r} (use {', '.join(LEVELS)})")

    console = logging.StreamHandler(sys.stderr)
    console.addFilter(_escape_message)
    console.setLevel(logging.WARNING)
    console.setFormatter(_Brief("%(levelname)s: %(message)s"))
    root.addHandler(console)
    root.setLevel(logging.WARNING)

    if level is None or path is not None:
        # Each layer logs its own view of a failure; on the terminal the tool
        # prints one final error instead, and the details go to the log file.
        console.addFilter(lambda r: r.levelno < logging.ERROR)
    if path is None and level is None:
        return
    if path is not None:
        handler = _LogFile(path, mode="a", encoding="utf-8", maxBytes=MAX_LOG_BYTES, backupCount=2)
        handler.addFilter(_escape_message)
        handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
        handler.setLevel(file_level)
        root.addHandler(handler)
        # Native crashes (segfaults in the engine) go to the same file
        _crash_file = open(path, "a", encoding="utf-8", opener=_open_nofollow)  # noqa: SIM115 - for faulthandler
        faulthandler.enable(_crash_file, all_threads=True)
    else:
        console.setLevel(file_level)
        console.setFormatter(logging.Formatter(FORMAT, DATEFMT))
    root.setLevel(file_level)
    log_environment(argv if argv is not None else sys.argv)


def log_environment(argv: list[str]) -> None:
    log = logging.getLogger(LOGGER)
    log.info("pysacd %s, Python %s (%s), %s", __version__, platform.python_version(),
             platform.python_implementation(), platform.platform())
    log.info("command line: %s", " ".join(argv))
    log.info("working directory: %s", os.getcwd())
    log.debug("executable: %s; cpus: %s", sys.executable, os.cpu_count())
    cpu = _cpu_model()
    if cpu:
        log.debug("cpu: %s", cpu)
    try:
        from . import _native
        log.info("engine: %s (%s)", _native.library_path(), _native.build_info())
    except Exception as e:  # noqa: BLE001 - a missing engine is itself the diagnosis
        log.warning("engine not available: %s", e)
    try:
        import mutagen
        log.debug("mutagen %s", mutagen.version_string)
    except ImportError:
        log.debug("mutagen not installed")


def _cpu_model() -> str | None:
    try:
        for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None
