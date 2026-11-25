#!/usr/bin/env python3
"""Mark an x86-64 ELF object as compatible with CET shadow stacks (SHSTK).

    python3 tools/mark_shstk.py OBJECT [--objcopy OBJCOPY]

The linker marks a program SHSTK-compatible only when every object it links is marked (#94).
Rust's toolchains don't mark their code (the flag is nightly-only), so the dsd2pcm library, which
the Makefile links into one object first, would take the mark off pysacd's whole engine. Its
code returns only through `ret` to its callers, as the shadow stack needs, and unwinds through
libgcc's unwinder, which keeps the shadow stack in step. This adds a .note.gnu.property section
with GNU_PROPERTY_X86_FEATURE_1_AND = SHSTK to OBJECT, in place, with objcopy. IBT is not
claimed: the code has no ENDBR64 landing pads. An object that already has a .note.gnu.property
section (other x86 properties) gets the property merged into it, in type order, with
`objcopy --update-section`.

The object's own GNU property notes are read from its ELF sections, not from readelf's text:
only a GNU_PROPERTY_X86_FEATURE_1_AND property (readelf's "x86 feature:") is the mark. Others,
such as "x86 feature used:" (GNU_PROPERTY_X86_FEATURE_2_USED, from `-Wa,-mx86-used-note=yes`) or
"x86 ISA used:", don't count. An object whose FEATURE_1_AND has SHSTK is left alone; one whose
FEATURE_1_AND lacks it is refused (a second note would not be merged with it). After marking, the
object must have SHSTK, or this fails. glibc x86-64 Linux only: the Makefile doesn't mark musl
builds, where shadow stacks are never turned on. Build tooling only; nothing of it ends up in the
engine but the note.
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import tempfile

NT_GNU_PROPERTY_TYPE_0 = 5
SHT_NOTE = 7
GNU_PROPERTY_X86_FEATURE_1_AND = 0xC0000002
GNU_PROPERTY_X86_FEATURE_1_SHSTK = 0x2


def note(props: list[tuple[int, bytes]] | None = None) -> bytes:
    """One NT_GNU_PROPERTY_TYPE_0 note: the given properties plus FEATURE_1_AND = SHSTK, sorted by
    type as the linker expects, each padded to 8 bytes (ELFCLASS64)."""
    props = sorted([*(props or []), (GNU_PROPERTY_X86_FEATURE_1_AND,
                                     struct.pack("<I", GNU_PROPERTY_X86_FEATURE_1_SHSTK))])
    desc = b"".join(struct.pack("<II", t, len(d)) + d + b"\0" * (-len(d) % 8) for t, d in props)
    return struct.pack("<III", 4, len(desc), NT_GNU_PROPERTY_TYPE_0) + b"GNU\0" + desc


def _sections(data: bytes):
    """(name, type, offset, size) of each section of a little-endian x86-64 ELF file."""
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1 or struct.unpack_from("<H", data, 18)[0] != 62:
        raise ValueError("not a little-endian x86-64 ELF file")
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    if shoff == 0 or shentsize < 64 or shoff + shnum * shentsize > len(data) or shstrndx >= shnum:
        raise ValueError("bad section header table")
    heads = [struct.unpack_from("<IIQQQQ", data, shoff + i * shentsize) for i in range(shnum)]
    stroff, strsize = heads[shstrndx][4], heads[shstrndx][5]
    for name_off, sh_type, _, _, off, size in heads:
        if off + size > len(data) and sh_type != 8:     # SHT_NOBITS occupies no file space
            raise ValueError("section beyond the end of the file")
        end = data.find(b"\0", stroff + name_off, stroff + strsize)
        yield (data[stroff + name_off:end].decode("ascii", "replace") if end >= 0 else ""), sh_type, off, size


def _properties(data: bytes, off: int, size: int) -> list[tuple[int, bytes]]:
    """The properties of the GNU property notes in data[off:off + size]."""
    props = []
    pos, end = off, off + size
    while pos + 12 <= end:
        namesz, descsz, ntype = struct.unpack_from("<III", data, pos)
        name_at = pos + 12
        desc_at = name_at + ((namesz + 3) & ~3)
        if desc_at + descsz > end:
            raise ValueError("truncated note")
        if ntype == NT_GNU_PROPERTY_TYPE_0 and data[name_at:name_at + namesz] == b"GNU\0":
            p, pend = desc_at, desc_at + descsz
            while p + 8 <= pend:
                pr_type, pr_size = struct.unpack_from("<II", data, p)
                if p + 8 + pr_size > pend:
                    raise ValueError("truncated property")
                props.append((pr_type, data[p + 8:p + 8 + pr_size]))
                p += 8 + ((pr_size + 7) & ~7)
        pos = desc_at + ((descsz + 7) & ~7)     # ELFCLASS64 property notes: 8-byte aligned
    return props


def property_section(data: bytes) -> list[tuple[int, bytes]] | None:
    """The properties in the .note.gnu.property section, or None if there is no such section."""
    for name, sh_type, off, size in _sections(data):
        if name == ".note.gnu.property" and sh_type == SHT_NOTE:
            return _properties(data, off, size)
    return None


def feature_1_and(data: bytes) -> list[int]:
    """The GNU_PROPERTY_X86_FEATURE_1_AND bit sets in an x86-64 ELF file's GNU property notes."""
    found = []
    for _, sh_type, off, size in _sections(data):
        if sh_type == SHT_NOTE:
            found += [struct.unpack_from("<I", d)[0] for t, d in _properties(data, off, size)
                      if t == GNU_PROPERTY_X86_FEATURE_1_AND and len(d) >= 4]
    return found


def features_of(path: str) -> list[int]:
    with open(path, "rb") as f:
        return feature_1_and(f.read())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("object")
    ap.add_argument("--objcopy", default=os.environ.get("OBJCOPY", "objcopy"))
    args = ap.parse_args(argv)
    try:
        have = features_of(args.object)
    except ValueError as e:
        sys.exit(f"{args.object}: {e}")
    if have and all(bits & GNU_PROPERTY_X86_FEATURE_1_SHSTK for bits in have):
        print(f"{args.object}: already marked SHSTK; left alone")
        return 0
    if have:
        sys.exit(f"{args.object}: has an x86 feature property without SHSTK (0x{have[0]:x}); not marking it")
    with open(args.object, "rb") as f:
        existing = property_section(f.read())
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "note")
        with open(path, "wb") as f:
            f.write(note(existing))
        if existing is None:
            subprocess.run([args.objcopy, "--add-section", f".note.gnu.property={path}",
                            "--set-section-flags", ".note.gnu.property=alloc,readonly,data",
                            "--set-section-alignment", ".note.gnu.property=8", args.object], check=True)
        else:   # merged into the section that is there (other x86 properties)
            subprocess.run([args.objcopy, "--update-section", f".note.gnu.property={path}", args.object], check=True)
    have = features_of(args.object)
    if not have or not all(bits & GNU_PROPERTY_X86_FEATURE_1_SHSTK for bits in have):
        sys.exit(f"{args.object}: objcopy added no SHSTK property")
    print(f"{args.object}: marked SHSTK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
