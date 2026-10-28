/*
 * sacdconv - multithreaded SACD audio extraction engine for pysacd.
 *
 * One call converts any number of outputs (tracks, concatenations, an edit
 * master) from one area of an SACD image in a single sequential pass:
 *
 *   reader thread   reads the area once (large sequential reads), demuxes
 *                   audio frames, fills gaps with silence and groups the
 *                   frame stream into batches of BATCH_FRAMES
 *   N workers       decode DST and convert DSD->PCM per batch; each batch
 *                   carries PRE/POST context frames so the PCM filters see
 *                   exactly what a continuous run would (gapless, and the
 *                   result does not depend on the thread count)
 *   writer thread   takes batches in order and writes every output that
 *                   overlaps them (DSF, DSDIFF DSD/DST, WAV, FLAC)
 *
 * The reader can also copy the whole image to a plain 2048-byte ISO in the
 * same pass. Called from Python through ctypes, which releases the GIL.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifdef HAVE_FLAC
#include <FLAC/metadata.h>
#include <FLAC/stream_encoder.h>
#endif

#include "demux.h"
#include "dsp.h"
#include "dstdec.h"
#include "sacd.h"

#define BATCH_FRAMES   32
#define PRE_FRAMES     2               /* PCM filter warm-up context */
#define POST_FRAMES    1               /* covers the filter group delay (< 1 frame) */
#define SLOT_FRAMES    (PRE_FRAMES + BATCH_FRAMES + POST_FRAMES)
#define READ_SECTORS   1024            /* 2 MiB reads */
#define DSF_BLOCK      4096
#define OUT_BUFFER     (1 << 20)
#define DST_ERROR_FILL 0x55            /* undecodable DST frame: what sacd_extract writes */

enum { OUT_DSF = 0, OUT_FLAC = 1, OUT_WAV = 2, OUT_DFF_DSD = 3, OUT_DFF_DST = 4 };

/* Must match pysacd/_native.py */
typedef struct {
    int             format;
    const char     *path;
    uint32_t        tc_start, tc_end;   /* frames [tc_start, tc_end) */
    int32_t         head_frame;         /* DSF nopad: file starts with the last */
    uint32_t        head_bytes;         /*   head_bytes/channel of this frame (-1: none) */
    uint32_t        tail_bytes;         /* DSF nopad: drop this many bytes/channel at the end */
    const uint8_t  *header;             /* DSDIFF: written at offset 0 */
    uint32_t        header_len;
    const uint8_t  *footer;             /* DSF: ID3 tag; DSDIFF: chunks after the data */
    uint32_t        footer_len;
    uint64_t       *dst_offsets;        /* DSDIFF DST: frame index (absolute offsets) */
    uint32_t       *dst_lengths;
    uint32_t        dst_cap;
    int             flac_level;
    uint32_t        flac_padding;       /* PADDING block so tags can be added in place */
    /* results */
    uint32_t        frames_written, frames_missing, dst_errors;
    uint32_t        dst_count;
    uint64_t        samples;            /* per channel, at the output rate */
    uint64_t        data_bytes;         /* size of the audio data region */
    char            error[256];
} sc_output_t;

typedef struct {
    const char     *iso_path;
    int             sector_size, data_offset;
    uint32_t        scan_lsn_start, scan_lsn_end;   /* audio sectors to demux */
    int             channels;
    int             pcm_rate;
    double          gain_db;
    int             threads;
    const char     *iso_out_path;       /* also copy the image here (NULL: no) */
    uint32_t        iso_sectors;
    volatile int    cancel;
    /* progress (read by Python while running) */
    volatile uint64_t frames_done, frames_total;
    volatile uint64_t sectors_done, sectors_total;
    char            error[256];
} sc_area_t;

/* ------------------------------------------------------------ batches */

typedef struct batch {
    uint64_t  seq;
    uint32_t  tc0;              /* timecode of the first core frame */
    int       pre, n, post;     /* context before, core, context after */
    int       need_dsd, need_pcm, need_planar;
    uint8_t  *raw;              /* raw frame data, concatenated */
    size_t    raw_cap;
    uint32_t  off[SLOT_FRAMES], len[SLOT_FRAMES];
    uint8_t   dst[SLOT_FRAMES], missing[SLOT_FRAMES], bad[SLOT_FRAMES];
    uint8_t  *dsd;              /* SLOT_FRAMES * channels * 4704 */
    uint8_t  *planar;           /* DSF layout: per frame, per channel 4704 bit-reversed bytes */
    int32_t  *pcm;              /* BATCH_FRAMES * out_per_frame * channels */
    struct batch *next;
} batch_t;

typedef struct {
    sc_area_t      *a;
    sc_output_t    *outs;
    int             nouts;
    int             channels, frame_bytes, opf, delay;
    uint32_t        need_lo, need_hi;           /* frame range any output needs */

    pthread_mutex_t mu;
    pthread_cond_t  cv_free, cv_work, cv_done;
    batch_t        *pool;
    int             npool;
    batch_t        *free_list, *work_head, *work_tail;
    batch_t       **done;                       /* ring indexed by seq % npool */
    int             reader_done;
    uint64_t        total_batches;
    _Atomic int     failed;
} engine_t;

