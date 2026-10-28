/*
 * Test-only DST encoder core and synthetic DSD64 source for pysacd's tests.
 *
 * Written from docs/dst-bitstream.md (sections 3 and 6): it runs the
 * decoder's prediction model forwards and arithmetic-codes the symbols.
 * Frame headers (segmentation, mapping, filters, ptables) are written by
 * tests/dstenc.py, which drives this code through ctypes. Built by
 * `make test-tools` into build/dstenc.so.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define NB          (588 * 64)      /* bits per channel per frame (DSD64) */
#define FL          (NB / 8)
#define MAX_CH      6
#define MAX_SEG     9       /* segment table stride (decoder accepts up to 9) */

/* ------------------------------------------------------------ source */

/*
 * Deterministic DSD64 test signal: a second-order sigma-delta modulator per
 * channel driven by two sine tones (different per channel) and a small
 * LCG dither. Output layout is the decoder's: per frame, byte
 * floor(n/8)*channels + c holds bit n of channel c at bit 7 - n%8.
 */
void dstenc_sdm(int channels, int frames, uint32_t seed, uint8_t *out)
{
    for (int c = 0; c < channels; c++) {
        double i1 = 0, i2 = 0;
        double f1 = 440.0 * (1 + c) + 37 * (seed % 7), f2 = 3000.0 + 1234.5 * c;
        double a1 = 0.25 + 0.03 * c, a2 = 0.1;
        uint32_t rng = seed * 2654435761u + 12345u * (c + 1);
        uint64_t n = 0;
        for (int f = 0; f < frames; f++) {
            uint8_t *fr = out + (size_t)f * FL * channels;
            for (int i = 0; i < NB; i++, n++) {
                double t = (double)n / 2822400.0;
                rng = rng * 1103515245u + 12345u;
                double x = a1 * sin(2 * M_PI * f1 * t) + a2 * sin(2 * M_PI * f2 * t) +
                           ((double)(rng >> 8) / 16777216.0 - 0.5) * 1e-3;
                int y = i2 >= 0;
                double fb = y ? 1.0 : -1.0;
                i1 += x - fb;
                i2 += i1 - fb;
                uint8_t *b = fr + (size_t)(i >> 3) * channels + c;
                if (i % 8 == 0)
                    *b = 0;
                *b |= y << (7 - (i & 7));
            }
        }
    }
}

/* ------------------------------------------------------------ model */

/* Channel c as +-1 values, preceded by the 128-bit initial history (3.3):
   x[c][128 + n] is bit n, x[c][128 - 1 - i] is h[i] before bit 0. */
static int8_t *unpack(int channels, const uint8_t *dsd)
{
    int8_t *x = malloc((size_t)channels * (128 + NB));
    if (!x)
        return NULL;
    for (int c = 0; c < channels; c++) {
        int8_t *xc = x + (size_t)c * (128 + NB);
        for (int i = 0; i < 128; i++)
            xc[128 - 1 - i] = i & 1 ? 1 : -1;
        for (int n = 0; n < NB; n++)
            xc[128 + n] = (dsd[(size_t)(n >> 3) * channels + c] >> (7 - (n & 7))) & 1 ? 1 : -1;
    }
    return x;
}

/* Spred (3.4) for bit n of the channel whose +-1 samples are xc */
static inline int predict(const int8_t *xc, const int32_t *a, int order, int n)
{
    const int8_t *h = xc + 128 + n - 1;
    int s = 0;
    for (int i = 0; i < order; i++)
        s += a[i] * h[-i];
    return (int16_t)s;
}

/*
 * For every (channel, bit): the ptable index before clamping, min(|S|/8, 63),
 * and the symbol e = y XOR s. Layout [c][n]. Segment tables are
 * [MAX_CH][MAX_SEG + 1] bit offsets (terminated by NB) and [MAX_CH][MAX_SEG]
 * table numbers. Returns 0, or -1 when out of memory.
 */
int dstenc_analyze(int channels, const uint8_t *dsd, const int32_t *fstart, const int32_t *fmap,
                   const int32_t *order, const int32_t *coef, uint8_t *bucket, uint8_t *sym)
{
    int8_t *x = unpack(channels, dsd);
    if (!x)
        return -1;
    for (int c = 0; c < channels; c++) {
        const int8_t *xc = x + (size_t)c * (128 + NB);
        const int32_t *st = fstart + c * (MAX_SEG + 1);
        int seg = 0;
        for (int n = 0; n < NB; n++) {
            while (st[seg + 1] <= n)
                seg++;
            int f = fmap[c * MAX_SEG + seg];
            int s = predict(xc, coef + f * 128, order[f], n);
            int j = abs(s) >> 3;
            bucket[(size_t)c * NB + n] = j > 63 ? 63 : j;
            sym[(size_t)c * NB + n] = (xc[128 + n] > 0) ^ (s < 0);
        }
    }
    free(x);
    return 0;
}

