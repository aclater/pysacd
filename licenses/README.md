# Third-party licences

Wheels built by `.github/workflows/workflow.yml` bundle these libraries next to
the engine (`pysacd.libs/`), so FLAC output works without installing anything:

| Library | Licence | File |
|---|---|---|
| libFLAC | BSD-3-Clause (Xiph.Org) | [libFLAC-COPYING.Xiph](libFLAC-COPYING.Xiph) |
| libogg (a libFLAC dependency) | BSD-3-Clause (Xiph.Org) | [libogg-COPYING](libogg-COPYING) |

The engine itself has the [scarletbook-rs](https://github.com/aclater/scarletbook-rs) C library
linked in statically (#85), in every wheel and every build from source:

| Library | Licence | File |
|---|---|---|
| scarletbook-rs | MIT OR Apache-2.0, used under MIT | [scarletbook-rs-LICENSE-MIT](scarletbook-rs-LICENSE-MIT) |
| the Rust standard library, rayon, crossbeam, either, thiserror (inside it) | MIT OR Apache-2.0, used under MIT | [scarletbook-rs-runtime-MIT](scarletbook-rs-runtime-MIT) |

pysacd takes the dual-licensed code under MIT because MIT is compatible with GPL-2.0, and
Apache-2.0 is not compatible with GPL-2.0 (only with GPL-3.0).

`csrc/dstdec.c` is LGPL-2.1-or-later, from FFmpeg; its header says so.
pysacd itself is GPL-2.0-or-later ([COPYING](../COPYING)).
