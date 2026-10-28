/* Scarletbook audio sector demultiplexer (see demux.c). */
#ifndef PYSACD_DEMUX_H
#define PYSACD_DEMUX_H

#include <stdint.h>

#include "sacd.h"

/* Frame being reassembled from audio packets */
typedef struct {
    uint8_t  data[MAX_FRAME_SIZE];
    int      size;
    int      started;
    int      dst;
    int      sector_count;
    uint32_t tc;
} frame_acc_t;

/* Called for each complete frame; return 1 to stop scanning, -1 on error */
typedef int (*frame_cb)(void *ctx, uint32_t tc, const uint8_t *data, int size, int dst);

/* demux_sector() results besides 0 and the callback's own codes */
#define DEMUX_STOP     1    /* the callback asked to stop */
#define DEMUX_CORRUPT  2    /* malformed sector: some or all of it was dropped */

int frame_complete(const frame_acc_t *f, int channels);
/* Demux one 2048-byte audio sector; returns 0, DEMUX_CORRUPT, or the callback's
   stop (1) / error (-1) code */
int demux_sector(const uint8_t *s, frame_acc_t *f, int channels, frame_cb cb, void *ctx);

#endif
