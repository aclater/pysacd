# pysacd

**SACD in your Python process, and a clean-room, drop-in replacement for
`sacd_extract`: the same options, the same file names, the same bytes.**

```python
from pysacd import read_toc                 # pure Python: no binary, no subprocess

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

## Why pysacd

- **No fork, no scraping.**
  - **What you get back:** objects (or `--json`), not `sacd_extract -P` text.
  - **Failures** are exceptions, not exit codes and stderr.
  - **Progress** is a callback; to cancel, raise from it.
  - **The TOC reader** is pure Python and needs no binary.
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
- **Plain-DSD discs extract 2.5–3.6× faster.** On DST discs it's essentially
  a tie, because both tools saturate every core. See
  [performance](docs/performance.md).

## Why you shouldn't use pysacd (yet)

- **Not on PyPI (yet):** wheels for Linux x86-64 and aarch64 come from GitHub
  releases. On macOS and Windows you build from source ([quick start](docs/quickstart.md)).
- **Never tested against a real PS3.** The network client is verified against
  a protocol simulator and `sacd_extract`, but not against hardware (#10).
- **It isn't faster on DST discs.**
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

## License

GPL-2.0-or-later, with `csrc/dstdec.c` under LGPL-2.1-or-later. See
[clean room and licences](docs/clean-room.md).
