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

## PS3 network protocol

The SACD Ripper protocol (`docs/ps3-protocol.md`) has **no authentication and no encryption**.
Real PS3s speak it as is, so pysacd can't change that. Anyone on the network path can read or
alter the sectors. Use it on trusted networks only. pysacd treats everything the server sends
as hostile:

- **Messages:** length-delimited fields are capped at one 512-sector read plus 1 KiB, and a
  message may have at most 16 fields.
- **Reads:** a reply must return between 1 and the requested number of sectors, with exactly
  that much data.
- **`DISC_SIZE`:** capped at 2^22 sectors (8 GiB), just above a dual-layer SACD, so a server
  can't make the client create an enormous sparse file.
- **TOC over the network:** areas whose TOC or audio lies outside the disc, or whose end comes
  before their start, are rejected as malformed. Fetch ranges are clamped to the disc.
- **Text on the disc:** it's still disc text. Names, tags and output are neutralised as
  described under [Hostile text](#hostile-text), and every output path stays inside the output
  directory.

`pysacd ps3sim` serves an ISO to anyone who can reach its port. It listens on `127.0.0.1` by
default, warns when given another address, and closes connections that stay idle for
`--idle-timeout` seconds (default 60), so one silent client can't hold the single connection
for ever.
