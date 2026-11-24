"""Minimal MusicBrainz + Cover Art Archive client (stdlib only).

Finds the MusicBrainz release (and medium) for an SACD area by barcode or
catalog number, then checks the match against the disc's track count,
track durations and ISRCs.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import __version__, safetext
from .toc import Area, Disc

API = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"
RELEASE_INC = "artist-credits+labels+recordings+release-groups+isrcs"
# A MusicBrainz identifier (MBID): a lowercase UUID. Release, release-group, track, recording and
# artist ids are kept only in this form, because they go into request paths, cover URLs, tags and
# the terminal; anything else a server sends in their place is dropped.
MBID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

log = logging.getLogger("pysacd.musicbrainz")

_lock = threading.Lock()
_last_request = 0.0


class MBError(RuntimeError):
    pass


class RedirectHostError(MBError):
    """A redirect to a host outside the request's allowed hosts."""


def _user_agent(contact: str | None) -> str:
    return f"pysacd/{__version__} ( {contact or 'https://musicbrainz.org/doc/MusicBrainz_API'} )"


MAX_JSON = 8 << 20          # largest MusicBrainz response read (a big release is ~1 MB)
MAX_COVER = 25 << 20        # largest cover image read ("full" size covers can be ~10 MB)
_IMAGE_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))

# Each socket operation times out after SOCKET_TIMEOUT, but that alone lets a server trickle a
# response a byte at a time for as long as it likes. These deadlines bound a whole request,
# from connecting to the last byte. A large release (~1 MB of JSON) arrives in a second or two;
# 60 s leaves room for a slow link. A 25 MB "full" cover needs 300 s at ~85 kB/s, far below any
# broadband or mobile link, and covers are usually 1200 px images of a few hundred kB.
SOCKET_TIMEOUT = 30.0
JSON_DEADLINE = 60.0
COVER_DEADLINE = 300.0
MAX_REDIRECTS = 5           # Cover Art Archive redirects once or twice (to archive.org)
# The only hosts a cover download may be redirected to (subdomains included): the Cover Art
# Archive's images are served by the Internet Archive (ia*.us.archive.org and the like). A
# response can't steer the download anywhere else (#155).
COVER_HOSTS = ("coverartarchive.org", "archive.org")


def _read_capped(r, limit: int, url: str, deadline: float | None = None) -> bytes:
    """The response body, read in chunks; MBError beyond `limit` bytes (#14) or after the
    monotonic `deadline`. read1() returns what one socket read brings, so the deadline is
    checked at least every SOCKET_TIMEOUT seconds however slowly the data comes."""
    out = bytearray()
    read = getattr(r, "read1", None) or r.read
    while True:
        if deadline is not None and time.monotonic() > deadline:
            raise MBError(f"{url}: response not complete in time")
        chunk = read(min(1 << 16, limit + 1 - len(out)))
        if not chunk:
            left = getattr(r, "length", None)       # http.client: bytes of Content-Length still due
            if isinstance(left, int) and left > 0:
                raise http.client.IncompleteRead(bytes(out), left)
            return bytes(out)
        if not isinstance(chunk, (bytes, bytearray)):
            # never loop on something that isn't data (a test double, a broken response object)
            raise MBError(f"{url}: unreadable response")
        out += chunk
        if len(out) > limit:
            raise MBError(f"{url}: response larger than {limit} bytes")


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _split(url: str) -> urllib.parse.SplitResult | None:
    """urlsplit(), or None for a URL it refuses (an unbalanced or non-address `[...]` host)."""
    try:
        return urllib.parse.urlsplit(url)
    except ValueError:
        return None


def _allowed_url(url: str) -> bool:
    """https only. Plain http is allowed only to a loopback address, and only when API itself is
    an http URL on a loopback address. That exception is test scaffolding: the tests point API
    at local servers by patching it; set_server(), the only way a user sets it, takes https only."""
    u = _split(url)
    if u is None:
        return False
    if u.scheme == "https":
        return True
    api = urllib.parse.urlsplit(API)
    return u.scheme == "http" and api.scheme == "http" and _loopback(api.hostname) and _loopback(u.hostname)


_HOST_NAME = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.?")


