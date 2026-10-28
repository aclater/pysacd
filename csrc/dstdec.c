/*
 * Direct Stream Transfer (DST) decoder
 * Copyright (c) 2014 Peter Ross <pross@xvid.org>
 *
 * Standalone port of FFmpeg's libavcodec/dstdec.c (FFmpeg n7.1) for pysacd:
 * libavcodec plumbing and the DSD->PCM step are removed so the decoder
 * returns the DSD bits themselves; the bit reader and Golomb reader are
 * reimplemented here.
 * Segmentation, per-segment mapping, one-entry probability tables, the
 * table-count limit and the reference validity checks (including the
 * end-of-frame check) were written for pysacd from docs/dst-bitstream.md.
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

#include "compat.h"
#include "dstdec.h"

#define DST_MAX_CHANNELS 6
#define DST_MAX_ELEMENTS (2 * DST_MAX_CHANNELS)
#define SAMPLES_PER_FRAME (588 * 64)    /* DSD64: NB */
#define FRAME_LEN_BYTES (SAMPLES_PER_FRAME / 8)     /* FL */

/*
 * Segmentation limits: explicit segment lengths per channel, and Lmin in bytes
 * (docs/dst-bitstream.md 2.4 and section 8). Filters: up to 4 lengths (5
 * segments) when one segmentation is shared by all channels, 3 per channel.
 * Probability tables: up to 7 lengths (8 segments) either way; the
 * reference cannot decode a ninth ptable segment.
 */
#define FILTER_LENGTHS_SHARED 4
#define FILTER_LENGTHS 3
#define FILTER_LMIN 128
#define PTABLE_LENGTHS 7
#define PTABLE_LMIN 4
#define MAX_SEGMENTS (PTABLE_LENGTHS + 1)

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