/* ------------------------------------------------------------ arithmetic coder (6.7) */

typedef struct {
    uint8_t *bits;      /* cb[1], cb[2], ... one per byte */
    size_t cap, k;      /* k = renormalisation shifts = bits emitted */
    unsigned a, low;    /* low: the 12 low bits of Lo; bits above it are in `bits` */
    int overflow;
} enc_t;

static void enc_put(enc_t *e, int e_sym, int p)
{
    unsigned q = (e->a >> 8) | ((e->a >> 7) & 1);
    unsigned w0 = q * p, w1 = e->a - w0;
    if (e_sym) {
        e->a = w1;
    } else {
        e->low += w1;
        e->a = w0;
        if (e->low >= 4096) {           /* carry into the emitted bits */
            e->low -= 4096;
            size_t i = e->k;
            while (i > 0 && e->bits[i - 1]) {
                e->bits[--i] = 0;
            }
            if (i == 0)
                e->overflow = 1;        /* impossible: Lo < 2^(12+K) */
            else
                e->bits[i - 1] = 1;
        }
    }
    while (e->a < 2048) {
        e->a <<= 1;
        e->low <<= 1;
        if (e->k >= e->cap) {
            e->overflow = 1;
            e->k = 0;
        }
        e->bits[e->k++] = e->low >> 12;
        e->low &= 4095;
    }
}

/*
 * Arithmetic-code one frame: the DST_X_bit (symbol 0) then every bit, in
 * decoder order. Writes the code bits cb[0..n-1] (one per byte, cb[0] = 0)
 * to `code` and returns n, or -1 if `cap` is too small.
 */
long dstenc_code(int channels, const uint8_t *dsd,
                 const int32_t *fstart, const int32_t *fmap, const int32_t *pstart, const int32_t *pmap,
                 const int32_t *order, const int32_t *coef, const int32_t *plen, const int32_t *ptab,
                 const int32_t *half_len, uint8_t *code, size_t cap)
{
    if (cap < 2)
        return -1;
    int8_t *x = unpack(channels, dsd);
    if (!x)
        return -1;
    enc_t e = {.bits = code + 1, .cap = cap - 2, .a = 4095};
    int fseg[MAX_CH] = {0}, pseg[MAX_CH] = {0};

    /* DST_X_bit (3.8): p = bitrev7(a0 mod 128) + 1 */
    int a0 = coef[0] & 127, r = 0;
    for (int b = 0; b < 7; b++)
        if (a0 & (1 << b))
            r |= 64 >> b;
    enc_put(&e, 0, r + 1);

    for (int n = 0; n < NB; n++) {
        for (int c = 0; c < channels; c++) {
            const int8_t *xc = x + (size_t)c * (128 + NB);
            const int32_t *fs = fstart + c * (MAX_SEG + 1), *ps = pstart + c * (MAX_SEG + 1);
            while (fs[fseg[c] + 1] <= n)
                fseg[c]++;
            while (ps[pseg[c] + 1] <= n)
                pseg[c]++;
            int f = fmap[c * MAX_SEG + fseg[c]];
            int s = predict(xc, coef + f * 128, order[f], n);
            int p;
            if (n < half_len[c]) {
                p = 128;
            } else {
                int t = pmap[c * MAX_SEG + pseg[c]];
                int j = abs(s) >> 3, last = plen[t] - 1;
                p = ptab[t * 64 + (j < last ? j : last)];
            }
            enc_put(&e, (xc[128 + n] > 0) ^ (s < 0), p);
        }
    }
    free(x);
    if (e.overflow)
        return -1;

    /* Termination: Mv = 2^11 * ceil(Lo / 2^11); send floor(Mv / 2^11) */
    unsigned mv = (e.low + 2047) >> 11;     /* 0, 1 or 2 */
    if (e.k + 1 > e.cap)
        return -1;
    if (mv == 2) {
        size_t i = e.k;
        while (i > 0 && e.bits[i - 1])
            e.bits[--i] = 0;
        if (i == 0)
            return -1;
        e.bits[i - 1] = 1;
        mv = 0;
    }
    e.bits[e.k] = mv;
    code[0] = 0;
    return (long)e.k + 2;
}