def _host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    """Whether `url` is an https URL whose host is one of `hosts` or a subdomain of one.

    urllib connects to the netloc after percent-decoding it, which is not always the host
    urlsplit() reports, so only a netloc that is exactly a plain host name, or that name and
    `:443`, is accepted: no userinfo, `%`, backslash, empty label or other port (#172). Anything
    but https passes only as the loopback test scaffolding _allowed_url() describes."""
    u = _split(url)
    if u is None or any(c <= " " or c == "\x7f" for c in url):    # urlsplit() drops tabs and newlines
        return False
    host = (u.hostname or "").lower()
    if u.scheme != "https":
        return _allowed_url(url) and _loopback(host)
    if not _HOST_NAME.fullmatch(host) or u.netloc.lower() not in (host, host + ":443"):
        return False
    host = host.rstrip(".")
    return any(host == h or host.endswith("." + h) for h in hosts)


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follow at most MAX_REDIRECTS redirects, and only to URLs _allowed_url() accepts. With
    follow=False (requests that carry credentials) refuse every redirect, so an Authorization
    header or a password never reaches another URL, whatever the Python version does. With
    `hosts`, a redirect must also stay on those hosts or their subdomains."""

    max_redirections = MAX_REDIRECTS

    def __init__(self, follow: bool = True, hosts: tuple[str, ...] | None = None):
        super().__init__()
        self.follow = follow
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.follow and _allowed_url(newurl) and self.hosts is not None \
                and not _host_allowed(newurl, self.hosts):
            fp.close()
            u = _split(newurl)
            host = safetext.for_terminal((u.netloc if u else "") or "an unreadable address")
            raise RedirectHostError(
                f"redirect to {host} refused: only {' and '.join(self.hosts)} hosts are followed here")
        if not self.follow or not _allowed_url(newurl):
            fp.close()
            why = "redirect refused for an authenticated request" if not self.follow else \
                "redirect to a non-https or malformed URL refused"
            raise MBError(f"{req.full_url}: HTTP {code}: {why}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(req: urllib.request.Request, timeout: float = SOCKET_TIMEOUT, follow: bool = True,
          handlers: tuple = (), deadline: float | None = None,
          redirect_hosts: tuple[str, ...] | None = None):
    """Open `req` with the redirect rules above (every MusicBrainz, Cover Art Archive and OAuth
    request goes through here).

    `timeout` bounds each socket operation only, so a server that sends its status line and
    headers a byte at a time could hold the call for as long as the headers last. With the
    monotonic `deadline`, connecting, redirects and headers together end by then: the request
    runs in a thread, and if it has no response in time the caller gets MBError and moves on.
    The abandoned connection is closed when its own next read returns or times out."""
    if not _allowed_url(req.full_url):
        raise MBError(f"{req.full_url}: only https:// URLs are used")
    opener = urllib.request.build_opener(_Redirects(follow, redirect_hosts), *handlers)
    if deadline is None:
        return opener.open(req, timeout=timeout)
    box: dict = {}
    guard = threading.Lock()

    def run() -> None:
        try:
            r = opener.open(req, timeout=timeout)
        except BaseException as e:  # noqa: BLE001 - handed to the caller, which raises it
            with guard:
                box.setdefault("error", e)
            return
        with guard:
            if "abandoned" in box:
                r.close()
            else:
                box["response"] = r

    t = threading.Thread(target=run, name="pysacd-http", daemon=True)
    t.start()
    t.join(max(0.0, deadline - time.monotonic()))
    with guard:
        if "response" in box:
            return box["response"]
        if "error" in box:
            raise box["error"]
        box["abandoned"] = True
    raise MBError(f"{req.full_url}: no response in time")


def _net_error(e: BaseException) -> str:
    """A short description of a network failure (OSError, http.client.HTTPException, URLError)."""
    if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
        return str(e.reason)[:200]
    return f"{type(e).__name__}: {str(e)[:200]}" if str(e) else type(e).__name__


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
         limit: int = MAX_JSON, deadline: float = JSON_DEADLINE,
         redirect_hosts: tuple[str, ...] | None = None) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(contact), "Accept": accept})
    for attempt in range(retries):
        _throttle(url)
        try:
            # The deadline covers one attempt: connecting, the headers (_open) and the whole
            # body (_read_capped)
            end = time.monotonic() + deadline
            with _open(req, timeout=min(SOCKET_TIMEOUT, deadline), deadline=end,
                       redirect_hosts=redirect_hosts) as r:
                return _read_capped(r, limit, url, end), r.headers.get_content_type()
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 404:
                raise MBError(f"not found: {url}") from None
            if e.code in (429, 503) and attempt + 1 < retries:
                time.sleep(2 ** (attempt + 1))  # rate limited: back off
                continue
            raise MBError(f"HTTP {e.code} for {url}") from None
        except (OSError, http.client.HTTPException) as e:
            # URLError, and what reading the body raises: timeouts, resets, TLS errors,
            # IncompleteRead. All are retried, then reported as MBError.
            if attempt + 1 < retries:
                time.sleep(2)
                continue
            raise MBError(f"{url}: {_net_error(e)}") from None
        except ValueError as e:
            # a redirect or URL that urllib can't parse (e.g. "https://[x]/"): not retried
            kind = RedirectHostError if redirect_hosts is not None else MBError
            raise kind(f"{url}: malformed address in the response ({safetext.for_terminal(e)})") from None
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
    # True when a multichannel area matched through the disc's stereo area, not its own track
    # lengths (#218): the release is the stereo area's, the medium its multichannel layer
    via_stereo: bool = False

    @property
    def release_id(self) -> str:
        return self.release["id"]


def candidate_ids(disc: Disc, contact: str | None, text_search: bool = False) -> list[tuple[str, str]]:
    """(release id, method) candidates, best methods first."""
    out: list[tuple[str, str]] = []
    seen = set()

    def add(releases, method):
        for r in _dicts(releases):
            if _mbid(r.get("id")) is not None and r["id"] not in seen:
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


def release_id_arg(value: str) -> str:
    """A user-supplied release id (--mb-release, the mb_release config key) in canonical form;
    MBError with a clear message if it isn't an MBID."""
    rid = value.strip().lower() if isinstance(value, str) else ""
    if not MBID_RE.fullmatch(rid):
        shown = safetext.for_terminal(str(value)[:80])
        raise MBError(f"--mb-release '{shown}' is not a MusicBrainz release id "
                      "(expected the 36-character id from the release's URL, like 0a1b2c3d-...-...)")
    return rid


