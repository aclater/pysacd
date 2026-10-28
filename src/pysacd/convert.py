"""Convert an SACD ISO to tagged DSF / FLAC / WAV files."""

from __future__ import annotations

import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from . import _native, musicbrainz, tagging
from .toc import FRAME_RATE, Area, Disc, SACDImage, Track

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pysacd" / "config.toml"
EXTENSIONS = {"dsf": "dsf", "flac": "flac", "wav": "wav"}


@dataclass
class Options:
    format: str = "dsf"                       # dsf | flac | wav
    output_dir: str = "."
    dir_template: str = "{albumartist}/{album} [{edition}]"
    file_template: str = "{disc_prefix}{tracknumber:02d} - {title}"
    stereo_edition: str = "SACD"
    multichannel_edition: str = "SACD {layout}"
    areas: str = "stereo"                     # stereo | multichannel | both
    tracks: list[int] = field(default_factory=list)   # empty = all
    pcm_rate: int = 88200                     # flac/wav: 88200 | 176400
    gain_db: float = 0.0
    flac_level: int = 5
    jobs: int = 0                             # worker threads, 0 = CPU count
    musicbrainz: bool = True
    mb_release: str | None = None
    mb_text_search: bool = False
    mb_contact: str | None = None             # contact info for the MusicBrainz User-Agent
    cover: bool = True                        # embed front cover
    cover_size: str = "1200"                  # 250 | 500 | 1200 | full
    cover_file: str = "cover.jpg"             # saved next to the tracks; "" to skip
    id3_version: int = 3                      # DSF/WAV tags: 3 or 4
    portable_names: bool = False              # also replace <>:"\|?* in names
    overwrite: bool = False
    dry_run: bool = False

    @classmethod
    def from_config(cls, path: Path = CONFIG_PATH) -> Options:
        opts = cls()
        if path.exists():
            with open(path, "rb") as f:
                data = tomllib.load(f)
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                k = k.replace("-", "_")
                if k not in known:
                    raise ValueError(f"{path}: unknown option {k!r}")
                setattr(opts, k, v)
        return opts


# ------------------------------------------------------------ naming

class _Blank(dict):
    def __missing__(self, key):
        return ""


_UNSAFE = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def _clean_component(s: str, portable: bool) -> str:
    s = s.replace("/", "-").replace("\0", "")
    if portable:
        s = _UNSAFE.sub("_", s)
    s = re.sub(r"\s*(\[\s*\]|\(\s*\))", "", s)       # empty [] / () from blank fields
    s = re.sub(r"\s{2,}", " ", s).strip().rstrip(".")
    s = s.lstrip(".") or "_"
    while len(s.encode()) > 240:
        s = s[:-1]
    return s


def render(template: str, values: dict, portable: bool) -> str:
    parts = [_clean_component(p.format_map(_Blank(values)), portable)
             for p in template.split("/") if p.strip()]
    return os.path.join(*parts) if parts else "_"


def _layout(area: Area) -> str:
    return {"Stereo": "Stereo", "5ch": "5.0", "5.1ch": "5.1"}.get(area.speaker_config, f"{area.channel_count}ch")


def naming_values(tags: tagging.Tags, area: Area, opts: Options) -> dict:
    first = lambda k: tags.get(k, [""])[0]
    date = first("DATE")
    discnumber = int(first("DISCNUMBER") or 1)
    disctotal = int(first("DISCTOTAL") or 1)
    layout = _layout(area)
    edition_t = opts.stereo_edition if area.is_stereo else opts.multichannel_edition
    v = {
        "album": first("ALBUM") or "Unknown Album",
        "albumartist": first("ALBUMARTIST") or first("ARTIST") or "Unknown Artist",
        "artist": first("ARTIST"),
        "title": first("TITLE") or f"Track {first('TRACKNUMBER')}",
        "composer": first("COMPOSER"),
        "date": date,
        "year": date[:4],
        "originalyear": first("ORIGINALDATE")[:4],
        "label": first("LABEL"),
        "catalognumber": first("CATALOGNUMBER"),
        "barcode": first("BARCODE"),
        "tracknumber": int(first("TRACKNUMBER") or 0),
        "tracktotal": int(first("TRACKTOTAL") or 0),
        "discnumber": discnumber,
        "disctotal": disctotal,
        "disc_prefix": f"{discnumber}-" if disctotal > 1 else "",
        "disc": f"Disc {discnumber}" if disctotal > 1 else "",
        "layout": layout,
        "channels": area.channel_count,
        "format": opts.format.upper(),
    }
    v["edition"] = edition_t.format_map(_Blank(v))
    return v


# ------------------------------------------------------------ main flow

def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _select_areas(disc: Disc, which: str) -> list[Area]:
    out = []
    if which in ("stereo", "both") and disc.stereo:
        out.append(disc.stereo)
    if which in ("multichannel", "both") and disc.multichannel:
        out.append(disc.multichannel)
    if not out:
        raise SystemExit(f"disc has no {which} area")
    return out


def convert(iso: str, opts: Options) -> int:
    if opts.format not in EXTENSIONS:
        raise SystemExit(f"unknown format {opts.format!r}")
    if opts.format == "flac" and not opts.dry_run and not _native.has_flac():
        raise SystemExit("this build has no FLAC support: install flac-devel, then `make clean all`")

    with SACDImage(iso) as img:
        disc = img.read_toc()
        for w in disc.warnings:
            _log(f"warning: {w}")
        failures = 0
        for area in _select_areas(disc, opts.areas):
            failures += _convert_area(img, iso, disc, area, opts)
    return 1 if failures else 0


