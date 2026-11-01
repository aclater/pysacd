/*
 * sacdconv - multithreaded SACD audio extraction engine for pysacd.
 *
 * One call converts any number of outputs (tracks, concatenations, an edit
 * master) from one area of an SACD image in a single sequential pass:
 *
 *   reader thread   reads the frame range the outputs need through
 *                   scarletbook-rs (#85): its pool of N threads decodes DST
 *                   ahead of the reader, which receives every position in
 *                   order (missing ones as 0x69 silence, invalid DST frames as
 *                   0x55) and groups them into batches of BATCH_FRAMES
 *   N workers       lay out DSF and convert DSD->PCM per batch; each batch
 *                   carries PRE/POST context frames so the PCM filters see
 *                   exactly what a continuous run would (gapless, and the
 *                   result does not depend on the thread count)
 *   writer thread   takes batches in order and writes every output that
 *                   overlaps them (DSF, DSDIFF DSD/DST, WAV, FLAC)
 *
 * An ISO copy (a plain 2048-byte image) is written first, in one sequential
 * pass, and the audio then read from that local copy. Called from Python
 * through ctypes, which releases the GIL.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#define _GNU_SOURCE
#include <errno.h>
#include <stddef.h>
#include <math.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifdef HAVE_FLAC
#include <FLAC/metadata.h>
#include <FLAC/stream_encoder.h>
#endif

#include "compat.h"
#include "dsp.h"
#include "sacd.h"
#include "scarletbook.h"

_Static_assert(MAX_FRAME_SIZE >= SB_MAX_FRAME_BYTES, "a stage slot holds any stored frame scarletbook returns");

/* The scarletbook-rs C library is linked in statically (#85; Makefile, scarletbook.lock), and
 * its sb_* functions are exported from this library for _native.py. This file is written
 * against C ABI 2: refuse a header of any other ABI at compile time; _native.py checks the
 * linked library's sb_abi_version() and sb_version() when it loads. */
#if !defined(SB_ABI_VERSION) || SB_ABI_VERSION != 2
#error "pysacd needs the scarletbook C library with SB_ABI_VERSION 2 (see scarletbook.lock)"
#endif

#define BATCH_FRAMES   32
#define PRE_FRAMES     2               /* PCM filter warm-up context */
#define POST_FRAMES    1               /* covers the filter group delay (< 1 frame) */
#define SLOT_FRAMES    (PRE_FRAMES + BATCH_FRAMES + POST_FRAMES)
#define READ_SECTORS   1024            /* 2 MiB reads */
#define DSF_BLOCK      4096
#define OUT_BUFFER     (1 << 20)

enum { OUT_DSF = 0, OUT_FLAC = 1, OUT_WAV = 2, OUT_DFF_DSD = 3, OUT_DFF_DST = 4 };

#include "sacdconv_api.h"

/* ------------------------------------------------------------ batches */

typedef struct batch {
    uint64_t  seq;
    uint32_t  tc0;              /* timecode of the first core frame */
    int       pre, n, post;     /* context before, core, context after */
    int       need_dsd, need_pcm, need_planar;
    int       decoded;          /* dsd already holds every frame, decoded by scarletbook */
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
    int             need_decode, need_stored;   /* decoded DSD, stored frames (DSDIFF DST) */
    int             reader_done;
    uint64_t        total_batches;
    _Atomic int     failed;
} engine_t;

static void sc_log(sc_area_t *a, int level, const char *fmt, ...)
    __attribute__((format(SC_PRINTF, 3, 4)));

static void set_error(engine_t *e, const char *fmt, ...)
{
    pthread_mutex_lock(&e->mu);
    if (!e->failed) {
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(e->a->error, sizeof(e->a->error), fmt, ap);
        va_end(ap);
        e->failed = 1;
        pthread_mutex_unlock(&e->mu);
        sc_log(e->a, SC_ERROR, "engine: %s", e->a->error);
        pthread_mutex_lock(&e->mu);
    }
    pthread_cond_broadcast(&e->cv_free);
    pthread_cond_broadcast(&e->cv_work);
    pthread_cond_broadcast(&e->cv_done);
    pthread_mutex_unlock(&e->mu);
}

/* The cancel flag is set by sacdconv_cancel() on the caller's thread while the engine runs */
static int cancelled(const sc_area_t *a)
{
    return __atomic_load_n(&a->cancel, __ATOMIC_ACQUIRE);
}

static int stopping(engine_t *e)
{
    return e->failed || cancelled(e->a);
}

static void sc_log(sc_area_t *a, int level, const char *fmt, ...)
{
    char msg[512];
    va_list ap;
    if (!a->log_fn || level < a->log_level)
        return;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    a->log_fn(a->log_ctx, level, msg);
}

static double now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1e3 + ts.tv_nsec / 1e6;
}

