# Third-party licences

Wheels built by `.github/workflows/workflow.yml` bundle these libraries next to
the engine (`pysacd.libs/`), so FLAC output works without installing anything:

| Library | Licence | File |
|---|---|---|
| libFLAC | BSD-3-Clause (Xiph.Org) | [libFLAC-COPYING.Xiph](libFLAC-COPYING.Xiph) |
| libogg (a libFLAC dependency) | BSD-3-Clause (Xiph.Org) | [libogg-COPYING](libogg-COPYING) |

`csrc/dstdec.c` is LGPL-2.1-or-later, from FFmpeg; its header says so.
pysacd itself is GPL-2.0-or-later ([COPYING](../COPYING)).
