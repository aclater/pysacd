/* SPDX-License-Identifier: MIT OR Apache-2.0 */
/*
 * DSD-to-PCM test vector generator.
 *
 *   pcm_vectors <in.dsd> <channels> <rate> <gain_db> <tc0> <out.f32> <out.i32> [batch|frame]
 *
 * in.dsd:  raw SACD frames, channel-interleaved, MSB-first (as dsd2pcm_frame takes them).
 * out.f32: little-endian float32 out[c][s] per frame, before quantising
 *          (per frame: channel 0 samples, then channel 1, ...).
 * out.i32: little-endian int32 final samples, channel-interleaved, after
 *          skip/keep; the whole input is one batch with pre = 0.
 * batch (default): the dither is seeded once, from tc0, as pysacd up to 0.5 seeded each batch.
 * frame: it is seeded at the start of each frame from tc0 + its index, as pysacd since 0.6.0
 *        (dsd2pcm's pysacd filter) does.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "dsp.c"

static inline uint32_t xorshift(uint32_t *s)
{
    uint32_t x = *s;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    return *s = x;
}

static void put_le32(FILE *f, uint32_t v)
{
    uint8_t b[4] = {(uint8_t)v, (uint8_t)(v >> 8), (uint8_t)(v >> 16), (uint8_t)(v >> 24)};
    fwrite(b, 1, 4, f);
}

int main(int argc, char **argv)
{
    if (argc != 8 && argc != 9) {
        fprintf(stderr, "usage: %s in.dsd channels rate gain_db tc0 out.f32 out.i32 [batch|frame]\n", argv[0]);
        return 2;
    }
    int per_frame = argc == 9 && !strcmp(argv[8], "frame");
    if (argc == 9 && !per_frame && strcmp(argv[8], "batch")) {
        fprintf(stderr, "seeding must be batch or frame\n");
        return 2;
    }
    int ch = atoi(argv[2]), rate = atoi(argv[3]);
    double gain_db = atof(argv[4]);
    uint32_t tc0 = (uint32_t)strtoul(argv[5], NULL, 0);
    if (ch < 1 || ch > MAX_CHANNELS_SACD || (rate != 88200 && rate != 176400)) {
        fprintf(stderr, "bad channels or rate\n");
        return 2;
    }
    size_t fb = (size_t)ch * FRAME_BYTES_PER_CH;
    FILE *fi = fopen(argv[1], "rb");
    if (!fi) {
        perror(argv[1]);
        return 1;
    }
    fseek(fi, 0, SEEK_END);
    long sz = ftell(fi);
    fseek(fi, 0, SEEK_SET);
    if (sz <= 0 || (size_t)sz % fb) {
        fprintf(stderr, "input size is not a whole number of frames\n");
        return 1;
    }
    int nf = (int)((size_t)sz / fb);
    uint8_t *dsd = malloc((size_t)sz), *silence = malloc(fb);
    dsd2pcm_t *dsp = malloc(sizeof(*dsp));
    if (!dsd || !silence || !dsp || fread(dsd, 1, (size_t)sz, fi) != (size_t)sz) {
        fprintf(stderr, "read/alloc failed\n");
        return 1;
    }
    fclose(fi);
    memset(silence, DSD_SILENCE, fb);
    if (dsd2pcm_init(dsp, ch, rate) < 0) {
        fprintf(stderr, "dsd2pcm_init failed\n");
        return 1;
    }
    FILE *ff = fopen(argv[6], "wb"), *fq = fopen(argv[7], "wb");
    if (!ff || !fq) {
        perror("output");
        return 1;
    }

    /* Quantiser: copied from process_batch() in csrc/sacdconv.c (pre = 0, b->n = nf).
     * The float dump is added; everything else is the same code. */
    int opf = dsp->out_per_frame;
    uint32_t skip = (uint32_t)dsp->delay;
    uint32_t keep = (uint32_t)nf * opf, got = 0, pos = 0;
    uint32_t rng = 0x9E3779B9u ^ (tc0 * 2654435761u);
    const float scale = 8388607.0f * (float)pow(10.0, gain_db / 20.0);
    const float lsb = 1.0f / 4294967296.0f;

    dsd2pcm_reset(dsp);
    for (int i = 0; got < keep; i++) {
        dsd2pcm_frame(dsp, i < nf ? dsd + (size_t)i * fb : silence);
        for (int c = 0; c < ch; c++)
            for (int s = 0; s < opf; s++) {
                union { float f; uint32_t u; } w = {dsp->out[c][s]};
                put_le32(ff, w.u);
            }
        for (int s = 0; s < opf && got < keep; s++, pos++) {
            if (pos < skip)
                continue;
            if (per_frame && got % opf == 0)
                rng = 0x9E3779B9u ^ ((tc0 + got / opf) * 2654435761u);
            for (int c = 0; c < ch; c++) {
                uint32_t r1 = xorshift(&rng), r2 = xorshift(&rng); /* fixed order, as in sacdconv.c */
                float dith = ((float)r1 - (float)r2) * lsb;
                long v = lrintf(dsp->out[c][s] * scale + dith);
                put_le32(fq, (uint32_t)(int32_t)(v > 8388607 ? 8388607 : v < -8388608 ? -8388608 : v));
            }
            got++;
        }
    }
    fclose(ff);
    fclose(fq);
    dsd2pcm_free(dsp);
    free(dsp);
    free(dsd);
    free(silence);
    return 0;
}
