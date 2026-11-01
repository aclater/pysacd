/*
 * libFuzzer harness for pysacd's engine (#85): an input is a disc image, which goes through
 * sacdconv_area_run() the way pysacd-extract and pysacd convert use it.
 *
 * scarletbook-rs parses the image (TOC, sectors, frames, DST) and is fuzzed in its own
 * repository; what this harness exercises is pysacd's side: the reader thread's staging of
 * scarletbook's positions, batches with PCM context, the workers (DSF layout, DSD to PCM), the
 * writers (DSF with and without padding, DSDIFF DSD, DSDIFF DST passthrough, WAV) and a sector
 * source. For each area scarletbook reports, the harness writes:
 *
 *   - the whole area to DSF, DSDIFF (decoded) and, for a DST area, DSDIFF with the frames as
 *     stored, all in one pass;
 *   - the area's last track to WAV, starting provisionally (seek gap 0), read through a sector
 *     source (sb_disc_read_sectors on the image in memory);
 *
 * capped at MAX_FRAMES frames so that one input stays fast. Outputs go to a private temporary
 * directory and are removed after each run.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "scarletbook.h"

#define MAX_FRAMES 600

/* sc_area_t and sc_output_t, as in csrc/sacdconv.c (the harness is built with it) */
#include "../csrc/sacdconv_api.h"

static char dir[64];

static void out_path(char *buf, size_t cap, const char *name)
{
    snprintf(buf, cap, "%s/%s", dir, name);
    unlink(buf);
}

/* the sector source: the image's 2048-byte logical sectors, read through scarletbook */
static int read_mem(void *ctx, uint32_t lsn, uint32_t count, uint8_t *buf)
{
    return sb_disc_read_sectors(ctx, lsn, count, buf, (size_t)count * 2048) == SB_STATUS_OK ? 0 : -1;
}

static void run_area(const char *image, SbDisc *disc, const SbDiscInfo *di, const SbAreaInfo *ai)
{
    if (ai->channel_count < 1 || ai->channel_count > 6 || ai->playtime_frames == 0)
        return;
    uint32_t end = ai->playtime_frames < MAX_FRAMES ? ai->playtime_frames : MAX_FRAMES;
    char dsf[96], dff[96], dst[96], wav[96];
    out_path(dsf, sizeof dsf, "a.dsf");
    out_path(dff, sizeof dff, "a.dff");
    out_path(dst, sizeof dst, "d.dff");
    out_path(wav, sizeof wav, "t.wav");

    /* the whole area (capped), one pass, from the image file */
    sc_output_t outs[3];
    memset(outs, 0, sizeof outs);
    outs[0] = (sc_output_t){.format = 0, .path = dsf, .tc_start = 0, .tc_end = end, .head_frame = -1};
    outs[1] = (sc_output_t){.format = 3, .path = dff, .tc_start = 0, .tc_end = end, .head_frame = -1};
    static uint64_t offs[MAX_FRAMES];
    static uint32_t lens[MAX_FRAMES];
    outs[2] = (sc_output_t){.format = 4, .path = dst, .tc_start = 0, .tc_end = end, .head_frame = -1,
                            .dst_offsets = offs, .dst_lengths = lens, .dst_cap = MAX_FRAMES};
    int nouts = ai->frame_format == 0 ? 3 : 2;
    sc_area_t a;
    memset(&a, 0, sizeof a);
    a.iso_path = image;
    a.sector_size = (int)di->sector_size;
    a.channels = (int)ai->channel_count;
    a.threads = 2;
    a.toc_lsn = ai->toc_lsn;
    a.image_sectors = di->sectors;
    a.exact = 1;
    a.seek_min_gap = -1;
    sacdconv_area_run(&a, outs, nouts);

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
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    if (!dir[0]) {
        snprintf(dir, sizeof dir, "/tmp/pysacd-fuzz-XXXXXX");
        if (!mkdtemp(dir))
            abort();
    }
    SbDisc *disc = NULL;
    if (sb_disc_open_memory(data, size, &disc) != SB_STATUS_OK)
        return 0;
    /* the engine opens the image by path, as pysacd does */
    char image[96];
    out_path(image, sizeof image, "in.iso");
    FILE *f = fopen(image, "wb");
    if (f) {
        fwrite(data, 1, size, f);
        fclose(f);
        SbDiscInfo di;
        if (sb_disc_info(disc, &di) == SB_STATUS_OK)
            for (uint32_t i = 0; i < di.area_count; i++) {
                SbAreaInfo ai;
                if (sb_disc_area(disc, i, &ai) == SB_STATUS_OK)
                    run_area(image, disc, &di, &ai);
            }
        unlink(image);
    }
    sb_disc_free(disc);
    return 0;
}
