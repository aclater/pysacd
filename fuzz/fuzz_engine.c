/*
 * libFuzzer harness for pysacd's engine (#85): an input is a disc image, which goes through
 * sacdconv_area_run() the way pysacd-extract and pysacd convert use it.
 *
 * scarletbook-rs parses the image (TOC, sectors, frames, DST) and is fuzzed in its own
 * repository; what this harness exercises is pysacd's side: the reader thread's staging of
 * scarletbook's positions, batches with PCM context, the workers (DSD to PCM), the writers
 * (DSF filled from scarletbook's layout, DSDIFF DSD, DSDIFF DST passthrough, WAV, FLAC), the
 * ISO copy, cancellation and a sector source.
 *
 * An input is one of two kinds.
 *
 * A plain disc image. For each area scarletbook reports, the harness writes:
 *
 *   - the whole area to DSF, DSDIFF (decoded), FLAC (when built with libFLAC) and, for a DST
 *     area, DSDIFF with the frames as stored, all in one pass;
 *   - the area's last track to WAV, starting provisionally (seek gap 0), read through a sector
 *     source (sb_disc_read_sectors on the image in memory);
 *
 *   capped at MAX_FRAMES frames so that one input stays fast.
 *
 * Or CFG_MAGIC, a CFG_SIZE-byte run configuration (cfg_t, below), then the disc image (#157).
 * For each area, one sacdconv_area_run() writes what the configuration selects: any of DSF,
 * FLAC, WAV, DSDIFF DSD, DSDIFF DST, a second WAV and a gapless DSF chain (-z, with a head
 * frame), each over its own range of at most MAX_RANGE frames that may start anywhere in the
 * area or past its end; with or without an ISO copy, a sector source, a provisional start, the
 * filler limit, a PCM rate of 88.2 or 176.4 kHz, 1 to MAX_THREADS threads, a log callback, a
 * cancel (from the log callback, from another thread, or before the run starts), and arguments
 * the engine must refuse.
 * fuzz/make_seeds.py writes such seeds; libFuzzer mutates the configuration with the image.
 *
 * Outputs go to a private temporary directory (under $TMPDIR, else /tmp) and are removed after
 * each run.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "scarletbook.h"

#define MAX_FRAMES  600         /* plain images: frames per area */
#define MAX_RANGE   200         /* configured runs: frames per output */
#define MAX_THREADS 20
#define CHAIN_FILES 3

/* sc_area_t and sc_output_t, as in csrc/sacdconv.c (the harness is built with it) */
#include "../csrc/sacdconv_api.h"

static char dir[256];

static void out_path(char *buf, size_t cap, const char *name)
{
    snprintf(buf, cap, "%s/%s", dir, name);
    unlink(buf);
}

static void remove_dir(void)
{
    if (dir[0])
        rmdir(dir);
}

/* the sector source: the image's 2048-byte logical sectors, read through scarletbook */
static int read_mem(void *ctx, uint32_t lsn, uint32_t count, uint8_t *buf)
{
    return sb_disc_read_sectors(ctx, lsn, count, buf, (size_t)count * 2048) == SB_STATUS_OK ? 0 : -1;
}

/* the DSF channel type for an area of unspecified layout, as pysacd gives */
static const int64_t channel_type[] = {0, 1, 2, 3, 5, 6, 7};

static SbOutputLayout *dsf_layout(SbDisc *disc, const SbAreaInfo *ai, uint32_t first, uint32_t end,
                                  int64_t chain_first, int64_t chain_end)
{
    SbOutputOptions *opts = NULL;
    SbOutputLayout *layout = NULL;
    if (sb_output_options_new(&opts) == SB_STATUS_OK &&
        sb_output_options_set(opts, SB_OUTPUT_OPTION_DSF_UNSPECIFIED_CHANNEL_TYPE,
                              channel_type[ai->channel_count]) == SB_STATUS_OK &&
        (chain_first < 0 ||
         (sb_output_options_set(opts, SB_OUTPUT_OPTION_DSF_CHAIN_FIRST, chain_first) == SB_STATUS_OK &&
          sb_output_options_set(opts, SB_OUTPUT_OPTION_DSF_CHAIN_END, chain_end) == SB_STATUS_OK)))
        sb_output_layout_new(disc, ai->slot, first, end, SB_OUTPUT_FORMAT_DSF, opts, &layout);
    sb_output_options_free(opts);
    return layout;
}

/* ------------------------------------------------------------ plain images */

