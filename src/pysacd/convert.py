"""Convert an SACD ISO to tagged DSF / FLAC / WAV files."""

from __future__ import annotations

import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from . import _native, musicbrainz, safetext, tagging
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
    disc: str | None = None                   # "N/M": disc N of an M-disc set (overrides TOC and MusicBrainz)
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
# Windows device names, reserved with any extension ("CON", "nul.txt", "COM1 - x" is fine)
_RESERVED = re.compile(r"(?i)^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?$")


def _clean_component(s: str, portable: bool) -> str:
    # Disc and MusicBrainz text is untrusted: no control/bidi characters, no command
    # substitution, pipes or redirection, no leading '-' or '.' (docs/security.md).
    s = safetext.name_component(s)
    if portable:
        s = _UNSAFE.sub("_", s)
    s = re.sub(r"\s*(\[\s*\]|\(\s*\))", "", s)       # empty [] / () from blank fields
    s = re.sub(r"\s{2,}", " ", s).strip().rstrip(".")
    s = s.lstrip(".") or "_"
    while len(s.encode()) > 240:
        s = s[:-1]
    if portable and _RESERVED.match(s):
        s = "_" + s
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


# ------------------------------------------------------------ disc collisions (#4)

AUDIO_SUFFIXES = (".dsf", ".flac", ".wav")


def parse_disc(spec: str) -> tuple[int, int]:
    """'N/M' for --disc."""
    n, sep, m = spec.partition("/")
    if not (sep and n.isdigit() and m.isdigit() and 1 <= int(n) <= int(m)):
        raise ValueError(f"--disc must be N/M with 1 <= N <= M, got {spec!r}")
    return int(n), int(m)


def _first(tags: tagging.Tags, key: str) -> str:
    return (tags.get(key) or [""])[0].strip()


def _same_disc_or_set(a: tagging.Tags, b: tagging.Tags) -> bool | None:
    """True if two tag sets belong in one directory (same disc, or discs of
    one set), False if they are different discs, None if there's no way to tell."""
    mb_a, mb_b = _first(a, "MUSICBRAINZ_ALBUMID"), _first(b, "MUSICBRAINZ_ALBUMID")
    if mb_a and mb_b:
        return mb_a == mb_b
    total_a, total_b = _first(a, "DISCTOTAL"), _first(b, "DISCTOTAL")
    if (total_a.isdigit() and total_b.isdigit() and int(total_a) > 1 and int(total_b) > 1
            and _first(a, "DISCNUMBER") != _first(b, "DISCNUMBER")
            and _first(a, "ALBUM") == _first(b, "ALBUM") and _first(a, "ALBUMARTIST") == _first(b, "ALBUMARTIST")):
        return True     # different discs of one set: they share the directory by design
    for key in ("CATALOGNUMBER", "BARCODE"):
        x, y = _first(a, key), _first(b, key)
        if x and y:
            return x == y
    return None




def _disc_label(tags: tagging.Tags) -> str:
    return _first(tags, "CATALOGNUMBER") or _first(tags, "BARCODE")


def _set_hint(labels: list[str], others: list[tagging.Tags], tags: tagging.Tags) -> str | None:
    """Suggest --disc when the discs share title, artist and label and their
    catalogue numbers differ only in a trailing counter. Only a hint: the
    discs' TOCs say otherwise, so pysacd doesn't act on the guess."""
    same = all(_first(o, k) == _first(tags, k) for o in others for k in ("ALBUM", "ALBUMARTIST", "LABEL"))
    split = [re.fullmatch(r"(.*?)(\d+)", lab) for lab in labels]
    if not same or not all(split) or len(set(labels)) < 2:
        return None
    prefixes = {m.group(1) for m in split if m}
    if len(prefixes) != 1:
        return None
    ordered = sorted(labels, key=lambda lab: int(re.fullmatch(r".*?(\d+)", lab).group(1)))  # type: ignore[union-attr]
    total = len(ordered)
    return "these look like discs of one set; if they are, re-run with " + ", ".join(
        f"--disc {i}/{total} for {lab}" for i, lab in enumerate(ordered, 1))


