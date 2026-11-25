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

The engine also has the [dsd2pcm](https://github.com/dsd-toolbox/dsd2pcm-rs) C library linked in
statically, which converts DSD to PCM. It is built from the release source pinned in
`dsd2pcm.lock` without its default features, so it holds only dsd2pcm and its own copy of the Rust
standard library; `tools/dsd2pcm.py` refuses to build if `cargo tree` shows any crate without an
MIT-compatible licence alternative (dsd2pcm's FLAC reader, claxon, is Apache-2.0 only and is left
out):

| Library | Licence | File |
|---|---|---|
| dsd2pcm | MIT OR Apache-2.0, used under MIT | [dsd2pcm-LICENSE-MIT](dsd2pcm-LICENSE-MIT) |
| the Rust standard library (inside it) | MIT OR Apache-2.0, used under MIT | [scarletbook-rs-runtime-MIT](scarletbook-rs-runtime-MIT) (its standard library notice) |

`tools/mark_shstk.py`, which marks dsd2pcm's object for CET shadow stacks during the build, is
pysacd's own build tooling (GPL-2.0-or-later); it adds a note section to the object and is not
shipped.

The musllinux wheel also carries GCC's runtime support library, `libgcc_s`, which auditwheel
copies in next to the engine because musl systems may not have it (the Rust code in the
scarletbook library needs it to unwind). It is not modified:

| Library | Licence | Files |
|---|---|---|
| libgcc_s (musllinux wheel only) | GPL-3.0-or-later with the GCC Runtime Library Exception 3.1 | [libgcc-COPYING3](libgcc-COPYING3), [libgcc-COPYING.RUNTIME](libgcc-COPYING.RUNTIME) |

The exception lets code compiled with GCC, and linked with this library, be distributed under
its own licence.

pysacd takes the dual-licensed code under MIT because MIT is compatible with GPL-2.0, and
Apache-2.0 is not compatible with GPL-2.0 (only with GPL-3.0).

pysacd itself is GPL-2.0-or-later ([COPYING](../COPYING)), except for its former DSD-to-PCM
filter.

**The former DSD-to-PCM filter** (`tools/pcm_vectors/dsp.c`, `tools/pcm_vectors/dsp.h`; in
`csrc/` up to 0.5.5) is MIT OR Apache-2.0
([dsp-LICENSE-MIT](dsp-LICENSE-MIT), [dsp-LICENSE-APACHE](dsp-LICENSE-APACHE)). Its copyright
holder, the only author of both files, relicensed them from GPL-2.0-or-later on 2026-11-23, so that
other projects can reuse the filter. Earlier versions of the two files remain available under
GPL-2.0-or-later as well. The byte-wise lookup-table technique in its first stage is credited to
Sebastian Gesemann's dsd2pcm (BSD-3-Clause), as the file says. The `pysacd` filter of
the dsd2pcm library linked into the engine (dsd2pcm-rs, above) is a port of the two files. Since 0.6.0 the engine no longer compiles the files: they make the
reference vectors in `tools/pcm_vectors/` that dsd2pcm is checked against.

**Documentation.** The README's text, the documentation in `docs/` (icons included) and the
measurement data published in it are CC BY 4.0 ([CC-BY-4.0.txt](CC-BY-4.0.txt)), except for the parts the README lists under
[Licence and credits](../README.md#licence-and-credits). The licence covers the text and data,
not the code; the code's licences are the ones above.
