"""pysacd command line: `toc` prints SACD TOCs, `convert` extracts tagged audio."""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import sys
from pathlib import Path

from . import safetext
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


def print(*args, **kwargs) -> None:  # TOC text is untrusted: escape control characters
    import builtins
    builtins.print(*(safetext.for_terminal(str(a), keep_tab_newline=True) for a in args), **kwargs)


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


def _toc(args) -> int:
    rc = 0
    out = []
    from . import remote
    for path in args.images:
        try:
            addr = remote.parse_address(path)
            disc = remote.read_remote_toc(*addr)[0] if addr else read_toc(path)
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


def _submit_isrcs(args) -> int:
    from . import isrcsubmit, musicbrainz, remote
    try:
        if args.mb_server:
            musicbrainz.set_server(args.mb_server)
        addr = remote.parse_address(args.image)
        disc = remote.read_remote_toc(*addr)[0] if addr else read_toc(args.image)
    except (OSError, SACDError, musicbrainz.MBError) as e:
        print(f"{args.image}: {e}", file=sys.stderr)
        return 1
    areas = [a for a in (disc.stereo, disc.multichannel) if a is not None
             and (args.area == "both" or (args.area == "multichannel") == a.is_multichannel)]
    rc = 0
    for area in areas:
        if not any(t.isrc for t in area.tracks):
            print(f"{area.speaker_config}: the disc has no ISRCs")
            continue
        try:
            match = musicbrainz.find_release(disc, area, args.mb_contact, args.mb_release, text_search=False)
            if match is None:
                print(f"{area.speaker_config}: no confident MusicBrainz match (try --mb-release)", file=sys.stderr)
                rc = 1
                continue
            isrcsubmit.offer(match, area, args.mb_contact, dry_run=args.dry_run, yes=args.yes, user=args.mb_user)
        except musicbrainz.MBError as e:
            print(f"{area.speaker_config}: {e}", file=sys.stderr)
            rc = 1
    return rc


def _mb_login(args) -> int:
    from . import isrcsubmit, musicbrainz
    try:
        if args.mb_server:
            musicbrainz.set_server(args.mb_server)
        isrcsubmit.login(args.client_id, args.client_secret or getpass.getpass("client secret: "), args.mb_contact)
    except (OSError, musicbrainz.MBError) as e:
        print(f"mb-login: {e}", file=sys.stderr)
        return 1
    return 0


def _bool(ap, name, help):
    ap.add_argument(f"--{name}", dest=name.replace("-", "_"), action=argparse.BooleanOptionalAction,
                    default=None, help=help)


