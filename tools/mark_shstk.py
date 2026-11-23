#!/usr/bin/env python3
"""Mark an x86-64 ELF object as compatible with CET shadow stacks (SHSTK).

    python3 tools/mark_shstk.py OBJECT [--objcopy OBJCOPY] [--readelf READELF]

The linker marks a program SHSTK-compatible only when every object it links is marked (#94).
Rust's toolchains don't mark their code (the flag is nightly-only), so the dsd2pcm library, which
the Makefile links into one object first, would take the mark off pysacd's whole engine. Its
code returns only through `ret` to its callers, as the shadow stack needs, and unwinds through
libgcc's unwinder, which keeps the shadow stack in step. This adds a .note.gnu.property section
with GNU_PROPERTY_X86_FEATURE_1_AND = SHSTK to OBJECT, in place, with objcopy. IBT is not
claimed: the code has no ENDBR64 landing pads. An object that already has the property is left
alone. Build tooling only; nothing of it ends up in the engine but the note.
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import tempfile

NT_GNU_PROPERTY_TYPE_0 = 5
GNU_PROPERTY_X86_FEATURE_1_AND = 0xC0000002
GNU_PROPERTY_X86_FEATURE_1_SHSTK = 0x2


def note() -> bytes:
    # one property: type, data size 4, the feature bits, padded to 8 bytes (ELFCLASS64)
    desc = struct.pack("<III", GNU_PROPERTY_X86_FEATURE_1_AND, 4, GNU_PROPERTY_X86_FEATURE_1_SHSTK) + b"\0" * 4
    return struct.pack("<III", 4, len(desc), NT_GNU_PROPERTY_TYPE_0) + b"GNU\0" + desc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("object")
    ap.add_argument("--objcopy", default=os.environ.get("OBJCOPY", "objcopy"))
    ap.add_argument("--readelf", default=os.environ.get("READELF", "readelf"))
    args = ap.parse_args(argv)
    with open(args.object, "rb") as f:
        head = f.read(20)
    if head[:4] != b"\x7fELF" or head[4] != 2 or struct.unpack_from("<H", head, 18)[0] != 62:
        sys.exit(f"{args.object}: not an x86-64 ELF object")
    notes = subprocess.run([args.readelf, "-n", args.object], check=True, capture_output=True, text=True).stdout
    if "x86 feature" in notes:
        print(f"{args.object}: already has an x86 feature property; left alone")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "note")
        with open(path, "wb") as f:
            f.write(note())
        subprocess.run([args.objcopy, "--add-section", f".note.gnu.property={path}",
                        "--set-section-flags", ".note.gnu.property=alloc,readonly,data",
                        "--set-section-alignment", ".note.gnu.property=8", args.object], check=True)
    notes = subprocess.run([args.readelf, "-n", args.object], check=True, capture_output=True, text=True).stdout
    if "SHSTK" not in notes:
        sys.exit(f"{args.object}: objcopy added no SHSTK property")
    print(f"{args.object}: marked SHSTK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
