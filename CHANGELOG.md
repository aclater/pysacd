# Changelog

Notable changes in each release of pysacd. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html); before 1.0, a minor version may change
behaviour, and a patch version only adds or fixes. Each release on
[GitHub](https://github.com/dsd-toolbox/pysacd/releases) and
[PyPI](https://pypi.org/project/pysacd/) carries signed build provenance (see
[development: packaging and releases](docs/development.md#packaging-and-releases)).

## [Unreleased]

### Added
- docs/install/windows.md: installing on Windows 11 from PyPI with python.org Python and
  `py -m pip install pysacd` or pipx, the first commands with Windows paths, Windows file names,
  long paths, and the MSYS2 build for developers. The README and docs/install/README.md point
  to it, where they used to give only the MSYS2 build. `tools/docs_commands.py` accepts
  PowerShell blocks skipped with a reason that starts with "Windows".
- `tools/docs_commands.py` runs every command block of the Markdown against a release on
  generated discs (`tools/docs_fixtures.py`). Each block carries a marker of the DSD Toolbox docs
  tools that runs it or says why it can't; the shared `docs-run` workflow reads the same markers
  and runs the blocks against the release on PyPI
  ([development](docs/development.md#docs-commands-against-a-release)) (#215).

### Changed
- The release quality suite (`suites/pysacd.toml`) pins dsd-measure 0.1.2: THD is compared apart
  from SINAD, each output's word length and rate are read from the file and must match between the
  two releases, figures at the measurement's floor or limited by the 24-bit word length are marked,
  and reused reports must name the same dsd-measure and pysacd versions
  ([development](docs/development.md#release-quality-comparison)).
- docs/security.md: the container example builds a local image named `pysacd` and reads
  `album.iso` from the current folder, with no placeholders, as tested (#216).
- docs/sacd_extract.md: the migration creates `~/bin` first; on a new account the link failed
  without it (found by `tools/docs_commands.py`, #215).
- docs/logging.md: the `PYSACD_LOG` example writes `~/pysacd.log`, not a fixed `/tmp` name that
  another user's file can block (found by `tools/docs_commands.py`, #215).
- The engine links scarletbook 1.0.2 (was 0.7.0): the same C ABI and the same output; scarletbook's
  interface is now stable (1.0).
- Documentation (the README's text and the icons included) and published measurement data are
  now licensed CC BY 4.0; the code licence is unchanged (GPL-2.0-or-later, with the DSD-to-PCM filter MIT OR Apache-2.0). The exceptions are
  listed in the README's "Licence and credits". A `CITATION.cff` describes how to cite pysacd.

### Fixed
- docs/install: checking a download of 0.5.5 or earlier with `gh attestation verify --repo` fails
  since the repository moved (HTTP 404 from GitHub's attestation lookup). The page now downloads
  the wheel with its attestation bundle and verifies against the bundle, with the account that
  signed those releases; that works without a GitHub sign-in.
- Building from source on a fresh Windows no longer fails to download the scarletbook library with
  `CERTIFICATE_VERIFY_FAILED`: the download also trusts certifi's root certificates there, now a
  Windows-only build requirement, unless `SSL_CERT_FILE` is set. Roots in the Windows store still
  count ([install](docs/install/windows.md#build-from-source-developers)) (#222).
- The progress count no longer ends above the total ("frames processed: 13 / total frames: 12"):
  the engine counted the PCM warm-up frames before a range and the frame after it. `run_area`'s
  result now carries the final count (`frames_done`, `frames_total`) (#205).
- A multichannel area whose track lengths miss MusicBrainz's by more than 3 s on average (often,
  because MusicBrainz lists the stereo lengths for the multichannel medium) now matches the release
  its disc's stereo area matches, if that release has a multichannel SACD medium on the same disc
  with the same track count. The match carries a warning, and ISRCs are not submitted for it
  ([converting](docs/converting.md)) (#218).
- A one-track area no longer matches any one-track medium whatever its length: an ISRC match now
  needs at least one ISRC (#218).
- A cancelled run (Ctrl-C during `pysacd convert` or `pysacd-extract`, or `sacdconv_cancel()`) no
  longer hangs now and then: the cancel now wakes the engine's threads that wait for a batch, so
  the run ends within a batch, as documented.

### Security
- The private-names check (`scripts/check-private-names.sh`) now fails when it cannot run,
  and refuses a name list that would match every line.

## [0.5.5] - 2026-11-23

`pysacd-extract -z` now splits a gapless chain where `sacd_extract` does, so those files are
byte-identical to its files, and the engine links scarletbook 0.7.0. Other output and speed are
unchanged (release checks, below; [performance](docs/performance.md#scarletbook-rs-070)). The
DSD-to-PCM filter's source files are now MIT OR Apache-2.0.

Release checks for 0.5.5 (docs/development.md, "To release"): `tools/extract_compare.py
--require-sacd-extract` against `sacd_extract` built from CI's pinned commit, 0.5.4 against this
release's source at #195's first reviewed commit, on the same five real discs as for 0.5.3, in all
37 modes (185 runs, 1,386 files). One run differs between the trees: `-z` with pauses on the plain
stereo hybrid disc, whose two files differed from `sacd_extract`'s by the block carry before and are
now identical to them. Of `sacd_extract`'s 698 files, 436 are identical and 262 differ only where
docs/sacd_extract.md says so; none differs otherwise. The later change that keeps the block
`sacd_extract` drops at the end of a chain (#182) reaches none of these discs' runs, and the tests
on generated discs cover it.

### Changed
- The engine links scarletbook 0.7.0 (was 0.6.2): the same C ABI. It moves `-z` chain boundaries
  (below) and changes no other output
  ([performance](docs/performance.md#scarletbook-rs-070)) (#194).
- `tools/extract_compare.py` no longer excuses `sacd_extract`'s `-z` block carry ("known"): a file
  that differs by it is a difference like any other. A `-z` file that `sacd_extract` wrote without
  the last block pysacd keeps counts as documented.
- The DSD-to-PCM filter (`csrc/dsp.c`, `csrc/dsp.h`) is now MIT OR Apache-2.0, relicensed by its
  copyright holder; the rest of pysacd stays GPL-2.0-or-later ([licences](licenses/README.md)).

### Fixed
- `pysacd-extract -z`: a file whose audio, with the bytes carried into it, ends exactly on a DSF
  block (for example a first track of 128 frames) now ends one block earlier, and that block opens
  the next file, as `sacd_extract` does. These files are now byte-identical to `sacd_extract`'s;
  pysacd kept the block in the file before (#182). Where no file follows to take the block (a
  chain that stops before the area's last track), `sacd_extract` drops it, losing that audio;
  pysacd keeps it, by decision. That and a `-z -k` concatenation that holds the area's last track
  stay documented differences (docs/sacd_extract.md).

## [0.5.4] - 2026-11-23

The engine links scarletbook 0.6.2. Output and speed are unchanged (release checks, below;
[performance](docs/performance.md#scarletbook-rs-062)).

Release checks for 0.5.4 (docs/development.md, "To release"), on this release's source before the
version bump: `tools/extract_compare.py --require-sacd-extract` against `sacd_extract` built from
CI's pinned commit, 0.5.3 against this release, on the same five real discs as for 0.5.3, in all
37 modes (185 runs, 1,386 files). No run shows a difference between 0.5.3 and this release. Of
`sacd_extract`'s 698 files, 434 are identical, 262 differ only where docs/sacd_extract.md says so,
and 2 differ by the known `-z` block carry (#182); none differs otherwise.

### Changed
- The engine links scarletbook 0.6.2 (was 0.6.1): the same C ABI, the same output and no measurable
  change in speed ([performance](docs/performance.md#scarletbook-rs-062)) (#191).

## [0.5.3] - 2026-11-21

Two `pysacd-extract -z` cases are now byte-identical to `sacd_extract`'s, and the engine links
scarletbook 0.6.1. Other output is unchanged (release checks, below). DST discs extract faster on
all threads: 9 % less time for a stereo DST disc and 6 % less for a 5.1 one on the machine measured
([performance](docs/performance.md#scarletbook-rs-061)).

Release checks for 0.5.3 (docs/development.md, "To release"), on this release's source before the
version bump: `tools/extract_compare.py --require-sacd-extract` against `sacd_extract` built from
CI's pinned commit, 0.5.2 against this release, on five real discs (plain stereo with 5.1 DST,
hybrid; stereo and 5.1, both DST; stereo DST; plain stereo, hybrid, no disc text; plain stereo,
single-layer) in all 37 modes (185 runs, 1,386 files). 175 runs show no difference between 0.5.2
and this release. The other 10 are the two `-z` modes fixed below, on each disc, where this
release's files are now identical to `sacd_extract`'s. `-P` matches `sacd_extract` on all five. Of
`sacd_extract`'s 698 files, 434 are identical, 262 differ only where docs/sacd_extract.md says so,
and 2 differ by the known `-z` block carry below; none differs otherwise.

Known incompatibility: with `-z`, where a file's audio ends exactly on a DSF block (4,096 bytes
per channel, for example a first track of 128 frames), `sacd_extract` ends the file one block early and moves
that block to the next file, or drops it when no file follows. pysacd keeps the block: its DSF
layouts come from scarletbook, which can't place a file's end there yet. Tracked in #182 and in
docs/sacd_extract.md, with a `-z -k` concatenation that holds the area's last track, which
`sacd_extract` doesn't pad.

### Changed
- The engine links scarletbook 0.6.1 (was 0.5.9): the same C ABI and the same output. The build
  accepts the link that the library's archives now hold beside the shared library, and leaves it
  out (#188).
- The release comparison (`tools/extract_compare.py`) knows one more documented difference from
  `sacd_extract`, found on a real disc with a 5.1 DST area: in a DSDIFF edited master, `sacd_extract`
  writes the TOC's track mute flags on `TrackStop` markers, where DSDIFF 1.5 wants zero, and
  pysacd writes zero. pysacd's output is unchanged. A generated disc with gaps and mute flags
  reproduces it in the `sacd_extract` comparison job (#179).
- The release comparison no longer lets the `-b` modes pass without failing: any difference from
  `sacd_extract` that isn't documented or known fails in every mode. It recognises the `-z` block
  carry above precisely (one block per channel moved to the next file or dropped, every other byte
  the same) and reports it as known. The `-b` row of docs/sacd_extract.md is corrected from
  measurements: `sacd_extract -b` cuts on the TOC's track start sectors; frames repeated in both
  tracks were not reproduced. Two comparison modes are new: `-z` past a track not selected, and
  `-z -k` (#182).
- `TrackStop` markers of a DSDIFF edited master are passed to scarletbook with zero track flags,
  which it wrote anyway; the files are unchanged.

### Fixed
- `pysacd-extract -z` with a track selection that leaves a track out (`-t 1,3`): the bytes left
  over at the end of the file before the gap are no longer carried into the next file;
  `sacd_extract` drops them there, and the next file now starts at its own track, byte-identical
  to `sacd_extract`'s (#182).
- `pysacd-extract -z -k`: the concatenation is written without padding, as `sacd_extract` writes
  it, when it ends before the area's last track. It was padded as without `-z` (#182).

## [0.5.2] - 2026-11-15

Fixes for `pysacd-extract` writing into a folder that already holds files, byte-compatibility with
`sacd_extract` for blank ISRC fields, hardening that followed the review of 0.5.0, and the build
chain. DSF, DSDIFF, cue, XML and `-P` output is unchanged for a disc with ordinary titles and ISRCs
written into an empty folder (release checks, below). FLAC files from the Linux wheels are now
encoded by libFLAC 1.5.0: the audio is the same, the bytes are not.

Release checks for 0.5.2 (docs/development.md, "To release"), on the commit before the version
bump: `tools/extract_compare.py --require-sacd-extract` against `sacd_extract` built from CI's
pinned commit, 0.5.1 against this release, on four real discs (stereo and 5.1, both DST; plain
stereo with 5.1 DST; plain stereo; plain stereo with no disc text) in all 35 modes (140 runs, 872
files): no difference between 0.5.1 and this release in any run, and `-P` matches `sacd_extract` on
all four. Of `sacd_extract`'s 450 files, 270 are identical and 177 differ only where
docs/sacd_extract.md says they do. Three differ otherwise, in 0.5.1 exactly as in this release: the
multichannel edit master of the disc with a 5.1 DST area (60 bytes in length and about 3,300 bytes,
from the header on), which is not explained and is tracked as #179; and two DSF files of the
no-text disc in the `-z -b` mode, a mode the comparison reports without failing because
`sacd_extract` repeats frames at some boundaries. The full suite passes on a Fedora 44 machine (358
passed, the 21 expected skips). The changes since 0.5.1 had a security review before the release;
its three points are fixed in this release.

### Fixed
- `pysacd-extract` warns when a file already in the album folder can't be read for its tags: it is
  matched by name only, and replaced if the run writes that file. docs/sacd_extract.md says how
  files without tags are matched.
- `pysacd-extract` on Windows: a disc titled with a device name such as `CON` no longer fails the
  run; its folder and files get a leading `_` (`_CON`), as `pysacd convert` names them. Other
  systems keep `sacd_extract`'s names (#156).
- `pysacd-extract` no longer writes one track over another when their `sacd_extract` names would be
  the same file: titles that differ only in case on a case-insensitive filesystem (Windows, macOS
  by default), or equal titles of tracks past 99. The later track is written as `<name> (2)`
  (#156). See [the differences from sacd_extract](docs/sacd_extract.md#compatibility-policy). The
  filesystem is asked once per name, so a disc with many such titles doesn't slow the run.
- `pysacd-extract` writes into an album folder that holds this disc's own files from an earlier run in
  another mode, as `sacd_extract` does. An edit master (`-e`) or cue sheet (`-C`) run after the DSF or
  DSDIFF tracks, tracks after an edit master, a second `-e`, another `-t` selection or a `-k`
  concatenation went to `<album> [<catalogue no.>]` instead, as if the folder held another disc.
  Another disc's files still keep a disc out of the folder (#165).
- `pysacd-extract` keeps two editions of an album apart when one has ISRCs and the other doesn't
  but their titles and numbering are the same. With `sacd_extract`'s naming, the second replaced
  the first's files, with only a warning that their sizes differed. An ISRC on one side only now
  counts as a difference wherever the file would carry the track's ISRC: `sacd_extract`'s ID3
  modes 1, 3 and 4 (the default) and pysacd's own tags. The minimal ID3 modes (2 and 5) carry no
  ISRC, so their files are compared without it (#171;
  [the rule](docs/sacd_extract.md#compatibility-policy)).
- `pysacd-extract` writes a track's ISRC field as `sacd_extract` does when the disc leaves it blank
  or fills it only in part: as stored up to its first NUL, spaces kept. A field of spaces was
  written as an empty TSRC frame, left out of the cue sheet, XML and `-P`, and padded ones were
  trimmed, so the DSF, DSDIFF, edit master, cue, XML and `-P` output differed from
  `sacd_extract`'s. `pysacd convert` and pysacd's own tags still carry the ISRC itself, and a
  blank ISRC still counts as none when telling discs apart (#175). In `-P`, control characters in
  the field are shown as escapes, and a disc with such a field is still recognised as itself on a
  second run.

### Security
- Cover downloads follow redirects only to `coverartarchive.org` and `archive.org` hosts. A
  redirect anywhere else is refused and reported as a cover-art warning; the run continues
  without a cover (#155). The redirect must name its host plainly (no user part, escapes or other
  port), and a redirect that can't be parsed is reported as an error instead of ending the run
  with a traceback (#172).
- Reading a MusicBrainz or cover response stops with an error on anything that isn't bytes.
  The test suite can run under a memory limit (`PYSACD_TEST_MEMORY_MB`), and CI's `tests` job does.

### Changed
- The engine's fuzz harness also covers ranges that don't start at frame 0 in every format, DSF
  chains with a head frame, the ISO copy, cancellation, 176.4 kHz PCM, FLAC output, other thread
  counts and `allow_filler`: an input can carry a run configuration before the image, and the seed
  corpus has a configured run for each. CI replays the seeds under TSan as well as ASan and UBSan
  (#157).
- The macOS wheels' support is stated as it was tested: built for macOS 11 (arm64) and 10.13
  (x86-64), run on macOS 15 on both; older versions are untested (#142).
- The release-tag check runs in a Fedora container like the other release jobs, and on pull
  requests it also checks its clone, so its set-up is tested before a tag depends on it (#167).
- `licenses/` names the GCC runtime library (`libgcc_s`) that the musllinux wheel carries, with its
  licence texts (#167).
- The release workflow refuses a `v*` tag that isn't `v` plus the package version, or whose commit
  isn't on `master` or a `release/*` branch, before anything is built or published (#154).
- The Linux wheels link libFLAC 1.5.0, built in the job from its release source with the SHA-256
  checked, as the macOS wheels do, instead of bundling the build image's libFLAC and libogg (1.3.2
  in the manylinux wheels, 1.4.3 in the musllinux wheel). FLAC encoding in them is now multithreaded,
  as on macOS and Windows. The audio is the same; the encoder's vendor string in each FLAC file
  names the new version (#153).

## [0.5.1] - 2026-11-14

A security release. A private review of 0.5.0, asked for by the maintainer, found three problems
that a crafted disc image can trigger, and a number of smaller ones; all are fixed here. **Upgrade
if you convert images you did not rip yourself.** Two defaults change with the fixes:

- a run now stops when it would write mostly silence or filler for audio the image lacks (more than
  10 seconds beyond the audio it found). `--allow-silence` gives the old behaviour for a run, to
  rescue what a damaged rip still holds;
- `pysacd-extract --templates` keeps files that already exist; `--overwrite` replaces them.

Output for ordinary discs is unchanged, byte for byte (release checks, below).

### Security

- **Disc text could place `pysacd-extract`'s output outside the output folder**
  (advisory GHSA-gx45-h8q4-vwmc).
  For some titles made only of dots, spaces and control characters, the folder or file name derived
  from the title was empty, `.` or `..`. Such names are no longer produced, a relative `-o` starting
  with `../` is used as given, and every output path is checked against the output folder before
  anything is created. Names of ordinary discs don't change
  ([what differs from sacd_extract](docs/sacd_extract.md#compatibility-policy)). Upgrade to 0.5.1.
  Fixed with it: a `pysacd convert` name could start with `-` after an empty `[]` or `()` was
  removed, and `pysacd toc --json` wrote non-ASCII control characters from disc text to the terminal
  as they were (it now writes ASCII, with `\u` escapes).
- **A disc could replace another disc's files without a warning**
  (advisory GHSA-whv9-98xg-4xw9).
  `pysacd convert` writes a disc to `<folder> [<catalogue number>]` when the folder holds a different
  disc; with a very long album title the catalogue number was cut off and the disc went into the
  first folder after all. The number is now always kept, and the run stops if no separate folder can
  be named. `pysacd-extract --templates` replaced existing files; it now keeps them and reports them
  as skipped, and the new `--overwrite` replaces them. With `sacd_extract`'s naming nothing changes:
  a re-run replaces its files, and [the documentation](docs/sacd_extract.md#compatibility-policy) now
  says what tells two discs apart there. Upgrade to 0.5.1.
- **A crafted image could make one run write far more than the image could hold**
  (advisory GHSA-q45f-7qmf-37x3).
  The bound added for #17 held each output to what the image's sectors could hold, but not the whole
  run, and frames missing from the image were written as silence without limit, so a small image
  could fill a disk. Affects 0.5.0 and earlier; 0.5.1 fixes
  it. The outputs of one format in a run may now cover the area at most twice (an edit master and its
  tracks), checked before anything is written and again in the engine, and a run stops once it has
  written more than 10 seconds of silence beyond the audio it found. Ordinary discs, and truncated
  rips whose run still has more audio than silence, extract as before, byte for byte
  ([security](docs/security.md#hostile-images), [sacd_extract differences](docs/sacd_extract.md)).
- **Network and credential hardening**, found in the same review (low severity, no advisory):
  - MusicBrainz ids (release, release group, track, recording, artist) are kept only when they are
    MBIDs, and the ISRC plan escapes the release id it prints. A `--mb-release` value (or
    `mb_release` in the config) that isn't an MBID is now an error before any request.
  - MusicBrainz, Cover Art Archive and OAuth requests have a deadline for the whole request (60 s,
    300 s for covers), and network errors while reading a reply are retried and reported as
    MusicBrainz errors instead of escaping as other exceptions. A malformed ISRC lookup reply no
    longer raises `AttributeError`, and a token reply's `expires_in` must be a sane number.
  - Requests use https only and follow at most 5 redirects, each to https; the token exchange and
    the ISRC submission follow none, so credentials only go where they were meant to
    ([security](docs/security.md#network-responses)).
  - The MusicBrainz token is written through a new file with an unpredictable name, mode 0600 from
    creation, and is not saved in a folder that other users can write.
  - The log no longer records the value of `mb-login --client-secret`.
  - An empty or relative `XDG_CONFIG_HOME` is ignored (as the XDG spec says), instead of making
    pysacd read `pysacd/config.toml` from the current directory.
  - A copy from a PS3 must average at least 256 KiB/s after a 300 s allowance, so a server can't
    stretch it out indefinitely; real PS3s read 2-4 MB/s.
- **Output hardening**, found in the same review (low severity, no advisory):
  - frames that don't decode now count with the silence in the limit above, and the new
    `--allow-silence` (`pysacd-extract` and `pysacd convert`) lifts that limit for a run, to rescue
    what a damaged rip still holds;
  - a failed or cancelled `-I` no longer leaves a partial `.iso` under its final name;
  - a WAV track over 4 GiB is refused before the run starts, instead of failing at the end and
    taking the run's finished tracks with it;
  - `pysacd convert` checks every output path against the output folder, as `pysacd-extract` does;
  - `pysacd-extract` warns when a re-run replaces files of a different size, and with `--templates`
    when a folder already holds another disc's tracks; a run that skipped every file exits with 1;
  - before reopening a temporary file by name, pysacd checks it is still its own regular file;
  - an engine call that returns no result is a failure, not a success.

Release checks for 0.5.1 (docs/development.md, "To release"): `tools/extract_compare.py
--require-sacd-extract` against `sacd_extract` built from CI's pinned commit, 0.5.0 against the
fixes, on three real discs (stereo and 6-channel DST; plain stereo with 6-channel DST; plain stereo)
in all 35 modes (105 runs, 597 files): no difference between 0.5.0 and the fixes in any run. Against
`sacd_extract`, of its 319 files 191 are identical and 128 differ only where docs/sacd_extract.md
says they do; none differs otherwise, and `-P` matches on all three. That run was made before the
one-worker default below (#137) was merged in; with it, the seven plain-DSD and mixed modes were run
again on the two discs with plain areas (14 runs): no difference. The fixes were developed
privately, where CI doesn't run, so these were run by hand on the release tree before it was
published: the full suite (312 passed, the 16 expected skips), the whole suite under ASan and UBSan
(clang), the `sacd_extract` comparison tests, the engine leak check (memcheck), ruff and mypy. The
beets-pysacd test suite passes against this release's code. Not run by hand: macOS and Windows,
CodeQL, semgrep and TSan, which the release pull request's CI ran. That CI found one defect the
private checks could not: on Windows, a title that sanitises to a name ending in a dot or space
made the run fail (Windows drops them from folder names); such names lose them on Windows now. It
also found three tests that assumed POSIX or Python 3.11.

### Changed

- **CI builds and tests in Red Hat-derived containers.** The test suite on Python 3.10 to 3.15, the
  ASan/UBSan, leak, fuzz, `sacd_extract` comparison and benchmark jobs, the live MusicBrainz lookup
  and the release's sdist run in a Fedora 44 container; semgrep, zizmor and the private-names check
  in UBI 10, with semgrep and zizmor installed from PyPI by hash instead of their vendors' images.
  CI only; the wheels and the package are unchanged ([development](docs/development.md#ci)).
- **Plain DSD to DSF or DSDIFF uses one worker thread by default (#137).** Nothing is decoded or
  filtered there, so a pool the size of the CPU only cost wake-ups. On a 32-thread machine, a
  40-minute stereo disc from the page cache to local NVMe (`-2 -s`, 15 alternating runs, power
  profile `performance`): engine time 0.17–0.21 s with one worker against 0.20–0.23 s with 32
  (medians of three series), the same bytes out. `--threads` and `-j` still set the count. Not
  measured: multichannel plain DSD.
- **Python 3.10 is supported again (#140)**, so pysacd installs with Ubuntu 22.04's own Python. On 3.10 the
  config file is read with `tomli` (installed only there), and CI runs the suite on 3.10 too. The
  wheels are unchanged (one per platform serves every Python).
- **The engine builds with GCC 11** (Ubuntu 22.04's). GCC before 12 stopped with "no dispatcher found
  for the versioning attributes" on the PCM filters' load-time choice of AVX-512 or AVX2; such a
  compiler now builds them for the baseline only. Wheels are built with a newer GCC and keep the choice.
  Run on Python 3.10.12 and GCC 11.4 in an Ubuntu 22.04 container, as an ordinary user: ruff, mypy, the
  engine with `-Werror`, and the whole suite (267 tests, the expected 16 skipped) pass.
- `docs/quickstart.md` now gives the minimums: Python 3.10, and for wheels glibc 2.28, musl 1.2,
  macOS 11 (arm64) or macOS 10.13 (x86-64).
- **The macOS wheels run on macOS 11 and later (Apple silicon) and 10.13 and later (Intel)** (#142),
  instead of macOS 15 only. They link libFLAC 1.5.0, the release Homebrew ships, built in CI from its
  release source with the SHA-256 checked, statically, for those versions.

- **The engine links scarletbook-rs 0.5.9** (digests checked against the release's `.sha256` files
  and attestations; the CET shadow-stack mark is kept). Output is byte-identical to the 0.5.8 build in
  every mode on the five real test discs; speed is in docs/performance.md. 0.5.9 has nothing new for
  pysacd to adopt: its additions are Rust-only (`TrackAudioCursor::with_dst_lanes`) or documentation
  of how to write per-track DSF files, whose writer is planned for scarletbook 0.6.0 (scarletbook-rs#202),
  so pysacd's per-track writer stays until then.
- **A stale engine is refused with a rebuild message.** `src/pysacd/_sacdconv.so` is a build product that
  git ignores; one built before `sacdconv_cancel` was added failed with `AttributeError: undefined
  symbol: sacdconv_cancel`, because pysacd bound that function before it checked the engine's ABI.
  It now checks the ABI first, so the error says `engine ABI N, this pysacd needs M; rebuild it
  (make clean all)`. A clean `make` fixes such a tree (docs/development.md).

## [0.5.0] - 2026-11-05

0.5.0 is mostly about trust in what 0.3.0 to 0.4.3 shipped. **For those six releases (0.3.0, 0.3.1,
0.4.0, 0.4.1, 0.4.2 and 0.4.3), CI did not compare pysacd with `sacd_extract` at all.** From #86 a check for the engine looked for a
function the engine no longer exported, so 44 tests skipped, every comparison among them, in a job
that stayed green. Fixing that (#131) turned the comparison on again, and the comparison found two
differences from `sacd_extract`: the last line of `-P` output (fixed,
below) and the pad byte after odd-length text in a DSDIFF file, which `sacd_extract` leaves
uninitialised (documented, docs/sacd_extract.md). The by-hand run on real discs for this release, below,
found no other audio difference beyond what the docs already list, such as `-b`'s frame repeats. What changes for users:

- **`-P` is now identical to `sacd_extract`'s.** `pysacd-extract -P` ends its output with the final
  empty line `sacd_extract` prints. A script that compared the last line of `-P` output will see it.
- **The engine links scarletbook-rs 0.5.8** (see below). Output is unchanged, and so is speed.
- **Python 3.15 is tested in CI** (#132), in the Linux test matrix; `requires-python` stays `>=3.11`.
- **CI checks the C engine for leaks** (#122): LeakSanitizer on all 97 synthetic seeds, twice, and
  valgrind memcheck on six of them, on every pull request. It found none on master, and failed on a leak injected for the test. An informational benchmark job (#123) reports each pull request's speed against
  its base; it never fails a build.
- **The `sacd_extract` comparison runs again, and compares more** (#131), as below.

Release checks for 0.5.0 (docs/development.md, "To release"): `tools/extract_compare.py
--require-sacd-extract` against `sacd_extract` built from CI's pinned commit, 0.4.3 against this
tree, on the five real test discs in all 35 modes (175 runs): the audio files, ISO copies, cue
sheets and XML match 0.4.3 byte for byte in 170 runs, and the only difference between the trees is
`-P`'s final empty line, in the five `-P` runs, where 0.5.0 now matches `sacd_extract` and 0.4.3
did not. Against `sacd_extract`: 472 of 493 DSF files identical; the DSDIFF files, cue sheets and XML
files differ only where docs/sacd_extract.md says they do (262 of 266, 17 of 17, 20 of 20); 10 of
10 ISO copies identical. The 25 other files are all from `-b` runs on one raw-sector disc
(converted with `-I` first), where `sacd_extract` repeats frames at some track boundaries and,
with `-z`, shifts the later tracks: documented, and identical for 0.4.3 and 0.5.0. A wheel built
from master, installed in clean Fedora 44 containers on Python 3.11 and 3.15, UBI 9 on Python 3.11
and UBI 10 on Python 3.14, extracts the real hybrid disc to the same 12 DSF files as the
reference checksums. (UBI offers no Python 3.15; Fedora 44 covers it.) The published 0.5.0rc1, installed from PyPI in clean containers, did the same on Fedora 44 (Python 3.11 and 3.15), UBI 9 (3.11), UBI 10 (3.12), Debian 13 (3.13), Ubuntu 26.04 (3.14) and Alpine 3.22 (3.12, musl).

- The engine links scarletbook-rs 0.5.8 (digests checked against the release's `.sha256` files and
  attestations; the CET shadow-stack mark is kept). 0.5.8 fixes a seek-index loader case and adds a
  `bench` report in scarletbook itself; it changes no speed. Output is byte-identical to master before #133
  (0.4.3, on 0.5.7) in every mode on the five real test discs (175 runs, 0 differences). Wall s,
  medians of 3, alternating, output to `/dev/shm`: DST stereo to DSF on one thread 52.14 (0.4.3:
  52.16), on all threads 5.37 (5.36), DST 6-channel on all threads 19.89 (19.84), plain DSD on
  all threads 0.90 (0.93), all within the runs' spread (docs/performance.md).

- **The comparison with `sacd_extract` runs in CI again, and compares more (#131).** From #86 until
  now, so for every release from 0.3.0 to 0.4.3, a test's check for the engine looked for a function the engine no
  longer exported: 44 tests skipped, among them every comparison, in a job that stayed green. Even
  before that, the job compared decoded audio only. It now also compares whole output trees in
  every compatible mode, names, ID3, DSDIFF, cue, XML and `-P`, on generated discs. Real discs run
  by hand before each release; docs/development.md says what each covers.
- CI fails if a test starts skipping, stops skipping or changes its reason (`tests/expected-skips.json`).
- `pysacd-extract` ends its output with the final empty line `sacd_extract` prints, so `-P` is now
  identical.
- Documented a difference from `sacd_extract` that the comparison found: the pad byte after
  odd-length text and ID3 tags in a DSDIFF file, which `sacd_extract` leaves uninitialised.
- `tools/extract_compare.py` reports each run against `sacd_extract` per file format, and
  `--require-sacd-extract` fails on an unexplained difference or a `sacd_extract` that didn't run.
- Tests: the frame tests of `tests/test_dst_full.py` call scarletbook's `sb_dst_decode`, since the
  decoder they called was removed in 0.3.0. They pass unchanged. The sector-source test now runs
  the whole hostile corpus through a recording source, and a new test checks that a run exiting 0
  wrote everything the TOC promises. An area claiming to run past the end of the image is no longer
  refused before reading, since 0.3.0: the sectors that exist are read, and a source is still
  never asked for one that doesn't.

## [0.4.3] - 2026-11-02

The release candidate 0.4.3rc1, installed from PyPI, passed `tests/wheel_smoke.py` on clean
Fedora 44, UBI 10 and Debian 13 containers.

- The engine links scarletbook-rs 0.5.7 (digests checked against the release's `.sha256` files and
  attestations; the CET shadow-stack mark is kept). 0.5.7 wins back 0.5.6's one-thread stereo DST
  regression (scarletbook-rs#241), frees a raw image's TOC read buffer once opened (#228) and
  reads areas without a per-sector copy (#230). Output is byte-identical to master (0.4.2, on
  0.5.6) in every mode on the five real test discs (175 runs, 0 differences). Wall s, medians
  of 3, alternating, output to `/dev/shm`: DST stereo to DSF on one thread 32.94 (0.4.2: 33.50),
  on all threads 2.51 (2.57), DST 6-channel to DSF on all threads 9.24 (9.34), plain DSD to DSF
  on all threads 1.13 (1.16), mostly within the runs' spread (docs/performance.md).

## [0.4.2] - 2026-11-02

The release candidate 0.4.2rc1 passed its clean-machine smoke test (Fedora 44 and Debian 13 at
20/20, UBI 10 at 19/20 as before).

- Faster plain-DSD extraction to DSF (#114): the engine fills each DSF file from scarletbook's
  layout (`sb_output_layout_fill`, scarletbook 0.5.6's AVX2 fill), audio included, and reads
  decoded frames straight into its batches. pysacd's own DSF block layout is deleted. Plain-DSD
  stereo to DSF on local NVMe takes 0.73 s instead of 1.54 s (2.1×); to `/dev/shm` 0.85 s
  instead of 0.93 s, bound by the kernel's copies; DST unchanged (−1.5 %). Output is
  byte-identical in every mode on the five real test discs (docs/performance.md). Engine ABI 6:
  `sc_output_t` carries the DSF layout, and `head_bytes` and `tail_bytes` are gone;
  `sbwriter.dsf` returns the layout itself (`DsfLayout`), which `OutputSpec.layout` takes.
- The engine links scarletbook-rs 0.5.6 (digests checked against the release's `.sha256` files and
  attestations; the CET shadow-stack mark is kept). Output is byte-identical in every mode on
  the five real test discs (175 runs). 0.5.6's faster plain-DSD-to-DSF and DSD-to-PCM paths
  are not on pysacd's path, so extraction does not speed up: plain-DSD stereo to DSF 1.82 s
  (0.5.5: 1.76 s), DST stereo to DSF 2.88 s (2.78 s), DST 6-channel to DSF 9.40 s (9.30 s),
  all threads, medians of 3, within the runs' spread (docs/performance.md). The engine still
  accepts scarletbook 0.5.4.

## [0.4.1] - 2026-11-02

The release candidate 0.4.1rc1 installed and passed its tests on clean machines, matching 0.4.0
(a Fedora 44 VM and Debian 13 at 20/20, UBI 10 at 19/20 as before).

- Faster DST decoding (#109): the engine links scarletbook-rs 0.5.5, whose DST decoder runs an
  AVX2/BMI2 build of itself, chosen at run time, on x86-64 CPUs that have those features. DST
  stereo to DSF on all threads of a 16-core machine takes 2.34 s instead of 2.79 s (−16 %, 68
  CPU-s instead of 82), within 4 % of scarletbook's own `bench decode`; one thread −12 %;
  multichannel unchanged. Output is byte-identical in every mode (docs/performance.md). The
  engine still accepts scarletbook 0.5.4.

## [0.4.0] - 2026-11-01

The release candidate 0.4.0rc1 passed the clean-machine matrix with no regression against
0.3.1: 10 Linux VMs and Windows 11 at 20/20, and 12 containers matching the baseline.

- Workaround (#93): when an area's only missing frame is its last, and the frame before it is
  DSD silence, `pysacd-extract` fills it with silence as before, warns, and exits 0 instead of 1. Silence means that, in every 384-bit window of every channel, the count of ones is within 2 of half.
  Any other missing frame still fails. This is a workaround for what was seen on early Sony discs (Master TOC 1.00, pressed 2000–2001; four discs of four titles), whose last area sector holds no audio sector header. It may be revisited if other discs show the pattern.

- DSF and DSDIFF files are laid out by scarletbook-rs 0.5.4's output API (#94). Every byte of
  them that isn't audio (the DSF header, the DSDIFF chunks before and after the sound data)
  now comes from scarletbook, with the writer's choices that give `sacd_extract`'s bytes
  (scarletbook-rs#169), and pysacd's own DSF and DSDIFF writers are deleted. `sacd_extract`'s
  ID3 tags, markers and comments stay pysacd's, as content. Output is byte-identical to 0.3.1
  in every mode on the real test discs, `-z` included. Speed is unchanged within the runs'
  spread (+0.2 % for DST to DSF; docs/performance.md). The engine now needs scarletbook 0.5.4.
- A file scarletbook won't lay out from a hostile or damaged TOC (an empty track, a range past
  the area, an impossible date) is reported and not written, and the run exits with 255.
  0.3.1 wrote it from the TOC as it stood. In a DSDIFF file that keeps DST, a marker past the
  frames the file holds (when unreadable frames are left out) is left out too
  (docs/sacd_extract.md).
- Tools: `tools/extract_compare.py` has 12 more modes, the writer's choices one by one (ID3
  modes, `-z` on a selection and with pauses, DSDIFF with pauses, a multichannel edited
  master). `tools/output_compare.py`, which compared pysacd's files with scarletbook's layouts,
  is removed: the layouts are now the files'.

## [0.3.1] - 2026-11-01

pysacd moves onto scarletbook-rs 0.5.2 (#94). Reading a TOC is 4.9 times faster than in 0.3.0, as fast as 0.2's own parser was; DST extraction to DSF is a little faster; on x86-64 Linux the engine is marked for CET shadow stacks again. Output is byte-identical to 0.3.0. The release candidate 0.3.1rc1 passed the clean-machine matrix, identical to 0.3.0's baseline: 10 Linux VMs and Windows 11 at 20/20 each, and 12 containers (7 at 20/20; 5 at 19/20, where the image has no pipx package, as with 0.3.0).

- Docs and tools: `tools/output_compare.py` checks pysacd's DSF and DSDIFF files against
  scarletbook 0.5.2's output API (#94). The DSF header matches on every file it can describe.
  No DSDIFF file matches, mainly because of `sacd_extract`'s `ID3 ` chunks. pysacd keeps its own
  writers, and the missing choices are filed upstream (scarletbook-rs#169). Output is
  unchanged.

- DST extraction to DSF is a little faster (#94): scarletbook's decoding pool now has one thread
  fewer than `--threads` (the CPU count by default), leaving the reader thread a CPU of its own,
  as scarletbook recommends (scarletbook-rs#146). It now takes 3.82 s against 3.96 s for the
  50-minute stereo DST disc, +12.5 % against 0.2.2 (it was +16.7 %). Output is unchanged.

- Reading a TOC is 4.9 times faster (#94): 0.125 ms per disc against 0.618 ms in 0.3.0 (median
  of the 5 test discs), as fast as 0.2's own parser was. The TOC comes from scarletbook in one
  call (`sb_disc_toc`) instead of about a hundred, and a path is read by the library itself.
  The TOC is unchanged: every object and `toc --json` are identical to 0.3.0's on the
  conformance corpus, 3,000 TOC mutations, the real test discs and a library of 291 ISOs, and
  beets-pysacd fingerprints are unchanged. The engine now needs scarletbook 0.5.2.

- The engine links scarletbook-rs 0.5.2 (#94). Output is unchanged. On x86-64 Linux the engine
  is again marked for CET shadow stacks (SHSTK), as it was up to 0.2.2, because scarletbook's
  library now is: a process running with shadow stacks can load it. CI checks the mark on the
  engine and on the x86-64 manylinux wheel (`tools/check_cet.py`). The musllinux wheel stays
  unmarked, since musl never uses shadow stacks.

- Docs: the author's note gets an update at the bottom for 0.3.0 (scarletbook-rs, sacdfs); the
  original text is kept as written.

## [0.3.0] - 2026-11-01

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

## [0.2.2] - 2026-10-30

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

## [0.2.1] - 2026-10-29

- `pysacd convert --pauses` (config `pauses`, `Options.pauses`) includes the pauses
  between tracks, with the same track ranges as `pysacd-extract -b`, so a library
  extracted with pauses gets identical tracks (#62, #63).
- The privacy check in CI also catches private IP addresses and local paths (#61).
- Docs: front ends for `sacd_extract` work with `pysacd-extract` (#60); PyPI install
  instructions (#59).

No security fixes.

## [0.2.0] - 2026-10-29

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

[Unreleased]: https://github.com/dsd-toolbox/pysacd/compare/v0.5.5...HEAD
[0.5.5]: https://github.com/dsd-toolbox/pysacd/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/dsd-toolbox/pysacd/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/dsd-toolbox/pysacd/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/dsd-toolbox/pysacd/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/dsd-toolbox/pysacd/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/dsd-toolbox/pysacd/compare/v0.4.3...v0.5.0
[0.4.3]: https://github.com/dsd-toolbox/pysacd/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/dsd-toolbox/pysacd/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/dsd-toolbox/pysacd/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/dsd-toolbox/pysacd/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/dsd-toolbox/pysacd/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/dsd-toolbox/pysacd/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/dsd-toolbox/pysacd/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/dsd-toolbox/pysacd/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/dsd-toolbox/pysacd/releases/tag/v0.2.0
