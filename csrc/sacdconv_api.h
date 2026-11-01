/*
 * The engine's interface (csrc/sacdconv.c): its two structs, which pysacd/_native.py mirrors
 * by hand and checks field by field when it loads the library (sacdconv_layout), and its
 * exported functions. Also used by fuzz/fuzz_engine.c.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#ifndef PYSACD_SACDCONV_API_H
#define PYSACD_SACDCONV_API_H

#include <stdint.h>

/* Must match pysacd/_native.py */
typedef struct {
    int             format;
    const char     *path;
    uint32_t        tc_start, tc_end;   /* frames [tc_start, tc_end) */
    int32_t         head_frame;         /* DSF nopad: file starts with the last */
    uint32_t        head_bytes;         /*   head_bytes/channel of this frame (-1: none) */
    uint32_t        tail_bytes;         /* DSF nopad: drop this many bytes/channel at the end */
    const uint8_t  *header;             /* DSF, DSDIFF: written at offset 0 (scarletbook's layout) */
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
    int             channels;
    int             pcm_rate;
    double          gain_db;
    int             threads;
    const char     *iso_out_path;       /* also copy the image here (NULL: no) */
    volatile int    cancel;
    /* progress (read by Python while running) */
    volatile uint64_t frames_done, frames_total;
    volatile uint64_t sectors_done, sectors_total;
    char            error[256];
    /* optional sector source instead of reading iso_path (e.g. a disc on a
       network server): fill buf with count 2048-byte sectors from lsn,
       return 0, or -1 with a message in error */
    int           (*read_fn)(void *ctx, uint32_t lsn, uint32_t count, uint8_t *buf);
    void           *read_ctx;
    /* optional diagnostics sink; messages below log_level aren't formatted */
    void          (*log_fn)(void *ctx, int level, const char *msg);
    void           *log_ctx;
    int             log_level;
    /* scarletbook (#85): the area by the LSN of its TOC, the image's 2048-byte sectors (for a
       sector source), and the cancellation token while a run reads (sacdconv_cancel) */
    uint32_t        toc_lsn;
    uint64_t        image_sectors;
    void * _Atomic  sb_cancel;
    /* range starts (#85): exact = 1 reads the area up to the range; 0 lets scarletbook start
       provisionally (SB_SEEK_POLICY_SPECULATIVE). Results: provisional (1 provisional, 2 confirmed
       by the background check), refuted_from (UINT32_MAX, or the frame from which a refuted
       provisional reading was exact) */
    int             exact;
    int             provisional;
    uint32_t        refuted_from;
    int64_t         seek_min_gap;       /* sectors (SB_SEEK_DEFAULT_MIN_GAP when negative) */
} sc_area_t;

int sacdconv_abi(void);
int sacdconv_layout(int which, uint32_t *out, int cap);
const char *sacdconv_build_info(void);
int sacdconv_has_flac(void);
void sacdconv_cancel(sc_area_t *a);
int sacdconv_area_run(sc_area_t *a, sc_output_t *outs, int nouts);

#endif
