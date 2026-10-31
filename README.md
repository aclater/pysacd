# pysacd

[![PyPI](https://img.shields.io/pypi/v/pysacd)](https://pypi.org/project/pysacd/)
[![Python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Faclater%2Fpysacd%2Fmaster%2Fpyproject.toml)](pyproject.toml)
[![Wheel](https://img.shields.io/pypi/wheel/pysacd)](https://pypi.org/project/pysacd/#files)
[![Implementation](https://img.shields.io/pypi/implementation/pysacd)](https://pypi.org/project/pysacd/)
[![Downloads](https://static.pepy.tech/badge/pysacd/month)](https://pepy.tech/projects/pysacd)
[![License](https://img.shields.io/pypi/l/pysacd)](COPYING)

[![CI](https://github.com/aclater/pysacd/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/aclater/pysacd/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/aclater/pysacd/branch/master/graph/badge.svg)](https://codecov.io/gh/aclater/pysacd)
[![CodeQL](https://github.com/aclater/pysacd/actions/workflows/codeql.yml/badge.svg?branch=master)](https://github.com/aclater/pysacd/actions/workflows/codeql.yml)
[![Fuzzing](https://github.com/aclater/pysacd/actions/workflows/fuzz.yml/badge.svg)](https://github.com/aclater/pysacd/actions/workflows/fuzz.yml)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/aclater/pysacd/badge)](https://scorecard.dev/viewer/?uri=github.com/aclater/pysacd)
[![SLSA Build L2](https://slsa.dev/images/gh-badge-level2.svg)](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)

**SACD in your Python process, and a clean-room, drop-in replacement for
`sacd_extract`: the same options, the same file names, the same bytes.**

```python
from pysacd import read_toc                 # in-process: no subprocess, no text to parse

disc = read_toc("album.iso")
print(disc.artist, "-", disc.title)
for t in disc.stereo.tracks:
    print(t.number, t.title, t.duration)
```

pysacd reads SACD (Scarletbook) disc images, and PS3 SACD Ripper servers,
straight from Python. Its multithreaded C engine runs inside your process, so
a script, a tagger or a library catalogue drives it with function calls and
gets back structured results. It never spawns `sacd_extract` and parses its
text.

**Coming from `sacd_extract`?** `pysacd-extract` is a drop-in replacement,
verified file by file against the real thing:

```sh
ln -s "$(command -v pysacd-extract)" ~/bin/sacd_extract    # that's the whole migration
```

```sh
pip install pysacd     # wheels for Linux, macOS and Windows; engine and libFLAC included
```

## Why pysacd

Why it exists, in the author's words: [author's note](docs/author.md).

- **No fork, no scraping.**
  - **What you get back:** objects (or `--json`), not `sacd_extract -P` text.
  - **Failures** are exceptions, not exit codes and stderr.
  - **Progress** is a callback; to cancel, raise from it.
  - **The TOC reader** runs in-process, through scarletbook-rs: one call returns objects
    (see [the Python library](docs/library.md)).
- **One pass, every output.** One call reads an area once and writes DSF,
  DSDIFF, WAV and FLAC (any mix), plus an ISO copy. DST is decoded on every
  core, with the GIL released.
- **Byte-for-byte compatible** with `sacd_extract`, apart from a few
  [deliberate exceptions](docs/sacd_extract.md#compatibility-policy).
- **Clean by construction.** No Philips/ISO reference code, ever. DST
  decoding comes from FFmpeg's LGPL decoder, completed through an auditable
  two-agent clean room. See [clean room and licences](docs/clean-room.md).
- **Goes further than `sacd_extract`:**
  - gapless 24-bit FLAC/WAV;
  - MusicBrainz tags and cover art, with your own naming;
  - resume after a dropped PS3 connection;
  - raw-sector (2064/2054-byte) images;
  - protection against hostile disc text;
  - logs you can diagnose a failure from.
- **Faster than `sacd_extract`:** plain-DSD discs extract 2.5–3.6× faster, and DST
  discs 2.4–2.6× (a faster DST decoder). See [performance](docs/performance.md).

## Why you shouldn't use pysacd (yet)

- **Never tested against a real PS3.** The network client is verified against
  a protocol simulator and `sacd_extract`, but not against hardware (#10).
- **Optical drives:** like `sacd_extract`, it can't read a PC drive's SACD
  layer. You need an ISO or a PS3.

## Documentation

The [documentation index](docs/README.md) lists every document and its sections.

| | |
|---|---|
| [Quick start](docs/quickstart.md) | install, and a tour of the commands |
| [pysacd-extract](docs/sacd_extract.md) | migrating from `sacd_extract`, compatibility policy, verification |
| [Converting](docs/converting.md) | naming templates, tags, MusicBrainz, gapless PCM |
| [The Python library](docs/library.md) | the API, what's parsed, image formats |
| [Performance](docs/performance.md) | benchmarks and their conditions |
| [Reading from a PS3](docs/ps3-protocol.md) | usage, the wire protocol and the PS3's quirks |
| [Logging](docs/logging.md) | what to attach when something fails |
| [Security](docs/security.md) | hostile discs, servers and text; report vulnerabilities via [SECURITY.md](SECURITY.md) |
| [Clean room and licences](docs/clean-room.md) | where the code came from, acknowledgements |
| [DST bitstream](docs/dst-bitstream.md) | the clean-room specification |
| [Development](docs/development.md) | building, tests, CI; contributor rules in [CLAUDE.md](CLAUDE.md) |
| [Contributing](CONTRIBUTING.md) | reporting bugs, the pull-request process, project rules |
| [Changelog](CHANGELOG.md) | what changed in each release |

## License

GPL-2.0-or-later, with `csrc/dstdec.c` under LGPL-2.1-or-later. The engine links the
[scarletbook-rs](https://github.com/aclater/scarletbook-rs) C library statically, which pysacd
uses under MIT (it is MIT OR Apache-2.0). See [clean room and licences](docs/clean-room.md).
