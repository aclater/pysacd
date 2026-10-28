# pysacd

Pure-Python reader for SACD (Scarletbook) ISO images: no `sacd_extract`
subprocess and no C extension. It currently reads the TOC; audio extraction is planned.

The layouts are ported from [sacd-ripper](https://github.com/sacd-ripper/sacd-ripper)'s
`libs/libsacd/scarletbook.h` and `scarletbook_read.c` (GPL-2.0-or-later).

## Validation

On 2026-10-28 this was checked against `sacd_extract -P`, built from
sacd-ripper's current source, on 116 real SACD ISOs. Every field that
`sacd_extract` prints matched on all 116: disc and album text, catalog
numbers, genres, dates, area totals and speaker config, and per-track
text, start times, durations and ISRCs. The test set included stereo and
multichannel areas, DST and plain-DSD areas, and US-ASCII and ISO-8859-1
text. The 12 ISOs with raw 2064-byte sectors could not be compared,
because `sacd_extract` fails on them.

## Usage

```python
from pysacd import read_toc

disc = read_toc("/path/to/album.iso")
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
python -m pysacd IMAGE.iso [...]          # human readable
python -m pysacd --json IMAGE.iso         # full TOC as JSON
```

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
PYTHONPATH=src python3 -m unittest discover -s tests
PYSACD_TEST_ISO=/path/to/disc.iso PYTHONPATH=src python3 -m unittest discover -s tests
```

## Roadmap: extraction

The parsed TOC already includes what extraction needs (`Area.track_start` and
`track_end`, `Track.start_lsn` and `length_lsn`, `frame_format`). The
remaining pieces, following sacd-ripper's `scarletbook_read.c`
(`scarletbook_process_frames`), `dsf.c` and `dsdiff.c`, are:

1. Audio sector demux: the 1-byte frame header, then packet and frame info,
   then reassembly of DSD/DST frames.
2. DSF/DFF writers. Plain DSD (3-in-14) only needs bit reordering.
3. A DST decoder, for DST-coded areas (23 of the 143 areas in this
   library, and all of the multichannel ones). This is CPU-heavy, so it may
   need numpy or a small optional C accelerator.
