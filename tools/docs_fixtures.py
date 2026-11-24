#!/usr/bin/env python3
"""The SACD images the documentation's commands name, generated: the fixture hook for docs commands.

    python3 -I tools/docs_fixtures.py DIR                    # DIR/album.iso, disc.iso, other.iso
    python3 -I tools/docs_fixtures.py --offline-config DIR   # also DIR/.config/pysacd/config.toml

The docs use three names, and these are the only images a command that runs may name
(tests/test_docs_commands.py checks it): `album.iso` (plain DSD stereo + DST 5 channels),
`disc.iso` (DST stereo + DST 6 channels) and `other.iso` (plain DSD stereo). Each area has three
tracks of a DSD64 test signal; the DST frames come from the test-only encoder.

Needs only tests/sacdgen.py, tests/dstenc.py and tests/native/dstenc.c from this repository,
numpy and a C compiler (dstenc.py compiles its native part when build/dstenc.so is missing), so
it also runs from a source archive of the repository in a fresh container. Two runners use it:

- the shared docs-run (dsd-toolbox tools/docs/docs_run.py), through the `setup=` of the
  `discs` group in the Markdown, which fetches this repository at a pinned commit and runs this
  file with --offline-config in the container's home folder;
- tools/docs_commands.py, for each block it runs against a wheel or a pip requirement.

--offline-config writes `musicbrainz = false` to the pysacd config in DIR (the home folder), so
`pysacd convert` makes no MusicBrainz lookups for a generated disc.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

DISCS = {
    "album.iso": "plain DSD stereo + DST 5 channels",
    "disc.iso": "DST stereo + DST 6 channels",
    "other.iso": "plain DSD stereo",
}
FRAMES = 12                 # per area: 0.16 s, three tracks of four frames
TITLES = ["First", "Second", "Third"]


def _areas(name: str) -> list:
    import dstenc
    import sacdgen

    def frames(channels: int, seed: int, dst: bool) -> list[bytes]:
        raw = dstenc.synth_dsd(channels, FRAMES, seed=seed)
        n = dstenc.FL * channels
        out = [raw[i * n:(i + 1) * n] for i in range(FRAMES)]
        return [dstenc.encode_frame(f, channels) for f in out] if dst else out

    def area(channels: int, seed: int, dst: bool):
        return sacdgen.AreaSpec(channels, frames(channels, seed, dst), [4, 4, 4], dst=dst, titles=TITLES)

    return {
        "album.iso": [area(2, 1, False), area(5, 2, True)],
        "disc.iso": [area(2, 3, True), area(6, 4, True)],
        "other.iso": [area(2, 5, False)],
    }[name]


def make(folder: Path, offline_config: bool = False) -> dict[str, Path]:
    """Write the three images (and, with offline_config, the pysacd config) into folder."""
    import sacdgen

    folder.mkdir(parents=True, exist_ok=True)
    info = sacdgen.DiscInfo(title="Docs Album", artist="Docs Artist")
    out = {}
    for name in DISCS:
        out[name] = folder / name
        sacdgen.build_iso(out[name], _areas(name), info=info)
    if offline_config:
        cfg = folder / ".config" / "pysacd"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "config.toml").write_text("musicbrainz = false\n", encoding="utf-8")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    p.add_argument("dir", type=Path)
    p.add_argument("--offline-config", action="store_true",
                   help="also write musicbrainz = false to DIR/.config/pysacd/config.toml")
    a = p.parse_args()
    for name, path in make(a.dir, a.offline_config).items():
        print(f"{path}: {DISCS[name]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