static void set_error(engine_t *e, const char *fmt, ...)
{
    pthread_mutex_lock(&e->mu);
    if (!e->failed) {
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(e->a->error, sizeof(e->a->error), fmt, ap);
        va_end(ap);
        e->failed = 1;
    }
    pthread_cond_broadcast(&e->cv_free);
    pthread_cond_broadcast(&e->cv_work);
    pthread_cond_broadcast(&e->cv_done);
    pthread_mutex_unlock(&e->mu);
}

static int stopping(engine_t *e)
{
    return e->failed || e->a->cancel;
}

/* ------------------------------------------------------------ reader */

typedef struct {
    uint32_t tc;
    uint32_t len;
    uint8_t  dst, missing;
    uint8_t  data[MAX_FRAME_SIZE];
} frame_t;

typedef struct {
    engine_t *e;
    frame_t  *stage;            /* frames waiting to be batched (SLOT_FRAMES) */
    int       nstage;           /* frames staged */
    int       pre;              /* how many leading staged frames are context */
    uint32_t  next_tc;          /* next timecode expected in the stream */
    uint32_t  want_lo, want_hi; /* frame range to deliver (incl. context) */
    uint64_t  seq;
} reader_t;

static int batch_overlaps(engine_t *e, uint32_t lo, uint32_t hi, int *need_dsd, int *need_pcm, int *need_planar)
{
    *need_dsd = *need_pcm = *need_planar = 0;
    for (int i = 0; i < e->nouts; i++) {
        sc_output_t *o = &e->outs[i];
        int hit = o->tc_start < hi && o->tc_end > lo;
        if (o->head_frame >= 0 && (uint32_t)o->head_frame >= lo && (uint32_t)o->head_frame < hi)
            hit = 1;
        if (!hit)
            continue;
        if (o->format != OUT_DFF_DST)
            *need_dsd = 1;
        if (o->format == OUT_FLAC || o->format == OUT_WAV)
            *need_pcm = 1;
        if (o->format == OUT_DSF)
            *need_planar = 1;
    }
    return *need_dsd || *need_pcm;
}

/* Hand the staged frames (pre context + core [+ post context]) to the workers */
static int emit_batch(reader_t *r, int ncore, int npost)
{
    engine_t *e = r->e;
    batch_t *b;

    pthread_mutex_lock(&e->mu);
    while (!e->free_list && !stopping(e))
        pthread_cond_wait(&e->cv_free, &e->mu);
    if (stopping(e)) {
        pthread_mutex_unlock(&e->mu);
        return -1;
    }
    b = e->free_list;
    e->free_list = b->next;
    pthread_mutex_unlock(&e->mu);

    int nf = r->pre + ncore + npost;
    size_t need = 0;
    for (int i = 0; i < nf; i++)
        need += r->stage[i].len;
    if (need > b->raw_cap) {
        uint8_t *p = realloc(b->raw, need);
        if (!p) {
            set_error(e, "out of memory");
            return -1;
        }
        b->raw = p;
        b->raw_cap = need;
    }
    size_t pos = 0;
    for (int i = 0; i < nf; i++) {
        frame_t *f = &r->stage[i];
        memcpy(b->raw + pos, f->data, f->len);
        b->off[i] = (uint32_t)pos;
        b->len[i] = f->len;
        b->dst[i] = f->dst;
        b->missing[i] = f->missing;
        b->bad[i] = 0;
        pos += f->len;
    }
    b->seq = r->seq++;
    b->tc0 = r->stage[r->pre].tc;
    b->pre = r->pre;
    b->n = ncore;
    b->post = npost;
    batch_overlaps(e, b->tc0, b->tc0 + ncore, &b->need_dsd, &b->need_pcm, &b->need_planar);
    if (b->need_pcm)
        b->need_dsd = 1;
    b->next = NULL;

    pthread_mutex_lock(&e->mu);
    if (e->work_tail)
        e->work_tail->next = b;
    else
        e->work_head = b;
    e->work_tail = b;
    pthread_cond_signal(&e->cv_work);
    pthread_mutex_unlock(&e->mu);

    /* keep the last PRE core frames (+ the post frames) for the next batch */
    int keep_from = r->pre + ncore - PRE_FRAMES;
    if (keep_from < 0)
        keep_from = 0;
    int keep = nf - keep_from;
    for (int i = 0; i < keep; i++)
        if (i != keep_from + i)
            r->stage[i] = r->stage[keep_from + i];
    r->nstage = keep;
    r->pre = keep - npost;
    return 0;
}

static int stage_frame(reader_t *r, uint32_t tc, const uint8_t *data, int len, int dst, int missing)
{
    frame_t *f = &r->stage[r->nstage++];
    f->tc = tc;
    f->len = missing ? 0 : (uint32_t)len;
    f->dst = (uint8_t)dst;
    f->missing = (uint8_t)missing;
    if (!missing)
        memcpy(f->data, data, len);
    /* full batch plus its post context staged: emit */
    if (r->nstage == r->pre + BATCH_FRAMES + POST_FRAMES)
        return emit_batch(r, BATCH_FRAMES, POST_FRAMES);
    return 0;
}

static int fill_until(reader_t *r, uint32_t tc)
{
    for (; r->next_tc < tc; r->next_tc++)
        if (stage_frame(r, r->next_tc, NULL, 0, 0, 1) < 0)
            return -1;
    return 0;
}

