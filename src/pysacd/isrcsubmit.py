"""Submit a disc's ISRCs to the matched MusicBrainz release (#34).

The SACD's area TOC carries an ISRC per track. Many MusicBrainz recordings lack
them, and ISRCs make future matches by pysacd (and anyone else) more reliable.
This module plans a submission (which recording gets which ISRC, and why some
are skipped), shows it, and posts it only when the user confirms.

Safety rules:
- Only a confident match: barcode, catalog number or an explicit release id.
  A title search is never used.
- Track N of the area maps to track N of the matched medium. The counts must
  agree (find_release() already insists).
- ISRCs are validated (CCXXXYYNNNNN); malformed, all-zero and duplicate ones
  are skipped.
- An ISRC MusicBrainz already has on a *different* recording is reported and
  not submitted.
- Credentials: OAuth2 (scope submit_isrc) with the token in a private file, or
  HTTP digest with the password asked for each time. Passwords are never stored.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import quoteattr

from . import __version__, musicbrainz
from .toc import Area

ISRC_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}")
MBID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TOKEN_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "pysacd" / "musicbrainz-token.json"
OOB = "urn:ietf:wg:oauth:2.0:oob"
SCOPE = "submit_isrc"


def normalise_isrc(raw: str | None) -> str | None:
    """The canonical 12-character form, or None if `raw` isn't a plausible ISRC."""
    if not raw:
        return None
    s = raw.strip().upper().replace("-", "")
    if not ISRC_RE.fullmatch(s) or s[5:] == "0000000":
        return None
    return s


@dataclass
class Item:
    track: int                       # 1-based position on the disc and the medium
    title: str
    recording: str | None            # MusicBrainz recording id
    isrc: str | None                 # normalised ISRC from the disc
    action: str                      # submit | present | conflict | invalid | none | duplicate
    note: str = ""


@dataclass
class Plan:
    release_id: str
    medium: str
    items: list[Item] = field(default_factory=list)

    @property
    def to_submit(self) -> list[Item]:
        return [i for i in self.items if i.action == "submit"]


def plan(match: musicbrainz.Match, area: Area, contact: str | None, check_global: bool = True) -> Plan:
    """Decide, track by track, what to submit. `check_global` asks MusicBrainz which recordings
    already carry each ISRC (one request per ISRC), so one already used elsewhere isn't added."""
    if match.method not in ("barcode", "catno", "release-id"):
        raise musicbrainz.MBError(f"refusing to submit ISRCs for a {match.method!r} match; pass --mb-release")
    mtracks = match.medium.get("tracks") or []
    if len(mtracks) != len(area.tracks):
        raise musicbrainz.MBError(f"medium has {len(mtracks)} tracks, the disc area {len(area.tracks)}")
    p = Plan(match.release_id, match.medium.get("format") or "?")
    seen: dict[str, int] = {}
    for n, (mt, at) in enumerate(zip(mtracks, area.tracks), 1):
        rec = mt.get("recording") or {}
        rid = rec.get("id") if isinstance(rec.get("id"), str) and MBID_RE.fullmatch(rec["id"]) else None
        title = str(mt.get("title") or rec.get("title") or "")[:200]
        isrc = normalise_isrc(at.isrc)
        it = Item(n, title, rid, isrc, "submit")
        if not at.isrc:
            it.action = "none"
        elif isrc is None:
            it.action, it.note = "invalid", "malformed or all-zero ISRC on the disc"
        elif rid is None:
            it.action, it.note = "invalid", "no recording id on MusicBrainz"
        elif isrc in seen:
            it.action, it.note = "duplicate", f"same ISRC as track {seen[isrc]} on this disc"
        elif isrc in (rec.get("isrcs") or []):
            it.action = "present"
        if isrc and it.action != "duplicate":
            seen.setdefault(isrc, n)
        p.items.append(it)
    if check_global:
        for it in p.to_submit:
            assert it.isrc is not None
            others = [r for r in _isrc_recordings(it.isrc, contact) if r != it.recording]
            if others:
                it.action, it.note = "conflict", "already on recording " + ", ".join(others[:3])
    return p


def _isrc_recordings(isrc: str, contact: str | None) -> list[str]:
    try:
        data = musicbrainz._json(f"isrc/{isrc}", contact)
    except musicbrainz.MBError as e:
        if str(e).startswith("not found"):
            return []
        raise
    return [r["id"] for r in data.get("recordings") or [] if isinstance(r.get("id"), str) and MBID_RE.fullmatch(r["id"])]


def body(p: Plan) -> bytes:
    """The XML MusicBrainz expects for POST /ws/2/recording."""
    recs = "".join(f'<recording id={quoteattr(i.recording or "")}><isrc-list count="1">'
                   f'<isrc id={quoteattr(i.isrc or "")}/></isrc-list></recording>' for i in p.to_submit)
    return (f'<?xml version="1.0" encoding="UTF-8"?><metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">'
            f'<recording-list count="{len(p.to_submit)}">{recs}</recording-list></metadata>').encode()


# ---------------------------------------------------------------- credentials

def _open_private(path: Path, flags: int) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)    # no O_NOFOLLOW on Windows