def lookup_release(release_id: str, contact: str | None) -> dict:
    if _mbid(release_id) is None:
        raise MBError("release id is not a MusicBrainz id")
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


def _mbid(v) -> str | None:
    return v if isinstance(v, str) and MBID_RE.fullmatch(v) else None


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e12 else None


def _int(v) -> int | None:
    if isinstance(v, int) and not isinstance(v, bool):
        return v if 0 <= v < 1 << 31 else None
    # Bound the length first: int() of a huge digit string is slow, or ValueError past 4300 digits
    if isinstance(v, str) and v.isascii() and v.isdigit() and len(v) <= 10:
        n = int(v)
        return n if n < 1 << 31 else None
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
        if not isinstance(artist, dict) or _mbid(artist.get("id")) is None or _str(artist.get("name")) is None:
            continue
        out.append({"name": _str(c.get("name")) or artist["name"], "joinphrase": _str(c.get("joinphrase")) or "",
                    "artist": {"id": artist["id"], "name": artist["name"]}})
    return out


def _recording(v) -> dict | None:
    if not isinstance(v, dict):
        return None
    r = _pick(v, id=_mbid, title=_str, length=_num)
    r["isrcs"] = [x for x in v.get("isrcs") or [] if isinstance(x, str)] if isinstance(v.get("isrcs"), list) else []
    r["artist-credit"] = _credits(v.get("artist-credit"))
    return r


def _track(v: dict) -> dict:
    t = _pick(v, id=_mbid, title=_str, number=_str, length=_num, position=_int)
    t["artist-credit"] = _credits(v.get("artist-credit"))
    rec = _recording(v.get("recording"))
    if rec is not None:
        t["recording"] = rec
    return t


def clean_release(v) -> dict | None:
    """A release with only well-typed fields (see above), or None without an MBID."""
    if not isinstance(v, dict) or _mbid(v.get("id")) is None:
        return None
    rel = _pick(v, id=_mbid, title=_str, date=_str, barcode=_str, country=_str, status=_str)
    rel["artist-credit"] = _credits(v.get("artist-credit"))
    rg = v.get("release-group")
    if isinstance(rg, dict):
        rel["release-group"] = _pick(rg, id=_mbid, first_release_date=_str, primary_type=_str)
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


def _best_medium(releases: list[tuple[dict, str]], disc: Disc, area: Area) -> Match | None:
    """The best-ranked medium for `area` among the releases, whatever its score."""
    best: Match | None = None
    best_rank = 0.0
    for rel, method in releases:
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
    return best


