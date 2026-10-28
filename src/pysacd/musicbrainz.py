"""Minimal MusicBrainz + Cover Art Archive client (stdlib only).

Finds the MusicBrainz release (and medium) for an SACD area by barcode or
catalog number, then checks the match against the disc's track count,
track durations and ISRCs.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import __version__
from .toc import Area, Disc

API = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"
RELEASE_INC = "artist-credits+labels+recordings+release-groups+isrcs"

_lock = threading.Lock()
_last_request = 0.0


class MBError(RuntimeError):
    pass


def _user_agent(contact: str | None) -> str:
    return f"pysacd/{__version__} ( {contact or 'https://musicbrainz.org/doc/MusicBrainz_API'} )"


MAX_JSON = 8 << 20          # largest MusicBrainz response read (a big release is ~1 MB)
MAX_COVER = 25 << 20        # largest cover image read ("full" size covers can be ~10 MB)
_IMAGE_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))


def _read_capped(r, limit: int, url: str) -> bytes:
    """The response body, read in chunks; MBError beyond `limit` bytes (#14)."""
    out = bytearray()
    while chunk := r.read(min(1 << 16, limit + 1 - len(out))):
        out += chunk
        if len(out) > limit:
            raise MBError(f"{url}: response larger than {limit} bytes")
    return bytes(out)


def image_type(data: bytes) -> str | None:
    """'image/jpeg' or 'image/png' from the magic bytes, or None (whatever the server said)."""
    return next((mime for magic, mime in _IMAGE_MAGIC if data.startswith(magic)), None)


def set_server(base: str) -> None:
    """Use another MusicBrainz server, e.g. https://test.musicbrainz.org for trying submissions."""
    global API
    u = urllib.parse.urlsplit(base)
    if u.scheme != "https" or not u.hostname:
        raise MBError(f"MusicBrainz server must be an https:// URL: {base!r}")
    API = f"https://{u.netloc}/ws/2"


def _throttle(url: str) -> None:
    """MusicBrainz allows about one request per second per client."""
    global _last_request
    host = urllib.parse.urlsplit(url).hostname or ""
    with _lock:
        wait = _last_request + 1.1 - time.monotonic()
        if wait > 0 and (host == "musicbrainz.org" or host.endswith(".musicbrainz.org")):
            time.sleep(wait)
        _last_request = time.monotonic()


def _get(url: str, contact: str | None, accept: str = "application/json", retries: int = 5,
         limit: int = MAX_JSON) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(contact), "Accept": accept})
    for attempt in range(retries):
        _throttle(url)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return _read_capped(r, limit, url), r.headers.get_content_type()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise MBError(f"not found: {url}") from None
            if e.code in (429, 503) and attempt + 1 < retries:
                time.sleep(2 ** (attempt + 1))  # rate limited: back off
                continue
            raise MBError(f"HTTP {e.code} for {url}") from None
        except urllib.error.URLError as e:
            if attempt + 1 < retries:
                time.sleep(2)
                continue
            raise MBError(f"{url}: {e.reason}") from None
    raise MBError(f"giving up on {url}")


def _json(path: str, contact: str | None, **params) -> dict:
    params["fmt"] = "json"
    body, _ = _get(f"{API}/{path}?{urllib.parse.urlencode(params)}", contact)
    try:
        data = json.loads(body)
    except ValueError:
        raise MBError(f"{path}: response is not JSON") from None
    if not isinstance(data, dict):
        raise MBError(f"{path}: response is not a JSON object")
    return data


def _lucene_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def credit_name(credits: list[dict]) -> str:
    return "".join((_str(c.get("name")) or "") + (_str(c.get("joinphrase")) or "") for c in _dicts(credits))


@dataclass
class Match:
    release: dict
    medium: dict
    score: float                 # mean |duration diff| in seconds
    isrc_hits: int
    method: str                  # "barcode", "catno", "text", "release-id"
    warnings: list[str] = field(default_factory=list)

    @property
    def release_id(self) -> str:
        return self.release["id"]