def _avoid_other_discs(plan: list[tuple[Track, tagging.Tags, Path]], portable: bool,
                       ) -> list[tuple[Track, tagging.Tags, Path]]:
    """If a target directory already holds tracks of a different disc, write this
    disc to "<dir> [<catalogue number>]" instead and warn (issue #4)."""
    if not plan:
        return plan
    tags = plan[0][1]
    moved: dict[Path, Path] = {}
    for d in sorted({p.parent for _, _, p in plan}):
        if not d.is_dir():
            continue
        others = [tagging.read_tags(f) for f in sorted(d.iterdir())
                  if f.suffix.lower() in AUDIO_SUFFIXES and not f.name.startswith(".")]
        verdicts = [_same_disc_or_set(o, tags) for o in others if o]
        if False not in verdicts:
            if None in verdicts:
                _log(f"warning: {_term(d)} holds tracks without catalogue, barcode or MusicBrainz tags; "
                     "can't tell whether they are this disc")
            continue
        label = _disc_label(tags)
        if not label:
            raise SystemExit(f"{_term(d)} already holds a different disc, and this disc has no catalogue number or "
                             "barcode to tell them apart; use --dir-template or --disc")
        target = d.with_name(_clean_component(f"{d.name} [{label}]", portable))
        clash = [o for o in others if o and _same_disc_or_set(o, tags) is False]
        _log(f"warning: {_term(d)} already holds a different disc "
             f"({_term(', '.join(sorted({_disc_label(o) or '?' for o in clash})))}); "
             f"writing this disc ({_term(label)}) to {_term(target)}")
        hint = _set_hint(sorted({_disc_label(o) for o in clash if _disc_label(o)} | {label}), clash, tags)
        if hint:
            _log(f"hint: {_term(hint)}")
        moved[d] = target
    return [(t, tg, moved[p.parent] / p.name if p.parent in moved else p) for t, tg, p in plan]


def _skipped(n: int) -> str:
    return f"{n} track{'s' if n != 1 else ''} skipped (already exist; --overwrite replaces them)"


# ------------------------------------------------------------ main flow

def _term(text: object) -> str:
    return safetext.for_terminal(str(text))


def _log(msg: str) -> None:
    print(safetext.for_terminal(msg, keep_tab_newline=True), file=sys.stderr, flush=True)


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
    if opts.disc:
        try:
            parse_disc(opts.disc)
        except ValueError as e:
            raise SystemExit(str(e)) from None
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
        if opts.disc:
            n, m = parse_disc(opts.disc)
            tags["DISCNUMBER"], tags["DISCTOTAL"] = [str(n)], [str(m)]
        vals = naming_values(tags, area, opts)
        rel = Path(render(opts.dir_template, vals, opts.portable_names)) / (
            render(opts.file_template, vals, opts.portable_names) + "." + EXTENSIONS[opts.format])
        plan.append((t, tags, Path(opts.output_dir) / rel))

    plan = _avoid_other_discs(plan, opts.portable_names)
    dirs = sorted({p.parent for _, _, p in plan})
    _log(f"{label}: {len(plan)} tracks -> {', '.join(map(str, dirs))}")
    if opts.dry_run:
        for t, tags, path in plan:
            print(safetext.for_terminal(f"  {t.duration}  {path}"))
        return 0

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        if cover and opts.cover_file and not (d / opts.cover_file).exists():
            (d / opts.cover_file).write_bytes(cover[0])

    embed = cover if opts.cover else None
    fmt = opts.format
    specs, todo = [], []
    skipped = 0
    for t, tags, path in plan:
        if path.exists() and not opts.overwrite:
            _log(f"  {t.number:2d} exists, skipped: {path.name}")
            skipped += 1
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
        _log(f"{label}: nothing to do, {_skipped(skipped)}")
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
         + (f", {failures} failed" if failures else "")
         + (f", {_skipped(skipped)}" if skipped else ""))
    return failures


def _progress_printer(label: str):
    def show(p: _native.Progress) -> None:
        if p.frames_total and sys.stderr.isatty():
            pct = 100 * p.frames_done // p.frames_total
            print(f"\r{label}: {pct:3d}% ({p.frames_done // FRAME_RATE}s of {p.frames_total // FRAME_RATE}s audio)",
                  end="", file=sys.stderr, flush=True)
    return show