/* Timecode as mm:ss:ff (75 frames per second) */
static const char *tc_str(uint32_t tc, char buf[16])
{
    snprintf(buf, 16, "%02u:%02u:%02u", tc / (60 * FRAME_RATE), tc / FRAME_RATE % 60, tc % FRAME_RATE);
    return buf;
}

/* Does any output want a frame in [lo, hi)? */
static int range_wanted(engine_t *e, uint32_t lo, uint32_t hi)
{
    for (int i = 0; i < e->nouts; i++)
        if (e->outs[i].tc_start < hi && e->outs[i].tc_end > lo)
            return 1;
    return 0;
}

/* ------------------------------------------------------------ reader */

typedef struct {
    uint32_t tc;
    uint32_t len;               /* stored bytes in data (DSDIFF DST passthrough) */
    uint8_t  dst, missing, bad;
    uint8_t  data[MAX_FRAME_SIZE];
    uint8_t  dsd[MAX_CHANNELS_SACD * FRAME_BYTES_PER_CH];  /* decoded (when the outputs need it) */
} frame_t;

typedef struct {
    engine_t *e;
    int       decoded;          /* the staged frames carry decoded DSD */
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
        if (f->len)     /* missing frames have no data, and b->raw may still be NULL */
            memcpy(b->raw + pos, f->data, f->len);
        b->off[i] = (uint32_t)pos;
        b->len[i] = f->len;
        b->dst[i] = f->dst;
        b->missing[i] = f->missing;
        b->bad[i] = f->bad;
        if (r->decoded)
            memcpy(b->dsd + (size_t)i * e->frame_bytes, f->dsd, e->frame_bytes);
        pos += f->len;
    }
    b->decoded = r->decoded;
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

/* Stage one position: its stored bytes (data, len) and, when decoding, its decoded DSD (dsd).
   A missing position has no stored bytes; decoded, it is silence. */
static int stage_frame(reader_t *r, uint32_t tc, const uint8_t *data, int len, int dst, int missing,
                       const uint8_t *dsd, int bad)
{
    frame_t *f = &r->stage[r->nstage++];
    f->tc = tc;
    f->len = missing ? 0 : (uint32_t)len;
    f->dst = (uint8_t)dst;
    f->missing = (uint8_t)missing;
    f->bad = (uint8_t)bad;
    /* the scarletbook reader reads straight into the next slot: nothing to copy then */
    if (!missing && len && data != f->data)
        memcpy(f->data, data, len);
    if (r->decoded) {
        if (!dsd)
            memset(f->dsd, DSD_SILENCE, r->e->frame_bytes);
        else if (dsd != f->dsd)
            memcpy(f->dsd, dsd, r->e->frame_bytes);
    }
    /* full batch plus its post context staged: emit */
    if (r->nstage == r->pre + BATCH_FRAMES + POST_FRAMES)
        return emit_batch(r, BATCH_FRAMES, POST_FRAMES);
    return 0;
}

static int fill_until(reader_t *r, uint32_t tc)
{
    if (r->next_tc < tc && range_wanted(r->e, r->next_tc, tc)) {
        char a[16], b[16];
        sc_log(r->e->a, SC_WARNING, "frames %s-%s (%u) not found on the disc; filled with silence",
               tc_str(r->next_tc, a), tc_str(tc - 1, b), tc - r->next_tc);
    }
    for (; r->next_tc < tc; r->next_tc++)
        if (stage_frame(r, r->next_tc, NULL, 0, 0, 1, NULL, 0) < 0)
            return -1;
    return 0;
}


/* ------------------------------------------------------------ reader: scarletbook (#85) */

/* Serialises sacdconv_cancel() against the reader handing out and freeing its token */
static pthread_mutex_t cancel_mu = PTHREAD_MUTEX_INITIALIZER;

/* A sector source (read_fn, 2048-byte sectors by LSN) as scarletbook's positional reader */
typedef struct {
    sc_area_t *a;
    uint8_t   *scratch;         /* READ_SECTORS sectors */
} sb_source_t;

static int64_t sb_read_at(void *user, uint64_t offset, uint8_t *buf, size_t len)
{
    sb_source_t *src = user;
    sc_area_t *a = src->a;
    uint64_t total = a->image_sectors * LSN_SIZE;
    size_t done = 0;
    if (offset >= total)
        return 0;
    if (len > total - offset)
        len = (size_t)(total - offset);
    while (done < len) {
        uint64_t pos = offset + done;
        uint64_t lsn = pos / LSN_SIZE;
        size_t skip = (size_t)(pos % LSN_SIZE);
        uint64_t n = (skip + (len - done) + LSN_SIZE - 1) / LSN_SIZE;
        if (n > READ_SECTORS)
            n = READ_SECTORS;
        if (a->read_fn(a->read_ctx, (uint32_t)lsn, (uint32_t)n, src->scratch) != 0)
            return -1;
        size_t k = (size_t)n * LSN_SIZE - skip;
        if (k > len - done)
            k = len - done;
        memcpy(buf + done, src->scratch + skip, k);
        done += k;
    }
    return (int64_t)done;
}