static void run_area(const char *image, SbDisc *disc, const SbDiscInfo *di, const SbAreaInfo *ai)
{
    if (ai->channel_count < 1 || ai->channel_count > 6 || ai->playtime_frames == 0)
        return;
    uint32_t end = ai->playtime_frames < MAX_FRAMES ? ai->playtime_frames : MAX_FRAMES;
    char dsf[300], dff[300], dst[300], wav[300], flac[300];
    out_path(dsf, sizeof dsf, "a.dsf");
    out_path(dff, sizeof dff, "a.dff");
    out_path(dst, sizeof dst, "d.dff");
    out_path(wav, sizeof wav, "t.wav");
    out_path(flac, sizeof flac, "a.flac");

    /* the whole area (capped), one pass, from the image file; the DSF file through its layout,
       when scarletbook makes one */
    SbOutputLayout *layout = dsf_layout(disc, ai, 0, end, -1, -1);
    sc_output_t outs[4];
    int nouts = 0;
    memset(outs, 0, sizeof outs);
    if (layout)
        outs[nouts++] = (sc_output_t){.format = 0, .path = dsf, .tc_start = 0, .tc_end = end, .head_frame = -1,
                                      .layout = layout};
    outs[nouts++] = (sc_output_t){.format = 3, .path = dff, .tc_start = 0, .tc_end = end, .head_frame = -1};
    if (sacdconv_has_flac())
        outs[nouts++] = (sc_output_t){.format = 1, .path = flac, .tc_start = 0, .tc_end = end, .head_frame = -1,
                                      .flac_level = 0};
    static uint64_t offs[MAX_FRAMES];
    static uint32_t lens[MAX_FRAMES];
    if (ai->frame_format == 0)
        outs[nouts++] = (sc_output_t){.format = 4, .path = dst, .tc_start = 0, .tc_end = end, .head_frame = -1,
                                      .dst_offsets = offs, .dst_lengths = lens, .dst_cap = MAX_FRAMES};
    sc_area_t a;
    memset(&a, 0, sizeof a);
    a.iso_path = image;
    a.sector_size = (int)di->sector_size;
    a.channels = (int)ai->channel_count;
    a.pcm_rate = 88200;
    a.threads = 2;
    a.toc_lsn = ai->toc_lsn;
    a.image_sectors = di->sectors;
    a.exact = 1;
    a.seek_min_gap = -1;
    sacdconv_area_run(&a, outs, nouts);
    sb_output_layout_free(layout);

    /* the last track to WAV, provisionally, through a sector source */
    if (ai->track_count) {
        SbTrackInfo ti;
        if (sb_track_info(disc, ai->slot, ai->track_count, &ti) == SB_STATUS_OK && ti.duration_frames) {
            uint32_t s = ti.start_frame, n = ti.duration_frames < 40 ? ti.duration_frames : 40;
            sc_output_t w = {.format = 2, .path = wav, .tc_start = s, .tc_end = s + n, .head_frame = -1};
            memset(&a, 0, sizeof a);
            a.iso_path = image;
            a.sector_size = 2048;
            a.channels = (int)ai->channel_count;
            a.pcm_rate = 88200;
            a.threads = 2;
            a.toc_lsn = ai->toc_lsn;
            a.image_sectors = di->sectors;
            a.read_fn = read_mem;
            a.read_ctx = disc;
            a.seek_min_gap = 0;
            sacdconv_area_run(&a, &w, 1);
        }
    }
    unlink(dsf);
    unlink(dff);
    unlink(dst);
    unlink(wav);
    unlink(flac);
}

/* ------------------------------------------------------------ configured runs (#157) */

#define CFG_MAGIC "PSFZcfg1"
#define CFG_MAGIC_LEN 8

/* The run configuration: CFG_SIZE bytes after CFG_MAGIC. Every value is valid; the ranges are
   bounded here, whatever the bytes say. Multi-byte fields are little-endian. */
