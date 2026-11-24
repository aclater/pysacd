<img src="https://raw.githubusercontent.com/dsd-toolbox/pysacd/master/docs/assets/icon-256.png" alt="" width="96">

# pysacd

[![CI](https://github.com/dsd-toolbox/pysacd/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/dsd-toolbox/pysacd/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/dsd-toolbox/pysacd/branch/master/graph/badge.svg)](https://codecov.io/gh/dsd-toolbox/pysacd)
[![CodeQL](https://github.com/dsd-toolbox/pysacd/actions/workflows/codeql.yml/badge.svg?branch=master)](https://github.com/dsd-toolbox/pysacd/actions/workflows/codeql.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/dsd-toolbox/pysacd/badge)](https://scorecard.dev/viewer/?uri=github.com/dsd-toolbox/pysacd)
[![PyPI](https://img.shields.io/pypi/v/pysacd)](https://pypi.org/project/pysacd/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![Licence: GPL-2.0-or-later](https://img.shields.io/badge/licence-GPL--2.0--or--later-blue)](COPYING)

pysacd reads SACD (Scarletbook) images, and PS3 SACD Ripper servers, straight from Python,
and writes DSF, DFF (DSDIFF), WAV and FLAC in one pass with its multithreaded C engine. It also ships
`pysacd-extract`, a clean-room, drop-in replacement for `sacd_extract`: the same options, the
same file names, the same bytes.

## Platforms

| System | How | Guide |
|---|---|---|
| Linux | Wheels on PyPI for x86-64 and aarch64 with glibc 2.28 or later (manylinux), and x86-64 with musl 1.2 or later (musllinux). Engine, libFLAC and the scarletbook library included. Run-tested on the release's CI runners | [install](docs/install/README.md) |
| macOS | Wheels for Apple Silicon (macOS 11 or later) and Intel (macOS 10.13 or later). Not code-signed or notarised. Run-tested on macOS 15 only, on CI | [install](docs/install/README.md) |
| Windows | Wheel for x86-64: `py -m pip install pysacd`, no compiler needed. Not code-signed. Run-tested on the release's CI runner | [install on Windows](docs/install/windows.md) |
| Anything else | Builds from source with a C compiler and make (libFLAC for FLAC output) | [from source](docs/install/README.md#from-source) · [development](docs/development.md) |

Every wheel is for Python <!-- facts:python-min -->3.10<!-- /facts --> or later and carries a build provenance attestation on its
[GitHub release](https://github.com/dsd-toolbox/pysacd/releases), checkable with
`gh attestation verify`. On any system, pysacd also reads discs over the network from a PS3 or
Blu-ray player running SACD Ripper ([reading from a PS3](docs/ps3-protocol.md#reading-from-a-ps3)).

## Quick start

You need Python <!-- facts:python-min -->3.10<!-- /facts --> or later and an SACD image. The commands use
`album.iso` in the current folder; type your image's name instead. On Windows, get Python from the
[python.org](https://www.python.org/downloads/windows/) installer and tick **Add python.exe to PATH**
on its first screen; the Windows commands are in the box after step 1, and
[install on Windows](docs/install/windows.md) has the details.

1. Install pysacd in a virtual environment of its own (Linux and macOS):

   <!-- docs-run: packages=python3 -->
   ```sh
   python3 -m venv pysacd-env
   . pysacd-env/bin/activate
   # facts:install kind=pip name=pysacd
   pip install pysacd
   # /facts
   ```

   On Windows, open PowerShell and run this instead (it installs into your Python rather than a virtual environment); the rest of the steps work unchanged, with
   your image's path in place of `album.iso`:

   <!-- docs-run: skip="Windows: PowerShell and the py launcher; the same pip install runs on Linux above" -->
   ```powershell
   py -m pip install pysacd
   pysacd --version
   ```

2. Read the disc's table of contents: album, areas and tracks.

   <!-- docs-run: group=discs checkout=1 setup="dnf -y -q --setopt=install_weak_deps=False install python3-pip python3-numpy gcc && pip install -q pysacd && python3 -I tools/docs_fixtures.py --offline-config ~ && mv ~/album.iso ~/disc.iso ~/other.iso ." expect="^Disc:" -->
   ```sh
   pysacd toc album.iso
   ```

   *You should see* `Disc:` with the artist and title, then a line per area and one per track.

3. Extract the stereo area to DSF files, as `sacd_extract` would:

   <!-- docs-run: group=discs expect="Exporting DSF output in folder:" -->
   ```sh
   pysacd-extract -2 -s -i album.iso
   ```

   *You should see* `Exporting DSF output in folder:` and the album's title, then a folder of that
   name with one `.dsf` file per track. On a disc that also has a multichannel area, the tracks are
   in a `Stereo` folder inside it, as `sacd_extract` writes them.

Next: FLAC and WAV with your own naming and MusicBrainz tags in [converting](docs/converting.md),
building from source in [install](docs/install/README.md), a tour of the commands in
the [user guide](docs/user-guide.md), and the [Python API](docs/api.md).

## Documentation

The [documentation index](docs/README.md) lists every document and its sections.

- [Why pysacd](docs/why-pysacd.md): what it brings, a first Python example, and the
  [author's note](docs/author.md)
- [Install](docs/install/README.md): wheels, checking a download, building from source;
  [on Windows](docs/install/windows.md)
- [User guide](docs/user-guide.md): a tour of the commands, and troubleshooting
- [pysacd-extract](docs/sacd_extract.md): migrating from `sacd_extract`, compatibility policy
- [Converting](docs/converting.md): naming templates, tags, MusicBrainz, gapless PCM
- [Python API](docs/api.md) · [reading from a PS3](docs/ps3-protocol.md) ·
  [logging](docs/logging.md)
- [Performance](docs/performance.md) · [security](docs/security.md) ·
  [clean room and licences](docs/clean-room.md)
- [Development](docs/development.md) · [contributing](CONTRIBUTING.md) ·
  [changelog](CHANGELOG.md) · [releases](https://github.com/dsd-toolbox/pysacd/releases) ·
  [releases by major version](https://github.com/dsd-toolbox/dsd-toolbox/blob/main/RELEASES.md)

## Licence and credits

pysacd's code is free software under the [GNU General Public License](COPYING), version 2 or (at
your option) any later version (`GPL-2.0-or-later`). Its DSD-to-PCM filter, `csrc/dsp.c` and
`csrc/dsp.h`, is MIT OR Apache-2.0 ([licences](licenses/README.md)).

The text of this README, the documentation in `docs/` (the PCM quality pages in `docs/quality/`
and the icon files in `docs/assets/` included) and the measurement data published in it are
licensed under [CC BY 4.0](licenses/CC-BY-4.0.txt) (`CC-BY-4.0`). One part keeps its own terms:
the protobuf message definitions under "Messages" in the
[PS3 protocol page](docs/ps3-protocol.md#messages) are adapted from sacd-ripper's
`libs/libsacd/sacd_ripper.proto`, and stay under sacd-ripper's licence, GPL-2.0-or-later.

To cite pysacd, use [CITATION.cff](CITATION.cff).

- [scarletbook-rs](https://github.com/aclater/scarletbook-rs) (MIT OR Apache-2.0, used under MIT)
  reads the images, decodes DST and lays out DSF and DSDIFF files; it is linked into the engine
  with the Rust standard library and its crates (MIT OR Apache-2.0, used under MIT).
- [sacd-spec](https://github.com/aclater/sacd-spec): the specification and corpus scarletbook-rs
  was written from.
- libFLAC, and on Windows libogg (BSD-3-Clause), are linked into the wheels for FLAC output; the
  musllinux wheel carries GCC's `libgcc_s` (GPL-3.0-or-later with the GCC Runtime Library
  Exception). [mutagen](https://github.com/quodlibet/mutagen) (GPL-2.0-or-later) writes tags.
- The first DSD-to-PCM stage uses Sebastian Gesemann's byte-wise lookup-table technique
  (BSD-3-Clause).
- [sacd-ripper](https://github.com/sacd-ripper/sacd-ripper): `sacd_extract`'s behaviour, which
  `pysacd-extract` reproduces, used only as a compiled binary. pysacd speaks the PS3 SACD Ripper
  homebrew's network protocol.
- FFmpeg's DST decoder by Peter Ross (LGPL-2.1-or-later) was the base of pysacd's own DST
  decoder up to 0.2.
- [MusicBrainz](https://musicbrainz.org) and the Cover Art Archive supply tags and covers.

Details in [clean room and licences](docs/clean-room.md) and [licenses/](licenses/README.md).

Part of the [DSD Toolbox](https://github.com/dsd-toolbox/dsd-toolbox).
