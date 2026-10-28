# CLAUDE.md: pysacd

Instructions for coding agents working in this repository. Read
[README.md](README.md) first.

## What this project is

A toolkit for SACD (Scarletbook) disc images. It has four parts:

- a pure-Python TOC reader (`src/pysacd/toc.py`);
- a multithreaded C engine (`csrc/`, built by `make`, loaded through ctypes)
  that writes DSF, DSDIFF, WAV and FLAC in one pass per area;
- `pysacd-extract`, a drop-in replacement for `sacd_extract`;
- `pysacd convert`, with MusicBrainz tagging and configurable naming.

## Standing constraints

- **Clean room for DST.** The Philips/ISO MPEG-4 reference DST decoder (and
  anything derived from it, such as sacd-ripper's `libdstdec`) is not
  GPL-compatible and must never enter the repository, its history or its
  issues. Don't read it.
  - DST work is done from [docs/dst-bitstream.md](docs/dst-bitstream.md) and
    FFmpeg's LGPL code only.
  - `sacd_extract` may be used only as a compiled black box for comparisons.
  - Errata go in section 8 of the specification.
- **Licences.** New code is GPL-2.0-or-later. `csrc/dstdec.c` is
  LGPL-2.1-or-later and keeps its header. Add no dependency or copied code
  under incompatible terms.
- **sacd_extract compatibility is byte-for-byte.** For the same options,
  `pysacd-extract` must produce the same files, names and bytes as
  `sacd_extract`: DSF, DSDIFF, edit master, cue, xml and `-P` text. The only
  exceptions are documented in docs/sacd_extract.md: the creating-machine comment and
  version lines, and keeping a different disc out of a folder that
  `sacd_extract` would silently mix or overwrite (#4).
  Any change to `sbcompat.py`, `extract.py` or the writers needs a
  comparison test.
- **Diagnosable failures.** Code that talks to devices, servers or disc
  images logs what it did through the `pysacd.*` loggers: requests, reads,
  positions, timings and errors with their context, at debug level where the
  volume is high. A field report must be diagnosable from the log alone.
  Addresses and paths go only into debug lines (see docs/logging.md).
- **Hostile input.** Disc images and network data may be malformed. Bound
  every length and allocation; a bad frame or sector must not crash or
  corrupt other output.
- **Untrusted text.** Disc TOC text, MusicBrainz data and network input are
  attacker-controlled. Anything that writes or prints them (file names, tags, cue, XML,
  terminal, logs) must go through `pysacd.safetext`, and `tests/test_safetext.py` must cover
  the new path. Never pass such text to a shell, `subprocess` or `eval`. See
  [docs/security.md](docs/security.md).
- **Performance.** The engine is meant to be fast. Measure before and after
  any change to a hot path, using an image copied to local storage, never
  read directly from a network share (other clients skew the numbers). Run
  old and new builds alternately and report the mean.
- **Privacy.** The repository is private, but keep it clean anyway:
  - no hostnames, home-network names, IP plans or personal details in code,
    docs, tests or commit identities;
  - commits are authored as `aclater <aclater@gmail.com>`;
  - CI's `no private names` check enforces this.
- **Packages.** Don't install system packages; ask the owner to.

## Checks before every PR

```bash
ruff check .
mypy src
make && make test-tools
PYTHONPATH=src pytest -q tests
```

CI also runs:

- the tests under ASan and UBSan;
- `tests/test_dst_full.py` and `tests/test_sbcompat.py` against a
  `sacd_extract` built from a pinned upstream commit;
- CodeQL and semgrep. On this private repository these fail on findings
  instead of uploading them.

All of them must pass.

Locally, run the comparison tests against the real binary with
`SACD_EXTRACT=/path/to/sacd_extract`. Tests that need real discs read their
paths from environment variables and skip without them.

## Workflow

- Every task starts with an issue. Claim it with a comment
  ("🤖 Claude Code picking up this issue."), and log decisions and results
  there.
- Work on a branch and open a PR against `master`; never push to `master`
  directly. PRs say `Closes #N`.
- **Docs always ship with the change** (README, docs/, this file, usage text).
- **AI review.** The `ai-review` workflow posts a review from a second model
  on each PR and after each push. It sends the PR's title, description and
  diff to Google's Gemini API, so never put secrets or private details in a
  PR. Answer every finding (fix it, or reply with
  why not) before merging. A red `review` check means every model call
  failed (usually Gemini overload): re-run it once (`/review` as a comment).
  If no review has arrived 15 minutes after the last push, say so in the PR
  and merge when everything else is green. Comments starting with `/` are
  PR-Agent commands; other comments don't start a run.
- **Releases** follow docs/development.md "Packaging and releases". Never
  turn on PyPI publishing (`PYPI_PUBLISH`) yourself: it makes the code public.
- Commits explain why, one logical change each, and end with the
  `Co-Authored-By:` trailer for the model in use.
- Verify every version (actions, images, packages) from its primary source
  before using it. Prefer releases at least two weeks old. Pin actions by
  commit SHA and images by digest.