def save_token(token: dict, path: Path = TOKEN_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", opener=lambda p, f: _open_private(Path(p), f)) as f:
        json.dump(token, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def check_private(mode: int, path: Path) -> None:
    """Refuse a token file that anyone but its owner can read or write (POSIX only)."""
    if os.name == "posix" and mode & 0o077:
        raise musicbrainz.MBError(f"{path} is readable by others; chmod 600 it")


def load_token(path: Path = TOKEN_FILE) -> dict | None:
    try:
        with open(path, opener=lambda p, f: _open_private(Path(p), f)) as f:
            check_private(os.fstat(f.fileno()).st_mode, path)     # the file actually opened
            tok = json.load(f)
    except FileNotFoundError:
        return None
    return tok if isinstance(tok, dict) else None


def _oauth_base() -> str:
    return musicbrainz.API.rsplit("/ws/2", 1)[0] + "/oauth2"


def _token_request(params: dict, contact: str | None) -> dict:
    req = urllib.request.Request(_oauth_base() + "/token", data=urllib.parse.urlencode(params).encode(),
                                 headers={"User-Agent": musicbrainz._user_agent(contact)})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.loads(musicbrainz._read_capped(r, 1 << 20, "OAuth token response"))   # #14
    except urllib.error.HTTPError as e:
        raise musicbrainz.MBError(f"OAuth token request failed: HTTP {e.code}") from None
    if not isinstance(tok, dict) or not isinstance(tok.get("access_token"), str):
        raise musicbrainz.MBError("OAuth token response has no access_token")
    tok["expires_at"] = time.time() + float(tok.get("expires_in") or 3600) - 60
    return tok


def authorize_url(client_id: str) -> str:
    """Where the user signs in and allows pysacd to submit ISRCs (out-of-band OAuth2)."""
    return _oauth_base() + "/authorize?" + urllib.parse.urlencode(
        {"response_type": "code", "client_id": client_id, "redirect_uri": OOB, "scope": SCOPE,
         "access_type": "offline"})


def finish_login(client_id: str, client_secret: str, code: str, contact: str | None,
                 path: Path = TOKEN_FILE) -> None:
    """Exchange the code the user pasted for a token, and save it owner-only."""
    tok = _token_request({"grant_type": "authorization_code", "code": code.strip(), "client_id": client_id,
                          "client_secret": client_secret, "redirect_uri": OOB}, contact)
    tok.update(client_id=client_id, client_secret=client_secret, server=musicbrainz.API)
    save_token(tok, path)


def _bearer(contact: str | None, path: Path = TOKEN_FILE) -> str | None:
    tok = load_token(path)
    if not tok or tok.get("server", musicbrainz.API) != musicbrainz.API:
        return None
    if time.time() >= float(tok.get("expires_at") or 0):
        if not tok.get("refresh_token"):
            return None
        new = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                              "client_id": tok.get("client_id", ""), "client_secret": tok.get("client_secret", "")},
                             contact)
        tok.update({k: v for k, v in new.items() if k in ("access_token", "expires_at", "refresh_token")})
        save_token(tok, path)
    return str(tok["access_token"])


def submit(p: Plan, contact: str | None, user: str | None = None, token_path: Path = TOKEN_FILE) -> None:
    """POST the plan. Uses the OAuth token if there is one, else digest auth as `user`."""
    if not p.to_submit:
        return
    url = f"{musicbrainz.API}/recording/?client=pysacd-{__version__}"
    headers = {"User-Agent": musicbrainz._user_agent(contact), "Content-Type": "application/xml; charset=utf-8"}
    opener = urllib.request.build_opener()
    bearer = _bearer(contact, token_path)
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    elif user:
        password = os.environ.get("PYSACD_MB_PASSWORD") or getpass.getpass(f"MusicBrainz password for {user}: ")
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, musicbrainz.API, user, password)
        opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
    else:
        raise musicbrainz.MBError("not signed in: run `pysacd mb-login`, or pass --mb-user for digest auth")
    musicbrainz._throttle(url)
    req = urllib.request.Request(url, data=body(p), headers=headers, method="POST")
    try:
        with opener.open(req, timeout=60) as r:
            reply = r.read(65536)
    except urllib.error.HTTPError as e:
        detail = e.read(2048).decode("utf-8", "replace")
        m = re.search(r"<text>(.*?)</text>", detail, re.DOTALL)
        raise musicbrainz.MBError(f"submission refused: HTTP {e.code}"
                                  + (f": {m.group(1)[:200]}" if m else "")) from None
    if b"<text>OK</text>" not in reply:
        raise musicbrainz.MBError("unexpected reply from MusicBrainz: " + reply[:200].decode("utf-8", "replace"))


ACTION_TEXT = {"submit": "add", "present": "already there", "conflict": "skip (conflict)", "invalid": "skip",
               "none": "no ISRC on disc", "duplicate": "skip (duplicate)"}


def offer(match: musicbrainz.Match, area: Area, contact: str | None, *, dry_run: bool = False, yes: bool = False,
          user: str | None = None, out=print, ask=input, token_path: Path = TOKEN_FILE) -> int:
    """Show the plan for one area, ask, and submit. Returns the number of ISRCs submitted."""
    from .safetext import for_terminal
    p = plan(match, area, contact)
    out(f"ISRCs for {musicbrainz.API.rsplit('/ws/2', 1)[0]}/release/{p.release_id} ({for_terminal(p.medium)}):")
    for i in p.items:
        note = f"  ({for_terminal(i.note)})" if i.note else ""
        out(f"  {i.track:3d}. {i.isrc or '-':12s}  {ACTION_TEXT[i.action]:16s} {for_terminal(i.title)}{note}")
    n = len(p.to_submit)
    if not n:
        out("nothing to submit")
        return 0
    if dry_run:
        out(f"dry run: {n} ISRC(s) would be submitted")
        return 0
    if not yes:
        if not os.isatty(0):
            raise musicbrainz.MBError("not a terminal: pass --yes to submit without asking")
        if ask(f"Submit {n} ISRC(s) to MusicBrainz? [y/N] ").strip().lower() not in ("y", "yes"):
            out("not submitted")
            return 0
    submit(p, contact, user, token_path)
    out(f"submitted {n} ISRC(s); MusicBrainz applies them right away")
    return n