static int on_frame(void *ctx, uint32_t tc, const uint8_t *data, int size, int dst)
{
    reader_t *r = ctx;
    engine_t *e = r->e;

    if (tc >= r->want_hi)
        return 1;
    if (tc < r->next_tc)
        return 0;
    if (!dst && size != e->frame_bytes) {
        set_error(e, "DSD frame of %d bytes does not match %d channels", size, e->channels);
        return -1;
    }
    if (fill_until(r, tc) < 0 || stage_frame(r, tc, data, size, dst, 0) < 0)
        return -1;
    r->next_tc = tc + 1;
    return stopping(e) ? -1 : 0;
}

static void *reader_main(void *arg)
{
    engine_t *e = arg;
    sc_area_t *a = e->a;
    int fd = -1, rc = 0;
    FILE *iso = NULL;
    uint8_t *raw = NULL, *plain = NULL;
    frame_acc_t *acc = NULL;
    reader_t r = {.e = e};

    r.stage = malloc(sizeof(frame_t) * SLOT_FRAMES);
    raw = malloc((size_t)READ_SECTORS * a->sector_size);
    plain = malloc((size_t)READ_SECTORS * LSN_SIZE);
    acc = calloc(1, sizeof(*acc));
    if (!r.stage || !raw || !plain || !acc) {
        set_error(e, "out of memory");
        goto out;
    }
    if ((fd = open(a->iso_path, O_RDONLY | O_CLOEXEC)) < 0) {
        set_error(e, "%s: %s", a->iso_path, strerror(errno));
        goto out;
    }
    posix_fadvise(fd, 0, 0, POSIX_FADV_SEQUENTIAL);

    if (a->iso_out_path) {
        if (!(iso = fopen(a->iso_out_path, "wb"))) {
            set_error(e, "%s: %s", a->iso_out_path, strerror(errno));
            goto out;
        }
        setvbuf(iso, NULL, _IOFBF, 4 * OUT_BUFFER);
    }

    r.want_lo = e->need_lo > PRE_FRAMES ? e->need_lo - PRE_FRAMES : 0;
    r.want_hi = e->need_hi + POST_FRAMES;
    r.next_tc = r.want_lo;
    int demuxing = e->nouts > 0;

    uint32_t first = iso ? 0 : a->scan_lsn_start;
    uint32_t last = iso ? a->iso_sectors - 1 : a->scan_lsn_end;
    a->sectors_total = (uint64_t)last - first + 1;

    for (uint32_t lsn = first; lsn <= last && !stopping(e); lsn += READ_SECTORS) {
        uint32_t n = last - lsn + 1 < READ_SECTORS ? last - lsn + 1 : READ_SECTORS;
        size_t want = (size_t)n * a->sector_size, got = 0;
        while (got < want) {
            ssize_t k = pread(fd, raw + got, want - got, (off_t)lsn * a->sector_size + got);
            if (k <= 0) {
                set_error(e, "read error at LSN %u: %s", lsn, k < 0 ? strerror(errno) : "unexpected end of image");
                goto out;
            }
            got += k;
        }
        const uint8_t *sec = raw;
        if (a->sector_size != LSN_SIZE) {
            for (uint32_t i = 0; i < n; i++)
                memcpy(plain + (size_t)i * LSN_SIZE, raw + (size_t)i * a->sector_size + a->data_offset, LSN_SIZE);
            sec = plain;
        }
        if (iso && fwrite(sec, LSN_SIZE, n, iso) != n) {
            set_error(e, "%s: %s", a->iso_out_path, strerror(errno));
            goto out;
        }
        if (demuxing) {
            for (uint32_t i = 0; i < n; i++) {
                uint32_t s = lsn + i;
                if (s < a->scan_lsn_start || s > a->scan_lsn_end)
                    continue;
                rc = demux_sector(sec + (size_t)i * LSN_SIZE, acc, e->channels, on_frame, &r);
                if (rc < 0)
                    goto out;
                if (rc > 0) {
                    demuxing = 0;   /* past the last frame needed */
                    break;
                }
            }
            if (!demuxing && !iso)
                break;
        }
        a->sectors_done += n;
    }
    if (stopping(e))
        goto out;

    if (e->nouts) {
        if (demuxing && acc->started && frame_complete(acc, e->channels) &&
            on_frame(&r, acc->tc, acc->data, acc->size, acc->dst) < 0)
            goto out;
        /* frames never found on disc (or past the area end) become silence */
        if (fill_until(&r, r.want_hi) < 0)
            goto out;
        /* flush the remainder: everything staged after the context is core */
        int ncore = r.nstage - r.pre;
        if (ncore > 0 && emit_batch(&r, ncore, 0) < 0)
            goto out;
    }

out:
    if (iso && fclose(iso) != 0)
        set_error(e, "%s: %s", a->iso_out_path, strerror(errno));
    if (fd >= 0)
        close(fd);
    pthread_mutex_lock(&e->mu);
    e->reader_done = 1;
    e->total_batches = r.seq;
    pthread_cond_broadcast(&e->cv_work);
    pthread_cond_broadcast(&e->cv_done);
    pthread_mutex_unlock(&e->mu);
    free(r.stage);
    free(raw);
    free(plain);
    free(acc);
    return NULL;
}

/* ------------------------------------------------------------ workers */

static inline uint32_t xorshift(uint32_t *s)
{
    uint32_t x = *s;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    return *s = x;
}

