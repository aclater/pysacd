"""python -m pysacd [--json] IMAGE.iso ... — print the TOC of SACD ISO images."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from .toc import Disc, SACDError, read_toc


def _to_json(disc: Disc) -> dict:
    d = dataclasses.asdict(disc)
    d["manufacturer_info"] = disc.manufacturer_info.rstrip(b"\0").hex()
    for area, ad in zip(disc.areas, d["areas"]):
        ad["frame_format_name"] = area.frame_format_name
        ad["speaker_config"] = area.speaker_config
        for track, td in zip(area.tracks, ad["tracks"]):
            td["start_seconds"] = track.start.seconds_float
            td["duration_seconds"] = track.duration.seconds_float
    return d


def _print(disc: Disc, path: str) -> None:
    print(f"== {path}")
    print(f"Disc:      {disc.artist or '?'} - {disc.title or '?'}")
    for k in ("album_title", "album_artist", "album_publisher", "album_copyright"):
        if k in disc.text:
            print(f"  {k:16} {disc.text[k]}")
    y, m, day = disc.date
    print(f"  version {disc.version[0]}.{disc.version[1]:02d}  hybrid={disc.hybrid}"
          f"  set {disc.album_sequence_number}/{disc.album_set_size}"
          f"  date {y:04d}-{m:02d}-{day:02d}")
    if disc.disc_catalog_number:
        print(f"  catalog   {disc.disc_catalog_number}")
    genres = [str(g) for g in disc.disc_genres if g.genre > 1]
    if genres:
        print(f"  genre     {', '.join(genres)}")
    for area in disc.areas:
        print(f"Area {area.kind}: {area.speaker_config}, {area.frame_format_name},"
              f" {len(area.tracks)} tracks, {area.total_playtime}"
              f" (LSN {area.track_start}-{area.track_end})")
        for t in area.tracks:
            who = f" [{t.performer}]" if t.performer else ""
            print(f"  {t.number:2d}. {t.duration}  {t.title or ''}{who}")
    for w in disc.warnings + [w for a in disc.areas for w in a.warnings]:
        print(f"  warning: {w}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pysacd", description=__doc__)
    ap.add_argument("images", nargs="+")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    rc = 0
    out = []
    for path in args.images:
        try:
            disc = read_toc(path)
        except (OSError, SACDError) as e:
            print(f"{path}: {e}", file=sys.stderr)
            rc = 1
            continue
        if args.json:
            out.append({"path": path, **_to_json(disc)})
        else:
            _print(disc, path)
    if args.json:
        json.dump(out if len(out) != 1 else out[0], sys.stdout, ensure_ascii=False, indent=2)
        print()
    return rc


if __name__ == "__main__":
    sys.exit(main())
