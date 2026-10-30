# Changelog

Notable changes in each release of pysacd. Versions follow
[semantic versioning](https://semver.org/); before 1.0, a minor version may change
behaviour, and a patch version only adds or fixes. Each release on
[GitHub](https://github.com/aclater/pysacd/releases) and
[PyPI](https://pypi.org/project/pysacd/) carries signed build provenance (see
[development: packaging and releases](docs/development.md#packaging-and-releases)).

## 0.2.1 (2026-10-29)

- `pysacd convert --pauses` (config `pauses`, `Options.pauses`) includes the pauses
  between tracks, with the same track ranges as `pysacd-extract -b`, so a library
  extracted with pauses gets identical tracks (#62, #63).
- The privacy check in CI also catches private IP addresses and local paths (#61).
- Docs: front ends for `sacd_extract` work with `pysacd-extract` (#60); PyPI install
  instructions (#59).

No security fixes.

## 0.2.0 (2026-10-29)

First release on PyPI.

- A pure-Python reader for the SACD (Scarletbook) table of contents: disc and track
  text in every Scarletbook character set, ISRCs, genres, and plain and raw-sector
  (2048, 2054 and 2064-byte) images.
- A multithreaded C engine that writes DSF, DSDIFF, WAV and FLAC (any mix) in one
  pass over an area, decoding DST on every core with a clean-room-completed decoder
  based on FFmpeg's (see [clean room and licences](docs/clean-room.md)).
- `pysacd-extract`, a drop-in replacement for `sacd_extract`: the same options and
  byte-for-byte the same files, apart from a few
  [documented exceptions](docs/sacd_extract.md#compatibility-policy).
- `pysacd convert`, with MusicBrainz tags and cover art, naming templates, gapless
  24-bit FLAC/WAV, and optional ISRC submission to MusicBrainz.
- Reading straight from a PS3 or player running SACD Ripper, with resume after a
  dropped connection.
- Wheels for Linux (glibc and musl), macOS (Intel and Apple Silicon) and Windows.
- Hardening against hostile input: bounded reads and allocations, a fuzzed decoder
  and demuxer, a corpus of deliberately broken images, and filtering of disc and
  MusicBrainz text before it reaches file names, tags or the terminal (see
  [security](docs/security.md)).
