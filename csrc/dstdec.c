/*
 * Direct Stream Transfer (DST) decoder
 * Copyright (c) 2014 Peter Ross <pross@xvid.org>
 *
 * Standalone port of FFmpeg's libavcodec/dstdec.c (FFmpeg n7.1) for pysacd:
 * libavcodec plumbing and the DSD->PCM step are removed so the decoder
 * returns the DSD bits themselves; the bit reader and Golomb reader are
 * reimplemented here. The decoding algorithm is unchanged.
 *
 * This file is part of FFmpeg.
 *
 * FFmpeg is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * FFmpeg is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with FFmpeg; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
 *
 * SPDX-License-Identifier: LGPL-2.1-or-later
 */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "dstdec.h"

#define DST_MAX_CHANNELS 6
#define DST_MAX_ELEMENTS (2 * DST_MAX_CHANNELS)
#define SAMPLES_PER_FRAME (588 * 64)    /* DSD64 */

static const int8_t fsets_code_pred_coeff[3][3] = {
    {  -8 },
    { -16,  8 },
    {  -9, -5, 6 },
};

static const int8_t probs_code_pred_coeff[3][3] = {
    {  -8 },
    { -16,  8 },
    { -24, 24, -8 },
};

/* ------------------------------------------------------------ bit reader */

typedef struct {
    const uint8_t *buf;
    size_t size_bits;
    size_t index;
} BitReader;

static inline int av_log2(unsigned v)
{
    return 31 - __builtin_clz(v | 1);
}

static inline unsigned show_bits(const BitReader *b, int n)
{
    /* n <= 25; reads past the end return zeros */
    size_t byte = b->index >> 3;
    uint32_t v = 0;
    for (int i = 0; i < 4; i++)
        v = v << 8 | (byte + i < (b->size_bits >> 3) ? b->buf[byte + i] : 0);
    return (v << (b->index & 7)) >> (32 - n);
}

static inline unsigned get_bits(BitReader *b, int n)
{
    if (!n)
        return 0;
    unsigned v = show_bits(b, n);
    b->index += n;
    return v;
}

static inline unsigned get_bits1(BitReader *b)
{
    size_t i = b->index++;
    if (i >= b->size_bits)
        return 0;
    return (b->buf[i >> 3] >> (7 - (i & 7))) & 1;
}

static inline int get_sbits(BitReader *b, int n)
{
    int v = (int)get_bits(b, n);
    return (v ^ (1 << (n - 1))) - (1 << (n - 1));
}

static inline long get_bits_left(const BitReader *b)
{
    return (long)b->size_bits - (long)b->index;
}

/* Golomb-Rice code as read by FFmpeg's get_ur_golomb_jpegls(gb, k, limit, 0) */
static int get_ur_golomb(BitReader *b, unsigned k, long limit)
{
    long q = 0;
    while (q < limit && !get_bits1(b)) {
        if (get_bits_left(b) <= 0)
            return -1;
        q++;
    }
    if (q >= limit - 1)
        return q == limit - 1 ? 1 : -1;     /* escape with esc_len 0 */
    return (int)((unsigned)q << k) + (int)get_bits(b, k);
}

/* ------------------------------------------------------------ decoder */

typedef struct {
    unsigned a, c;
} ArithCoder;

typedef struct {
    unsigned elements;
    unsigned length[DST_MAX_ELEMENTS];
    int coeff[DST_MAX_ELEMENTS][128];
} Table;

struct dst_decoder {
    int channels;
    BitReader gb;
    ArithCoder ac;
    Table fsets, probs;
    uint8_t status[DST_MAX_CHANNELS][16] __attribute__((aligned(16)));
    int16_t filter[DST_MAX_ELEMENTS][16][256] __attribute__((aligned(16)));
    uint8_t reverse[256];
};

static int read_map(BitReader *gb, Table *t, unsigned map[DST_MAX_CHANNELS], int channels)
{
    t->elements = 1;
    map[0] = 0;
    if (!get_bits1(gb)) {
        for (int ch = 1; ch < channels; ch++) {
            int bits = av_log2(t->elements) + 1;
            map[ch] = get_bits(gb, bits);
            if (map[ch] == t->elements) {
                t->elements++;
                if (t->elements >= DST_MAX_ELEMENTS)
                    return -1;
            } else if (map[ch] > t->elements) {
                return -1;
            }
        }
    } else {
        memset(map, 0, sizeof(*map) * DST_MAX_CHANNELS);
    }
    return 0;
}

