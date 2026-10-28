"""sacd_extract-compatible command line, running on the pysacd engine.

Accepts sacd_extract's options and produces the same files and names
(DSF, DSDIFF, DSDIFF edit master, CUE, XML, ISO; `sacd_extract.cfg` in the
working directory is honoured). Extensions are long options only, so they
never collide with sacd_extract's: PCM output (--flac/--wav), MusicBrainz
tagging, pysacd naming templates and the thread count.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, _native, remote, sbcompat
from .toc import START_OF_MASTER_TOC, Area, Disc, SACDError, SACDImage

FRAME_BYTES = 4704
DSF_BLOCK = 4096
CHARSET_NAMES = ["US-ASCII", "ISO646-JP", "ISO-8859-1", "SHIFT_JISX0213", "KSC5601.1987-0",
                 "GB2312.1980-0", "BIG5", "ISO-8859-1"]
CATEGORIES = ["Not used", "General", "Japanese"]


def say(msg: str = "", end: str = "\n") -> None:
    sys.stdout.write(msg + end)
    sys.stdout.flush()


# ------------------------------------------------------------ options

@dataclass
class Opts:
    two_channel: bool = False
    multi_channel: bool = False
    output_dsf: bool = False
    output_dsdiff: bool = False
    output_dsdiff_em: bool = False
    output_iso: bool = False
    convert_dst: bool = False
    export_cue_sheet: bool = False
    print_: bool = False
    version: bool = False
    dsf_nopad: bool = False
    trimming: bool = True              # !pauses
    artist_flag: bool = False
    performer_flag: bool = False
    concatenate: bool = False
    selected: set[int] = field(default_factory=set)    # 0-based
    id3_mode: int = 4
    input: str | None = None
    output_dir: str | None = None
    output_dir_conc: str | None = None
    # extensions
    pcm: str | None = None             # "flac" | "wav"
    pcm_rate: int = 88200
    gain_db: float = 0.0
    flac_level: int = 5
    threads: int = 0
    musicbrainz: bool = False
    mb_release: str | None = None
    mb_contact: str | None = None
    cover: bool = False
    templates: bool = False            # name files with pysacd's config templates
    config: str | None = None
    use_cfg: bool = True


def parse_args(argv: list[str]) -> Opts:
    ap = argparse.ArgumentParser(
        prog="pysacd-extract", add_help=False,
        usage="%(prog)s [options] -i[=FILE]",
        description="sacd_extract-compatible SACD extractor (pysacd engine).",
        epilog="Extensions (long options only): --flac/--wav write 24-bit PCM, "
               "--musicbrainz tags from MusicBrainz, --templates names files from "
               "~/.config/pysacd/config.toml, --threads N.")
    g = ap.add_argument_group("sacd_extract options")
    flag = lambda *names, **kw: g.add_argument(*names, action="store_true", **kw)
    flag("-2", "--2ch-tracks", dest="two", help="Export two channel tracks (default)")
    flag("-m", "--mch-tracks", dest="mch", help="Export multi-channel tracks")
    flag("-e", "--output-dsdiff-em", dest="em", help="output as Philips DSDIFF (Edit Master) file")
    flag("-p", "--output-dsdiff", dest="dff", help="output as Philips DSDIFF file")
    flag("-s", "--output-dsf", dest="dsf", help="output as Sony DSF file")
    flag("-z", "--dsf-nopad", dest="nopad", help="Do not zero pad DSF (cannot be used with -t)")
    g.add_argument("-t", "--select-track", dest="tracks", action="append",
                   help="only output selected track(s) (ex. -t 1,5,13)")
    flag("-k", "--concatenate", dest="concat", help="concatenate consecutive selected track(s) (ex. -k -t 2,3,4)")
    flag("-I", "--output-iso", dest="iso", help="output as RAW ISO")
    flag("-w", "--concurrent", dest="concurrent", help="Concurrent ISO+DSF/DSDIFF processing mode")
    flag("-c", "--convert-dst", dest="convert_dst", help="convert DST to DSD")
    flag("-C", "--export-cue", dest="cue", help="Export a CUE Sheet")
    g.add_argument("-o", "--output-dir", dest="output_dir", help="Output directory for ISO or DSDIFF Edit Master")
    g.add_argument("-y", "--output-dir-conc", dest="output_dir_conc", help="Output directory for DSF or DSDIFF")
    flag("-P", "--print", dest="print_", help="display disc and track information")
    flag("-A", "--artist", dest="artist", help="artist name is added in folder name")
    flag("-a", "--performer", dest="performer", help="performer name is added in track filename")
    flag("-b", "--pauses", dest="pauses", help="all pauses will be included")
    flag("-v", "--version", dest="version", help="Display version")
    g.add_argument("-i", "--input", dest="input", help="input ISO image")
    g.add_argument("-?", "--help", action="help", help="Show this help message")
    flag("--usage", dest="usage", help="Display brief usage message")

    x = ap.add_argument_group("pysacd extensions")
    x.add_argument("--flac", dest="pcm", action="store_const", const="flac", help="write FLAC (24-bit PCM)")
    x.add_argument("--wav", dest="pcm", action="store_const", const="wav", help="write WAV (24-bit PCM)")
    x.add_argument("--pcm-rate", type=int, choices=[88200, 176400], default=88200)
    x.add_argument("--gain-db", type=float, default=0.0, help="PCM gain (default 0)")
    x.add_argument("--flac-level", type=int, choices=range(9), metavar="0-8", default=5)
    x.add_argument("--threads", type=int, default=0, help="worker threads (default: CPU count)")
    x.add_argument("--id3tag", type=int, choices=range(6), help="ID3 mode as in sacd_extract.cfg (default 4)")
    x.add_argument("--musicbrainz", action="store_true", help="tag from MusicBrainz (DSF/FLAC/WAV)")
    x.add_argument("--mb-release", help="MusicBrainz release id to use")
    x.add_argument("--mb-contact", help="contact for the MusicBrainz User-Agent")
    x.add_argument("--cover", action="store_true", help="embed Cover Art Archive front cover (with --musicbrainz)")
    x.add_argument("--templates", action="store_true", help="name output with pysacd config templates")
    x.add_argument("--config", help="pysacd config file for --templates")
    x.add_argument("--no-cfg", dest="no_cfg", action="store_true", help="ignore ./sacd_extract.cfg")

    a = ap.parse_args(argv)
    if a.usage:
        ap.print_usage()
        sys.exit(0)
    o = Opts(two_channel=a.two, multi_channel=a.mch, output_dsf=a.dsf, output_dsdiff=a.dff,
             output_dsdiff_em=a.em, output_iso=a.iso, convert_dst=a.convert_dst,
             export_cue_sheet=a.cue or a.em, print_=a.print_, version=a.version,
             dsf_nopad=a.nopad, trimming=not (a.pauses or a.concat), artist_flag=a.artist,
             performer_flag=a.performer, concatenate=a.concat,
             input=a.input.lstrip("=") if a.input else None,
             output_dir=_dirarg(a.output_dir), output_dir_conc=_dirarg(a.output_dir_conc),
             pcm=a.pcm, pcm_rate=a.pcm_rate, gain_db=a.gain_db, flac_level=a.flac_level,
             threads=a.threads, musicbrainz=a.musicbrainz, mb_release=a.mb_release,
             mb_contact=a.mb_contact, cover=a.cover, templates=a.templates, config=a.config,
             use_cfg=not a.no_cfg)
    if a.tracks:
        # like sacd_extract, the last -t wins; 0 and junk are ignored
        for tok in a.tracks[-1].replace(" ", ",").split(","):
            if tok.strip().isdigit() and int(tok):
                o.selected.add((int(tok) - 1) & 0xFF)
    if a.id3tag is not None:
        o.id3_mode = a.id3tag
    return o


def _dirarg(s: str | None) -> str | None:
    if not s:
        return None
    s = s.lstrip("=")
    if len(s) >= 2 and s[0] == '"':
        s = s[1:]
    s = s.removesuffix('"')
    return s if len(s) >= 2 else None


def read_cfg(o: Opts) -> bool:
    """./sacd_extract.cfg, parsed exactly as sacd_extract does (substring matches)."""
    path = Path("sacd_extract.cfg")
    if not path.is_file():
        return False
    for line in path.read_text(errors="replace").splitlines():
        def has(*keys: str, line: str = line) -> bool:
            return any(k in line for k in keys)
        if has("artist=1", "artist=yes"):
            o.artist_flag = True
        if has("performer=1", "performer=yes"):
            o.performer_flag = True
        if has("pauses=1", "pauses=yes"):
            o.trimming = False
        if has("nopad=1", "nopad=yes"):
            o.dsf_nopad = True
        if has("concatenate=1", "concatenate=yes"):
            o.concatenate = True
            o.trimming = False
        if has("id3tag=0", "id3tag=no"):
            o.id3_mode = 0
        for m in range(1, 6):
            if f"id3tag={m}" in line:
                o.id3_mode = m
    say("\nFound configuration 'sacd_extract.cfg' file...")
    _print_settings(o)
    return True


def _print_settings(o: Opts) -> None:
    yn = lambda b: "yes" if b else "no"
    say(f"\tArtist will be added in folder name (artist={int(o.artist_flag)}) {yn(o.artist_flag)}")
    say(f"\tPerformer will be added in filename of track (performer={int(o.performer_flag)}) {yn(o.performer_flag)}")
    say(f"\tPadding-less (nopad={int(o.dsf_nopad)}) {yn(o.dsf_nopad)}")
    say(f"\tPauses included (pauses={int(not o.trimming)}) {yn(not o.trimming)}")
    say(f"\tConcatenate (concatenate={int(o.concatenate)}) {yn(o.concatenate)}")
    say(f"\tID3tag (id3tag = {o.id3_mode})")


# ------------------------------------------------------------ -P

def print_disc(img: SACDImage, disc: Disc) -> None:
    """Same text as sacd_extract -P (scarletbook_print.c)."""
    raw = img.read_sectors(START_OF_MASTER_TOC, 1)
    t = disc.text
    say("\nDisc Information:")
    say(f"\tVersion: {disc.version[0]:2d}.{disc.version[1]:02d}")
    y, m, d = disc.date
    say(f"\tCreation date: {y:4d}-{m:02d}-{d:02d}")
    disc_catalog = raw[88:104].split(b"\0")[0].decode("latin-1")
    album_catalog = raw[24:40].split(b"\0")[0].decode("latin-1")
    say(f"\tDisc Catalog Number: {disc_catalog}")
    for g in disc.disc_genres:
        if g.category:
            say(f"\tDisc Category: {CATEGORIES[g.category] if g.category < 3 else '?'}")
            say(f"\tDisc Genre: {g.name}")
    lang, cs = raw[136:138], raw[138]
    if lang[0] and lang[1]:
        say(f"\tLocale: {lang.decode('latin-1')}, Code character set:[{cs}], {CHARSET_NAMES[cs & 7]}")
    else:
        say(f"\tLocale: (zero) unspecified, asume Code character set:[{cs}], {CHARSET_NAMES[cs & 7]}")
    _print_text(t, "disc")
    say("\nAlbum Information:")
    say(f"\tAlbum Catalog Number: {album_catalog}")
    say(f"\tSequence Number: {disc.album_sequence_number}")
    say(f"\tSet Size: {disc.album_set_size}")
    for g in disc.album_genres:
        if g.category:
            say(f"\tAlbum Category: {CATEGORIES[g.category] if g.category < 3 else '?'}")
            say(f"\tAlbum Genre: {g.name}")
    _print_text(t, "album")

    say(f"\nArea count: {len(disc.areas)}")
    labels = ("Title", "Performer", "Songwriter", "Composer", "Arranger", "Message", "Extra Message")
    for idx, area in enumerate(disc.areas):
        say(f"\tArea Information [{idx}]:\n")
        say(f"\tVersion: {area.version[0]:2d}.{area.version[1]:02d}")
        for label, val in (("Copyright", area.copyright), ("Copyright Phonetic", area.copyright_phonetic),
                           ("Area Description", area.description),
                           ("Area Description Phonetic", area.description_phonetic)):
            if val:
                say(f"\t{label}: {val}")
        say(f"\tTrack Count: {len(area.tracks)}")
        say(f"\tTotal play time: {area.total_playtime} [mins:secs:frames]")
        cfg = area.area_config & 7
        spk = {(2, 0): "2 Channel", (5, 3): "5 Channel", (6, 4): "6 Channel"}.get((area.channel_count, cfg), "Unknown")
        say(f"\tSpeaker config: {spk}")
        say(f"\tTrack list [{idx}]:")
        for i, tr in enumerate(area.tracks):
            for label in labels:
                key = label.lower().replace(" ", "_")
                for suffix, k in (("", key), (" Phonetic", key + "_phonetic")):
                    if tr.text.get(k):
                        say(f"\t\t{label}{suffix}[{i}]: {tr.text[k]}")
            say(f"\t\tTrack_Start_Time_Code: {tr.start} [mins:secs:frames]")
            say(f"\t\tDuration: {tr.duration} [mins:secs:frames]")
            say()
        for i, tr in enumerate(area.tracks):
            if tr.isrc:
                s = tr.isrc.ljust(12)
                say(f"\tISRC Track [{i}]:\n\t  Country: {s[0:2]}, Owner: {s[2:5]}, Year: {s[5:7]}, Designation: {s[7:12]}")


def _print_text(t: dict, prefix: str) -> None:
    for label in ("Title", "Artist", "Publisher", "Copyright"):
        for suffix, key in (("", f"{prefix}_{label.lower()}"), (" Phonetic", f"{prefix}_{label.lower()}_phonetic")):
            if t.get(key):
                say(f"\t{label}{suffix}: {t[key]}")


# ------------------------------------------------------------ planning

def track_range(area: Area, i: int, trimming: bool) -> tuple[int, int]:
    """Frame range of track i: [start, start+duration) without pauses; with
    pauses, from the track's start up to the next track (track 1 from the
    area start, the last track to the area end)."""
    t = area.tracks[i]
    if trimming:
        return t.start.total_frames, t.start.total_frames + t.duration.total_frames
    start = t.start.total_frames if i else 0
    end = area.tracks[i + 1].start.total_frames if i + 1 < len(area.tracks) else area.total_playtime.total_frames
    return start, max(end, t.start.total_frames + t.duration.total_frames)


@dataclass
class Job:
    spec: _native.OutputSpec
    final: Path
    kind: str                 # dsf | dff | dff-em | flac | wav
    track: int | None         # 0-based track for tags/footers (None: edit master)
    dst: bool = False         # DSDIFF carrying DST frames
    tags: dict | None = None  # FLAC/WAV: written after encoding
    cover: tuple | None = None


def _tags_for(disc, area, idx, o: Opts, match, cover):
    from . import tagging
    tags = tagging.base_tags(disc, area, area.tracks[idx])
    if match:
        tagging.apply_musicbrainz(tags, match, area.tracks[idx])
    return tags


def plan_area(img, disc: Disc, area: Area, o: Opts, out_dir: Path, album_filename: str,
              match, cover) -> list[Job]:
    from . import tagging

    jobs: list[Job] = []
    dst_in = area.is_dst
    dff_dst = dst_in and not o.convert_dst
    ntracks = len(area.tracks)

    if o.output_dsdiff_em:
        path = Path(sbcompat.unique_filename(str(out_dir), album_filename, "dff"))
        end = area.total_playtime.total_frames
        say(f"\n Exporting DFF edit master output in file: {path}")
        jobs.append(_dff_job(disc, area, None, path, 0, end, dff_dst, o))

    if not (o.output_dsf or o.output_dsdiff or o.pcm):
        return jobs
    ext = o.pcm or ("dsf" if o.output_dsf else "dff")
    kind = ext
    if o.output_dsf and not o.pcm:
        say(f"\n Exporting DSF output in folder: {out_dir}")
    elif not o.pcm:
        say(f"\n Exporting DSDIFF output in folder: {out_dir}")
    else:
        say(f"\n Exporting {ext.upper()} output in folder: {out_dir}")

    groups: list[tuple[list[int], str]] = []
    if o.concatenate:
        if not o.selected:
            say("\n\n Warning! Concatenation activated but no tracks selected!")
        else:
            first, last = min(o.selected), max(o.selected)
            if last < ntracks:
                say(f"\n Concatenate tracks: {first + 1} to {last + 1}")
                groups.append((list(range(first, last + 1)),
                               sbcompat.music_filename(disc, area, first, f"[{first + 1:02d}-{last + 1:02d}]",
                                                       o.performer_flag)))
    else:
        for i in range(ntracks):
            if o.selected and i not in o.selected:
                continue
            groups.append(([i], sbcompat.music_filename(disc, area, i, "", o.performer_flag)))

    carry = 0                      # DSF nopad: bytes/channel carried into the next file
    prev_end = None
    for idx, (tracks, name) in enumerate(groups):
        first, last = tracks[0], tracks[-1]
        start = track_range(area, first, o.trimming)[0]
        end = track_range(area, last, o.trimming)[1]
        path = Path(sbcompat.make_filename(str(out_dir), name, ext))
        if o.templates:
            path = _template_path(disc, area, first, ext, o, match)
        tmp = path.with_name(f".{path.name}.partial")

        if kind == "dff":
            jobs.append(_dff_job(disc, area, first, path, start, end, dff_dst, o))
            continue

        spec = _native.OutputSpec(kind, tmp, start, end, flac_level=o.flac_level)
        tags = _tags_for(disc, area, first, o, match, cover) if (o.musicbrainz or o.pcm) else None
        if kind == "dsf":
            if o.dsf_nopad and not o.concatenate:
                if carry and prev_end is not None and first > 0:
                    spec.head_frame, spec.head_bytes = prev_end - 1, carry
                total = (end - start) * FRAME_BYTES + spec.head_bytes
                last_of_area = last == ntracks - 1
                carry = 0 if last_of_area else total % DSF_BLOCK
                spec.tail_bytes = carry
            prev_end = end
            if o.musicbrainz:
                assert tags is not None
                spec.footer = tagging.id3_bytes(tags, cover, 3 if o.id3_mode < 4 else 4)
            else:
                spec.footer = sbcompat.id3_tag(disc, area, first, o.id3_mode)
        elif kind == "flac":
            assert tags is not None     # PCM outputs always carry tags
            spec.flac_padding = tagging.flac_metadata_size(tags, cover)
        jobs.append(Job(spec, path, kind, first, tags=tags, cover=cover))
    return jobs


def _dff_job(disc, area, track, path: Path, start, end, dst: bool, o: Opts) -> Job:
    tmp = path.with_name(f".{path.name}.partial")
    hsize = sbcompat.dsdiff_header_size(area, dst)
    if dst:
        # sizes and the frame index are only known afterwards; finished in finish_dff()
        header = sbcompat.dsdiff_header(disc, area, dst=True, audio_data_size=0, frame_count=0, footer_size=0)
        spec = _native.OutputSpec("dff-dst", tmp, start, end, header=header)
    else:
        data = (end - start) * FRAME_BYTES * area.channel_count
        footer = sbcompat.dsdiff_footer(disc, area, track, dst=False, frame_index=[], id3_mode=o.id3_mode,
                                        now=datetime.datetime.now())
        header = sbcompat.dsdiff_header(disc, area, dst=False, audio_data_size=data,
                                        frame_count=end - start, footer_size=len(footer))
        spec = _native.OutputSpec("dff", tmp, start, end, header=header, footer=footer)
    assert len(spec.header) == hsize
    return Job(spec, path, "dff-em" if track is None else "dff", track, dst)


def finish_dff(disc, area, job: Job, o: Opts) -> None:
    """Complete a DST DSDIFF: frame index footer, then the real header."""
    s = job.spec
    footer = sbcompat.dsdiff_footer(disc, area, job.track, dst=True, frame_index=s.dst_index,
                                    id3_mode=o.id3_mode, now=datetime.datetime.now())
    header = sbcompat.dsdiff_header(disc, area, dst=True, audio_data_size=s.data_bytes,
                                    frame_count=len(s.dst_index), footer_size=len(footer))
    with open(s.path, "r+b") as f:
        f.seek(0, os.SEEK_END)
        f.write(footer)
        f.seek(0)
        f.write(header)


def _template_path(disc, area, idx, ext, o: Opts, match) -> Path:
    from .convert import CONFIG_PATH, Options, naming_values, render
    opts = Options.from_config(Path(o.config) if o.config else CONFIG_PATH)
    opts.format = ext
    tags = _tags_for(disc, area, idx, o, match, None)
    v = naming_values(tags, area, opts)
    root = Path(o.output_dir or ".")
    return root / render(opts.dir_template, v, opts.portable_names) / (
        render(opts.file_template, v, opts.portable_names) + "." + ext)


# ------------------------------------------------------------ main

def _progress(p: _native.Progress) -> None:
    if p.frames_total:
        pct = min(100, 100 * p.frames_done // p.frames_total)
        say(f"\rCompleted: {pct}% (frames processed: {p.frames_done} / total frames: {p.frames_total})", end="")
    elif p.sectors_total:
        pct = 100 * p.sectors_done // p.sectors_total
        say(f"\rCompleted: {pct}% (sectors processed: {p.sectors_done} / total sectors: {p.sectors_total})", end="")


def main(argv: list[str] | None = None) -> int:
    o = parse_args(sys.argv[1:] if argv is None else argv)
    say(f"\nsacd_extract compatible client: pysacd {__version__}")
    say(f"\nCurrent (working) directory (for the app and 'sacd_extract.cfg' file): {os.getcwd()}")
    has_cfg = o.use_cfg and read_cfg(o)
    if o.version:
        if not has_cfg:
            say("Configuration settings:")
            _print_settings(o)
        say("\nProgram terminates!")
        return 0
    if not o.two_channel and not o.multi_channel:
        o.two_channel = True
    for d in (o.output_dir, o.output_dir_conc):
        if d and not os.path.isdir(d):
            say(f"{d} doesn't exist or is not a directory.")
            return 255
    o.output_dir = o.output_dir or o.output_dir_conc
    if not o.input:
        o.input = "/dev/cdrom"
    tmp_iso = None
    addr = remote.parse_address(o.input)
    if addr:
        try:
            o.input, tmp_iso = _fetch_remote(addr, o)
        except (OSError, SACDError) as e:
            print(f"{o.input}: {e}", file=sys.stderr)
            return 255
        except KeyboardInterrupt:
            say("\n\n Program interrupted...")
            return 255
    try:
        return _main_local(o)
    finally:
        if tmp_iso:
            Path(tmp_iso).unlink(missing_ok=True)


def _wants_audio(o: Opts) -> bool:
    return bool(o.output_dsf or o.output_dsdiff or o.output_dsdiff_em or o.export_cue_sheet or o.pcm)


def _fetch_remote(addr: tuple[str, int], o: Opts) -> tuple[str, str | None]:
    """Copy what's needed from a PS3 (SACD Ripper server) to a local file.

    With -I the whole disc goes straight into the output ISO, which then serves as the input
    for any other outputs, so the disc crosses the network once. Otherwise only the TOCs and
    the requested areas are fetched into a temporary sparse ISO. Returns (input path, temp path).
    """
    host, port = addr
    say(f"\n Reading from SACD Ripper server {host}:{port}")
    progress = remote.RateMeter(lambda m: (sys.stdout.write(m), sys.stdout.flush()))
    base = Path(o.output_dir) if o.output_dir else Path()
    if o.output_iso:
        disc, _total = remote.read_remote_toc(host, port)
        out_dir = base / sbcompat.album_path(disc, o.artist_flag)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = Path(sbcompat.unique_filename(str(out_dir), sbcompat.album_dir(disc, o.artist_flag), "iso"))
        say(f"\n Exporting ISO output in file: {path}")
        remote.fetch(host, port, path, None, progress=progress)
        o.output_iso = False        # written; the other outputs read from it
        say("\n We are done exporting ISO.")
        return str(path), None
    want = _wants_audio(o)
    two, mch = o.two_channel or not o.multi_channel, o.multi_channel
    tmp, _ = remote.fetch_temp(
        host, port, str(base),
        lambda a: want and ((a.is_stereo and two) or (a.is_multichannel and mch)), progress=progress)
    return tmp, tmp


def _main_local(o: Opts) -> int:
    assert o.input is not None      # set by main()
    if not os.path.isfile(o.input):
        print(f"{o.input}: only SACD ISO image files and SACD Ripper servers (host:port) are supported",
              file=sys.stderr)
        return 255

    try:
        img = SACDImage(o.input)
        disc = img.read_toc()
    except (OSError, SACDError) as e:
        print(f"{o.input}: {e}", file=sys.stderr)
        return 255
    try:
        return _run(img, disc, o)
    except KeyboardInterrupt:
        say("\n\n Program interrupted...")
        return 255
    except (OSError, _native.NativeError) as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 255
    finally:
        img.close()


def _run(img: SACDImage, disc: Disc, o: Opts) -> int:
    if o.print_:
        print_disc(img, disc)
    total = img.total_sectors
    say(f"\nThe size of sacd is ok (sectors={total}). Size is: {total * 2048} bytes, "
        f"{total * 2048 / 1e9:.3f} GB (gigabyte) ")

    album_filename = sbcompat.album_dir(disc, o.artist_flag)
    base = Path(o.output_dir) if o.output_dir else Path()
    out_dir = base / sbcompat.album_path(disc, o.artist_flag)

    if o.export_cue_sheet:
        out_dir.mkdir(parents=True, exist_ok=True)
        xml = Path(sbcompat.unique_filename(str(out_dir), album_filename, "xml"))
        say(f"\n\n Exporting metadata in XML file: [{xml}] ... ")
        xml.write_bytes(sbcompat.metadata_xml(disc, raw_catalog=sbcompat.raw_catalog_numbers(img)))
        say("\n\n We are done exporting metadata in XML file. ")

    iso_path = None
    if o.output_iso:
        out_dir.mkdir(parents=True, exist_ok=True)
        iso_path = Path(sbcompat.unique_filename(str(out_dir), album_filename, "iso"))
        say(f"\n Exporting ISO output in file: {iso_path}")

    match = cover = None
    if o.musicbrainz:
        from . import musicbrainz
        ref = disc.stereo or disc.areas[0]
        try:
            match = musicbrainz.find_release(disc, ref, o.mb_contact, o.mb_release)
            if match and o.cover:
                cover = musicbrainz.fetch_cover(match.release, o.mb_contact)
        except musicbrainz.MBError as e:
            say(f"\n Warning: MusicBrainz lookup failed: {e}")
        say(f"\n MusicBrainz: {'https://musicbrainz.org/release/' + match.release_id if match else 'no confident match'}")

    want = []
    if o.output_dsf or o.output_dsdiff or o.output_dsdiff_em or o.export_cue_sheet or o.pcm:
        if o.multi_channel:
            if disc.multichannel:
                want.append(disc.multichannel)
            else:
                say("\n Asked multichannel format but disc has no multichannel area. So skip processing...")
        if o.two_channel:
            if disc.stereo:
                want.append(disc.stereo)
            else:
                say("\n Asked for stereo format but disc has no stereo area. So skip processing...")

    rc = 0
    for area in want:
        area_dir = out_dir / sbcompat.area_dir_name(area) if disc.multichannel else out_dir
        area_dir.mkdir(parents=True, exist_ok=True)
        jobs = plan_area(img, disc, area, o, area_dir, album_filename, match, cover)
        if o.export_cue_sheet:
            cue = Path(sbcompat.unique_filename(str(area_dir), album_filename, "cue"))
            say(f"\n\n Exporting CUE sheet: [{cue}] ... ")
            dff_name = sbcompat.make_filename(None, album_filename, "dff")
            cue.write_bytes(sbcompat.cue_sheet(disc, area, dff_name))
            say("\n\n We are done exporting CUE sheet. ")
        if jobs or iso_path:
            rc |= _execute(img, disc, area, jobs, o, iso_path)
            iso_path = None     # the image is copied during the first pass only
    if iso_path:
        rc |= _execute(img, disc, None, [], o, iso_path)
    say("\nProgram terminates!")
    return rc


def _execute(img, disc, area, jobs: list[Job], o: Opts, iso_path) -> int:
    from . import tagging

    started = time.time()
    say(f"\n Started at: {time.asctime()}    ")
    specs = [j.spec for j in jobs]
    for j in jobs:
        j.final.parent.mkdir(parents=True, exist_ok=True)
    try:
        assert o.input is not None
        _native.run_area(img, o.input, area, specs, o.pcm_rate, o.gain_db, o.threads,
                         iso_out=iso_path, progress=_progress)
    except BaseException:
        for s in specs:
            Path(s.path).unlink(missing_ok=True)
        raise
    rc = 0
    for j in jobs:
        s = j.spec
        if j.dst:
            finish_dff(disc, area, j, o)
        if j.kind in ("flac", "wav"):
            assert j.tags is not None
            tagging.write_tags(str(s.path), j.kind, j.tags, j.cover, 4 if o.id3_mode >= 4 else 3)
        os.replace(s.path, j.final)
        if s.frames_missing or s.dst_errors:
            rc = 1
            say(f"\n Warning: {j.final.name}: {s.frames_missing} missing frames, {s.dst_errors} DST decode errors")
    elapsed = time.strftime("%H hours:%M minutes:%S seconds", time.gmtime(time.time() - started))
    say(f"\n\n Ended at: {time.asctime()} [elapsed: {elapsed}]")
    if iso_path:
        say("\n We are done exporting ISO.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
