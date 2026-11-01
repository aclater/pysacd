# Changelog

Notable changes in each release of pysacd. Versions follow
[semantic versioning](https://semver.org/); before 1.0, a minor version may change
behaviour, and a patch version only adds or fixes. Each release on
[GitHub](https://github.com/aclater/pysacd/releases) and
[PyPI](https://pypi.org/project/pysacd/) carries signed build provenance (see
[development: packaging and releases](docs/development.md#packaging-and-releases)).

## Unreleased

## 0.3.0 (2026-11-01)

pysacd now reads and decodes SACD images through [scarletbook-rs](https://github.com/aclater/scarletbook-rs), linked into the engine from a verified release; pysacd's own TOC parser, demultiplexer and DST decoder are removed (#85). Output is byte-identical to 0.2.2 on the corpus and the real test discs, and beets-pysacd disc fingerprints are unchanged. DSF extraction stays 2.5× (DST) to 3.6–4.0× (plain DSD) faster than `sacd_extract`; DST extraction is up to 15 % slower than 0.2.2's own decoder, which is reported upstream (scarletbook-rs#146). See [performance](docs/performance.md).

- pysacd's own SACD engine is gone (#85, phase 4): its TOC parser (and `PYSACD_TOC_ENGINE`),
  sector demultiplexer and DST decoder, with the DST specification that served them. All of
  it is in the history up to v0.2.2. scarletbook-rs reads and decodes everything, and
  `SACDImage.read_sectors()` reads through it too, so raw-sector images go through one reader.
  All of pysacd is now GPL-2.0-or-later. The fuzzing moves to one harness that feeds hostile
  images through the engine's writers.

- A track far into an area (`-t`, `convert -t`) starts provisionally again (#85): scarletbook
  finds its first frame without reading the area before it, which is exact on a well-formed
  disc and is what 0.2.2 did from the TOC's track positions. Output is byte-identical to 0.2.2.
  The run logs that it started provisionally and checks the start in the background. If the
  check finds it wrong, pysacd warns and suggests `--exact`, the new option that reads the
  area up to the track.

- Audio is now read and DST decoded by scarletbook-rs, on a pool of decoding threads (#85,
  phase 3). Output is byte-identical to 0.2.2 on the real test discs and on the sacd-spec
  conformance corpus, except on four hostile corpus images:
  - an image cut short inside the audio now gives the rest of each track as silence,
    counted as missing frames, where 0.2.2 stopped with a read error;
  - a DST frame the specification calls invalid is now filled with 0x55 and counted as a
    DST error, where 0.2.2 (and `sacd_extract`) decoded it into undefined audio.
  Ctrl-C stops a run within a second, also while scarletbook scans an area on the way to a
  track. An ISO copy (`-I`) is now written first and the audio read from the copy, so a PS3
  is still read once. See [development](docs/development.md#the-audio-through-scarletbook).

- The TOC is now read by scarletbook-rs, through the engine (#85, phase 2). The Disc, Area
  and Track objects are the same as before, field for field: on the sacd-spec conformance
  corpus (125 images read, 11 rejected by both), on real discs, and on a library of 139 ISOs,
  where the beets-pysacd fingerprints are unchanged too. Names, tags, cue sheets, XML, `-P`
  text and `toc --json` don't change. Only the warnings change: they are now the
  specification's diagnostic codes, filed under their area, such as
  `area-toc-copies-differ (2ch area)`. `PYSACD_TOC_ENGINE=python` (or
  `read_toc(path, engine="python")`) uses pysacd's own parser, and `tools/toc_compare.py`
  compares the two. Reading a TOC now needs the native engine, which every wheel has.

- Build: the engine now links the [scarletbook-rs](https://github.com/aclater/scarletbook-rs)
  C library 0.5.1 (C ABI 2) statically, in every wheel and every build from source. This is the
  first step of moving pysacd's SACD engine onto it (#85); nothing calls it yet, and output is
  unchanged. The build downloads the release archive for its target and refuses it unless it
  matches the SHA-256 pinned in `scarletbook.lock`. CI checks that every pin carries scarletbook's
  signed build provenance. `SCARLETBOOK_DIR` builds offline. `pysacd --version` names the
  linked scarletbook release. The engine is larger (about 2.4 MB) and, on x86-64 Linux, no
  longer CET-marked; see [development](docs/development.md#the-scarletbook-library).

- Docs: credit Sebastian Gesemann's dsd2pcm (BSD-3-Clause) for the byte-lookup-table technique in the DSD-to-PCM converter's first stage (`csrc/dsp.c`, [clean room and licences](docs/clean-room.md)).

## 0.2.2 (2026-10-30)

DST discs now extract 2.4–2.6× faster than `sacd_extract` (all threads, 10-core machine) and
3.0× faster on a single core, with byte-identical output. See
[performance](docs/performance.md).

- DST decoding is about 1.8× faster per frame: the prediction filter's older taps are
  computed once per output byte from vector tables, and the arithmetic decoder reads its
  code bits from a 64-bit window. Output is byte-identical (#78).
- DSF and DSDIFF extraction no longer decodes the context frames kept for the PCM
  filters, which saves about 9% of the DST decoding (#78).
- Releases attach signed build provenance for every wheel and the sdist as
  `<file>.intoto.jsonl` assets, verifiable with `gh attestation verify` (#70, #72).
- PyPI metadata: classifiers for Python versions, platforms and topic (#76, #77).
- Docs: `SECURITY.md` expanded (#71, #73); `CHANGELOG.md` and `CONTRIBUTING.md` (#74,
  #75); the author's note (#68, #69); README badges (#76, #77).

No security fixes.

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