static const uint8_t *bit_reverse_table(void);

static void process_batch(engine_t *e, batch_t *b, dst_decoder_t **dec, dsd2pcm_t *dsp, const uint8_t *silence)
{
    int nf = b->pre + b->n + b->post;
    size_t fb = e->frame_bytes;

    if (b->need_dsd) {
        for (int i = 0; i < nf; i++) {
            uint8_t *out = b->dsd + i * fb;
            if (b->missing[i]) {
                memset(out, DSD_SILENCE, fb);
            } else if (b->dst[i]) {
                if (!*dec && !(*dec = dst_decoder_new(e->channels))) {
                    set_error(e, "out of memory");
                    return;
                }
                if (dst_decode_frame(*dec, b->raw + b->off[i], b->len[i], out) != 0) {
                    b->bad[i] = 1;
                    memset(out, DST_ERROR_FILL, fb);
                }
            } else {
                memcpy(out, b->raw + b->off[i], fb);
            }
        }
    }

    if (b->need_planar) {
        /* DSF byte order: de-interleave and bit-reverse here, in parallel */
        const uint8_t *rev = bit_reverse_table();
        int ch = e->channels;
        for (int i = b->pre; i < b->pre + b->n; i++) {
            const uint8_t *src = b->dsd + i * fb;
            uint8_t *dst = b->planar + i * fb;
            for (int c = 0; c < ch; c++) {
                uint8_t *d = dst + (size_t)c * FRAME_BYTES_PER_CH;
                for (int k = 0; k < FRAME_BYTES_PER_CH; k++)
                    d[k] = rev[src[(size_t)k * ch + c]];
            }
        }
    }

    if (b->need_pcm) {
        int ch = e->channels, opf = e->opf;
        uint32_t skip = (uint32_t)b->pre * opf + e->delay;
        uint32_t keep = (uint32_t)b->n * opf, got = 0, pos = 0;
        uint32_t rng = 0x9E3779B9u ^ (b->tc0 * 2654435761u);
        const float scale = 8388607.0f * (float)pow(10.0, e->a->gain_db / 20.0);
        const float lsb = 1.0f / 4294967296.0f;

        dsd2pcm_reset(dsp);
        for (int i = 0; got < keep; i++) {
            /* past the staged frames (end of stream): flush with silence */
            dsd2pcm_frame(dsp, i < nf ? b->dsd + i * fb : silence);
            for (int s = 0; s < opf && got < keep; s++, pos++) {
                if (pos < skip)
                    continue;
                int32_t *dst = b->pcm + (size_t)got * ch;
                for (int c = 0; c < ch; c++) {
                    float dith = ((float)xorshift(&rng) - (float)xorshift(&rng)) * lsb;
                    long v = lrintf(dsp->out[c][s] * scale + dith);
                    dst[c] = (int32_t)(v > 8388607 ? 8388607 : v < -8388608 ? -8388608 : v);
                }
                got++;
            }
        }
    }
}

static void *worker_main(void *arg)
{
    engine_t *e = arg;
    dst_decoder_t *dec = NULL;
    dsd2pcm_t *dsp = NULL;
    uint8_t *silence = malloc(e->frame_bytes);

    if (!silence) {
        set_error(e, "out of memory");
        return NULL;
    }
    memset(silence, DSD_SILENCE, e->frame_bytes);
    if (e->a->pcm_rate) {
        dsp = malloc(sizeof(*dsp));
        if (!dsp || dsd2pcm_init(dsp, e->channels, e->a->pcm_rate) < 0) {
            set_error(e, "out of memory");
            free(dsp);
            free(silence);
            return NULL;
        }
    }

    for (;;) {
        pthread_mutex_lock(&e->mu);
        while (!e->work_head && !e->reader_done && !stopping(e))
            pthread_cond_wait(&e->cv_work, &e->mu);
        batch_t *b = e->work_head;
        if (!b || stopping(e)) {
            pthread_mutex_unlock(&e->mu);
            break;
        }
        e->work_head = b->next;
        if (!e->work_head)
            e->work_tail = NULL;
        pthread_mutex_unlock(&e->mu);

        process_batch(e, b, &dec, dsp, silence);

        pthread_mutex_lock(&e->mu);
        e->done[b->seq % e->npool] = b;
        pthread_cond_broadcast(&e->cv_done);
        pthread_mutex_unlock(&e->mu);
    }

    dst_decoder_free(dec);
    if (dsp) {
        dsd2pcm_free(dsp);
        free(dsp);
    }
    free(silence);
    return NULL;
}

/* ------------------------------------------------------------ writers */

static const uint8_t *bit_reverse_table(void)
{
    static uint8_t t[256];
    static atomic_int ready;
    if (!atomic_load(&ready)) {
        for (int i = 0; i < 256; i++) {
            uint8_t v = 0;
            for (int b = 0; b < 8; b++)
                if (i & (1 << b))
                    v |= 0x80 >> b;
            t[i] = v;
        }
        atomic_store(&ready, 1);
    }
    return t;
}

static void put_le(uint8_t *p, uint64_t v, int n)
{
    for (int i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * i));
}