enum {
    CFG_OUTS,           /* outputs: OUT_* bits */
    CFG_FLAGS,          /* FLAG_* bits */
    CFG_THREADS,        /* sc_area_t.threads: 1 + v % MAX_THREADS */
    CFG_CANCEL,         /* bits 7-6: CANCEL_*; bits 5-0: when (log messages, or 100 us steps) */
    CFG_FLAC,           /* bits 3-0: FLAC level (v % 9); bit 4: a PADDING block */
    CFG_LEAD,           /* DSF chain: frames of the unwritten lead range (0: none, v % 128) */
    CFG_HEAD,           /* DSF chain: head_frame = first written frame - 1 - (v % 4); 0xFF: none */
    CFG_GAP,            /* seek_min_gap in sectors; 0xFF: the default (-1) */
    CFG_START,          /* 2 bytes: the first frame, v % (play time + 256) */
    CFG_LEN = CFG_START + 2,    /* 2 bytes: frames per output, 1 + v % MAX_RANGE */
    CFG_SHIFT = CFG_LEN + 2,    /* 7 bytes, one per output: its start's shift from CFG_START, v % 64 */
    CFG_SPLIT = CFG_SHIFT + 7,  /* DSF chain: where its first written file ends */
    CFG_CHECK,          /* 2 bytes: check_frame (FLAG_CHECK) */
    CFG_GAIN = CFG_CHECK + 2,   /* gain_db, a signed byte, in quarter dB */
    CFG_MISUSE,         /* MISUSE_* bits: arguments the engine must refuse */
    CFG_SIZE
};
_Static_assert(CFG_SIZE == 24, "fuzz/make_seeds.py writes 24-byte configurations");
enum { OUT_DSF = 1, OUT_FLAC = 2, OUT_WAV = 4, OUT_DFF = 8, OUT_DST = 16, OUT_CHAIN = 32, OUT_WAV2 = 64 };
enum {
    FLAG_176K = 1,      /* PCM at 176.4 kHz, else 88.2 kHz */
    FLAG_FILLER = 2,    /* allow_filler */
    FLAG_COPY = 4,      /* iso_out_path: copy the image first */
    FLAG_SPECULATIVE = 8,   /* exact = 0: a range past frame 0 starts provisionally */
    FLAG_SOURCE = 16,   /* read through a sector source, not the file */
    FLAG_LOG = 32,      /* a log callback at debug level */
    FLAG_CHECK = 64,    /* the #93 silence check on CFG_CHECK */
    FLAG_DATA_OFFSET = 128  /* a nonzero data_offset (refused with a sector source) */
};
enum { CANCEL_NONE, CANCEL_LOG, CANCEL_THREAD, CANCEL_BEFORE };
enum {
    MISUSE_EMPTY = 1,   /* the first output's range is empty */
    MISUSE_FORMAT = 2,  /* the first output's format is unknown */
    MISUSE_RATE = 4,    /* a PCM rate of 44.1 kHz */
    MISUSE_CHANNELS = 8,    /* one channel more than the area has */
    MISUSE_TOC = 16     /* no area has its TOC at toc_lsn */
};

typedef struct {
    sc_area_t   *a;
    int          cancel_at;     /* CANCEL_LOG: cancel at this message; else -1 */
    _Atomic int  messages;
} log_ctx_t;

/* the log callback: may run on any of the engine's threads, several at once */
static void log_sink(void *ctx, int level, const char *msg)
{
    log_ctx_t *l = ctx;
    volatile size_t n = strlen(msg);    /* the message is a terminated string */
    (void)n;
    (void)level;
    if (atomic_fetch_add(&l->messages, 1) + 1 == l->cancel_at)
        sacdconv_cancel(l->a);
}

/* CANCEL_THREAD: cancel the run from another thread after a delay, unless it has ended */
typedef struct {
    sc_area_t      *a;
    long            delay_us;
    pthread_mutex_t mu;
    pthread_cond_t  cv;
    int             done;
} canceller_t;

static void *cancel_main(void *arg)
{
    canceller_t *c = arg;
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    t.tv_nsec += c->delay_us * 1000;
    t.tv_sec += t.tv_nsec / 1000000000;
    t.tv_nsec %= 1000000000;
    pthread_mutex_lock(&c->mu);
    int rc = 0;
    while (!c->done && rc != ETIMEDOUT)
        rc = pthread_cond_timedwait(&c->cv, &c->mu, &t);
    int done = c->done;
    pthread_mutex_unlock(&c->mu);
    if (!done)
        sacdconv_cancel(c->a);
    return NULL;
}

static uint32_t le16(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8;
}

