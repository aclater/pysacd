"""Build tags from the SACD TOC (+ optional MusicBrainz match) and write them.

Tags are kept as Vorbis-comment style {"KEY": [values]} and mapped to ID3
for DSF/WAV using Picard's conventions.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import __version__, safetext
from .musicbrainz import Match, barcode_of, credit_name, physical_discs
from .toc import Area, Disc, Track

Tags = dict[str, list[str]]

# Vorbis key -> ID3 frame id (text frames); everything else becomes TXXX
_ID3_TEXT = {
    "TITLE": "TIT2", "ARTIST": "TPE1", "ALBUM": "TALB", "ALBUMARTIST": "TPE2",
    "DATE": "TDRC", "ORIGINALDATE": "TDOR", "GENRE": "TCON", "COMPOSER": "TCOM",
    "LYRICIST": "TEXT", "LABEL": "TPUB", "ISRC": "TSRC", "COPYRIGHT": "TCOP",
    "MEDIA": "TMED", "ENCODER": "TSSE", "ARTISTSORT": "TSOP", "ALBUMARTISTSORT": "TSO2",
}
_ID3_TXXX = {
    "MUSICBRAINZ_ALBUMID": "MusicBrainz Album Id",
    "MUSICBRAINZ_RELEASEGROUPID": "MusicBrainz Release Group Id",
    "MUSICBRAINZ_ARTISTID": "MusicBrainz Artist Id",
    "MUSICBRAINZ_ALBUMARTISTID": "MusicBrainz Album Artist Id",
    "MUSICBRAINZ_RELEASETRACKID": "MusicBrainz Release Track Id",
    "RELEASECOUNTRY": "MusicBrainz Album Release Country",
    "RELEASESTATUS": "MusicBrainz Album Status",
    "RELEASETYPE": "MusicBrainz Album Type",
}

log = logging.getLogger("pysacd.tagging")


def _put(tags: Tags, key: str, *values) -> None:
    # Untrusted text (disc or MusicBrainz): no control, bidi or zero-width characters in tags.
    # Only strings and numbers: a list or dict from MusicBrainz must not become "['x']" (#47)
    vals = [safetext.clean_text(str(v)) for v in values
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip()]
    vals = [v for v in vals if v]
    if vals:
        tags[key] = vals


def base_tags(disc: Disc, area: Area, track: Track) -> Tags:
    """Tags from the disc's own TOC text."""
    t: Tags = {}
    album = disc.text.get("album_title") or disc.text.get("disc_title")
    albumartist = disc.text.get("album_artist") or disc.text.get("disc_artist")
    _put(t, "ALBUM", album)
    _put(t, "ALBUMARTIST", albumartist)
    _put(t, "TITLE", track.title)
    _put(t, "ARTIST", track.performer or albumartist)
    _put(t, "COMPOSER", track.text.get("composer"))
    _put(t, "LYRICIST", track.text.get("songwriter"))
    _put(t, "ARRANGER", track.text.get("arranger"))
    _put(t, "COMMENT", track.text.get("message"))
    _put(t, "ISRC", track.isrc)
    _put(t, "TRACKNUMBER", track.number)
    _put(t, "TRACKTOTAL", len(area.tracks))
    _put(t, "DISCNUMBER", disc.album_sequence_number or 1)
    _put(t, "DISCTOTAL", disc.album_set_size or 1)
    y, m, d = disc.date
    if y:
        _put(t, "DATE", f"{y:04d}-{m:02d}-{d:02d}" if m and d else f"{y:04d}")
    genre = track.genre if track.genre and track.genre.genre > 1 else next(
        (g for g in disc.album_genres if g.genre > 1), None)
    if genre:
        _put(t, "GENRE", genre.name)
    _put(t, "LABEL", disc.text.get("album_publisher") or disc.text.get("disc_publisher"))
    _put(t, "COPYRIGHT", disc.text.get("album_copyright") or disc.text.get("disc_copyright"))
    _put(t, "CATALOGNUMBER", disc.disc_catalog_number)
    _put(t, "BARCODE", barcode_of(disc))
    _put(t, "MEDIA", "Hybrid SACD" if disc.hybrid else "SACD")
    _put(t, "ENCODER", f"pysacd {__version__}")
    return t