typedef struct {
    FILE     *fp;
    int       opened, closed;
    /* DSF */
    uint8_t   blk[MAX_CHANNELS_SACD][DSF_BLOCK];
    int       fill;
    uint64_t  dsf_bytes;        /* real bytes per channel */
    uint64_t  limit;            /* bytes per channel to write (nopad tail) */
    /* WAV */
    uint8_t  *wbuf;
#ifdef HAVE_FLAC
    FLAC__StreamEncoder *enc;
    FLAC__StreamMetadata *pad;
#endif
} out_state_t;

static int out_fail(sc_output_t *o, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(o->error, sizeof(o->error), fmt, ap);
    va_end(ap);
    return -1;
}

static int write_dsf_header(FILE *fp, sc_output_t *o, int channels, uint64_t bytes_per_ch, uint64_t data_bytes)
{
    static const int channel_type[] = {0, 1, 2, 3, 5, 6, 7};
    uint8_t h[92] = {0};
    memcpy(h, "DSD ", 4);
    put_le(h + 4, 28, 8);
    put_le(h + 12, 92 + data_bytes + o->footer_len, 8);
    put_le(h + 20, o->footer_len ? 92 + data_bytes : 0, 8);
    memcpy(h + 28, "fmt ", 4);
    put_le(h + 32, 52, 8);
    put_le(h + 40, 1, 4);
    put_le(h + 44, 0, 4);
    put_le(h + 48, channel_type[channels], 4);
    put_le(h + 52, channels, 4);
    put_le(h + 56, 2822400, 4);
    put_le(h + 60, 1, 4);
    put_le(h + 64, bytes_per_ch * 8, 8);
    put_le(h + 72, DSF_BLOCK, 4);
    memcpy(h + 80, "data", 4);
    put_le(h + 84, 12 + data_bytes, 8);
    return fseeko(fp, 0, SEEK_SET) == 0 && fwrite(h, sizeof(h), 1, fp) == 1 ? 0 : -1;
}

static int write_wav_header(FILE *fp, int channels, int rate, uint64_t data_bytes)
{
    static const uint32_t masks[] = {0, 0x4, 0x3, 0x7, 0x33, 0x37, 0x3F};
    static const uint8_t guid[16] = {1, 0, 0, 0, 0, 0, 0x10, 0, 0x80, 0, 0, 0xAA, 0, 0x38, 0x9B, 0x71};
    uint8_t h[68] = {0};
    if (data_bytes > 0xFFFFFFFFull - 60)
        return -1;
    memcpy(h, "RIFF", 4);
    put_le(h + 4, 60 + data_bytes, 4);
    memcpy(h + 8, "WAVEfmt ", 8);
    put_le(h + 16, 40, 4);
    put_le(h + 20, 0xFFFE, 2);
    put_le(h + 22, channels, 2);
    put_le(h + 24, rate, 4);
    put_le(h + 28, (uint64_t)rate * channels * 3, 4);
    put_le(h + 32, channels * 3, 2);
    put_le(h + 34, 24, 2);
    put_le(h + 36, 22, 2);
    put_le(h + 38, 24, 2);
    put_le(h + 40, masks[channels], 4);
    memcpy(h + 44, guid, 16);
    memcpy(h + 60, "data", 4);
    put_le(h + 64, data_bytes, 4);
    return fseeko(fp, 0, SEEK_SET) == 0 && fwrite(h, sizeof(h), 1, fp) == 1 ? 0 : -1;
}

static int out_open(engine_t *e, sc_output_t *o, out_state_t *st)
{
    st->opened = 1;
    if (o->format == OUT_FLAC) {
#ifdef HAVE_FLAC
        FLAC__StreamEncoder *enc = st->enc = FLAC__stream_encoder_new();
        if (!enc)
            return out_fail(o, "out of memory");
        FLAC__stream_encoder_set_channels(enc, e->channels);
        FLAC__stream_encoder_set_bits_per_sample(enc, 24);
        FLAC__stream_encoder_set_sample_rate(enc, e->a->pcm_rate);
        FLAC__stream_encoder_set_compression_level(enc, o->flac_level);
        FLAC__stream_encoder_set_total_samples_estimate(enc, (uint64_t)(o->tc_end - o->tc_start) * e->opf);
#ifdef HAVE_FLAC_MT
        FLAC__stream_encoder_set_num_threads(enc, 4);
#endif
        if (o->flac_padding) {
            st->pad = FLAC__metadata_object_new(FLAC__METADATA_TYPE_PADDING);
            if (!st->pad)
                return out_fail(o, "out of memory");
            st->pad->length = o->flac_padding;
            FLAC__stream_encoder_set_metadata(enc, &st->pad, 1);
        }
        FLAC__StreamEncoderInitStatus s = FLAC__stream_encoder_init_file(enc, o->path, NULL, NULL);
        if (s != FLAC__STREAM_ENCODER_INIT_STATUS_OK)
            return out_fail(o, "%s: FLAC init: %s", o->path, FLAC__StreamEncoderInitStatusString[s]);
        return 0;
#else
        return out_fail(o, "built without libFLAC");
#endif
    }

    if (!(st->fp = fopen(o->path, "wb")))
        return out_fail(o, "%s: %s", o->path, strerror(errno));
    setvbuf(st->fp, NULL, _IOFBF, OUT_BUFFER);
    switch (o->format) {
    case OUT_DSF:
        st->limit = (uint64_t)(o->tc_end - o->tc_start) * FRAME_BYTES_PER_CH +
                    (o->head_frame >= 0 ? o->head_bytes : 0) - o->tail_bytes;
        return write_dsf_header(st->fp, o, e->channels, 0, 0) < 0 ? out_fail(o, "%s: write failed", o->path) : 0;
    case OUT_WAV:
        st->wbuf = malloc((size_t)e->opf * e->channels * 3);
        if (!st->wbuf)
            return out_fail(o, "out of memory");
        return write_wav_header(st->fp, e->channels, e->a->pcm_rate, 0) < 0 ? out_fail(o, "%s: write failed", o->path) : 0;
    default:    /* DSDIFF */
        if (o->header_len && fwrite(o->header, o->header_len, 1, st->fp) != 1)
            return out_fail(o, "%s: write failed", o->path);
        return 0;
    }
}