/* scarletbook's log, into the engine's: its errors as warnings, the rest at debug level (pysacd
   reports missing frames, invalid DST frames and malformed sectors itself, from the statuses) */
static void sb_log_cb(void *user, int32_t level, const char *msg)
{
    sc_log(user, level <= SB_LOG_ERROR ? SC_WARNING : SC_DEBUG, "scarletbook: %s", msg);
}

/* Open the image with scarletbook: by path, or through the sector source (src->scratch is
   allocated for it; the caller frees it) */
static SbStatus sb_open(sc_area_t *a, sb_source_t *src, SbDisc **disc)
{
    int32_t level = !a->log_fn ? SB_LOG_OFF : a->log_level <= SC_DEBUG ? SB_LOG_DEBUG : SB_LOG_ERROR;
    if (!a->read_fn)
        return sb_disc_open_file_with_log(a->iso_path, sb_log_cb, a, level, disc);
    if (!(src->scratch = malloc((size_t)READ_SECTORS * LSN_SIZE)))
        return SB_STATUS_OUT_OF_MEMORY;
    return sb_disc_open_callbacks_at_with_log(sb_read_at, a->image_sectors * LSN_SIZE, src, sb_log_cb, a, level, disc);
}

static void sb_fail(engine_t *e, SbStatus st, const char *what)
{
    const char *msg = sb_last_error_message();
    set_error(e, "%s: %s%s%s", what, sb_status_name(st), msg && *msg ? ": " : "", msg && *msg ? msg : "");
}

/* Log the malformed-sector warnings a handle has collected since the last call */
static void sb_log_malformed(sc_area_t *a, SbTrackAudio *audio, SbRawFrames *raw, uint32_t *seen)
{
    SbWarning w;
    while ((audio ? sb_track_audio_warning(audio, *seen, &w) : sb_raw_frames_warning(raw, *seen, &w)) == SB_STATUS_OK) {
        sc_log(a, SC_WARNING, "malformed audio sector at LSN %lld: part or all of it dropped (%s)",
               (long long)w.lsn, w.code ? w.code : "?");
        (*seen)++;
    }
}

/* Checks a provisional reading in the background, on a clone of the disc, while the run reads:
   scarletbook builds the area's seek index from its first sector and reports, once the index
   reaches the provisional start, whether the positions were those of an exact read. Stops when
   the run ends, whether or not it got that far. */
typedef struct {
    SbDisc     *clone;
    uint32_t    slot;
    _Atomic int stop;
} verifier_t;

#define VERIFY_STEP 8192        /* sectors per sb_disc_seek_verify call (16 MiB): how soon it stops */

static void *verify_main(void *arg)
{
    verifier_t *v = arg;
    uint8_t complete = 0;
    while (!atomic_load(&v->stop) && !complete)
        if (sb_disc_seek_verify(v->clone, v->slot, VERIFY_STEP, &complete) < 0)
            break;
    return NULL;
}

/* DSD silence, for the #93 workaround: in every window of 384 bits of every channel, the
   count of ones is within 2 of half (a DC level within about 1 % of full scale). Measured on
   the four discs of #93: the frames before the missing one deviate by at most 1; sampled music
   frames have a median of 24 to 27. `p` holds one frame, its channels interleaved by byte. */
static int dsd_silent(const uint8_t *p, size_t n, int ch)
{
    if (ch <= 0 || n != (size_t)ch * 4704)
        return 0;
    for (int c = 0; c < ch; c++)
        for (size_t w = 0; w < 4704; w += 48) {
            int ones = 0;
            for (size_t j = w; j < w + 48; j++)
                ones += __builtin_popcount(p[j * ch + c]);
            if (ones < 190 || ones > 194)
                return 0;
        }
    return 1;
}

static void log_missing(sc_area_t *a, uint32_t from, uint32_t to)
{
    char x[16], y[16];
    sc_log(a, SC_WARNING, "frames %s-%s (%u) not found on the disc; filled with silence",
           tc_str(from, x), tc_str(to - 1, y), to - from);
}