def apply_musicbrainz(t: Tags, match: Match, track: Track) -> None:
    """Overlay MusicBrainz data; SACD-only fields (composer, ISRC...) are kept.

    The release has been validated (musicbrainz.clean_release); if the overlay still fails,
    the track keeps its disc text (#47)."""
    new = dict(t)
    try:
        _overlay(new, match, track)
    except (TypeError, KeyError, ValueError, AttributeError, IndexError) as e:
        log.warning("MusicBrainz data for track %d is malformed (%s: %s); using the disc's text",
                    track.number, type(e).__name__, e)
        return
    t.clear()
    t.update(new)


def _overlay(t: Tags, match: Match, track: Track) -> None:
    rel, med = match.release, match.medium
    mt = med["tracks"][track.number - 1]
    rec = mt.get("recording", {})
    rg = rel.get("release-group", {})
    _put(t, "ALBUM", rel.get("title"))
    _put(t, "ALBUMARTIST", credit_name(rel.get("artist-credit", [])))
    _put(t, "TITLE", mt.get("title") or rec.get("title"))
    credits = mt.get("artist-credit") or rec.get("artist-credit") or rel.get("artist-credit", [])
    _put(t, "ARTIST", credit_name(credits))
    _put(t, "DATE", rel.get("date"))
    _put(t, "ORIGINALDATE", rg.get("first-release-date"))
    discs = physical_discs(rel.get("media", []))
    _put(t, "DISCNUMBER", discs[rel["media"].index(med)])
    _put(t, "DISCTOTAL", max(discs))
    _put(t, "TRACKNUMBER", mt.get("position"))
    labels = rel.get("label-info") or []
    _put(t, "LABEL", *dict.fromkeys(li["label"]["name"] for li in labels if li.get("label")))
    _put(t, "CATALOGNUMBER", *dict.fromkeys(li["catalog-number"] for li in labels if li.get("catalog-number")))
    _put(t, "BARCODE", rel.get("barcode"))
    _put(t, "MEDIA", med.get("format"))
    _put(t, "RELEASECOUNTRY", rel.get("country"))
    _put(t, "RELEASESTATUS", (rel.get("status") or "").lower())
    _put(t, "RELEASETYPE", (rg.get("primary-type") or "").lower())
    _put(t, "MUSICBRAINZ_ALBUMID", rel["id"])
    _put(t, "MUSICBRAINZ_RELEASEGROUPID", rg.get("id"))
    _put(t, "MUSICBRAINZ_RELEASETRACKID", mt.get("id"))
    _put(t, "MUSICBRAINZ_TRACKID", rec.get("id"))
    _put(t, "MUSICBRAINZ_ARTISTID", *[c["artist"]["id"] for c in credits])
    _put(t, "MUSICBRAINZ_ALBUMARTISTID", *[c["artist"]["id"] for c in rel.get("artist-credit", [])])
    if not track.isrc:
        _put(t, "ISRC", *(rec.get("isrcs") or [])[:1])


# ------------------------------------------------------------ writers

def write_tags(path: str, fmt: str, tags: Tags, cover: tuple[bytes, str] | None,
               id3_version: int = 3) -> None:
    """Tag a finished FLAC or WAV file (DSF tags are written by the engine, see id3_bytes)."""
    if fmt == "flac":
        _write_flac(path, tags, cover)
    else:
        from mutagen.wave import WAVE

        f = WAVE(path)
        if f.tags is None:
            f.add_tags()
        assert f.tags is not None
        f.tags.clear()
        _fill_id3(f.tags, tags, cover)
        f.save(v2_version=id3_version)


def id3_bytes(tags: Tags, cover: tuple[bytes, str] | None, version: int = 3) -> bytes:
    """A complete ID3v2 tag, e.g. for the engine to append to a DSF file."""
    import io

    from mutagen import id3

    t = id3.ID3()
    _fill_id3(t, tags, cover)
    buf = io.BytesIO()
    t.save(buf, v2_version=version, padding=lambda info: 0)
    return buf.getvalue()