static int dsf_flush(FILE *fp, out_state_t *st, sc_output_t *o, int ch)
{
    for (int c = 0; c < ch; c++)
        if (fwrite(st->blk[c], DSF_BLOCK, 1, fp) != 1)
            return out_fail(o, "%s: write failed: %s", o->path, strerror(errno));
    o->data_bytes += (uint64_t)DSF_BLOCK * ch;
    st->fill = 0;
    return 0;
}

/* Append bytes [from, to) of each channel of one frame (planar, DSF bit order) */
static int dsf_put(engine_t *e, sc_output_t *o, out_state_t *st, const uint8_t *frame, int from, int to)
{
    int ch = e->channels;
    uint64_t room = st->limit - st->dsf_bytes;
    if ((uint64_t)(to - from) > room)
        to = from + (int)room;
    for (int i = from; i < to;) {
        int n = to - i < DSF_BLOCK - st->fill ? to - i : DSF_BLOCK - st->fill;
        for (int c = 0; c < ch; c++)
            memcpy(st->blk[c] + st->fill, frame + (size_t)c * FRAME_BYTES_PER_CH + i, n);
        st->fill += n;
        i += n;
        st->dsf_bytes += n;
        if (st->fill == DSF_BLOCK && dsf_flush(st->fp, st, o, ch) < 0)
            return -1;
    }
    return 0;
}

static int pcm_put(engine_t *e, sc_output_t *o, out_state_t *st, const int32_t *pcm, int count)
{
    int ch = e->channels;
    if (o->format == OUT_WAV) {
        size_t nb = (size_t)count * ch * 3;
        for (int i = 0; i < count * ch; i++) {
            uint32_t v = (uint32_t)pcm[i];
            st->wbuf[i * 3] = (uint8_t)v;
            st->wbuf[i * 3 + 1] = (uint8_t)(v >> 8);
            st->wbuf[i * 3 + 2] = (uint8_t)(v >> 16);
        }
        if (fwrite(st->wbuf, nb, 1, st->fp) != 1)
            return out_fail(o, "%s: write failed: %s", o->path, strerror(errno));
        o->data_bytes += nb;
    }
#ifdef HAVE_FLAC
    else if (!FLAC__stream_encoder_process_interleaved(st->enc, pcm, count)) {
        return out_fail(o, "%s: FLAC: %s", o->path,
                        FLAC__StreamEncoderStateString[FLAC__stream_encoder_get_state(st->enc)]);
    }
#endif
    o->samples += count;
    return 0;
}

static int out_close(engine_t *e, sc_output_t *o, out_state_t *st)
{
    int ch = e->channels, rc = 0;
    st->closed = 1;
    switch (o->format) {
    case OUT_DSF:
        if (st->fill) {
            for (int c = 0; c < ch; c++)
                memset(st->blk[c] + st->fill, 0, DSF_BLOCK - st->fill);    /* as sacd_extract */
            rc = dsf_flush(st->fp, st, o, ch);
        }
        o->samples = st->dsf_bytes * 8;
        if (!rc && o->footer_len && fwrite(o->footer, o->footer_len, 1, st->fp) != 1)
            rc = out_fail(o, "%s: write failed", o->path);
        if (!rc && write_dsf_header(st->fp, o, ch, st->dsf_bytes, o->data_bytes) < 0)
            rc = out_fail(o, "%s: write failed", o->path);
        break;
    case OUT_WAV:
        if (write_wav_header(st->fp, ch, e->a->pcm_rate, o->data_bytes) < 0)
            rc = out_fail(o, "%s: WAV header failed (track too long for WAV?)", o->path);
        break;
    case OUT_FLAC:
#ifdef HAVE_FLAC
        if (!FLAC__stream_encoder_finish(st->enc))
            rc = out_fail(o, "%s: FLAC finish: %s", o->path,
                          FLAC__StreamEncoderStateString[FLAC__stream_encoder_get_state(st->enc)]);
        FLAC__stream_encoder_delete(st->enc);
        st->enc = NULL;
        if (st->pad)
            FLAC__metadata_object_delete(st->pad);
        st->pad = NULL;
#endif
        break;
    default:    /* DSDIFF: data is padded to even length; Python finishes DST files */
        if (o->data_bytes % 2) {
            if (fputc(0, st->fp) == EOF)
                rc = out_fail(o, "%s: write failed", o->path);
            o->data_bytes++;
        }
        if (!rc && o->footer_len && fwrite(o->footer, o->footer_len, 1, st->fp) != 1)
            rc = out_fail(o, "%s: write failed", o->path);
        break;
    }
    if (st->fp && fclose(st->fp) != 0 && !rc)
        rc = out_fail(o, "%s: close failed: %s", o->path, strerror(errno));
    st->fp = NULL;
    free(st->wbuf);
    st->wbuf = NULL;
    return rc;
}