def _convert_area(img: SACDImage, iso: str, disc: Disc, area: Area, opts: Options) -> int:
    label = f"{area.speaker_config} {area.frame_format_name}"
    match = cover = None
    if opts.musicbrainz:
        try:
            match = musicbrainz.find_release(disc, area, opts.mb_contact, opts.mb_release, opts.mb_text_search)
        except musicbrainz.MBError as e:
            _log(f"warning: MusicBrainz lookup failed: {e}")
        if match:
            _log(f"MusicBrainz: {match.release['title']} — {musicbrainz.credit_name(match.release.get('artist-credit', []))}"
                 f" ({match.release.get('date', '?')}, {match.medium.get('format')}) "
                 f"https://musicbrainz.org/release/{match.release_id}  [{match.method}, "
                 f"mean Δ {match.score:.2f}s, ISRC {match.isrc_hits}/{len(area.tracks)}]")
            for w in match.warnings:
                _log(f"warning: {w}")
        else:
            _log("MusicBrainz: no confident match; using SACD text")
        if match and (opts.cover or opts.cover_file) and not opts.dry_run:
            try:
                cover = musicbrainz.fetch_cover(match.release, opts.mb_contact, opts.cover_size)
            except musicbrainz.MBError as e:
                _log(f"warning: cover art: {e}")
            if not cover:
                _log("note: no cover art in the Cover Art Archive")

    tracks = [t for t in area.tracks if not opts.tracks or t.number in opts.tracks]
    plan: list[tuple[Track, tagging.Tags, Path]] = []
    for t in tracks:
        tags = tagging.base_tags(disc, area, t)
        if match:
            tagging.apply_musicbrainz(tags, match, t)
        if "ALBUM" not in tags:  # disc without text and no MusicBrainz match
            tags["ALBUM"] = [Path(iso).stem]
        vals = naming_values(tags, area, opts)
        rel = Path(render(opts.dir_template, vals, opts.portable_names)) / (
            render(opts.file_template, vals, opts.portable_names) + "." + EXTENSIONS[opts.format])
        plan.append((t, tags, Path(opts.output_dir) / rel))

    dirs = sorted({p.parent for _, _, p in plan})
    _log(f"{label}: {len(plan)} tracks -> {', '.join(map(str, dirs))}")
    if opts.dry_run:
        for t, tags, path in plan:
            print(f"  {t.duration}  {path}")
        return 0

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        if cover and opts.cover_file and not (d / opts.cover_file).exists():
            (d / opts.cover_file).write_bytes(cover[0])

    embed = cover if opts.cover else None
    fmt = opts.format
    specs, todo = [], []
    for t, tags, path in plan:
        if path.exists() and not opts.overwrite:
            _log(f"  {t.number:2d} exists, skipped: {path.name}")
            continue
        tmp = path.with_name(f".{path.name}.partial")
        spec = _native.OutputSpec(fmt, tmp, t.start.total_frames,
                                  t.start.total_frames + t.duration.total_frames,
                                  flac_level=opts.flac_level)
        if fmt == "dsf":
            spec.footer = tagging.id3_bytes(tags, embed, opts.id3_version)
        elif fmt == "flac":
            spec.flac_padding = tagging.flac_metadata_size(tags, embed)
        specs.append(spec)
        todo.append((t, tags, path, spec))
    if not specs:
        return 0

    start = time.monotonic()
    try:
        _native.run_area(img, iso, area, specs, opts.pcm_rate, opts.gain_db, opts.jobs,
                         progress=_progress_printer(label))
    except BaseException as e:
        for spec in specs:
            Path(spec.path).unlink(missing_ok=True)
        if isinstance(e, _native.NativeError):
            _log(f"\n{label}: FAILED: {e}")
            return len(specs)
        raise
    _log("")

    failures = 0
    for t, tags, path, spec in todo:
        try:
            if fmt != "dsf":
                tagging.write_tags(str(spec.path), fmt, tags, embed, opts.id3_version)
            os.replace(spec.path, path)
        except Exception as e:  # noqa: BLE001 - one track's tagging failure mustn't abort the disc
            failures += 1
            Path(spec.path).unlink(missing_ok=True)
            _log(f"  {t.number:2d} FAILED: {e}")
            continue
        msg = ""
        if spec.frames_missing or spec.dst_errors:
            msg = f"  WARNING {spec.frames_missing} missing frames, {spec.dst_errors} DST errors"
        _log(f"  {t.number:2d} {t.duration}  {path.name}{msg}")
    _log(f"{label}: done in {time.monotonic() - start:.1f}s"
         + (f", {failures} failed" if failures else ""))
    return failures


def _progress_printer(label: str):
    def show(p: _native.Progress) -> None:
        if p.frames_total and sys.stderr.isatty():
            pct = 100 * p.frames_done // p.frames_total
            print(f"\r{label}: {pct:3d}% ({p.frames_done // FRAME_RATE}s of {p.frames_total // FRAME_RATE}s audio)",
                  end="", file=sys.stderr, flush=True)
    return show
