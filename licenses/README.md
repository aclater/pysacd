# Third-party licences

Wheels built by `.github/workflows/workflow.yml` have libFLAC linked into the engine
statically, so FLAC output works without installing anything. The Linux and macOS wheels
build it from its release source without Ogg support (#142, #153); the Windows DLL links
MSYS2's libFLAC, which brings libogg with it:

| Library | Licence | File |
|---|---|---|
| libFLAC | BSD-3-Clause (Xiph.Org) | [libFLAC-COPYING.Xiph](libFLAC-COPYING.Xiph) |
| libogg (a libFLAC dependency; Windows wheel only) | BSD-3-Clause (Xiph.Org) | [libogg-COPYING](libogg-COPYING) |

The engine itself has the [scarletbook-rs](https://github.com/aclater/scarletbook-rs) C library
linked in statically (#85), in every wheel and every build from source:

| Library | Licence | File |
|---|---|---|
| scarletbook-rs | MIT OR Apache-2.0, used under MIT | [scarletbook-rs-LICENSE-MIT](scarletbook-rs-LICENSE-MIT) |
| the Rust standard library, rayon, crossbeam, either, thiserror (inside it) | MIT OR Apache-2.0, used under MIT | [scarletbook-rs-runtime-MIT](scarletbook-rs-runtime-MIT) |

pysacd takes the dual-licensed code under MIT because MIT is compatible with GPL-2.0, and
Apache-2.0 is not compatible with GPL-2.0 (only with GPL-3.0).

pysacd itself is GPL-2.0-or-later ([COPYING](../COPYING)).