static inline int get_sr_golomb_dst(BitReader *gb, unsigned k)
{
    int v = get_ur_golomb(gb, k, get_bits_left(gb));
    if (v && get_bits1(gb))
        v = -v;
    return v;
}

static void read_uncoded_coeff(BitReader *gb, int *dst, unsigned elements,
                               int coeff_bits, int is_signed, int offset)
{
    for (unsigned i = 0; i < elements; i++)
        dst[i] = (is_signed ? get_sbits(gb, coeff_bits) : (int)get_bits(gb, coeff_bits)) + offset;
}

static int read_table(BitReader *gb, Table *t, const int8_t code_pred_coeff[3][3],
                      int length_bits, int coeff_bits, int is_signed, int offset)
{
    for (unsigned i = 0; i < t->elements; i++) {
        t->length[i] = get_bits(gb, length_bits) + 1;
        if (!get_bits1(gb)) {
            read_uncoded_coeff(gb, t->coeff[i], t->length[i], coeff_bits, is_signed, offset);
        } else {
            int method = get_bits(gb, 2), lsb_size;
            if (method == 3)
                return -1;

            read_uncoded_coeff(gb, t->coeff[i], method + 1, coeff_bits, is_signed, offset);

            lsb_size = get_bits(gb, 3);
            for (unsigned j = method + 1; j < t->length[i]; j++) {
                int c, x = 0;
                for (int k = 0; k < method + 1; k++)
                    x += code_pred_coeff[method][k] * (unsigned)t->coeff[i][j - k - 1];
                c = get_sr_golomb_dst(gb, lsb_size);
                if (x >= 0)
                    c -= (x + 4) / 8;
                else
                    c += (-x + 3) / 8;
                if (!is_signed) {
                    if (c < offset || c >= offset + (1 << coeff_bits))
                        return -1;
                }
                t->coeff[i][j] = c;
            }
        }
    }
    return 0;
}

static void ac_init(ArithCoder *ac, BitReader *gb)
{
    ac->a = 4095;
    ac->c = get_bits(gb, 12);
}

static inline int ac_get(ArithCoder *ac, BitReader *gb, int p)
{
    unsigned k = (ac->a >> 8) | ((ac->a >> 7) & 1);
    unsigned q = k * p;
    unsigned a_q = ac->a - q;
    int e = ac->c < a_q;

    if (e) {
        ac->a = a_q;
    } else {
        ac->a = q;
        ac->c -= a_q;
    }
    if (ac->a < 2048) {
        int n = 11 - av_log2(ac->a);
        ac->a <<= n;
        ac->c = (ac->c << n) | get_bits(gb, n);
    }
    return e;
}

static int build_filter(int16_t table[DST_MAX_ELEMENTS][16][256], const Table *fsets)
{
    for (unsigned i = 0; i < fsets->elements; i++) {
        int length = fsets->length[i];
        for (int j = 0; j < 16; j++) {
            int total = length - j * 8;
            total = total < 0 ? 0 : total > 8 ? 8 : total;
            for (int k = 0; k < 256; k++) {
                int64_t v = 0;
                for (int l = 0; l < total; l++)
                    v += (((k >> l) & 1) * 2 - 1) * fsets->coeff[i][j * 8 + l];
                if ((int16_t)v != v)
                    return -1;
                table[i][j][k] = (int16_t)v;
            }
        }
    }
    return 0;
}

dst_decoder_t *dst_decoder_new(int channels)
{
    if (channels < 1 || channels > DST_MAX_CHANNELS)
        return NULL;
    dst_decoder_t *s = aligned_alloc(64, (sizeof(*s) + 63) & ~(size_t)63);
    if (!s)
        return NULL;
    memset(s, 0, sizeof(*s));
    s->channels = channels;
    for (int i = 0; i < 256; i++) {
        uint8_t r = 0;
        for (int b = 0; b < 8; b++)
            if (i & (1 << b))
                r |= 0x80 >> b;
        s->reverse[i] = r;
    }
    return s;
}

void dst_decoder_free(dst_decoder_t *s)
{
    free(s);
}