/* Write the part of batch b that belongs to output o */
static int write_output(engine_t *e, sc_output_t *o, out_state_t *st, batch_t *b)
{
    uint32_t lo = b->tc0, hi = b->tc0 + b->n;
    size_t fb = e->frame_bytes;
    int head = o->head_frame >= 0 && (uint32_t)o->head_frame >= lo && (uint32_t)o->head_frame < hi;
    uint32_t s = o->tc_start > lo ? o->tc_start : lo;
    uint32_t t = o->tc_end < hi ? o->tc_end : hi;

    if (!head && s >= t)
        return 0;
    if (!st->opened && out_open(e, o, st) < 0)
        return -1;

    if (head) {   /* DSF nopad carry-in: tail of an earlier frame */
        const uint8_t *f = b->planar + (size_t)(b->pre + o->head_frame - lo) * fb;
        if (dsf_put(e, o, st, f, FRAME_BYTES_PER_CH - o->head_bytes, FRAME_BYTES_PER_CH) < 0)
            return -1;
    }

    for (uint32_t tc = s; tc < t; tc++) {
        int i = b->pre + (int)(tc - lo);
        if (b->missing[i])
            o->frames_missing++;
        if (b->bad[i])
            o->dst_errors++;

        switch (o->format) {
        case OUT_DSF:
            if (dsf_put(e, o, st, b->planar + i * fb, 0, FRAME_BYTES_PER_CH) < 0)
                return -1;
            break;
        case OUT_DFF_DSD:
            if (fwrite(b->dsd + i * fb, fb, 1, st->fp) != 1)
                return out_fail(o, "%s: write failed: %s", o->path, strerror(errno));
            o->data_bytes += fb;
            o->samples += FRAME_BYTES_PER_CH * 8;
            break;
        case OUT_DFF_DST: {
            if (b->missing[i])
                break;      /* nothing to pass through */
            uint32_t len = b->len[i];
            uint8_t hdr[12] = {'D', 'S', 'T', 'F'};
            for (int k = 0; k < 8; k++)
                hdr[4 + k] = (uint8_t)((uint64_t)len >> (56 - 8 * k));
            if (o->dst_count < o->dst_cap) {
                o->dst_offsets[o->dst_count] = o->header_len + o->data_bytes + 12;
                o->dst_lengths[o->dst_count] = len;
            }
            o->dst_count++;
            if (fwrite(hdr, 12, 1, st->fp) != 1 || fwrite(b->raw + b->off[i], len, 1, st->fp) != 1 ||
                ((len & 1) && fputc(0, st->fp) == EOF))
                return out_fail(o, "%s: write failed: %s", o->path, strerror(errno));
            o->data_bytes += 12 + len + (len & 1);
            o->samples += FRAME_BYTES_PER_CH * 8;
            break;
        }
        default:    /* PCM */
            if (pcm_put(e, o, st, b->pcm + (size_t)(tc - lo) * e->opf * e->channels, e->opf) < 0)
                return -1;
            break;
        }
        o->frames_written++;
    }

    if (t == o->tc_end && hi >= o->tc_end)
        return out_close(e, o, st);
    return 0;
}

static int writer_run(engine_t *e)
{
    out_state_t *st = calloc(e->nouts, sizeof(out_state_t));
    int rc = 0;
    if (!st) {
        set_error(e, "out of memory");
        return -1;
    }

    for (uint64_t seq = 0;; seq++) {
        pthread_mutex_lock(&e->mu);
        batch_t *b;
        while (!(b = e->done[seq % e->npool]) || b->seq != seq) {
            if (stopping(e) || (e->reader_done && seq >= e->total_batches))
                break;
            pthread_cond_wait(&e->cv_done, &e->mu);
        }
        if (!b || b->seq != seq) {
            pthread_mutex_unlock(&e->mu);
            break;
        }
        e->done[seq % e->npool] = NULL;
        pthread_mutex_unlock(&e->mu);

        for (int i = 0; i < e->nouts && !rc; i++) {
            if (st[i].closed)
                continue;
            if (write_output(e, &e->outs[i], &st[i], b) < 0) {
                set_error(e, "%s", e->outs[i].error);
                rc = -1;
            }
        }
        e->a->frames_done += b->n;

        pthread_mutex_lock(&e->mu);
        b->next = e->free_list;
        e->free_list = b;
        pthread_cond_signal(&e->cv_free);
        pthread_mutex_unlock(&e->mu);
        if (rc)
            break;
    }

    /* outputs left open (error/cancel) are closed; their files are incomplete */
    for (int i = 0; i < e->nouts; i++) {
        if (st[i].opened && !st[i].closed) {
            if (!stopping(e)) {
                set_error(e, "%s: stream ended before the output was complete", e->outs[i].path);
            }
            out_close(e, &e->outs[i], &st[i]);
        }
    }
    free(st);
    return stopping(e) ? -1 : 0;
}

/* ------------------------------------------------------------ API */

int sacdconv_has_flac(void)
{
#ifdef HAVE_FLAC
    return 1;
#else
    return 0;
#endif
}

