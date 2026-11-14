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
import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import quoteattr

from . import __version__, musicbrainz, safeio
from .toc import Area

ISRC_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}")
MBID_RE = musicbrainz.MBID_RE
TOKEN_FILE = safeio.config_dir() / "musicbrainz-token.json"
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
    recs = musicbrainz._dicts(data.get("recordings")) if isinstance(data, dict) else []    # any shape may come back
    return [r["id"] for r in recs if musicbrainz._mbid(r.get("id"))]


def body(p: Plan) -> bytes:
    """The XML MusicBrainz expects for POST /ws/2/recording."""
    recs = "".join(f'<recording id={quoteattr(i.recording or "")}><isrc-list count="1">'
                   f'<isrc id={quoteattr(i.isrc or "")}/></isrc-list></recording>' for i in p.to_submit)
    return (f'<?xml version="1.0" encoding="UTF-8"?><metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">'
            f'<recording-list count="{len(p.to_submit)}">{recs}</recording-list></metadata>').encode()


# ---------------------------------------------------------------- credentials

def _open_private(path: Path, flags: int) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)    # no O_NOFOLLOW on Windows


def check_folder(folder: Path) -> None:
    """Refuse a token folder that group or others can write (POSIX only): whoever can write it
    can replace the token file. mkdir(exist_ok=True) doesn't correct an existing folder's mode."""
    if os.name != "posix":
        return
    st = os.stat(folder)
    if st.st_uid != os.geteuid():
        raise musicbrainz.MBError(f"{folder} belongs to another user; the token is not saved there")
    if st.st_mode & 0o022:
        raise musicbrainz.MBError(f"{folder} is writable by other users; run chmod go-w on it (or chmod 700)")


def save_token(token: dict, path: Path = TOKEN_FILE) -> None:
    """Write the token owner-only: a new file under an unpredictable name (O_EXCL, O_NOFOLLOW,
    mode 0600 from creation, as safeio does), then moved into place."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    check_folder(path.parent)
    tmp = safeio.temp_name(path)
    fd = os.open(tmp, safeio.FLAGS, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(token, f)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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


def _expires_in(v) -> float:
    """The token lifetime in seconds: a finite number, clamped to between 2 minutes and a year;
    1 hour (MusicBrainz's usual) if the server sent none or something else."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return 3600.0
    return min(max(float(v), 120.0), 365 * 86400.0)


def _token_request(params: dict, contact: str | None) -> dict:
    req = urllib.request.Request(_oauth_base() + "/token", data=urllib.parse.urlencode(params).encode(),
                                 headers={"User-Agent": musicbrainz._user_agent(contact)})
    try:
        # The body carries the client secret (and a code or refresh token): no redirects
        end = time.monotonic() + musicbrainz.JSON_DEADLINE
        with musicbrainz._open(req, follow=False, deadline=end) as r:
            tok = json.loads(musicbrainz._read_capped(r, 1 << 20, "OAuth token response", end))   # #14
    except urllib.error.HTTPError as e:
        e.close()
        raise musicbrainz.MBError(f"OAuth token request failed: HTTP {e.code}") from None
    except (OSError, http.client.HTTPException) as e:
        raise musicbrainz.MBError(f"OAuth token request failed: {musicbrainz._net_error(e)}") from None
    except ValueError:
        raise musicbrainz.MBError("OAuth token response is not JSON") from None
    if not isinstance(tok, dict) or not isinstance(tok.get("access_token"), str):
        raise musicbrainz.MBError("OAuth token response has no access_token")
    tok["expires_at"] = time.time() + _expires_in(tok.get("expires_in")) - 60
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
    handlers: tuple = ()
    bearer = _bearer(contact, token_path)
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    elif user:
        password = os.environ.get("PYSACD_MB_PASSWORD") or getpass.getpass(f"MusicBrainz password for {user}: ")
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, musicbrainz.API, user, password)
        handlers = (urllib.request.HTTPDigestAuthHandler(mgr),)
    else:
        raise musicbrainz.MBError("not signed in: run `pysacd mb-login`, or pass --mb-user for digest auth")
    musicbrainz._throttle(url)
    req = urllib.request.Request(url, data=body(p), headers=headers, method="POST")
    try:
        # Credentials go with this request: no redirects, so they never reach another URL
        end = time.monotonic() + musicbrainz.JSON_DEADLINE
        with musicbrainz._open(req, timeout=60, follow=False, handlers=handlers, deadline=end) as r:
            reply = musicbrainz._read_capped(r, 65536, "submission reply", end)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(2048).decode("utf-8", "replace")
        except (OSError, http.client.HTTPException):
            detail = ""
        e.close()
        m = re.search(r"<text>(.*?)</text>", detail, re.DOTALL)
        raise musicbrainz.MBError(f"submission refused: HTTP {e.code}"
                                  + (f": {m.group(1)[:200]}" if m else "")) from None
    except (OSError, http.client.HTTPException) as e:
        raise musicbrainz.MBError(f"submission failed: {musicbrainz._net_error(e)}; "
                                  "check on MusicBrainz whether it was applied before retrying") from None
    except musicbrainz.MBError as e:
        # the deadline or the size cap: the request went out, so it may have been applied
        raise musicbrainz.MBError(f"submission failed: {e}; "
                                  "check on MusicBrainz whether it was applied before retrying") from None
    if b"<text>OK</text>" not in reply:
        raise musicbrainz.MBError("unexpected reply from MusicBrainz: " + reply[:200].decode("utf-8", "replace"))


ACTION_TEXT = {"submit": "add", "present": "already there", "conflict": "skip (conflict)", "invalid": "skip",
               "none": "no ISRC on disc", "duplicate": "skip (duplicate)"}


def offer(match: musicbrainz.Match, area: Area, contact: str | None, *, dry_run: bool = False, yes: bool = False,
          user: str | None = None, out=print, ask=input, token_path: Path = TOKEN_FILE) -> int:
    """Show the plan for one area, ask, and submit. Returns the number of ISRCs submitted."""
    from .safetext import for_terminal
    p = plan(match, area, contact)
    out(f"ISRCs for {musicbrainz.API.rsplit('/ws/2', 1)[0]}/release/{for_terminal(p.release_id)} "
        f"({for_terminal(p.medium)}):")
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
