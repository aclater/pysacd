# pysacd

Read and convert SACD (Scarletbook) ISO images from Python, without running
`sacd_extract` as a subprocess.

- `pysacd.read_toc()` is a pure-Python TOC reader with no dependencies.
- `pysacd convert` extracts tracks to DSF (native DSD, the default), FLAC or
  WAV (24-bit PCM). A small C engine does the audio work, and the files are
  tagged from MusicBrainz and the Cover Art Archive.
- `pysacd-extract` is a drop-in for `sacd_extract`: the same options, the same
  files and names, and the same bytes (see Validation). Extra features are
  available as long options.

The layouts are ported from [sacd-ripper](https://github.com/sacd-ripper/sacd-ripper)'s
`libs/libsacd/scarletbook.h` and `scarletbook_read.c` (GPL-2.0-or-later).

## Validation

On 2026-10-28 the TOC reader was checked against `sacd_extract -P`, built from
sacd-ripper's current source, on 116 real SACD ISOs. Every field that
`sacd_extract` prints matched on all 116: disc and album text, catalog
numbers, genres, dates, area totals and speaker config, and per-track
text, start times, durations and ISRCs. The test set included stereo and
multichannel areas, DST and plain-DSD areas, and US-ASCII and ISO-8859-1
text. The 12 ISOs with raw 2064-byte sectors could not be compared,
because `sacd_extract` fails on them.

Audio, also on 2026-10-28:

- DSF output was bit-identical to `sacd_extract -s` for plain-DSD stereo, DST
  stereo and DST 5.1 tracks.
- PCM output matched ffmpeg's DSD decoder: the same level, a 1-sample offset,
  and a −104 dB difference below 20 kHz.
- Tracks converted separately joined to within ±2 LSB (the dither) of a single
  continuous conversion.

`pysacd-extract` vs `sacd_extract`, run with the same options and compared file
by file:

- **Identical:** `-s`, `-s -z`, `-s -k -t`, `-s -b -t`, `-2 -m -s` (DST) and
  `-A -a` produced byte-identical trees. Those cover the ID3 tags and the
  nopad carry-over.
- **Identical apart from the creating-machine string:** `-p` (DSD), `-p`
  (DST passthrough), `-p -c -m`, and `-e -C` (a 930 MB DST edit master with
  its cue and xml).
- **Further identical cases:**
  - `-P` on all 99 plain-SACD ISOs.
  - `sacd_extract.cfg` driving artist, performer, nopad, pauses, concatenate
    and id3tag 0/1/3.
  - `-o`, `-y` and a missing output directory (same exit status).
  - `-z -t`, repeated `-t`, and `-s -p` together.
  - 5-channel `-m -s`, `-m -p` and `-m -e -C`.
  - Multi-disc sets, and titles with trailing spaces.
  - A disc with no text.
  - `-I`, and `-I -s -w` in one pass.
- **DST decoding:** every DST frame in a 27-area library (6,676,925 frames,
  about 24.7 hours) decoded with no errors.
- **Full DST syntax:** segmentation, general mapping, one-entry probability
  tables and the reference decoder's validity checks are covered by
  `tests/test_dst_full.py`. A test-only encoder forces each feature at 2, 5
  and 6 channels and wraps the frames in a synthetic SACD ISO. The original
  DSD, pysacd's decode and `sacd_extract`'s decode agree bit for bit.
- **Speed (32 threads, ISO in page cache):**
  - A full DST stereo + 5.1 disc to DSF took 33 s, against 104 s for
    `sacd_extract`.
  - A plain-DSD album (79 min) to DSF took 1.0 s.
  - Cold reads over 2.5 GbE NFS run at about 280 MB/s.

## Usage

```python
from pysacd import read_toc

disc = read_toc("/mnt/media/music/.../album.iso")
print(disc.artist, "-", disc.title, disc.date, disc.disc_catalog_number)

for area in disc.areas:                 # disc.stereo / disc.multichannel
    print(area.kind, area.speaker_config, area.frame_format_name)
    for t in area.tracks:
        print(t.number, t.title, t.performer, t.duration, t.duration.seconds_float,
              t.isrc, t.start_lsn, t.length_lsn)
```

`read_toc()` accepts a path or any seekable binary file object. Use
`SACDImage` if you want to keep the image open and call `read_sectors()` yourself.

Command line:

```
pysacd toc IMAGE.iso [...]                # human readable
pysacd toc --json IMAGE.iso               # full TOC as JSON
```

(Use `python -m pysacd` if it isn't installed.)

## Converting

```
make                                      # builds src/pysacd/_sacdconv.so
pysacd convert disc.iso                   # stereo area -> DSF, tagged
pysacd convert -f flac disc.iso           # 24-bit/88.2 kHz FLAC
pysacd convert -f flac -r 176400 -a both -o ~/Music disc.iso
pysacd convert -n disc.iso                # dry run: show the MB match and paths
```

Requirements:

- gcc and make
- `flac-devel`, for FLAC output only. `make` builds without it, and DSF and
  WAV still work.
- `mutagen`, for tagging.

The defaults are set in `~/.config/pysacd/config.toml`, using the same names
as the long options with underscores; command-line flags override them:

```toml
dir_template = "{albumartist}/{year} - {album} [{edition}]"
file_template = "{disc_prefix}{tracknumber:02d} {title}"
format = "flac"
```

Template fields:

- Album: `albumartist`, `album`, `date`, `year`, `originalyear`, `label`,
  `catalognumber`, `barcode`
- Track: `artist`, `title`, `composer`, `tracknumber`, `tracktotal`
- Disc: `discnumber`, `disctotal`, `disc` (`Disc N`, only set on multi-disc
  releases), `disc_prefix` (`N-`, multi-disc only). These come from `--disc N/M`
  if given, otherwise from the MusicBrainz match, otherwise from the TOC's
  album set fields (set size and sequence number).
- Area and format: `edition` (from `stereo_edition` or `multichannel_edition`),
  `layout` (`Stereo`, `5.0` or `5.1`), `channels`, `format`

**Different discs, same folder.** Before writing, `pysacd convert` checks
whether the target folder already holds tracks of a *different* disc. That
means a different MusicBrainz release, or failing that a different catalogue
number or barcode. Discs of one set (same album and artist, different disc
numbers) and re-runs of the same disc are fine. On a clash, the new disc goes
to `<folder> [<catalogue number>]` and a warning says so.

Some sets are mastered with every disc claiming to be disc 1 of 1, such as
Miles Davis's *Live-Evil*. For those, the warning may add a hint: when the
discs share title, artist and label and their catalogue numbers differ only
in a trailing counter, it suggests re-running with `--disc N/M`. pysacd never
acts on that guess by itself. With `--disc` the discs share one folder,
separated by `{disc_prefix}`.

Tracks that already exist are skipped unless `--overwrite` is given, and the
run summary says how many were skipped.

Python format specs work (`{tracknumber:02d}`). Empty `[]` or `()` left by blank
fields are removed, and `/` inside values is replaced. `--portable-names` also
replaces characters that are invalid on Windows/SMB.

How it works:

- **Audio:** one call into `_sacdconv.so` (through ctypes, which releases the
  GIL) produces every output for an area in one sequential pass over the image:
  - **Reader thread:** demuxes the audio sectors into batches of 32 frames.
  - **Worker pool** (`-j` or `--threads`, defaulting to the CPU count):
    decodes DST, converts DSD to PCM, and lays out DSF blocks.
  - **Writer thread:** writes every overlapping output in order. That covers
    DSF, DSDIFF (DSD or DST passthrough), WAV, FLAC, and optionally a plain
    ISO copy.

  Even a single long DST track uses every core. DST is decoded by a port of
  FFmpeg's decoder (`csrc/dstdec.c`).
- **PCM (FLAC/WAV):** a three-stage linear-phase FIR (2.8224 MHz → 352.8 →
  176.4 → 88.2 kHz) with a stopband of about 105 dB and TPDF dither. The
  default gain is 0 dB, which puts SACD's 0 dB reference at −6 dBFS, so
  nothing clips; use `--gain-db 6` if you want louder files. Each batch is
  filtered with two frames of context on either side and trimmed by the exact
  filter delay. Track joins are therefore sample-exact (gapless), and the
  output doesn't depend on the thread count.
- **Tags:** the MusicBrainz release is found by barcode (the SACD's album
  catalog number) or by catalog number. The match is checked against track
  count, durations and ISRCs, and picks the medium for the right layer:
  MusicBrainz lists "Hybrid SACD (SACD layer, 2 channels)" and "…multichannel"
  as separate media. If there's no confident match, the disc's own text is used.
  Use `--mb-release ID` to force a release, and `--mb-text-search` to allow a
  fallback search by title and artist. FLAC gets Vorbis comments; DSF and WAV
  get ID3 tags (v2.3 by default). Both follow Picard's field names, including
  the MusicBrainz IDs.

## sacd_extract compatibility

```
pysacd-extract -2 -m -s -i disc.iso          # same as sacd_extract
pysacd-extract -e -C -i disc.iso             # DSDIFF edit master + cue + xml
pysacd-extract -s --musicbrainz --cover -i disc.iso
pysacd-extract --flac --pcm-rate 176400 -m -i disc.iso
```

All `sacd_extract` options are supported: `-2 -m -e -p -s -z -t -k -I -w -c
-C -o -y -P -A -a -b -v -i`. `./sacd_extract.cfg` is honoured, including
`artist`, `performer`, `pauses`, `nopad`, `concatenate` and `id3tag`.

Extensions (long options only):

| Option | What it does |
|---|---|
| `--flac`, `--wav` | 24-bit PCM output (`--pcm-rate`, `--gain-db`, `--flac-level`) |
| `--musicbrainz` | MusicBrainz tags instead of SACD text (`--mb-release`, `--mb-contact`, `--cover`) |
| `--templates` | name files with the `pysacd convert` templates from `~/.config/pysacd/config.toml` |
| `--threads N` | number of worker threads |
| `--id3tag N` | the cfg's `id3tag` setting, as a flag |
| `--no-cfg` | ignore `./sacd_extract.cfg` |

Differences from `sacd_extract`:

- **Input:** ISO images only. Drives and PS3 network servers aren't
  supported.
- **Raw-sector images** (2064 or 2054 bytes per sector) are read, where
  `sacd_extract` refuses them. `-I` writes them out as plain 2048-byte ISOs.
- **The DSDIFF "creating machine" comment, and the version lines in `.cue` and
  `.xml`,** name pysacd rather than sacd-ripper. With sacd-ripper's string
  substituted, the files are byte-identical.
- **`-b` (pauses):** each track runs from its start to the next track's start.
  sacd_extract cuts on sector boundaries, which can put a boundary frame in both
  tracks.
- **Speed:** everything for an area is produced in one read pass, and DST is
  decoded on all cores.

## What's parsed

- Master TOC: version, hybrid flag, album set and sequence number, album and
  disc catalog numbers, genres, date, locales, and album/disc text (title,
  artist, publisher, copyright, plus phonetic variants).
- Area TOCs (2ch `TWOCHTOC` and multichannel `MULCHTOC`): channel count and
  speaker config, DST or plain DSD, total time, and the audio LSN range. If
  TOC-1 and the backup TOC-2 differ, the reader uses TOC-1 and records a warning.
- Per track: start and duration timecodes (75 frames/s), start LSN and length,
  ISRC, genre, and text (title, performer, composer, songwriter, arranger,
  message, plus phonetic variants).

Like sacd-ripper, only the first text language is decoded. Text is decoded
using the locale's character set (ASCII, Latin-1, Shift-JIS, KSC5601, GB2312
or Big5).

## Image formats

The sector layout is detected by looking for `SACDMTOC` at LSN 510:

| sector | data offset | source                                       |
|--------|-------------|----------------------------------------------|
| 2048   | 0           | plain ISO (`sacd_extract -I`, most rips)     |
| 2064   | 12          | raw DVD sectors (ID/IED/CPR_MAI header + EDC) |
| 2054   | 6           | raw sectors with ID/IED header only          |

## Tests

```
make && PYTHONPATH=src python3 -m unittest discover -s tests
PYSACD_TEST_ISO=/path/to/disc.iso PYTHONPATH=src python3 -m unittest discover -s tests
```

## CI

Every pull request runs:

- **ruff and mypy** on the Python code.
- **The test suite** on Python 3.11 to 3.14, with the engine built with
  `-Werror` (FLAC included).
- **The tests under AddressSanitizer and UndefinedBehaviorSanitizer.**
- **The comparison tests** against a `sacd_extract` built from a pinned
  upstream commit inside the runner. Its code never enters the repository.
- **CodeQL** (Python, C, workflows) and **semgrep**.
- **A check for private names**, in files and commit identities.
- **An AI review** from a second model, using PR-Agent. It needs the
  `GEMINI_API_KEY` repository secret.

Dependabot keeps the SHA-pinned actions and the Python dev dependencies
current, with a two-week cooldown. Contributor rules are in
[CLAUDE.md](CLAUDE.md).

## License

GPL-2.0-or-later. `csrc/dstdec.c` is derived from FFmpeg's DST decoder and
stays LGPL-2.1-or-later, which is compatible with the GPL. No Philips/ISO
reference code is included.

FFmpeg's decoder lacks parts of the DST syntax. Those parts were added through
a clean-room process:

1. One agent studied the reference decoder and wrote
   `docs/dst-bitstream.md`, a specification in prose and syntax tables with
   no code.
2. A separate agent, which never saw the reference code, implemented the
   decoder extensions and a test encoder from that document alone.
3. The compiled `sacd_extract` binary served only as a black-box oracle.