static void run_area_cfg(const uint8_t *cfg, const char *image, SbDisc *disc, const SbDiscInfo *di,
                         const SbAreaInfo *ai)
{
    if (ai->channel_count < 1 || ai->channel_count > 6)
        return;
    int mask = cfg[CFG_OUTS], flags = cfg[CFG_FLAGS];
    uint32_t start = (uint32_t)(le16(cfg + CFG_START) % (ai->playtime_frames + UINT64_C(256)));
    uint32_t len = 1 + le16(cfg + CFG_LEN) % MAX_RANGE;
    static const char *names[] = {"a.dsf", "a.flac", "a.wav", "a.dff", "d.dff", "c.dsf", "b.wav"};
    static const int formats[] = {0, 1, 2, 3, 4, 0, 2};
    char paths[7 + CHAIN_FILES][300], copy[300];
    sc_output_t outs[7 + CHAIN_FILES];
    SbOutputLayout *layouts[1 + CHAIN_FILES] = {0};
    int nouts = 0, nlayouts = 0;
    static uint64_t offs[MAX_RANGE];
    static uint32_t lens[MAX_RANGE];
    /* stand-ins for scarletbook's DSDIFF header and trailing chunks: the engine copies them */
    static const uint8_t header[16] = {'F', 'R', 'M', '8', [12] = 'D', 'S', 'D', ' '};
    static const uint8_t footer[12] = {'D', 'I', 'I', 'N'};
    memset(outs, 0, sizeof outs);

    for (int k = 0; k < 7; k++) {
        if (!(mask & 1 << k) || k == 5)
            continue;
        if (k == 1 && !sacdconv_has_flac())
            continue;
        if (k == 4 && ai->frame_format != 0)
            continue;   /* DSDIFF with the frames as stored: DST areas only */
        uint32_t s = start + cfg[CFG_SHIFT + k] % 64;
        sc_output_t *o = &outs[nouts];
        out_path(paths[nouts], sizeof paths[nouts], names[k]);
        *o = (sc_output_t){.format = formats[k], .path = paths[nouts], .tc_start = s, .tc_end = s + len,
                           .head_frame = -1};
        if (k == 0) {
            if (!(o->layout = layouts[nlayouts] = dsf_layout(disc, ai, s, s + len, -1, -1)))
                continue;   /* scarletbook refuses the range: pysacd doesn't write the file */
            nlayouts++;
        } else if (k == 1) {
            o->flac_level = cfg[CFG_FLAC] % 9;
            o->flac_padding = cfg[CFG_FLAC] & 16 ? 1024 : 0;
        } else if (k == 3) {
            o->header = header;
            o->header_len = sizeof header;
            o->footer = footer;
            o->footer_len = sizeof footer;
        } else if (k == 4) {
            o->header = header;
            o->header_len = sizeof header;
            o->dst_offsets = offs;
            o->dst_lengths = lens;
            o->dst_cap = MAX_RANGE;
        }
        nouts++;
    }

    /* a gapless DSF chain (-z) as pysacd-extract plans one: [s, m), [m, e) written, after a lead
       range [s - lead, s) that isn't, whose last frame the head frame stands in for */
    if (mask & OUT_CHAIN) {
        uint32_t s = start + cfg[CFG_SHIFT + 5] % 64, e = s + len, lead = cfg[CFG_LEAD] % 128;
        uint32_t m = len > 1 ? s + 1 + cfg[CFG_SPLIT] % (len - 1) : e;
        uint32_t bounds[4];
        size_t nb = 0, got = 0;
        if (lead && lead <= s)
            bounds[nb++] = s - lead;
        else
            lead = 0;
        bounds[nb++] = s;
        if (m < e)
            bounds[nb++] = m;
        bounds[nb++] = e;
        SbDsfChainFile files[CHAIN_FILES];
        if (sb_output_dsf_chain_plan(disc, ai->slot, bounds, nb, files, CHAIN_FILES, &got) == SB_STATUS_OK)
            for (size_t i = lead ? 1 : 0; i < got; i++) {
                if (files[i].status != SB_STATUS_OK)
                    continue;
                SbOutputLayout *l = dsf_layout(disc, ai, files[i].first, files[i].end, files[i].chain_first,
                                               files[i].chain_end);
                if (!l)
                    continue;
                layouts[nlayouts++] = l;
                char name[16];
                snprintf(name, sizeof name, "c%zu.dsf", i);
                out_path(paths[nouts], sizeof paths[nouts], name);
                int32_t head = -1;
                if (lead && i == 1 && cfg[CFG_HEAD] != 0xFF && files[i].first >= 1u + cfg[CFG_HEAD] % 4)
                    head = (int32_t)(files[i].first - 1 - cfg[CFG_HEAD] % 4);
                outs[nouts] = (sc_output_t){.format = 0, .path = paths[nouts], .tc_start = files[i].first,
                                            .tc_end = files[i].end, .head_frame = head, .layout = l};
                nouts++;
            }
    }

    sc_area_t a;
    memset(&a, 0, sizeof a);
    a.iso_path = image;
    a.sector_size = (int)di->sector_size;
    a.data_offset = flags & FLAG_DATA_OFFSET ? 16 : 0;
    a.channels = (int)ai->channel_count;
    a.pcm_rate = flags & FLAG_176K ? 176400 : 88200;
    a.gain_db = (int8_t)cfg[CFG_GAIN] / 4.0;
    a.threads = 1 + cfg[CFG_THREADS] % MAX_THREADS;
    a.toc_lsn = ai->toc_lsn;
    a.image_sectors = di->sectors;
    a.exact = !(flags & FLAG_SPECULATIVE);
    a.seek_min_gap = cfg[CFG_GAP] == 0xFF ? -1 : cfg[CFG_GAP];
    a.allow_filler = !!(flags & FLAG_FILLER);
    a.check_frame = flags & FLAG_CHECK ? le16(cfg + CFG_CHECK) : UINT32_MAX;
    if (flags & FLAG_SOURCE) {
        a.sector_size = 2048;
        a.read_fn = read_mem;
        a.read_ctx = disc;
    }
    int misuse = cfg[CFG_MISUSE];
    if (nouts && (misuse & MISUSE_EMPTY))
        outs[0].tc_end = outs[0].tc_start;
    if (nouts && (misuse & MISUSE_FORMAT))
        outs[0].format = 5;
    if (misuse & MISUSE_RATE)
        a.pcm_rate = 44100;
    if (misuse & MISUSE_CHANNELS)
        a.channels++;
    if (misuse & MISUSE_TOC)
        a.toc_lsn++;
    if (flags & FLAG_COPY) {
        out_path(copy, sizeof copy, "copy.iso");
        a.iso_out_path = copy;
    }
    int mode = cfg[CFG_CANCEL] >> 6, when = cfg[CFG_CANCEL] & 63;
    log_ctx_t lc = {.a = &a, .cancel_at = mode == CANCEL_LOG ? when + 1 : -1};
    if ((flags & FLAG_LOG) || mode == CANCEL_LOG) {
        a.log_fn = log_sink;
        a.log_ctx = &lc;
        a.log_level = 10;   /* SC_DEBUG: every message */
    }
    if (mode == CANCEL_BEFORE)
        sacdconv_cancel(&a);

    canceller_t c = {.a = &a, .delay_us = (long)when * 100};
    pthread_t t;
    int threaded = 0;
    if (mode == CANCEL_THREAD) {
        pthread_mutex_init(&c.mu, NULL);
        pthread_cond_init(&c.cv, NULL);
        threaded = pthread_create(&t, NULL, cancel_main, &c) == 0;
    }
    sacdconv_area_run(&a, outs, nouts);
    if (threaded) {
        pthread_mutex_lock(&c.mu);
        c.done = 1;
        pthread_cond_signal(&c.cv);
        pthread_mutex_unlock(&c.mu);
        pthread_join(t, NULL);
    }
    if (mode == CANCEL_THREAD) {
        pthread_mutex_destroy(&c.mu);
        pthread_cond_destroy(&c.cv);
    }
    for (int i = 0; i < nlayouts; i++)
        sb_output_layout_free(layouts[i]);
    for (int i = 0; i < nouts; i++)
        unlink(paths[i]);
    if (flags & FLAG_COPY)
        unlink(copy);
}

