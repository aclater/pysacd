# Contributing to pysacd

Thanks for helping. Bug reports, logs from real players, test images and pull requests
are all welcome.

## Reporting a bug

Open an [issue](https://github.com/aclater/pysacd/issues). Include the command you ran,
what you expected, what happened, and a log: run again with `--log-file pysacd.log` and
attach it (see [logging](docs/logging.md) for what it records). Logs from a real PS3 or
Blu-ray player running SACD Ripper or AutoScript are especially useful (#10).

**Security problems** go through a private report instead, never a public issue: see
[SECURITY.md](SECURITY.md).

## Changing code

1. **Start with an issue,** so the change can be discussed first.
2. **Work on a branch and open a pull request** against `master`. The default branch only
   accepts pull requests whose checks pass.
3. **Run the checks before you push:**

   ```bash
   ruff check .
   mypy src
   make && make test-tools
   PYTHONPATH=src pytest -q tests
   ```

   CI also runs the tests under ASan, UBSan and TSan, compares the output with a
   `sacd_extract` built from source, and runs CodeQL, semgrep and the fuzzers. See
   [development](docs/development.md) for building, testing without a PS3, and the full
   list of CI jobs.
4. **Add tests with every change:** a regression test for a bug, tests for new
   behaviour. Any change to what `pysacd-extract` writes needs a comparison test against
   `sacd_extract`.
5. **Update the docs in the same pull request** (README, `docs/`, usage text), and add
   an entry to the [changelog](CHANGELOG.md) for anything a user would notice.
6. **Answer the review.** An AI reviewer comments on every pull request; reply to each
   finding (fix it, or say why not).

## Rules that keep the project clean

- **Clean room for DST.** Never read, copy or adapt the Philips/ISO reference DST
  decoder or anything derived from it, such as sacd-ripper's `libdstdec`. DST decoding
  is [scarletbook-rs](https://github.com/aclater/scarletbook-rs)'s, a separate clean room:
  changes it needs go to it as issues, never as code. See
  [clean room and licences](docs/clean-room.md).
- **Licences.** Code is GPL-2.0-or-later. Add no dependency or code under incompatible
  terms.
- **Untrusted input.** Disc images, network data, disc text and MusicBrainz data are
  hostile: bound every length, and send any text that is written or printed through
  `pysacd.safetext` (see [security](docs/security.md)).
- **Privacy.** No host names, private addresses, local paths or personal details in
  code, tests, docs or commits. CI checks for some of these.

Contributions are accepted under the project's licence,
[GPL-2.0-or-later](COPYING).