def candidate_ids(disc: Disc, contact: str | None, text_search: bool = False) -> list[tuple[str, str]]:
    """(release id, method) candidates, best methods first."""
    out: list[tuple[str, str]] = []
    seen = set()

    def add(releases, method):
        for r in _dicts(releases):
            if _str(r.get("id")) is not None and r["id"] not in seen:
                seen.add(r["id"])
                out.append((r["id"], method))

    barcode = barcode_of(disc)
    if barcode:
        add(_json("release", contact, query=f"barcode:{barcode}", limit=25).get("releases", []), "barcode")
    for catno in {disc.disc_catalog_number, disc.album_catalog_number} - {""}:
        if catno and not re.fullmatch(r"0+", catno.replace(" ", "")) and catno != barcode:
            add(_json("release", contact, query=f"catno:{_lucene_quote(catno)}", limit=25).get("releases", []), "catno")
    if text_search and not out:
        title = disc.text.get("album_title") or disc.text.get("disc_title")
        artist = disc.text.get("album_artist") or disc.text.get("disc_artist")
        if title:
            q = f"release:{_lucene_quote(title)}"
            if artist:
                q += f" AND artist:{_lucene_quote(artist)}"
            add(_json("release", contact, query=q, limit=10).get("releases", []), "text")
    return out


def barcode_of(disc: Disc) -> str | None:
    """The album catalog number is a UPC/EAN on most SACDs."""
    s = disc.album_catalog_number.replace(" ", "")
    if re.fullmatch(r"\d{12,14}", s) and not re.fullmatch(r"0+", s):
        return s
    return None


def lookup_release(release_id: str, contact: str | None) -> dict:
    rel = clean_release(_json(f"release/{release_id}", contact, inc=RELEASE_INC))
    if rel is None:
        raise MBError(f"release {release_id}: malformed response")
    return rel


# --- validating MusicBrainz JSON (#47) -------------------------------------------------------
# A release is third-party data: any field can be null, a list where a string belongs, or a
# string where a number belongs. clean_release() keeps only the fields pysacd uses, each of
# the right type. Anything else is dropped, and the disc's own text is used instead.

def _str(v) -> str | None:
    return v if isinstance(v, str) else None


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e12 else None


def _int(v) -> int | None:
    if isinstance(v, int) and not isinstance(v, bool):
        return v if 0 <= v < 1 << 31 else None
    if isinstance(v, str) and v.isascii() and v.isdigit() and len(v) < 10:
        return int(v)
    return None


def _dicts(v) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _pick(d: dict, **types) -> dict:
    """The keys of d whose values pass their checker (None = drop)."""
    out = {}
    for key, check in types.items():
        val = check(d.get(key.replace("_", "-")))
        if val is not None:
            out[key.replace("_", "-")] = val
    return out


def _credits(v) -> list[dict]:
    out = []
    for c in _dicts(v):
        artist = c.get("artist")
        if not isinstance(artist, dict) or _str(artist.get("id")) is None or _str(artist.get("name")) is None:
            continue
        out.append({"name": _str(c.get("name")) or artist["name"], "joinphrase": _str(c.get("joinphrase")) or "",
                    "artist": {"id": artist["id"], "name": artist["name"]}})
    return out


def _recording(v) -> dict | None:
    if not isinstance(v, dict):
        return None
    r = _pick(v, id=_str, title=_str, length=_num)
    r["isrcs"] = [x for x in v.get("isrcs") or [] if isinstance(x, str)] if isinstance(v.get("isrcs"), list) else []
    r["artist-credit"] = _credits(v.get("artist-credit"))
    return r


def _track(v: dict) -> dict:
    t = _pick(v, id=_str, title=_str, number=_str, length=_num, position=_int)
    t["artist-credit"] = _credits(v.get("artist-credit"))
    rec = _recording(v.get("recording"))
    if rec is not None:
        t["recording"] = rec
    return t


def clean_release(v) -> dict | None:
    """A release with only well-typed fields (see above), or None without a string id."""
    if not isinstance(v, dict) or _str(v.get("id")) is None:
        return None
    rel = _pick(v, id=_str, title=_str, date=_str, barcode=_str, country=_str, status=_str)
    rel["artist-credit"] = _credits(v.get("artist-credit"))
    rg = v.get("release-group")
    if isinstance(rg, dict):
        rel["release-group"] = _pick(rg, id=_str, first_release_date=_str, primary_type=_str)
    rel["label-info"] = [
        {**({"label": {"name": li["label"]["name"]}} if isinstance(li.get("label"), dict)
            and _str(li["label"].get("name")) is not None else {}),
         **_pick(li, catalog_number=_str)}
        for li in _dicts(v.get("label-info"))]
    rel["media"] = [{**_pick(m, format=_str, position=_int), "tracks": [_track(t) for t in _dicts(m.get("tracks"))]}
                    for m in _dicts(v.get("media"))]
    return rel