int sacdconv_area_run(sc_area_t *a, sc_output_t *outs, int nouts)
{
    engine_t e = {.a = a, .outs = outs, .nouts = nouts};
    pthread_t reader, *workers = NULL;
    int nworkers = 0, rc = -1;

    a->error[0] = 0;
    a->frames_done = a->sectors_done = 0;
    if (a->channels < 1 || a->channels > MAX_CHANNELS_SACD) {
        snprintf(a->error, sizeof(a->error), "unsupported channel count %d", a->channels);
        return -1;
    }
    e.channels = a->channels;
    e.frame_bytes = a->channels * FRAME_BYTES_PER_CH;
    e.need_lo = UINT32_MAX;
    e.need_hi = 0;
    int want_pcm = 0;
    for (int i = 0; i < nouts; i++) {
        sc_output_t *o = &outs[i];
        o->frames_written = o->frames_missing = o->dst_errors = o->dst_count = 0;
        o->samples = o->data_bytes = 0;
        o->error[0] = 0;
        if (o->tc_end <= o->tc_start) {
            snprintf(a->error, sizeof(a->error), "%s: empty range", o->path);
            return -1;
        }
        uint32_t lo = o->head_frame >= 0 && (uint32_t)o->head_frame < o->tc_start ? (uint32_t)o->head_frame : o->tc_start;
        if (lo < e.need_lo)
            e.need_lo = lo;
        if (o->tc_end > e.need_hi)
            e.need_hi = o->tc_end;
        if (o->format == OUT_FLAC || o->format == OUT_WAV)
            want_pcm = 1;
        if (o->format == OUT_FLAC && !sacdconv_has_flac()) {
            snprintf(a->error, sizeof(a->error), "built without libFLAC (install flac-devel and rebuild)");
            return -1;
        }
    }
    if (want_pcm && a->pcm_rate != 88200 && a->pcm_rate != 176400) {
        snprintf(a->error, sizeof(a->error), "PCM rate must be 88200 or 176400");
        return -1;
    }
    if (!want_pcm)
        a->pcm_rate = 0;
    if (!nouts)
        e.need_lo = e.need_hi = 0;
    a->frames_total = e.need_hi - (nouts ? e.need_lo : 0);

    if (want_pcm) {
        dsd2pcm_t *probe = malloc(sizeof(*probe));
        if (!probe || dsd2pcm_init(probe, a->channels, a->pcm_rate) < 0) {
            free(probe);
            snprintf(a->error, sizeof(a->error), "out of memory");
            return -1;
        }
        e.opf = probe->out_per_frame;
        e.delay = probe->delay;
        dsd2pcm_free(probe);
        free(probe);
    }

    nworkers = a->threads > 0 ? a->threads : (int)sysconf(_SC_NPROCESSORS_ONLN);
    if (nworkers < 1)
        nworkers = 1;
    e.npool = nworkers * 2 + 4;
    pthread_mutex_init(&e.mu, NULL);
    pthread_cond_init(&e.cv_free, NULL);
    pthread_cond_init(&e.cv_work, NULL);
    pthread_cond_init(&e.cv_done, NULL);
    e.pool = calloc(e.npool, sizeof(batch_t));
    e.done = calloc(e.npool, sizeof(batch_t *));
    workers = calloc(nworkers, sizeof(pthread_t));
    if (!e.pool || !e.done || !workers)
        goto oom;
    for (int i = 0; i < e.npool; i++) {
        batch_t *b = &e.pool[i];
        b->dsd = malloc((size_t)SLOT_FRAMES * e.frame_bytes);
        b->planar = malloc((size_t)SLOT_FRAMES * e.frame_bytes);
        if (want_pcm)
            b->pcm = malloc(sizeof(int32_t) * BATCH_FRAMES * e.opf * a->channels);
        if (!b->dsd || !b->planar || (want_pcm && !b->pcm))
            goto oom;
        b->next = e.free_list;
        e.free_list = b;
    }

    int started = 0;
    for (; started < nworkers; started++)
        if (pthread_create(&workers[started], NULL, worker_main, &e) != 0)
            break;
    if (started == 0 || pthread_create(&reader, NULL, reader_main, &e) != 0) {
        set_error(&e, "cannot start threads");
    } else {
        writer_run(&e);
        pthread_join(reader, NULL);
    }
    pthread_mutex_lock(&e.mu);
    e.reader_done = 1;
    pthread_cond_broadcast(&e.cv_work);
    pthread_mutex_unlock(&e.mu);
    for (int i = 0; i < started; i++)
        pthread_join(workers[i], NULL);

    rc = e.failed ? -1 : a->cancel ? (snprintf(a->error, sizeof(a->error), "cancelled"), -1) : 0;
    goto cleanup;

oom:
    snprintf(a->error, sizeof(a->error), "out of memory");
cleanup:
    if (e.pool)
        for (int i = 0; i < e.npool; i++) {
            free(e.pool[i].raw);
            free(e.pool[i].dsd);
            free(e.pool[i].planar);
            free(e.pool[i].pcm);
        }
    free(e.pool);
    free(e.done);
    free(workers);
    pthread_mutex_destroy(&e.mu);
    pthread_cond_destroy(&e.cv_free);
    pthread_cond_destroy(&e.cv_work);
    pthread_cond_destroy(&e.cv_done);
    return rc;
}