/* bitlen(x): bits needed to write x (bitlen(0) = 0) */
static inline int bitlen(unsigned v)
{
    return v ? av_log2(v) + 1 : 0;
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

/* Signed Rice code (2.10); running out of frame data inside it is an error */
static int get_rice(BitReader *b, unsigned m, int *out)
{
    unsigned q = 0;
    for (;;) {
        if (b->index >= b->size_bits)
            return -1;
        if (get_bits1(b))
            break;
        q++;
    }
    if (q > (1u << 20))
        return -1;      /* far outside any valid coefficient or entry */
    int u = (int)((q << m) + get_bits(b, m));
    *out = u && get_bits1(b) ? -u : u;
    return 0;
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

/* One segmentation (filters or ptables) and its table mapping */
typedef struct {
    int nseg[DST_MAX_CHANNELS];
    unsigned start[DST_MAX_CHANNELS][MAX_SEGMENTS + 1];  /* bit offsets; start[nseg] = NB */
    unsigned map[DST_MAX_CHANNELS][MAX_SEGMENTS];
} Segments;

struct dst_decoder {
    int channels;
    BitReader gb;
    ArithCoder ac;
    Table fsets, probs;
    Segments fseg, pseg;
    uint8_t status[DST_MAX_CHANNELS][16] __attribute__((aligned(16)));
    int16_t filter[DST_MAX_ELEMENTS][16][256] __attribute__((aligned(16)));
    uint8_t reverse[256];
};

/* table_segmentation() (2.4). The resolution is sent at most once, before the
   first explicit segment length. */
static int read_segmentation(BitReader *gb, Segments *sg, int channels,
                             int max_shared, int max_per_channel, int lmin_bytes)
{
    const unsigned rem0 = FRAME_LEN_BYTES - lmin_bytes;
    const int res_bits = bitlen(rem0);
    unsigned res = 0;
    int shared = get_bits1(gb);
    const int max_lengths = shared ? max_shared : max_per_channel;

    for (int ch = 0; ch < (shared ? 1 : channels); ch++) {
        unsigned rem = rem0, pos = 0;
        int n = 0;
        while (!get_bits1(gb)) {        /* end_of_channel_segm */
            if (n >= max_lengths)
                return -1;
            if (!res) {
                res = get_bits(gb, res_bits);
                if (!res || res > rem0)
                    return -1;
            }
            unsigned len = get_bits(gb, bitlen(rem / res));
            if (res * len < (unsigned)lmin_bytes || res * len > rem)
                return -1;      /* shorter than Lmin, or leaves less than Lmin */
            rem -= res * len;
            pos += 8 * res * len;
            sg->start[ch][++n] = pos;
        }
        sg->start[ch][0] = 0;
        sg->start[ch][n + 1] = SAMPLES_PER_FRAME;
        sg->nseg[ch] = n + 1;
    }
    for (int ch = 1; shared && ch < channels; ch++) {
        sg->nseg[ch] = sg->nseg[0];
        memcpy(sg->start[ch], sg->start[0], sizeof(sg->start[0]));
    }
    return 0;
}

/* table_mapping() (2.5); returns the number of tables or -1 */
static int read_mapping(BitReader *gb, Segments *sg, int channels)
{
    unsigned t = 1;
    int shared = get_bits1(gb);

    for (int ch = 0; ch < (shared ? 1 : channels); ch++) {
        for (int j = 0; j < sg->nseg[ch]; j++) {
            if (!ch && !j) {
                sg->map[0][0] = 0;
                continue;
            }
            unsigned v = get_bits(gb, bitlen(t));
            if (v > t)
                return -1;
            if (v == t)
                t++;
            sg->map[ch][j] = v;
        }
    }
    for (int ch = 1; shared && ch < channels; ch++) {
        if (sg->nseg[ch] != sg->nseg[0])
            return -1;
        memcpy(sg->map[ch], sg->map[0], sizeof(sg->map[0]));
    }
    if (t > 2u * channels)
        return -1;
    return (int)t;
}

static void read_uncoded_coeff(BitReader *gb, int *dst, unsigned elements,
                               int coeff_bits, int is_signed, int offset)
{
    for (unsigned i = 0; i < elements; i++)
        dst[i] = (is_signed ? get_sbits(gb, coeff_bits) : (int)get_bits(gb, coeff_bits)) + offset;
}

/* Filter coefficient sets (2.7, is_signed) or probability tables (2.8) */
static int read_table(BitReader *gb, Table *t, const int8_t code_pred_coeff[3][3],
                      int length_bits, int coeff_bits, int is_signed, int offset)
{
    const int lo = is_signed ? -(1 << (coeff_bits - 1)) : offset;
    const int hi = is_signed ? (1 << (coeff_bits - 1)) - 1 : offset + (1 << coeff_bits) - 1;

    for (unsigned i = 0; i < t->elements; i++) {
        t->length[i] = get_bits(gb, length_bits) + 1;
        if (!is_signed && t->length[i] == 1) {
            t->coeff[i][0] = 128;       /* one-entry ptable: nothing more is sent */
            continue;
        }
        if (!get_bits1(gb)) {
            read_uncoded_coeff(gb, t->coeff[i], t->length[i], coeff_bits, is_signed, offset);
        } else {
            unsigned method = get_bits(gb, 2);
            if (method == 3 || method + 1 >= t->length[i])
                return -1;

            read_uncoded_coeff(gb, t->coeff[i], method + 1, coeff_bits, is_signed, offset);

            unsigned lsb_size = get_bits(gb, 3);
            for (unsigned j = method + 1; j < t->length[i]; j++) {
                int c, x = 0;
                for (unsigned k = 0; k < method + 1; k++)
                    x += code_pred_coeff[method][k] * t->coeff[i][j - k - 1];
                if (get_rice(gb, lsb_size, &c) < 0)
                    return -1;
                c -= (x + 4) >> 3;
                if (c < lo || c > hi)
                    return -1;
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
    dst_decoder_t *s = sc_aligned_alloc(64, sizeof(dst_decoder_t));
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
    sc_aligned_free(s);
}

/* Code bits for the arithmetic decoder; bits past the end of the frame read as 0 */
static inline unsigned peek_bits(const uint8_t *buf, size_t nbytes, size_t index, int n)
{
    size_t byte = index >> 3;
    uint32_t v;
    if (__builtin_expect(byte + 4 <= nbytes, 1)) {
        memcpy(&v, buf + byte, 4);
        v = __builtin_bswap32(v);
    } else {
        v = 0;
        for (int i = 0; i < 4; i++)
            v = v << 8 | (byte + i < nbytes ? buf[byte + i] : 0);
    }
    return (v << (index & 7)) >> (32 - n);
}

/* Decode bits [from, to) of every channel (both multiples of 8); the filter
   and ptable of each channel are fixed within the range. */
static void decode_range(dst_decoder_t *s, uint8_t *dsd, unsigned from, unsigned to,
                         const unsigned *felem_of, const unsigned *pelem_of,
                         const unsigned *half_len)
{
    const int channels = s->channels;
    const uint8_t *buf = s->gb.buf;
    const size_t nbytes = s->gb.size_bits >> 3;
    size_t index = s->gb.index;
    unsigned a = s->ac.a, c = s->ac.c;
    int16_t (*filters[DST_MAX_CHANNELS])[256];
    const int *probs[DST_MAX_CHANNELS];
    unsigned last[DST_MAX_CHANNELS], half[DST_MAX_CHANNELS];

    for (int ch = 0; ch < channels; ch++) {
        filters[ch] = s->filter[felem_of[ch]];
        probs[ch] = s->probs.coeff[pelem_of[ch]];
        last[ch] = s->probs.length[pelem_of[ch]] - 1;
        half[ch] = half_len[ch];
    }

    for (unsigned byte = from >> 3; byte < to >> 3; byte++) {
        for (unsigned i = byte * 8; i < byte * 8 + 8; i++) {
            for (int ch = 0; ch < channels; ch++) {
                int16_t (*filter)[256] = filters[ch];
                uint8_t *status = s->status[ch];
                int prob;

#define F(x) filter[(x)][status[(x)]]
                const int16_t predict = F( 0) + F( 1) + F( 2) + F( 3) +
                                        F( 4) + F( 5) + F( 6) + F( 7) +
                                        F( 8) + F( 9) + F(10) + F(11) +
                                        F(12) + F(13) + F(14) + F(15);
#undef F

                if (i >= half[ch]) {
                    unsigned j = (unsigned)abs(predict) >> 3;
                    prob = probs[ch][j < last[ch] ? j : last[ch]];
                } else {
                    prob = 128;
                }

                /* ac_get() with the coder state in registers */
                unsigned k = (a >> 8) | ((a >> 7) & 1);
                unsigned q = k * prob;
                unsigned a_q = a - q;
                int residual = c < a_q;
                if (residual) {
                    a = a_q;
                } else {
                    a = q;
                    c -= a_q;
                }
                if (a < 2048) {
                    int n = 11 - av_log2(a);
                    a <<= n;
                    c = (c << n) | peek_bits(buf, nbytes, index, n);
                    index += n;
                }

                uint64_t v = ((predict >> 15) ^ residual) & 1;

                /* 128-bit shift register, newest bit in status[0] bit 0 */
                uint64_t lo, hi;
                memcpy(&lo, status, 8);
                memcpy(&hi, status + 8, 8);
                hi = (hi << 1) | (lo >> 63);
                lo = (lo << 1) | v;
                memcpy(status, &lo, 8);
                memcpy(status + 8, &hi, 8);
            }
        }
        /* the last 8 output bits, earliest in the MSB */
        for (int ch = 0; ch < channels; ch++)
            dsd[(size_t)byte * channels + ch] = s->status[ch][0];
    }
    s->ac.a = a;
    s->ac.c = c;
    s->gb.index = index;
}

int dst_decode_frame(dst_decoder_t *s, const uint8_t *data, int size, uint8_t *dsd)
{
    unsigned half_len[DST_MAX_CHANNELS];
    const int channels = s->channels;
    const size_t out_bytes = (size_t)SAMPLES_PER_FRAME / 8 * channels;
    BitReader *gb = &s->gb;
    ArithCoder *ac = &s->ac;
    Segments *fseg = &s->fseg, *pseg = &s->pseg;
    int ntab;

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

    /* Segmentation (2.3, 2.4) */
    int same_segm = get_bits1(gb);
    if (read_segmentation(gb, fseg, channels, FILTER_LENGTHS_SHARED, FILTER_LENGTHS, FILTER_LMIN) < 0)
        return DST_ERR_INVALID;
    if (same_segm) {
        memcpy(pseg->nseg, fseg->nseg, sizeof(fseg->nseg));
        memcpy(pseg->start, fseg->start, sizeof(fseg->start));
    } else if (read_segmentation(gb, pseg, channels, PTABLE_LENGTHS, PTABLE_LENGTHS, PTABLE_LMIN) < 0) {
        return DST_ERR_INVALID;
    }

    /* Mapping (2.5) */
    unsigned same_map = get_bits1(gb);
    if ((ntab = read_mapping(gb, fseg, channels)) < 0)
        return DST_ERR_INVALID;
    s->fsets.elements = ntab;
    if (same_map) {
        for (int ch = 0; ch < channels; ch++)
            if (pseg->nseg[ch] != fseg->nseg[ch])
                return DST_ERR_INVALID;
        memcpy(pseg->map, fseg->map, sizeof(fseg->map));
        s->probs.elements = ntab;
    } else {
        if ((ntab = read_mapping(gb, pseg, channels)) < 0)
            return DST_ERR_INVALID;
        s->probs.elements = ntab;
    }

    /* Half Probability (2.6) */
    for (int ch = 0; ch < channels; ch++)
        half_len[ch] = get_bits1(gb);

    /* Filter Coef Sets (2.7) */
    if (read_table(gb, &s->fsets, fsets_code_pred_coeff, 7, 9, 1, 0) < 0)
        return DST_ERR_INVALID;

    /* Probability Tables (2.8) */
    if (read_table(gb, &s->probs, probs_code_pred_coeff, 6, 7, 0, 1) < 0)
        return DST_ERR_INVALID;

    if (gb->index > gb->size_bits)
        return DST_ERR_INVALID;         /* header ran past the end of the frame */

    /* Half-probability length: order of the channel's first filter (3.6) */
    for (int ch = 0; ch < channels; ch++)
        half_len[ch] = half_len[ch] ? s->fsets.length[fseg->map[ch][0]] : 0;

    /* Arithmetic Coded Data (2.9); bits past the end of the frame read as 0 */
    if (get_bits1(gb))
        return DST_ERR_INVALID;
    ac_init(ac, gb);

    if (build_filter(s->filter, &s->fsets) < 0)
        return DST_ERR_INVALID;

    memset(s->status, 0xAA, sizeof(s->status));

    /* DST_X_Bit, unused */
    ac_get(ac, gb, (s->reverse[s->fsets.coeff[0][0] & 127] >> 1) + 1);

    unsigned felem_of[DST_MAX_CHANNELS], pelem_of[DST_MAX_CHANNELS];
    int fi[DST_MAX_CHANNELS] = {0}, pi[DST_MAX_CHANNELS] = {0};
    for (int ch = 0; ch < channels; ch++) {
        felem_of[ch] = fseg->map[ch][0];
        pelem_of[ch] = pseg->map[ch][0];
    }
    /* Split the frame at every segment boundary of any channel */
    for (unsigned from = 0; from < SAMPLES_PER_FRAME;) {
        unsigned to = SAMPLES_PER_FRAME;
        for (int ch = 0; ch < channels; ch++) {
            if (fseg->start[ch][fi[ch] + 1] < to)
                to = fseg->start[ch][fi[ch] + 1];
            if (pseg->start[ch][pi[ch] + 1] < to)
                to = pseg->start[ch][pi[ch] + 1];
        }
        decode_range(s, dsd, from, to, felem_of, pelem_of, half_len);
        from = to;
        for (int ch = 0; ch < channels && from < SAMPLES_PER_FRAME; ch++) {
            if (fseg->start[ch][fi[ch] + 1] == from)
                felem_of[ch] = fseg->map[ch][++fi[ch]];
            if (pseg->start[ch][pi[ch] + 1] == from)
                pelem_of[ch] = pseg->map[ch][++pi[ch]];
        }
    }

    /* End-of-frame check (3.7): at most 7 code bits may remain unread */
    if (gb->index + 7 < gb->size_bits)
        return DST_ERR_INVALID;
    return 0;
}