def physical_discs(media: list[dict]) -> list[int]:
    """Physical disc number for each medium.

    MusicBrainz lists each layer of a disc as its own medium, e.g.
    "Hybrid SACD (CD layer)", "Hybrid SACD (SACD layer, 2 channels)",
    "Hybrid SACD (SACD layer, multichannel)" for one hybrid disc.
    """
    out, n, base = [], 0, None
    seen: set[str] = set()
    for m in media:
        f = m.get("format") or ""
        b = f.split(" (")[0]
        layered = "(" in f and ("layer" in f or "channel" in f)
        new_disc = (not layered or b != base or f in seen or
                    ("(CD layer" in f and seen))
        if new_disc:
            n, base, seen = n + 1, b, set()
        seen.add(f)
        out.append(n)
    return out


def score_medium(release: dict, medium: dict, disc: Disc, area: Area) -> tuple[float, int] | None:
    tracks = medium.get("tracks") or []
    if len(tracks) != len(area.tracks):
        return None
    diffs = []
    isrc_hits = 0
    for mt, at in zip(tracks, area.tracks):
        if mt.get("length"):
            diffs.append(abs(mt["length"] / 1000 - at.duration.seconds_float))
        if at.isrc and at.isrc in (mt.get("recording", {}).get("isrcs") or []):
            isrc_hits += 1
    score = sum(diffs) / len(diffs) if diffs else 99.0
    return score, isrc_hits


def find_release(disc: Disc, area: Area, contact: str | None, release_id: str | None = None,
                 text_search: bool = False, max_mean_diff: float = 3.0) -> Match | None:
    cands = [(release_id, "release-id")] if release_id else candidate_ids(disc, contact, text_search)
    best: Match | None = None
    best_rank = 0.0
    for rid, method in cands:
        rel = lookup_release(rid, contact)
        for medium in rel.get("media", []):
            s = score_medium(rel, medium, disc, area)
            if s is None:
                continue
            score, hits = s
            fmt = medium.get("format") or ""
            # Prefer the SACD layer matching this area, then the disc's position in a set
            rank = score - hits * 0.2
            if "SACD" in fmt and "(CD layer" not in fmt:
                rank -= 0.5
            if "(CD layer" in fmt:
                rank += 0.5
            if ("multichannel" in fmt) == area.is_multichannel and "channel" in fmt:
                rank -= 0.5
            if disc.album_set_size > 1:
                discno = physical_discs(rel["media"])[rel["media"].index(medium)]
                if discno == disc.album_sequence_number:
                    rank -= 1
            if best is None or rank < best_rank:
                best, best_rank = Match(rel, medium, score, hits, method), rank
    if best is None:
        return None
    if best.score > max_mean_diff and best.isrc_hits < len(area.tracks) // 2 and best.method != "release-id":
        return None
    if "SACD" not in (best.medium.get("format") or "") or "(CD layer" in best.medium.get("format", ""):
        best.warnings.append(f"matched medium format is {best.medium.get('format')!r}, not SACD")
    if best.method == "text":
        best.warnings.append("matched by title search: release-level data (date, label, catalog) may be a different pressing")
    return best


def fetch_cover(release: dict, contact: str | None, size: str = "1200") -> tuple[bytes, str] | None:
    """Front cover from the Cover Art Archive (release, then release group)."""
    suffix = "front" if size == "full" else f"front-{size}"
    urls = [f"{CAA}/release/{release['id']}/{suffix}"]
    rg = release.get("release-group", {}).get("id")
    if rg:
        urls.append(f"{CAA}/release-group/{rg}/{suffix}")
    for url in urls:
        try:
            data, _ = _get(url, contact, accept="image/*", limit=MAX_COVER)
        except MBError:
            continue
        # Embed only what is really a JPEG or PNG, whatever the Content-Type says (#14)
        mime = image_type(data)
        if mime:
            return data, mime
    return None
