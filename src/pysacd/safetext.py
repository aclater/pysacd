"""Neutralising untrusted text (disc TOC text, MusicBrainz data, network input) before pysacd
writes or prints it.

A crafted disc can put anything in its title, artist or other text fields. pysacd never
executes that text, but it does turn it into file and directory names, cue/XML/tag contents and
terminal output, where other programs (backup scripts, `tar *`, shell loops, the terminal
itself) might act on it. See docs/security.md, "Hostile text".

Characters are judged by Unicode category, not by listed ranges (#47). "Invisible" means
categories Cc (controls), Cf (format: bidi controls, zero-width characters, BOM, soft hyphen,
tag characters U+E0000-E007F), Cs (surrogates), Co (private use), Cn (unassigned, including the
noncharacters U+FFFE/U+FFFF), Zl and Zp (U+2028/U+2029, which splitlines() and JavaScript
treat as newlines).

- name_component(): one file or directory name component. Removes invisible characters,
  command-substitution and redirection characters, a leading '-' (option injection) and
  leading dots.
- clean_text(): text for tags, cue sheets and XML. Invisible characters are removed (a
  control or line separator becomes a space, so words stay apart), other
  spaces (Zs: no-break, ideographic, ...) become a plain space, whitespace runs collapse.
- strip_controls(): the sacd_extract-compatible paths. Invisible characters are removed
  (lone surrogates excepted, see below); nothing else changes, so ordinary discs keep
  byte-identical names.
- for_terminal(): text for stdout/stderr and logs. Invisible characters become visible escapes
  (\\x1b, \\u202e), so they can't drive the terminal or disguise what is shown.
- format_exception(): a traceback whose exception messages are escaped like for_terminal().

Ordinary names are unchanged: everything here only touches characters that real titles
almost never contain.
"""

from __future__ import annotations

import re
import sys
import traceback
import unicodedata
from types import TracebackType

INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# The compat paths keep lone surrogates: there they are raw bytes of a name cut on a byte
# count, exactly as sacd_extract cuts it, carried through "surrogateescape" (sbcompat.py).
# Disc text itself never holds any: the TOC reader decodes with "replace".
_COMPAT_CATEGORIES = INVISIBLE_CATEGORIES - {"Cs"}
# Command substitution, pipes, command separators and redirection
NAME_UNSAFE = "`$|;<>"
_NAME_UNSAFE_RE = re.compile("[" + re.escape(NAME_UNSAFE) + "]")
# The only invisible characters plain ASCII can hold: a fast path for the common case
_ASCII_BAD = re.compile("[\x00-\x1f\x7f]")
_ASCII_BAD_KEEP_TAB_NL = re.compile("[\x00-\x08\x0b-\x1f\x7f]")


_BREAKS = frozenset({"Cc", "Zl", "Zp"})     # these separate words: they become a space


def _drop(c: str) -> str:
    """Removing a character: a control or line/paragraph separator becomes a space so the words
    around it stay apart; format characters (zero-width, bidi, soft hyphen, tags) and the rest
    vanish."""
    return " " if unicodedata.category(c) in _BREAKS else ""


def is_invisible(c: str) -> bool:
    """True for a character in one of INVISIBLE_CATEGORIES."""
    return unicodedata.category(c) in INVISIBLE_CATEGORIES


def _map_invisible(s: str, repl, keep_tab_newline: bool, categories=INVISIBLE_CATEGORIES) -> str:
    """s with every character c in categories replaced by repl(c)."""
    if s.isascii():
        pattern = _ASCII_BAD_KEEP_TAB_NL if keep_tab_newline else _ASCII_BAD
        return pattern.sub(lambda m: repl(m.group()), s)
    keep = "\t\n" if keep_tab_newline else ""
    cat = unicodedata.category
    return "".join(repl(c) if c not in keep and cat(c) in categories else c for c in s)


def strip_controls(s: str, keep_tab_newline: bool = False) -> str:
    """Remove invisible characters (controls and line separators become a space), except lone
    surrogates (raw bytes on the sacd_extract-compatible paths). Nothing else changes."""
    return _map_invisible(s, _drop, keep_tab_newline, _COMPAT_CATEGORIES)


def clean_text(s: str) -> str:
    """Text without invisible characters, NFC-normalised, every space a plain one, whitespace
    collapsed."""
    # drop first: removing a character can leave a decomposed sequence that NFC then composes
    s = unicodedata.normalize("NFC", _map_invisible(s, _drop, False))
    if not s.isascii():
        s = "".join(" " if c != " " and unicodedata.category(c) == "Zs" else c for c in s)
    return re.sub(r"\s{2,}", " ", s).strip()


def name_component(s: str, replacement: str = "_") -> str:
    """A single path component that is safe to create and to handle in scripts.

    Removes '/', invisible characters and NAME_UNSAFE, and stops it starting with '-' or '.'.
    Never returns '', '.' or '..'.
    """
    s = clean_text(s).replace("/", "-").replace("\\", "-")
    s = _NAME_UNSAFE_RE.sub(replacement, s)
    # dots and spaces together: ". .x" must not become ".x" (clean_text left only plain spaces)
    s = s.lstrip(". ").rstrip()
    if s.startswith("-"):
        s = replacement + s
    return s or replacement


def has_unsafe_name_chars(s: str) -> bool:
    """True if name_component() would change more than whitespace in s."""
    return (_map_invisible(s, _drop, False) != s or bool(_NAME_UNSAFE_RE.search(s))
            or s.lstrip().startswith("-"))


def _escape(c: str) -> str:
    o = ord(c)
    return f"\\x{o:02x}" if o < 0x100 else f"\\u{o:04x}" if o < 0x10000 else f"\\U{o:08x}"


def for_terminal(s: object, keep_tab_newline: bool = False) -> str:
    """Make every invisible character visible (\\x1b, \\u202e, \\U000e0041) so text can't drive a
    terminal or disguise itself (Trojan Source).

    Accepts any object (paths, numbers) and formats it with str() first."""
    return _map_invisible(str(s), _escape, keep_tab_newline)


def format_exception(exc: BaseException) -> str:
    """A traceback like Python's own, with every exception message escaped by for_terminal().

    Stack frames (pysacd's own file names and source lines) are kept as they are. Messages can
    carry disc text, e.g. an OSError's file name, and their newlines are escaped too, so they
    can't forge traceback or log lines.
    """
    out: list[str] = []

    def emit(te: traceback.TracebackException) -> None:
        if te.__cause__ is not None:
            emit(te.__cause__)
            out.append("\nThe above exception was the direct cause of the following exception:\n\n")
        elif te.__context__ is not None and not te.__suppress_context__:
            emit(te.__context__)
            out.append("\nDuring handling of the above exception, another exception occurred:\n\n")
        if te.stack:
            out.append("Traceback (most recent call last):\n")
            out.extend(te.stack.format())
        out.append(for_terminal("".join(te.format_exception_only()).rstrip("\n")) + "\n")

    emit(traceback.TracebackException.from_exception(exc))
    return "".join(out)


def _excepthook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    sys.stderr.write(format_exception(exc.with_traceback(tb)))


def install_excepthook() -> None:
    """Print uncaught exceptions through format_exception() (command-line entry points)."""
    sys.excepthook = _excepthook
