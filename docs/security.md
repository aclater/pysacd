# Security

pysacd reads disc images and metadata that may come from anyone. This document records what
pysacd treats as untrusted, and how each kind of input is contained. Related issues: #12
(PS3 network protocol), #14 (engine, filesystem, HTTP, build), #17 (untrusted images).

## Hostile text

A disc's TOC text (album and track titles, artists, publisher, catalogue numbers) is chosen by
whoever made the image, and MusicBrainz data is third-party. A crafted disc can put command
substitutions, option-like names, newlines or terminal escape sequences in those fields,
hoping that something downstream acts on them once pysacd has written them out. The
"something" might be a backup script running `tar *` as root, a shell loop over file names,
or the terminal displaying a name.

pysacd never executes text: it runs no shell, no `subprocess`, no `eval`. On top of that,
`pysacd.safetext` neutralises untrusted text everywhere pysacd writes or prints it.

| Where | Function | What happens |
|---|---|---|
| File and directory names (`convert`, and the `sacd_extract`-compatible names) | `name_component()`, `sbcompat._sanitize()` | control, bidi and zero-width characters removed; `` ` $ \| ; < > `` replaced with `_`; no component starts with `-` or `.`; `/` can't split a component; names stay inside the output directory |
| Tags (ID3, Vorbis), cue sheets, XML | `clean_text()`, `strip_controls()` | control, bidi and zero-width characters removed (XML keeps tab and newline, escaped); cue fields can't break onto a new line; XML is escaped |
| Terminal and logs (`-P`, `pysacd toc`, progress, warnings, errors) | `for_terminal()` | control characters are shown as escapes (`\x1b`), so text can't drive the terminal (colour, title, OSC 52 clipboard writes, keystroke injection) |

Characters that are common in real titles and only dangerous to scripts that already mishandle
names (`&`, `'`, `()`, `[]`, `!`) are kept. No sanitiser can make a name safe for a script that
`eval`s it. The rules above stop the attacks that work against *correctly written* scripts and
tools: option injection, newline splitting and terminal escapes. They also remove command
substitution, which real titles practically never contain.

### Differences from sacd_extract

The `sacd_extract`-compatible names follow sacd_extract's rules, plus the ones above. A disc
whose text contains `` ` ``, `$`, `;` or a leading `-`, or non-ASCII control or bidi
characters, therefore gets different names from `sacd_extract` (`Ke$ha` becomes `Ke_ha`). Every
other disc is byte-identical. That deviation is deliberate.

### Rule for contributors

Any new code path that writes or prints disc, MusicBrainz or network text must go through
`pysacd.safetext`. `tests/test_safetext.py` builds a disc with hostile titles and checks every
writer and printer. Extend it when you add one.