def _confident(m: Match, area: Area, max_mean_diff: float) -> bool:
    """Durations within the limit, ISRCs on at least half the tracks (and at least one), or a
    release the user named. Without "at least one", any one-track medium matched a one-track
    area whatever its length."""
    return m.score <= max_mean_diff or m.isrc_hits >= max(1, len(area.tracks) // 2) or m.method == "release-id"


def _via_stereo(releases: list[tuple[dict, str]], disc: Disc, area: Area, max_mean_diff: float) -> Match | None:
    """A match for a multichannel `area` whose own track lengths miss the limit, through the
    stereo area of the same disc (#218).

    MusicBrainz often gives a hybrid disc's "SACD layer, multichannel" medium the stereo (or CD)
    track lengths, and a multichannel mix can differ from them by several seconds a track. When
    the disc's stereo area matches a release on its own terms (the normal limit, or ISRCs), that
    release is the disc; its multichannel SACD medium on the same physical disc is then this
    area's medium, provided it has exactly as many tracks. Nothing else is loosened."""
    stereo = getattr(disc, "stereo", None)
    if stereo is None or stereo is area:
        return None
    sm = _best_medium(releases, disc, stereo)
    # The stereo area must match on its own terms (a named release never gets here: its
    # multichannel area is accepted directly)
    if sm is None or not _confident(sm, stereo, max_mean_diff):
        return None
    rel = sm.release
    fmt = sm.medium.get("format") or ""
    if "SACD" not in fmt or "(CD layer" in fmt or "multichannel" in fmt:
        return None                     # the stereo area must sit on the release's SACD stereo layer
    media = rel["media"]
    discs = physical_discs(media)
    discno = next(n for m, n in zip(media, discs) if m is sm.medium)
    mc = [m for m, n in zip(media, discs) if n == discno and "SACD" in (m.get("format") or "")
          and "multichannel" in (m.get("format") or "") and len(m.get("tracks") or []) == len(area.tracks)]
    if len(mc) != 1:
        return None
    s = score_medium(rel, mc[0], disc, area)
    if s is None:
        return None
    score, hits = s
    log.debug("multichannel area: release %s by %s, stereo area mean diff %.2f s, multichannel %.2f s "
              "(limit %.1f s)", rel.get("id"), sm.method, sm.score, score, max_mean_diff)
    warning = (f"multichannel track lengths differ from MusicBrainz's by {score:.1f} s on average "
               f"(limit {max_mean_diff:.1f} s); matched through the disc's stereo area, which matches "
               f"this release by {sm.score:.1f} s")
    return Match(rel, mc[0], score, hits, sm.method, via_stereo=True, warnings=[warning])


def find_release(disc: Disc, area: Area, contact: str | None, release_id: str | None = None,
                 text_search: bool = False, max_mean_diff: float = 3.0) -> Match | None:
    """The MusicBrainz release and medium for `area`, or None without a confident match.

    A match is confident when the medium has the area's track count and its track lengths are
    within `max_mean_diff` seconds of the area's on average, or ISRCs match on at least half the
    tracks, or the release was named (`release_id`). A multichannel area that misses this can
    still match through the disc's stereo area (see _via_stereo); its Match then has
    via_stereo set and a warning."""
    cands = [(release_id_arg(release_id), "release-id")] if release_id else candidate_ids(disc, contact, text_search)
    releases = [(lookup_release(rid, contact), method) for rid, method in cands]
    best = _best_medium(releases, disc, area)
    if best is not None and not _confident(best, area, max_mean_diff):
        best = None
    if best is None and area.is_multichannel:
        best = _via_stereo(releases, disc, area, max_mean_diff)
    if best is None:
        return None
    if "SACD" not in (best.medium.get("format") or "") or "(CD layer" in best.medium.get("format", ""):
        best.warnings.append(f"matched medium format is {best.medium.get('format')!r}, not SACD")
    if best.method == "text":
        best.warnings.append("matched by title search: release-level data (date, label, catalog) may be a different pressing")
    return best


def fetch_cover(release: dict, contact: str | None, size: str = "1200") -> tuple[bytes, str] | None:
    """Front cover from the Cover Art Archive (release, then release group).

    Redirects are followed only to COVER_HOSTS. When no cover was found and a redirect went
    elsewhere, that is raised (RedirectHostError) rather than reported as "no cover": if the
    archive ever moves its images to other hosts, the user has to be told why covers stopped."""
    suffix = "front" if size == "full" else f"front-{size}"
    urls = []
    if _mbid(release.get("id")):
        urls.append(f"{CAA}/release/{release['id']}/{suffix}")
    rg = _mbid(release.get("release-group", {}).get("id"))
    if rg:
        urls.append(f"{CAA}/release-group/{rg}/{suffix}")
    refused: RedirectHostError | None = None
    for url in urls:
        try:
            data, _ = _get(url, contact, accept="image/*", limit=MAX_COVER, deadline=COVER_DEADLINE,
                           redirect_hosts=COVER_HOSTS)
        except RedirectHostError as e:
            refused = refused or e
            continue
        except MBError:
            continue
        # Embed only what is really a JPEG or PNG, whatever the Content-Type says (#14)
        mime = image_type(data)
        if mime:
            return data, mime
    if refused:
        raise RedirectHostError(
            f"the Cover Art Archive sent the download to a host pysacd doesn't follow ({refused}). "
            "If the archive has moved its images, this needs a newer pysacd; no cover was fetched")
    return None
