"""Neutralising untrusted text (disc TOC text, MusicBrainz data, network input) before pysacd
writes or prints it.

A crafted disc can put anything in its title, artist or other text fields. pysacd never
executes that text, but it does turn it into file and directory names, cue/XML/tag contents and
terminal output, where other programs (backup scripts, `tar *`, shell loops, the terminal
itself) might act on it. See docs/security.md, "Hostile text".

- name_component(): one file or directory name component. Removes control, bidi and
  zero-width characters, command-substitution and redirection characters, a leading '-'
  (option injection) and leading dots.
- clean_text(): text for tags, cue sheets and XML. Control, bidi and zero-width characters
  are removed; whitespace runs collapse to one space.
- for_terminal(): text for stdout/stderr and logs. Control characters become visible escapes
  (\\x1b), so they can't drive the terminal.

Ordinary names are unchanged: everything here only touches characters that real titles
almost never contain.
"""

from __future__ import annotations

import re
import unicodedata

# C0 and C1 controls and DEL; bidi embeddings/overrides/isolates; zero-width characters and BOM
_CONTROL = "\x00-\x1f\x7f-\x9f"
_INVISIBLE = "\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff"
_BAD = re.compile(f"[{_CONTROL}{_INVISIBLE}]")
_BAD_KEEP_TAB_NL = re.compile(f"[\x00-\x08\x0b-\x1f\x7f-\x9f{_INVISIBLE}]")
# Command substitution, pipes, command separators and redirection
NAME_UNSAFE = "`$|;<>"
_NAME_UNSAFE_RE = re.compile("[" + re.escape(NAME_UNSAFE) + "]")


def strip_controls(s: str, keep_tab_newline: bool = False) -> str:
    """Replace control, bidi and zero-width characters with a space (nothing else changes)."""
    return (_BAD_KEEP_TAB_NL if keep_tab_newline else _BAD).sub(" ", s)


def clean_text(s: str) -> str:
    """Text without control, bidi or zero-width characters, NFC-normalised, whitespace collapsed."""
    s = unicodedata.normalize("NFC", s)
    s = _BAD.sub(" ", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def name_component(s: str, replacement: str = "_") -> str:
    """A single path component that is safe to create and to handle in scripts.

    Removes '/', control/bidi/zero-width characters and NAME_UNSAFE, and stops it starting with
    '-' or '.'. Never returns '', '.' or '..'.
    """
    s = clean_text(s).replace("/", "-").replace("\\", "-")
    s = _NAME_UNSAFE_RE.sub(replacement, s)
    s = s.lstrip(".").strip()
    if s.startswith("-"):
        s = replacement + s
    return s or replacement


def has_unsafe_name_chars(s: str) -> bool:
    """True if name_component() would change more than whitespace in s."""
    return bool(_BAD.search(s) or _NAME_UNSAFE_RE.search(s) or s.lstrip().startswith("-"))


def for_terminal(s: object, keep_tab_newline: bool = False) -> str:
    """Make every control character visible (\\x1b, \\u202e) so text can't drive a terminal.

    Accepts any object (paths, numbers) and formats it with str() first."""
    pattern = _BAD_KEEP_TAB_NL if keep_tab_newline else _BAD

    def esc(m: re.Match) -> str:
        c = ord(m.group())
        return f"\\x{c:02x}" if c < 0x100 else f"\\u{c:04x}"
    return pattern.sub(esc, str(s))