/* ------------------------------------------------------------ entry */

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    if (!dir[0]) {
        const char *tmp = getenv("TMPDIR");
        snprintf(dir, sizeof dir, "%s/pysacd-fuzz-XXXXXX", tmp && *tmp && strlen(tmp) < 200 ? tmp : "/tmp");
        if (!mkdtemp(dir))
            abort();
        atexit(remove_dir);
    }
    const uint8_t *cfg = NULL;
    if (size >= CFG_MAGIC_LEN + CFG_SIZE && memcmp(data, CFG_MAGIC, CFG_MAGIC_LEN) == 0) {
        cfg = data + CFG_MAGIC_LEN;
        data += CFG_MAGIC_LEN + CFG_SIZE;
        size -= CFG_MAGIC_LEN + CFG_SIZE;
    }
    SbDisc *disc = NULL;
    if (sb_disc_open_memory(data, size, &disc) != SB_STATUS_OK)
        return 0;
    /* the engine opens the image by path, as pysacd does */
    char image[300];
    out_path(image, sizeof image, "in.iso");
    FILE *f = fopen(image, "wb");
    if (f) {
        fwrite(data, 1, size, f);
        fclose(f);
        SbDiscInfo di;
        if (sb_disc_info(disc, &di) == SB_STATUS_OK)
            for (uint32_t i = 0; i < di.area_count; i++) {
                SbAreaInfo ai;
                if (sb_disc_area(disc, i, &ai) != SB_STATUS_OK)
                    continue;
                if (cfg)
                    run_area_cfg(cfg, image, disc, &di, &ai);
                else
                    run_area(image, disc, &di, &ai);
            }
        unlink(image);
    }
    sb_disc_free(disc);
    return 0;
}
