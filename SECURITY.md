# Security policy

## Supported versions

Security fixes go into the latest release and `master`. There are no maintained release
branches: if you're on an older release, the fix is to upgrade.

| Version | Supported |
| --- | --- |
| latest release ([releases](https://github.com/aclater/pysacd/releases)) | yes |
| `master` | yes |
| anything older | no |

## Reporting a vulnerability

Please don't open a public issue, pull request or discussion for a vulnerability. Report it
privately through GitHub's private vulnerability reporting:

**<https://github.com/aclater/pysacd/security/advisories/new>**

(or the **Report a vulnerability** button on the repository's
[Security tab](https://github.com/aclater/pysacd/security)). Only the maintainer sees the
report.

Please include:

- the pysacd version (`pysacd --version` shows it and how the engine was built), your OS
  and Python version;
- the command line or API call;
- the input that triggers it: a disc image, a network capture, or best of all a
  `tests/sacdgen.py` script that builds a hostile image. Don't send copyrighted disc
  contents; a synthetic image or the few sectors involved are enough;
- what happens (crash, hang, memory or disk use, a file written where it shouldn't be) and,
  if you know, why.

## What to expect

pysacd is maintained by one person, so these are targets, not guarantees:

- **acknowledgement** within 7 days;
- **assessment** (confirmed or not, and how severe) within 14 days;
- **a fix** for a confirmed issue within 90 days, usually much sooner, released as a new
  version with the fix noted in its release notes.

## Coordinated disclosure

Please keep the details private until a fixed release is out, or until 90 days after your
report, whichever comes first; if a fix needs longer, we'll agree a date with you. Once it's
fixed, the issue is published as a GitHub security advisory (with a CVE when it warrants one),
crediting you unless you'd rather not be named.

## Scope

pysacd treats these as attacker-controlled:

- disc images and their TOC text;
- everything a PS3 (SACD Ripper) server sends;
- MusicBrainz and cover-art responses.

Any of the following in pysacd's handling of those inputs is in scope:

- a crash, a hang or unbounded memory or disk use;
- a write outside the output directory;
- text that reaches a shell or drives a terminal;
- output for one disc corrupting another's.

Out of scope, by design:

- **The PS3 protocol has no authentication or encryption.** Anyone on the network path can read or
  alter sectors. pysacd only guarantees that what it receives can't hurt the client.
- **Running `pysacd ps3sim` on a non-loopback address** shares that image with anyone who can
  reach the port.

How each kind of input is contained, and the tests that check it, are described in the threat
model, [docs/security.md](https://github.com/aclater/pysacd/blob/master/docs/security.md).