def flac_metadata_size(tags: Tags, cover: tuple[bytes, str] | None) -> int:
    """Generous size for the PADDING block so tags fit without rewriting the file."""
    size = 1024 + sum(len(k) + 1 + len(v.encode()) + 4 for k, vals in tags.items() for v in vals)
    if cover:
        size += len(cover[0]) + len(cover[1]) + 64
    return size + 4096


def read_tags(path: str | os.PathLike) -> Tags:
    """Tags of an existing DSF, DSDIFF, FLAC or WAV file, as Vorbis-style keys
    (the inverse of the writers above); {} if the file has none or can't be read."""
    p = Path(path)
    try:
        if p.suffix.lower() == ".flac":
            from mutagen.flac import FLAC

            f = FLAC(p)
            vorbis: Tags = {}
            items: list[tuple[object, object]] = list(f.tags.items()) if f.tags is not None else []
            for key, value in items:
                values = value if isinstance(value, list) else [value]   # mutagen versions differ
                vorbis.setdefault(str(key).upper(), []).extend(str(v) for v in values)
            return vorbis
        from mutagen.dsdiff import DSDIFF
        from mutagen.dsf import DSF
        from mutagen.wave import WAVE

        reader = {".dsf": DSF, ".dff": DSDIFF}.get(p.suffix.lower(), WAVE)
        id3 = reader(p).tags
    except Exception:  # noqa: BLE001 - unreadable files just have no identity
        return {}
    if id3 is None:
        return {}
    out: Tags = {}
    for key, frame in _ID3_TEXT.items():
        if frame in id3:
            out[key] = [str(x) for x in id3[frame].text]
    for key, desc in _ID3_TXXX.items():
        f = id3.get(f"TXXX:{desc}")
        if f is not None:
            out[key] = [str(x) for x in f.text]
    for key in ("CATALOGNUMBER", "BARCODE"):
        f = id3.get(f"TXXX:{key}")
        if f is not None:
            out[key] = [str(x) for x in f.text]
    for frame, num, tot in (("TRCK", "TRACKNUMBER", "TRACKTOTAL"), ("TPOS", "DISCNUMBER", "DISCTOTAL")):
        if frame in id3:
            n, _, t = str(id3[frame].text[0]).partition("/")
            out[num] = [n]
            if t:
                out[tot] = [t]
    return out


def _write_flac(path: str, tags: Tags, cover) -> None:
    from mutagen.flac import FLAC, Picture

    f = FLAC(path)
    f.delete()
    for k, v in tags.items():
        f[k] = v
    if cover:
        pic = Picture()
        pic.type = 3
        pic.data, pic.mime = cover
        pic.desc = "Front"
        f.clear_pictures()
        f.add_picture(pic)
    # Shrink the engine's PADDING block instead of moving the audio
    f.save(padding=lambda info: info.padding if info.padding >= 0 else 1024)


def _fill_id3(t, tags: Tags, cover) -> None:
    from mutagen import id3

    for k, v in tags.items():
        if k in ("TRACKNUMBER", "TRACKTOTAL", "DISCNUMBER", "DISCTOTAL"):
            continue
        if k in _ID3_TEXT:
            t.add(getattr(id3, _ID3_TEXT[k])(encoding=3, text=v))
        elif k == "MUSICBRAINZ_TRACKID":
            t.add(id3.UFID(owner="http://musicbrainz.org", data=v[0].encode()))
        elif k == "COMMENT":
            t.add(id3.COMM(encoding=3, lang="eng", desc="", text=v))
        else:
            t.add(id3.TXXX(encoding=3, desc=_ID3_TXXX.get(k, k), text=v))
    for frame, num, tot in (("TRCK", "TRACKNUMBER", "TRACKTOTAL"), ("TPOS", "DISCNUMBER", "DISCTOTAL")):
        if num in tags:
            s = tags[num][0] + (f"/{tags[tot][0]}" if tot in tags else "")
            t.add(getattr(id3, frame)(encoding=3, text=[s]))
    if cover:
        t.add(id3.APIC(encoding=3, mime=cover[1], type=3, desc="Front", data=cover[0]))