static void *sb_reader_main(void *arg)
{
    engine_t *e = arg;
    sc_area_t *a = e->a;
    reader_t r = {.e = e, .decoded = e->need_decode};
    sb_source_t src = {.a = a};
    SbDisc *disc = NULL;
    SbTrackAudio *audio = NULL;
    SbRawFrames *raw = NULL;
    SbPool *pool = NULL;
    SbProgress *prog = NULL;
    SbCancel *token = NULL;
    uint8_t *dsd = NULL, *stored = NULL;     /* the stage slot being read into */
    uint32_t nwarn = 0, miss_from = UINT32_MAX;
    verifier_t ver = {0};       /* the background check of a provisional start */
    pthread_t ver_thread;
    int ver_started = 0;
    SbStatus st;

    r.stage = malloc(sizeof(frame_t) * SLOT_FRAMES);
    if (!r.stage) {
        set_error(e, "out of memory");
        goto out;
    }
    if ((st = sb_open(a, &src, &disc)) < 0) {
        sb_fail(e, st, "opening the image");
        goto out;
    }
    if (sb_disc_cancel_token(disc, &token) == SB_STATUS_OK) {
        pthread_mutex_lock(&cancel_mu);
        atomic_store(&a->sb_cancel, token);
        if (cancelled(a))
            sb_cancel_request(token);
        pthread_mutex_unlock(&cancel_mu);
    }

    /* the area whose TOC the caller read (toc.Area.toc_start) */
    SbDiscInfo di;
    SbAreaInfo ai;
    int found = 0;
    if ((st = sb_disc_info(disc, &di)) < 0) {
        sb_fail(e, st, "reading the disc");
        goto out;
    }
    for (uint32_t i = 0; i < di.area_count && !found; i++)
        found = sb_disc_area(disc, i, &ai) == SB_STATUS_OK && ai.toc_lsn == a->toc_lsn;
    if (!found) {
        set_error(e, "no area with its TOC at LSN %u", a->toc_lsn);
        goto out;
    }
    if ((int)ai.channel_count != a->channels) {
        set_error(e, "the area has %u channels, not %d", ai.channel_count, a->channels);
        goto out;
    }

    r.want_lo = e->need_lo > PRE_FRAMES ? e->need_lo - PRE_FRAMES : 0;
    r.want_hi = e->need_hi + POST_FRAMES;
    r.next_tc = r.want_lo;
    /* scarletbook reads the area's timeline, [0, play time); positions past it are silence */
    uint32_t end = r.want_hi < ai.playtime_frames ? r.want_hi : ai.playtime_frames;
    sc_log(a, SC_DEBUG, "scarletbook: area slot %u, frames %u-%u of %u, %s%s", ai.slot, r.want_lo, end,
           ai.playtime_frames, e->need_decode ? "decoded" : "stored frames only",
           e->need_decode && e->need_stored ? " and stored" : "");

    /* A range far into the area starts provisionally unless the caller asked for exact reading:
       on a well-formed disc that is exact by the specification (4.2 item 8), and it avoids
       reading the area up to the range. With a file, the area's seek index is kept so that a
       clone can check the provisional start while the run reads (sector sources can't be
       read from two threads). */
    int speculative = !a->exact && r.want_lo > 0;
    int verifiable = speculative && !a->read_fn;
    ver.slot = ai.slot;
    uint64_t gap = a->seek_min_gap < 0 ? SB_SEEK_DEFAULT_MIN_GAP : (uint64_t)a->seek_min_gap;
    if (speculative && (st = sb_disc_seek_policy_set(disc, ai.slot, SB_SEEK_POLICY_SPECULATIVE, gap)) < 0) {
        sb_fail(e, st, "setting the seek policy");
        goto out;
    }
    if (verifiable && sb_disc_seek_index_enable(disc, ai.slot, SB_SEEK_DEFAULT_INTERVAL,
                                                SB_SEEK_DEFAULT_INLINE_BUDGET) < 0)
        verifiable = 0;

    if (r.want_lo < end) {
        if (e->need_decode) {
            if ((st = sb_range_audio_open(disc, ai.slot, r.want_lo, end, &audio)) < 0) {
                sb_fail(e, st, "opening the area's audio");
                goto out;
            }
            /* One decoding thread fewer than the CPUs (or than --threads): this reader thread
               does real work with every frame (staging, batching), and a full-size pool plus
               the reader is one runnable thread more than CPUs (scarletbook-rs#146, #94). */
            int threads = (a->threads > 0 ? a->threads : sc_ncpus()) - 1;
            if (threads < 1)
                threads = 1;
            sc_log(a, SC_DEBUG, "decoding pool: %d threads", threads);
            if ((st = sb_pool_new((uint32_t)(threads > SB_MAX_POOL_THREADS ? SB_MAX_POOL_THREADS : threads), &pool)) < 0 ||
                (st = sb_track_audio_set_pool(audio, pool)) < 0 ||
                (e->need_stored && (st = sb_track_audio_keep_stored(audio)) < 0) ||
                (st = sb_track_audio_progress(audio, &prog)) < 0) {
                sb_fail(e, st, "setting up the area's audio");
                goto out;
            }
        } else {
            if ((st = sb_raw_frames_open(disc, ai.slot, r.want_lo, end, &raw)) < 0 ||
                (st = sb_raw_frames_progress(raw, &prog)) < 0) {
                sb_fail(e, st, "opening the area's frames");
                goto out;
            }
        }

        for (uint32_t k = 0; !stopping(e); k++) {
            SbFrameInfo fi = {0};
            SbRawFrameInfo ri = {0};
            /* read into the stage's next slot (stage_frame then copies nothing) */
            dsd = r.stage[r.nstage].dsd;
            stored = r.stage[r.nstage].data;
            if (audio && e->need_stored)
                st = sb_track_audio_read_with_stored(audio, disc, dsd, e->frame_bytes, &fi, stored,
                                                     SB_MAX_FRAME_BYTES, &ri, 0);
            else if (audio)
                st = sb_track_audio_read(audio, disc, dsd, e->frame_bytes, &fi);
            else
                st = sb_raw_frames_read(raw, disc, stored, SB_MAX_FRAME_BYTES, &ri);
            if (st == SB_STATUS_END)
                break;
            if (st == SB_STATUS_CANCELLED)
                break;          /* a->cancel is set: the run ends as cancelled */
            if (st < 0) {
                sb_fail(e, st, "reading the area");
                goto out;
            }
            if (k == 0 && speculative) {
                SbSpeculation sp;
                if ((audio ? sb_track_audio_speculation(audio, &sp) : sb_raw_frames_speculation(raw, &sp)) ==
                        SB_STATUS_OK && sp.state != SB_SPECULATION_EXACT) {
                    a->provisional = 1;
                    sc_log(a, SC_INFO, "scarletbook: frames from %u start provisionally at LSN %llu, without "
                           "reading the area before them (exact on a well-formed disc; --exact reads it); %s",
                           sp.first, (unsigned long long)sp.lsn,
                           verifiable ? "checking it in the background" : "not checked (sector source)");
                    if (verifiable && sb_disc_clone(disc, &ver.clone) == SB_STATUS_OK)
                        ver_started = pthread_create(&ver_thread, NULL, verify_main, &ver) == 0;
                }
            }
            uint32_t number = audio ? fi.number : ri.number;
            if (number != r.next_tc) {
                set_error(e, "scarletbook returned frame %u where %u was expected", number, r.next_tc);
                goto out;
            }
            int missing = audio ? fi.status == SB_FRAME_STATUS_MISSING : ri.kind == SB_FRAME_KIND_MISSING;
            int bad = audio && fi.status == SB_FRAME_STATUS_INVALID;
            int with_stored = !audio || e->need_stored;
            int is_dst = with_stored ? ri.kind == SB_FRAME_KIND_DST
                                     : fi.status == SB_FRAME_STATUS_DECODED || fi.status == SB_FRAME_STATUS_INVALID;
            if (number == a->check_frame && !missing) {
                /* #93 workaround: is the frame before the area's last one DSD silence? */
                const uint8_t *p = audio ? dsd : !is_dst ? stored : NULL;
                a->check_silent = p ? dsd_silent(p, audio ? e->frame_bytes : (size_t)ri.size, e->channels) : -1;
            }
            if (bad && range_wanted(e, number, number + 1)) {
                char t[16];
                sc_log(a, SC_WARNING, "DST frame at %s (frame %u) could not be decoded (rule %u); filled with 0x55",
                       tc_str(number, t), number, fi.dst_rule);
            }
            if (missing && range_wanted(e, number, number + 1)) {
                if (a->missing_count++ == 0)
                    a->missing_first = number;
                a->missing_last = number;
                if (miss_from == UINT32_MAX)
                    miss_from = number;
            } else if (miss_from != UINT32_MAX) {
                log_missing(a, miss_from, number);
                miss_from = UINT32_MAX;
            }
            if (stage_frame(&r, number, stored, with_stored && !missing ? (int)ri.size : 0, is_dst, missing,
                            audio ? dsd : NULL, bad) < 0)
                goto out;
            r.next_tc = number + 1;
            if (k % 32 == 31) {
                uint64_t done = 0, expected = 0;
                if (sb_progress_get(prog, &done, &expected) == SB_STATUS_OK) {
                    a->sectors_done = done;
                    a->sectors_total = expected;
                }
                sb_log_malformed(a, audio, raw, &nwarn);
            }
        }
        if (miss_from != UINT32_MAX)
            log_missing(a, miss_from, r.next_tc);
        sb_log_malformed(a, audio, raw, &nwarn);
        if (ver_started) {
            atomic_store(&ver.stop, 1);
            pthread_join(ver_thread, NULL);
            ver_started = 0;
        }
        if (a->provisional) {
            SbSpeculation ev;
            int verdict = 0;
            while (sb_disc_seek_event_next(disc, ai.slot, &ev) == SB_STATUS_OK) {
                if (ev.state == SB_SPECULATION_REFUTED) {
                    if (ev.exact_from < a->refuted_from)
                        a->refuted_from = ev.exact_from;
                    verdict = 1;
                } else if (ev.state == SB_SPECULATION_CONFIRMED && !verdict) {
                    a->provisional = 2;
                }
            }
            if (a->refuted_from != UINT32_MAX)
                sc_log(a, SC_WARNING, "scarletbook: the provisional start was wrong on this image: frames before %u "
                       "may differ from an exact read; run again with --exact", a->refuted_from);
            else
                sc_log(a, SC_INFO, "scarletbook: provisional start %s", a->provisional == 2 ? "confirmed"
                       : "not checked before the run ended (exact on a well-formed disc)");
        }
        uint64_t done = 0, expected = 0;
        if (prog && sb_progress_get(prog, &done, &expected) == SB_STATUS_OK) {
            a->sectors_done = done;
            a->sectors_total = done;
        }
    }
    if (stopping(e))
        goto out;
    /* positions past the play time (or past the area's end) become silence */
    if (fill_until(&r, r.want_hi) < 0)
        goto out;
    int ncore = r.nstage - r.pre;
    if (ncore > 0 && emit_batch(&r, ncore, 0) < 0)
        goto out;

out:
    if (ver_started) {
        atomic_store(&ver.stop, 1);
        pthread_join(ver_thread, NULL);
    }
    sb_disc_free(ver.clone);
    pthread_mutex_lock(&cancel_mu);
    atomic_store(&a->sb_cancel, NULL);
    pthread_mutex_unlock(&cancel_mu);
    sb_cancel_free(token);
    sb_progress_free(prog);
    sb_track_audio_free(audio);
    sb_raw_frames_free(raw);
    sb_pool_free(pool);
    sb_disc_free(disc);
    pthread_mutex_lock(&e->mu);
    e->reader_done = 1;
    e->total_batches = r.seq;
    pthread_cond_broadcast(&e->cv_work);
    pthread_cond_broadcast(&e->cv_done);
    pthread_mutex_unlock(&e->mu);
    free(r.stage);
    free(src.scratch);
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

static void process_batch(engine_t *e, batch_t *b, dsd2pcm_t *dsp, const uint8_t *silence)
{
    int nf = b->pre + b->n + b->post;
    size_t fb = e->frame_bytes;

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

        process_batch(e, b, dsp, silence);

        pthread_mutex_lock(&e->mu);
        e->done[b->seq % e->npool] = b;
        pthread_cond_broadcast(&e->cv_done);
        pthread_mutex_unlock(&e->mu);
    }

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
        FILE *f = sc_fopen_update(o->path);
        if (!f)
            return out_fail(o, "%s: %s", o->path, strerror(errno));
        /* the encoder owns f from here and closes it in FLAC__stream_encoder_finish() */
        FLAC__StreamEncoderInitStatus s = FLAC__stream_encoder_init_FILE(enc, f, NULL, NULL);
        if (s != FLAC__STREAM_ENCODER_INIT_STATUS_OK)
            return out_fail(o, "%s: FLAC init: %s", o->path, FLAC__StreamEncoderInitStatusString[s]);
        return 0;
#else
        return out_fail(o, "built without libFLAC");
#endif
    }

    if (!(st->fp = sc_fopen_write(o->path)))
        return out_fail(o, "%s: %s", o->path, strerror(errno));
    setvbuf(st->fp, NULL, _IOFBF, OUT_BUFFER);
    switch (o->format) {
    case OUT_DSF:
        st->limit = (uint64_t)(o->tc_end - o->tc_start) * FRAME_BYTES_PER_CH +
                    (o->head_frame >= 0 ? o->head_bytes : 0) - o->tail_bytes;
        /* the header scarletbook laid out (pysacd.sbwriter): its sizes are the file's */
        if (o->header_len && fwrite(o->header, o->header_len, 1, st->fp) != 1)
            return out_fail(o, "%s: write failed", o->path);
        return 0;
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
#ifdef HAVE_FLAC
    const int not_opened = o->format == OUT_FLAC ? !st->enc : !st->fp;
#else
    const int not_opened = !st->fp;     /* a FLAC output never opens without libFLAC */
#endif
    if (not_opened) {
        /* out_open failed (e.g. the directory isn't writable) and recorded why: nothing to finish */
#ifdef HAVE_FLAC
        if (st->pad)
            FLAC__metadata_object_delete(st->pad);
        st->pad = NULL;
#endif
        free(st->wbuf);
        st->wbuf = NULL;
        return -1;
    }
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
    sc_log(e->a, rc ? SC_ERROR : (o->frames_missing || o->dst_errors) ? SC_WARNING : SC_DEBUG,
           "%s %s: %u frames, %u missing, %u DST errors, %llu data bytes%s%s",
           rc ? "failed" : "wrote", o->path, o->frames_written, o->frames_missing, o->dst_errors,
           (unsigned long long)o->data_bytes, rc ? ": " : "", rc ? o->error : "");
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

/*
 * ABI handshake (#14). pysacd/_native.py mirrors sc_area_t and sc_output_t by hand, and
 * _load() refuses a library whose version, struct sizes or field offsets differ from its
 * ctypes mirrors. Bump SACDCONV_ABI whenever a struct or an exported signature changes.
 */
#define SACDCONV_ABI 5     /* 2: scarletbook (toc_lsn, image_sectors, sb_cancel; sacdconv_cancel);
                              3: provisional range starts (exact, provisional, refuted_from);
                              4: the old engine's scan range and ISO sector count removed;
                              5: missing frames and the #93 silence check */

#define OFF(T, f) (uint32_t)offsetof(T, f)
static const uint32_t area_layout[] = {
    sizeof(sc_area_t),
    OFF(sc_area_t, iso_path), OFF(sc_area_t, sector_size), OFF(sc_area_t, data_offset),
    OFF(sc_area_t, channels),
    OFF(sc_area_t, pcm_rate), OFF(sc_area_t, gain_db), OFF(sc_area_t, threads),
    OFF(sc_area_t, iso_out_path), OFF(sc_area_t, cancel),
    OFF(sc_area_t, frames_done), OFF(sc_area_t, frames_total), OFF(sc_area_t, sectors_done),
    OFF(sc_area_t, sectors_total), OFF(sc_area_t, error), OFF(sc_area_t, read_fn),
    OFF(sc_area_t, read_ctx), OFF(sc_area_t, log_fn), OFF(sc_area_t, log_ctx), OFF(sc_area_t, log_level),
    OFF(sc_area_t, toc_lsn), OFF(sc_area_t, image_sectors), OFF(sc_area_t, sb_cancel),
    OFF(sc_area_t, exact), OFF(sc_area_t, provisional), OFF(sc_area_t, refuted_from),
    OFF(sc_area_t, seek_min_gap), OFF(sc_area_t, check_frame), OFF(sc_area_t, check_silent),
    OFF(sc_area_t, missing_count), OFF(sc_area_t, missing_first), OFF(sc_area_t, missing_last),
};
static const uint32_t output_layout[] = {
    sizeof(sc_output_t),
    OFF(sc_output_t, format), OFF(sc_output_t, path), OFF(sc_output_t, tc_start), OFF(sc_output_t, tc_end),
    OFF(sc_output_t, head_frame), OFF(sc_output_t, head_bytes), OFF(sc_output_t, tail_bytes),
    OFF(sc_output_t, header), OFF(sc_output_t, header_len), OFF(sc_output_t, footer),
    OFF(sc_output_t, footer_len), OFF(sc_output_t, dst_offsets), OFF(sc_output_t, dst_lengths),
    OFF(sc_output_t, dst_cap), OFF(sc_output_t, flac_level), OFF(sc_output_t, flac_padding),
    OFF(sc_output_t, frames_written), OFF(sc_output_t, frames_missing), OFF(sc_output_t, dst_errors),
    OFF(sc_output_t, dst_count), OFF(sc_output_t, samples), OFF(sc_output_t, data_bytes),
    OFF(sc_output_t, error),
};
#undef OFF

int sacdconv_abi(void)
{
    return SACDCONV_ABI;
}

/* which 0: sc_area_t, 1: sc_output_t. Writes up to cap entries (size, then each field's
 * offset in declaration order) and returns how many there are. */
int sacdconv_layout(int which, uint32_t *out, int cap)
{
    const uint32_t *t = which ? output_layout : area_layout;
    int n = which ? (int)(sizeof output_layout / sizeof *output_layout)
                  : (int)(sizeof area_layout / sizeof *area_layout);
    for (int i = 0; i < n && i < cap; i++)
        out[i] = t[i];
    return n;
}

/* Build details for diagnostic logs */
const char *sacdconv_build_info(void)
{
#ifdef __clang__
    return "clang " __clang_version__
#else
    return "gcc " __VERSION__
#endif
#ifdef HAVE_FLAC
           ", libFLAC"
#endif
#ifdef HAVE_FLAC_MT
           " (multithreaded)"
#endif
#ifdef __AVX512F__
           ", AVX-512"
#elif defined(__AVX2__)
           ", AVX2"
#elif defined(__SSE4_2__)
           ", SSE4.2"
#endif
#ifdef SC_DISPATCH
           ", portable (PCM filter SIMD chosen at load time)"
#endif
#ifdef __SANITIZE_ADDRESS__
           ", ASan"
#endif
#ifndef __OPTIMIZE__
           ", unoptimized"
#endif
           ;
}

/* Cancel a running sacdconv_area_run() from another thread: the engine's own threads stop at
   their next batch, and scarletbook's reads within 32 sectors (SbCancel). */
void sacdconv_cancel(sc_area_t *a)
{
    __atomic_store_n(&a->cancel, 1, __ATOMIC_RELEASE);
    pthread_mutex_lock(&cancel_mu);
    SbCancel *token = atomic_load(&a->sb_cancel);
    if (token)
        sb_cancel_request(token);
    pthread_mutex_unlock(&cancel_mu);
}

/* Copy the whole image to a->iso_out_path, as plain 2048-byte sectors, in one sequential pass:
   scarletbook reads the user data of each sector, whatever the image's sector size */
static int copy_image(sc_area_t *a)
{
    engine_t e = {.a = a};
    sb_source_t src = {.a = a};
    SbDisc *disc = NULL;
    FILE *iso = NULL;
    uint8_t *buf = malloc((size_t)READ_SECTORS * LSN_SIZE);
    SbStatus st;
    SbDiscInfo di;

    pthread_mutex_init(&e.mu, NULL);
    pthread_cond_init(&e.cv_free, NULL);
    pthread_cond_init(&e.cv_work, NULL);
    pthread_cond_init(&e.cv_done, NULL);
    if (!buf) {
        set_error(&e, "out of memory");
        goto out;
    }
    if ((st = sb_open(a, &src, &disc)) < 0 || (st = sb_disc_info(disc, &di)) < 0) {
        sb_fail(&e, st, "opening the image");
        goto out;
    }
    if (!(iso = sc_fopen_write(a->iso_out_path))) {
        set_error(&e, "%s: %s", a->iso_out_path, strerror(errno));
        goto out;
    }
    setvbuf(iso, NULL, _IOFBF, 4 * OUT_BUFFER);
    sc_log(a, SC_INFO, "engine: copying %llu sectors to %s", (unsigned long long)di.sectors, a->iso_out_path);
    a->sectors_total = di.sectors;
    for (uint64_t lsn = 0; lsn < di.sectors && !cancelled(a); lsn += READ_SECTORS) {
        uint32_t n = di.sectors - lsn < READ_SECTORS ? (uint32_t)(di.sectors - lsn) : READ_SECTORS;
        double t0 = now_ms();
        if ((st = sb_disc_read_sectors(disc, lsn, n, buf, (size_t)n * LSN_SIZE)) < 0) {
            char what[64];
            snprintf(what, sizeof(what), "read error at LSN %llu (+%u)", (unsigned long long)lsn, n);
            sb_fail(&e, st, what);
            goto out;
        }
        sc_log(a, SC_DEBUG, "read LSN %llu+%u in %.1f ms", (unsigned long long)lsn, n, now_ms() - t0);
        if (fwrite(buf, LSN_SIZE, n, iso) != n) {
            set_error(&e, "%s: %s", a->iso_out_path, strerror(errno));
            goto out;
        }
        a->sectors_done += n;
    }
out:
    if (iso && fclose(iso) != 0 && !e.failed)
        set_error(&e, "%s: %s", a->iso_out_path, strerror(errno));
    sb_disc_free(disc);
    free(src.scratch);
    free(buf);
    pthread_mutex_destroy(&e.mu);
    pthread_cond_destroy(&e.cv_free);
    pthread_cond_destroy(&e.cv_work);
    pthread_cond_destroy(&e.cv_done);
    if (e.failed)
        return -1;
    if (cancelled(a)) {
        snprintf(a->error, sizeof(a->error), "cancelled");
        return -1;
    }
    return 0;
}

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
    a->provisional = 0;
    a->refuted_from = UINT32_MAX;
    a->check_silent = -1;
    a->missing_count = 0;
    a->missing_first = a->missing_last = UINT32_MAX;
    if (a->read_fn && (a->sector_size != LSN_SIZE || a->data_offset != 0)) {
        snprintf(a->error, sizeof(a->error), "a sector source must deliver plain 2048-byte sectors");
        return -1;
    }
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
        if (o->format == OUT_DFF_DST)
            e.need_stored = 1;
        else
            e.need_decode = 1;
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

    /* An ISO copy first, in one pass; the audio is then read from that local copy, so a sector
       source (a PS3) is read once */
    if (a->iso_out_path) {
        if (copy_image(a) < 0)
            return -1;
        a->iso_path = a->iso_out_path;
        a->iso_out_path = NULL;
        a->read_fn = NULL;
        a->sector_size = LSN_SIZE;
        a->data_offset = 0;
    }
    if (!nouts)
        return 0;
    a->sectors_done = 0;

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

    nworkers = a->threads > 0 ? a->threads : sc_ncpus();
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

    sc_log(a, SC_INFO, "engine: %d channels, %d outputs, frames %u-%u, area TOC at LSN %u%s, %d worker threads%s%s",
           a->channels, nouts, e.need_lo, e.need_hi, a->toc_lsn,
           a->read_fn ? " via sector source" : "", nworkers, want_pcm ? ", PCM " : "",
           want_pcm ? (a->pcm_rate == 88200 ? "88.2 kHz" : "176.4 kHz") : "");
    double t_start = now_ms();
    int started = 0;
    for (; started < nworkers; started++)
        if (pthread_create(&workers[started], NULL, worker_main, &e) != 0)
            break;
    if (started == 0 || pthread_create(&reader, NULL, sb_reader_main, &e) != 0) {
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

    rc = e.failed ? -1 : cancelled(a) ? (snprintf(a->error, sizeof(a->error), "cancelled"), -1) : 0;
    sc_log(a, rc ? SC_ERROR : SC_INFO, "engine: %s after %.2f s: %llu frames, %llu sectors read%s%s",
           rc ? "failed" : "finished", (now_ms() - t_start) / 1e3, (unsigned long long)a->frames_done,
           (unsigned long long)a->sectors_done, rc ? ": " : "", rc ? a->error : "");
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
