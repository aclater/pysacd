# Security policy

## Reporting a vulnerability

Please don't open a public issue. Instead, report it privately:

- use GitHub's **Report a vulnerability** button on the repository's Security tab, if it's
  available;
- otherwise, open a draft security advisory, or contact the maintainer (`aclater` on GitHub).

Include a disc image, network capture or command line that reproduces the problem. A
`tests/sacdgen.py` script that builds a hostile image is ideal.

You'll get an acknowledgement within a week. Fixes land on `master`; there are no maintained
release branches.

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

How each kind of input is contained, and the tests that check it, are described in
[docs/security.md](docs/security.md).
