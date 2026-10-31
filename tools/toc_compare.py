#!/usr/bin/env python3
"""Compare the two TOC engines on disc images (#85, phase 2).

    PYTHONPATH=src python3 tools/toc_compare.py [--no-values] [--json] IMAGE_OR_DIR...

Reads every image (directories are searched for *.iso) with pysacd's own parser
(`python`) and with scarletbook-rs (`scarletbook`), and compares:

- every field of the Disc, Area and Track objects (dataclasses.asdict), which is all that
  names, tags, cue, XML, -P text and `pysacd toc` are made from; the warnings separately,
  since their wording differs by design;
- `pysacd toc --json` output, byte for byte, with the warnings left out;
- the beets-pysacd disc fingerprint: SHA-256 of the master TOC, each area TOC and the image's
  sector count (beetsplug/sacd.py, fingerprint()).

An image one engine rejects and the other reads counts as a difference. --no-values prints
which fields differ without their contents (for private discs). Exit status 1 if anything
other than warnings differs.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from pysacd.__main__ import _to_json
from pysacd.toc import MASTER_TOC_LEN, START_OF_MASTER_TOC, SACDError, SACDImage

ENGINES = ("python", "scarletbook")


def fingerprint(img: SACDImage, disc) -> str:
    """beets-pysacd's sacd_iso_sha."""
    h = hashlib.sha256(img.read_sectors(START_OF_MASTER_TOC, MASTER_TOC_LEN))
    for a in disc.areas:
        h.update(img.read_sectors(a.toc_start, a.toc_size))
    h.update(str(img.total_sectors).encode())
    return h.hexdigest()


def _strip_warnings(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: _strip_warnings(v) for k, v in d.items() if k != "warnings"}
    if isinstance(d, list):
        return [_strip_warnings(v) for v in d]
    return d


def _diff(a: Any, b: Any, path: str, out: list[tuple[str, Any, Any]]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            _diff(a.get(k, "<absent>"), b.get(k, "<absent>"), f"{path}.{k}" if path else str(k), out)
    elif isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f"{path}[{i}]", out)
    elif a != b:
        out.append((path, a, b))


def read(path: Path, engine: str):
    try:
        with SACDImage(path) as img:
            disc = img.read_toc(engine)
            return disc, fingerprint(img, disc), None
    except (OSError, SACDError) as e:
        return None, None, f"{type(e).__name__}: {e}"


def compare(path: Path) -> dict:
    res = {name: read(path, name) for name in ENGINES}
    (pd, pf, pe), (sd, sf, se) = res["python"], res["scarletbook"]
    r: dict[str, Any] = {"image": str(path), "fields": [], "warnings": None, "json_equal": None,
                         "fingerprint_equal": None, "errors": {"python": pe, "scarletbook": se}}
    if pd is None or sd is None:
        r["outcome"] = "both rejected" if pd is None and sd is None else \
            ("only scarletbook reads it" if pd is None else "only python reads it")
        return r
    a, b = dataclasses.asdict(pd), dataclasses.asdict(sd)
    _diff(_strip_warnings(a), _strip_warnings(b), "", r["fields"])
    r["warnings"] = {"python": pd.warnings + [w for x in pd.areas for w in x.warnings],
                     "scarletbook": sd.warnings + [w for x in sd.areas for w in x.warnings]}
    ja = json.dumps(_strip_warnings(_to_json(pd)), ensure_ascii=False, indent=2)
    jb = json.dumps(_strip_warnings(_to_json(sd)), ensure_ascii=False, indent=2)
    r["json_equal"] = ja == jb
    r["fingerprint_equal"] = pf == sf
    r["outcome"] = "identical" if not r["fields"] and r["json_equal"] and r["fingerprint_equal"] else "differs"
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--no-values", action="store_true", help="name differing fields, not their contents")
    ap.add_argument("--json", action="store_true", help="one JSON object per image")
    args = ap.parse_args(argv)
    images = []
    for p in args.paths:
        images += sorted(x for x in p.rglob("*") if x.suffix.lower() == ".iso" and x.is_file()) if p.is_dir() else [p]
    summary: dict[str, int] = {}
    bad = 0
    for path in images:
        r = compare(path)
        summary[r["outcome"]] = summary.get(r["outcome"], 0) + 1
        if r["outcome"] != "identical" and r["outcome"] != "both rejected":
            bad += 1
        if r["fingerprint_equal"] is False:
            summary["fingerprint differs"] = summary.get("fingerprint differs", 0) + 1
        if r["warnings"] and bool(r["warnings"]["python"]) != bool(r["warnings"]["scarletbook"]):
            summary["warned by one engine only"] = summary.get("warned by one engine only", 0) + 1
        if args.no_values:
            r["fields"] = [f for f, _, _ in r["fields"]]
            r["warnings"] = r["warnings"] and {k: len(v) for k, v in r["warnings"].items()}
            r["image"] = f"image {images.index(path) + 1}"
            r["errors"] = {k: v and v.split(":", 1)[0] for k, v in r["errors"].items()}
        if args.json:
            print(json.dumps(r, ensure_ascii=False, default=repr))
        elif r["outcome"] != "identical":
            print(f"== {r['image']}: {r['outcome']}")
            for k, v in r["errors"].items():
                if v:
                    print(f"   {k} error: {v}")
            for f in r["fields"]:
                print(f"   field {f[0]}: python {f[1]!r} / scarletbook {f[2]!r}" if isinstance(f, tuple)
                      else f"   field {f}")
            if r["json_equal"] is False:
                print("   toc --json differs")
            if r["fingerprint_equal"] is False:
                print("   fingerprint differs")
    print(f"{len(images)} images: " + ", ".join(f"{k} {v}" for k, v in sorted(summary.items())), file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