int dst_decode_frame(dst_decoder_t *s, const uint8_t *data, int size, uint8_t *dsd)
{
    unsigned map_ch_to_felem[DST_MAX_CHANNELS];
    unsigned map_ch_to_pelem[DST_MAX_CHANNELS];
    unsigned half_prob[DST_MAX_CHANNELS];
    const int channels = s->channels;
    const size_t out_bytes = (size_t)SAMPLES_PER_FRAME / 8 * channels;
    BitReader *gb = &s->gb;
    ArithCoder *ac = &s->ac;

    if (size <= 1)
        return DST_ERR_INVALID;

    gb->buf = data;
    gb->size_bits = (size_t)size * 8;
    gb->index = 0;

    if (!get_bits1(gb)) {
        /* Uncompressed frame: plain interleaved DSD after a 1-byte header */
        get_bits1(gb);
        if (get_bits(gb, 6))
            return DST_ERR_INVALID;
        size_t n = (size_t)size - 1 < out_bytes ? (size_t)size - 1 : out_bytes;
        memcpy(dsd, data + 1, n);
        memset(dsd + n, 0, out_bytes - n);
        return 0;
    }

    /* Segmentation (10.4, 10.5, 10.6) */
    if (!get_bits1(gb) || !get_bits1(gb) || !get_bits1(gb))
        return DST_ERR_UNSUPPORTED;     /* per-channel/segmented filters: not implemented */

    /* Mapping (10.7, 10.8, 10.9) */
    unsigned same_map = get_bits1(gb);
    if (read_map(gb, &s->fsets, map_ch_to_felem, channels) < 0)
        return DST_ERR_INVALID;
    if (same_map) {
        s->probs.elements = s->fsets.elements;
        memcpy(map_ch_to_pelem, map_ch_to_felem, sizeof(map_ch_to_felem));
    } else if (read_map(gb, &s->probs, map_ch_to_pelem, channels) < 0) {
        return DST_ERR_INVALID;
    }

    /* Half Probability (10.10) */
    for (int ch = 0; ch < channels; ch++)
        half_prob[ch] = get_bits1(gb);

    /* Filter Coef Sets (10.12) */
    if (read_table(gb, &s->fsets, fsets_code_pred_coeff, 7, 9, 1, 0) < 0)
        return DST_ERR_INVALID;

    /* Probability Tables (10.13) */
    if (read_table(gb, &s->probs, probs_code_pred_coeff, 6, 7, 0, 1) < 0)
        return DST_ERR_INVALID;

    /* Arithmetic Coded Data (10.11) */
    if (get_bits1(gb))
        return DST_ERR_INVALID;
    ac_init(ac, gb);

    if (build_filter(s->filter, &s->fsets) < 0)
        return DST_ERR_INVALID;

    memset(s->status, 0xAA, sizeof(s->status));
    memset(dsd, 0, out_bytes);

    /* DST_X_Bit, unused */
    ac_get(ac, gb, (s->reverse[s->fsets.coeff[0][0] & 127] >> 1) + 1);

    for (unsigned i = 0; i < SAMPLES_PER_FRAME; i++) {
        for (int ch = 0; ch < channels; ch++) {
            const unsigned felem = map_ch_to_felem[ch];
            int16_t (*filter)[256] = s->filter[felem];
            uint8_t *status = s->status[ch];
            int prob;

#define F(x) filter[(x)][status[(x)]]
            const int16_t predict = F( 0) + F( 1) + F( 2) + F( 3) +
                                    F( 4) + F( 5) + F( 6) + F( 7) +
                                    F( 8) + F( 9) + F(10) + F(11) +
                                    F(12) + F(13) + F(14) + F(15);
#undef F

            if (!half_prob[ch] || i >= s->fsets.length[felem]) {
                unsigned pelem = map_ch_to_pelem[ch];
                unsigned index = (unsigned)abs(predict) >> 3;
                unsigned last = s->probs.length[pelem] - 1;
                prob = s->probs.coeff[pelem][index < last ? index : last];
            } else {
                prob = 128;
            }

            int residual = ac_get(ac, gb, prob);
            int v = ((predict >> 15) ^ residual) & 1;
            dsd[(i >> 3) * channels + ch] |= v << (7 - (i & 7));

            /* 128-bit shift register, newest bit in status[0] bit 0 */
            uint64_t lo, hi;
            memcpy(&lo, status, 8);
            memcpy(&hi, status + 8, 8);
            hi = (hi << 1) | (lo >> 63);
            lo = (lo << 1) | (uint64_t)v;
            memcpy(status, &lo, 8);
            memcpy(status + 8, &hi, 8);
        }
    }
    return 0;
}