def _convert(args) -> int:
    from .convert import CONFIG_PATH, Options, convert
    from .rootcheck import refuse_root

    if refuse_root("pysacd convert", args.allow_root):
        return 2

    try:
        opts = Options.from_config(Path(args.config) if args.config else CONFIG_PATH)
    except (ValueError, OSError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    for k, v in vars(args).items():
        if v is not None and hasattr(opts, k):
            setattr(opts, k, v)
    from . import remote

    rc = 0
    for iso in args.images:
        tmp = None
        try:
            addr = remote.parse_address(iso)
            if addr:
                # Fetch the TOCs and the selected areas from the PS3 into a temporary sparse ISO
                print(f"{iso}: reading from SACD Ripper server", file=sys.stderr)
                def wanted(a, which=opts.areas, dry=opts.dry_run):
                    return not dry and ((a.is_stereo and which in ("stereo", "both")) or
                                        (a.is_multichannel and which in ("multichannel", "both")))
                tmp, _ = remote.fetch_temp(
                    *addr, opts.output_dir or ".", wanted,
                    progress=remote.RateMeter(lambda m: (sys.stderr.write(m), sys.stderr.flush())))
            rc |= convert(tmp or iso, opts)
        except (OSError, SACDError) as e:
            print(f"{iso}: {e}", file=sys.stderr)
            rc = 1
        finally:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
    return rc


def _ps3sim(args) -> int:
    import ipaddress

    from .remote import PS3Simulator
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not loopback:
        print(f"warning: listening on {args.host}: anyone who can reach this port can read {args.image}. "
              "The protocol has no authentication or encryption; use only on a trusted network.",
              file=sys.stderr)
    sim = PS3Simulator(args.image, args.host, args.port, rate=args.rate * 1e6, latency=args.latency / 1000,
                       drop_after=args.drop_after, fail_lsn=args.fail_lsn, no_disc=args.no_disc,
                       idle_timeout=args.idle_timeout,
                       log=lambda m: print(m, file=sys.stderr, flush=True))
    host, port = sim.address[0], sim.address[1]
    print(f"serving {args.image} as a SACD Ripper server on {host!s}:{port} (Ctrl-C to stop)", file=sys.stderr)
    try:
        sim.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pysacd", description="SACD ISO tools")
    ap.add_argument("--log-file", help="write a diagnostic log here (default level: debug)")
    ap.add_argument("--log-level", choices=["debug", "info", "warning", "error"],
                    help="log detail; without --log-file the log goes to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("toc", help="print the TOC of SACD ISO images")
    t.add_argument("images", nargs="+")
    t.add_argument("--json", action="store_true", help="emit JSON")
    t.set_defaults(func=_toc)

    c = sub.add_parser(
        "convert", help="extract tracks to DSF/FLAC/WAV and tag them",
        description="Extract an SACD ISO to tagged files. Defaults come from "
                    "~/.config/pysacd/config.toml (same names as the long options, "
                    "with underscores); flags override it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="template fields: albumartist album artist title composer date year originalyear\n"
               "  label catalognumber barcode tracknumber tracktotal discnumber disctotal\n"
               "  disc disc_prefix edition layout channels format\n"
               "  e.g. --dir-template '{albumartist}/{year} - {album} [{edition}]'\n"
               "       --file-template '{tracknumber:02d} {title}'\n"
               "  empty [] / () left by blank fields are removed")
    c.add_argument("images", nargs="+", help="ISO images, or HOST:PORT of a PS3 running SACD Ripper")
    c.add_argument("--config", help=f"config file (default {Path('~/.config/pysacd/config.toml')})")
    c.add_argument("-f", "--format", choices=["dsf", "flac", "wav"], help="output format (default dsf)")
    c.add_argument("-o", "--output-dir", help="root output directory (default .)")
    c.add_argument("--dir-template", help="album directory template, may contain /")
    c.add_argument("--file-template", help="track file name template (no extension)")
    c.add_argument("--stereo-edition", help="{edition} for the stereo area (default 'SACD')")
    c.add_argument("--multichannel-edition", help="{edition} for the multichannel area (default 'SACD {layout}')")
    c.add_argument("-a", "--areas", choices=["stereo", "multichannel", "both"], help="areas to extract (default stereo)")
    c.add_argument("--disc", metavar="N/M",
                   help="this is disc N of an M-disc set: sets the disc number tags and {disc}/{disc_prefix}, "
                        "overriding the TOC and MusicBrainz (for sets mastered as 1/1, e.g. Live-Evil)")
    c.add_argument("-t", "--tracks", type=lambda s: [int(x) for x in s.split(",")], help="only these tracks, e.g. 1,4,5")
    c.add_argument("-r", "--pcm-rate", type=int, choices=[88200, 176400], help="FLAC/WAV sample rate (default 88200)")
    c.add_argument("--gain-db", type=float, help="PCM gain in dB (default 0; SACD 0 dB = -6 dBFS)")
    c.add_argument("--flac-level", type=int, choices=range(9), metavar="0-8", help="FLAC compression (default 5)")
    c.add_argument("-j", "--jobs", type=int, help="worker threads (default: CPU count)")
    _bool(c, "musicbrainz", "look up the release on MusicBrainz (default on)")
    c.add_argument("--mb-release", help="use this MusicBrainz release id")
    _bool(c, "mb-text-search", "fall back to title/artist search when barcode/catalog fail (default off)")
    c.add_argument("--mb-contact", help="contact (URL or e-mail) for the MusicBrainz User-Agent")
    _bool(c, "submit-isrcs", "offer the disc's ISRCs to the matched MusicBrainz release; asks first, needs `pysacd mb-login` (default off)")
    _bool(c, "cover", "embed front cover from the Cover Art Archive (default on)")
    c.add_argument("--cover-size", choices=["250", "500", "1200", "full"], help="cover size (default 1200)")
    c.add_argument("--cover-file", help="save cover as this file in the album dir ('' to skip)")
    c.add_argument("--id3-version", type=int, choices=[3, 4], help="ID3 version for DSF/WAV (default 3)")
    _bool(c, "portable-names", "replace characters invalid on Windows/SMB (default off)")
    _bool(c, "overwrite", "overwrite existing files (default: skip)")
    c.add_argument("--allow-root", action="store_true",
                   help="run even as root (not recommended: disc images are untrusted; see docs/security.md)")
    c.add_argument("-n", "--dry-run", action="store_true", default=None, help="show match and paths only")
    c.set_defaults(func=_convert)

    i = sub.add_parser(
        "submit-isrcs", help="add the disc's ISRCs to its MusicBrainz release (asks first)",
        description="Submit the ISRCs in a disc's TOC to the matching MusicBrainz release. Only a barcode, "
                    "catalog-number or --mb-release match is used; ISRCs already on another recording are "
                    "skipped. Sign in once with `pysacd mb-login`, or use --mb-user (HTTP digest).")
    i.add_argument("image", help="ISO image or PS3 address (host:port)")
    i.add_argument("--area", choices=["stereo", "multichannel", "both"], default="both")
    i.add_argument("--mb-release", help="use this MusicBrainz release id")
    i.add_argument("--mb-contact", help="contact (URL or e-mail) for the MusicBrainz User-Agent")
    i.add_argument("--mb-server", help="another MusicBrainz server, e.g. https://test.musicbrainz.org")
    i.add_argument("--mb-user", help="MusicBrainz user name for digest auth (password asked, never stored)")
    i.add_argument("-n", "--dry-run", action="store_true", help="show what would be submitted")
    i.add_argument("-y", "--yes", action="store_true", help="submit without asking")
    i.set_defaults(func=_submit_isrcs)

    g = sub.add_parser(
        "mb-login", help="sign in to MusicBrainz (OAuth) for ISRC submission",
        description="Out-of-band OAuth2 sign-in with the submit_isrc scope. Register an application at "
                    "https://musicbrainz.org/account/applications (type: installed) to get a client id and "
                    "secret. The token is saved owner-only under ~/.config/pysacd/.")
    g.add_argument("--client-id", required=True)
    g.add_argument("--client-secret", help="asked for if not given")
    g.add_argument("--mb-contact", help="contact (URL or e-mail) for the MusicBrainz User-Agent")
    g.add_argument("--mb-server", help="another MusicBrainz server, e.g. https://test.musicbrainz.org")
    g.set_defaults(func=_mb_login)

    s = sub.add_parser(
        "ps3sim", help="serve an ISO like a PS3 running SACD Ripper (for testing network clients)",
        description="Serve an already-decrypted SACD ISO over the SACD Ripper network protocol, "
                    "so `sacd_extract -i HOST:PORT` or pysacd can be tested without a PS3.")
    s.add_argument("image")
    s.add_argument("--host", default="127.0.0.1",
                   help="listen address (default 127.0.0.1; use 0.0.0.0 only on a trusted network)")
    s.add_argument("--port", type=int, default=2002, help="listen port (default 2002, as on the PS3)")
    s.add_argument("--rate", type=float, default=0, help="throttle to this many MB/s (0 = unlimited)")
    s.add_argument("--latency", type=float, default=0, help="delay each request by this many ms")
    s.add_argument("--drop-after", type=int, default=0, help="close the connection after N reads")
    s.add_argument("--fail-lsn", type=int, help="make reads covering this LSN fail (result -1)")
    s.add_argument("--no-disc", action="store_true", help="fail DISC_OPEN, as with an empty drive")
    s.add_argument("--idle-timeout", type=float, default=60.0,
                   help="close a connection idle for this many seconds (default 60; 0 = never)")
    s.set_defaults(func=_ps3sim)

    args = ap.parse_args(argv)
    from . import log as pslog
    level, path = args.log_level, args.log_file
    if level is None and path is None:
        level, path = pslog.from_env()
    try:
        pslog.setup(level, path, ["pysacd", *(sys.argv[1:] if argv is None else argv)])
    except (OSError, ValueError) as e:
        print(f"logging: {e}", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
