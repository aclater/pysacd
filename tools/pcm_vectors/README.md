<!-- SPDX-License-Identifier: MIT OR Apache-2.0 -->
# DSD-to-PCM test vectors

Produces DSD-to-PCM reference vectors from pysacd's filter (`csrc/dsp.c`) and
its 24-bit quantiser (copied from `process_batch()` in `csrc/sacdconv.c`).

Licence: MIT OR Apache-2.0 for this directory (as `csrc/dsp.c`).

## Build and run

    tools/pcm_vectors/run_all.sh [outdir]      # builds, makes inputs, writes ./vectors by default

Or step by step:

    tools/pcm_vectors/build.sh                 # build/pcm_vectors_ref, build/pcm_vectors_wheel
    python3 tools/pcm_vectors/make_inputs.py inputs
    build/pcm_vectors_ref inputs/music_2ch.dsd 2 88200 0 0 out.f32 out.i32

Arguments: `in.dsd channels rate gain_db tc0 out.f32 out.i32`. `run_all.sh` uses
gain 0 dB and tc0 0 for channels {2, 6} x rates {88200, 176400} x inputs
{silence, ff, music}.

## Formats

- Input `*.dsd`: raw SACD frames, 4704 bytes per channel per frame,
  channel-interleaved by whole per-channel blocks, MSB-first, as
  `dsd2pcm_frame` takes them. `make_inputs.py` writes 20 frames each of
  silence (0x69), all 0xFF, and a synthetic "music" signal (1 kHz + 7 kHz
  sines through a first-order 1-bit delta-sigma modulator at DSD64; not a
  recording).
- `*.f32`: little-endian float32, `out[c][s]` for each frame (channel 0's
  samples, then channel 1's, ...) before quantising, one block per frame
  including the flush frames. Samples per frame: 1176 (88.2 kHz), 2352
  (176.4 kHz).
- `*.i32`: little-endian int32, final 24-bit-range samples, channel-interleaved.
  The whole input is one batch with pre = 0: the first `delay` output samples
  are skipped, `frames * samples_per_frame` are kept, and DSD silence frames
  flush the tail. Quantiser: `scale = 8388607 * 10^(gain_db/20)`, TPDF dither
  from xorshift32 seeded `0x9E3779B9 ^ (tc0 * 2654435761)`, `lrintf`, clip to
  24 bits.

## Reference

pysacd's shipped bytes vary by CPU clone (x86-64-v4, v3, default) and by
build flags (`-ffast-math`, FMA contraction, `SC_DISPATCH`). The `ref`
variant (`-O2 -ffp-contract=off`, no `SC_DISPATCH`, no `-march`) is the
defined reference. The `wheel` variant uses the wheel CFLAGS
(`-O3 -ffast-math -fno-finite-math-only -DSC_DISPATCH`); on the machine used
here it differs from `ref` by at most 7 in the int32 output and 8e-7 in the
float output.

## SHA-256 of the `ref` int32 outputs (gain 0 dB, tc0 0)


    46455c1ab2fe2c549122877ede59ef6c6694e90bf30f55e4563eb845c6779567  ff_2ch_176400.i32
    664e0bebcb9c43af1e181cb3ffcc65e930789188f9ffd4e308059c38458ee7e7  ff_2ch_88200.i32
    991e2ed7baded73e72a98ac826b1fc4ea71b127f64bb89ccbd1d8015de3779f1  ff_6ch_176400.i32
    f76b54c48bcfb626962d61c3444cdfddf9a14f9179792523e5c45a2b39359bdf  ff_6ch_88200.i32
    2572fe056718bb1d4c9a24dae403882f5ad80c3c755b719bcc5784ef11db9f31  music_2ch_176400.i32
    2bad5fe4f8dc1ebf48231f1a8c2cbd93fc2c163c68e7b1b0cb85573869d31940  music_2ch_88200.i32
    26edbf5b4ba3865ad58088831a2be93f11eea4d080761581a7f418a189f47628  music_6ch_176400.i32
    dd8af40a1c4eec6b2c251683922658f62f3615b7f495b38763ad4503b2e9a242  music_6ch_88200.i32
    1cec0ebe5630f70fed2703645cbc534ef526bae152cf4c8ce4cd8b998d93d39d  silence_2ch_176400.i32
    e51c398d168df53c17197ccfc22d513117350363eb5d4a9e33c370cdafdc061e  silence_2ch_88200.i32
    be1a942057cef6720691e41020186495c2f5c968fe7221859ae8473cfeb0be8a  silence_6ch_176400.i32
    ae6b35b2fdd17c0691c9ab3352a64c2cf2848ba4df6112095c25f131404b4cd3  silence_6ch_88200.i32

The `music` input is generated with Python's `math.sin`; the hashes assume the
inputs from `make_inputs.py` as run with CPython on x86-64 glibc.
