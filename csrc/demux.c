/*
 * Scarletbook audio sector demultiplexer (after sacd-ripper's
 * scarletbook_process_frames()).
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <string.h>

#include "demux.h"



int frame_complete(const frame_acc_t *f, int channels)
{
    return f->size > 0 &&
           ((f->dst && f->sector_count == 0) ||
            (!f->dst && f->size == channels * FRAME_BYTES_PER_CH));
}

/* returns 1 to stop scanning, 0 to continue, -1 on error */
int demux_sector(const uint8_t *s, frame_acc_t *f, int channels, frame_cb cb, void *ctx)
{
    const uint8_t *p = s, *end = s + LSN_SIZE;
    uint8_t hdr = *p++;
    int dst = hdr & 1;
    int nframes = (hdr >> 2) & 7;
    int npackets = (hdr >> 5) & 7;
    struct { int start, type, len; } pk[7];
    uint32_t ftc[7];
    int fsec[7];

    for (int i = 0; i < npackets; i++, p += 2) {
        pk[i].start = p[0] >> 7;
        pk[i].type = (p[0] >> 3) & 7;
        pk[i].len = (p[0] & 7) << 8 | p[1];
    }
    for (int i = 0; i < nframes; i++) {
        ftc[i] = (uint32_t)p[0] * 60 * FRAME_RATE + p[1] * FRAME_RATE + p[2];
        fsec[i] = dst ? (p[3] >> 2) & 0x1f : 0;
        p += dst ? 4 : 3;
    }

    int fi = 0;
    for (int i = 0; i < npackets; i++) {
        if (p + pk[i].len > end)
            return 0;   /* corrupt sector: drop the rest of it */
        if (pk[i].type == 2) {
            if (pk[i].start) {
                if (f->started && frame_complete(f, channels)) {
                    int r = cb(ctx, f->tc, f->data, f->size, f->dst);
                    if (r)
                        return r;
                }
                f->started = 0;
                if (fi < nframes) {
                    f->started = 1;
                    f->size = 0;
                    f->dst = dst;
                    f->sector_count = fsec[fi];
                    f->tc = ftc[fi];
                    fi++;
                }
            }
            if (f->started) {
                if (f->size + pk[i].len <= MAX_FRAME_SIZE) {
                    memcpy(f->data + f->size, p, pk[i].len);
                    f->size += pk[i].len;
                    if (f->dst)
                        f->sector_count--;
                } else {
                    f->started = 0;
                }
            }
        }
        p += pk[i].len;
    }
    return 0;
}
