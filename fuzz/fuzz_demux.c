/*
 * libFuzzer harness: audio sectors through the demuxer, and every frame it
 * reassembles through the DST decoder or the plain-DSD size check (#36).
 *
 * Input: byte 0 picks the channel count (2, 5 or 6); the rest is cut into
 * 2048-byte sectors (a short last sector is zero-padded), as the engine's
 * reader hands them over.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "../csrc/demux.h"
#include "../csrc/dstdec.h"
#include "../csrc/sacd.h"

static const int channel_counts[] = {2, 5, 6};

typedef struct {
    dst_decoder_t *dec;
    uint8_t       *out;
    int            frames;
} ctx_t;

static int on_frame(void *opaque, uint32_t tc, const uint8_t *data, int size, int dst)
{
    ctx_t *c = opaque;
    (void)tc;
    if (dst && c->dec)
        dst_decode_frame(c->dec, data, size, c->out);
    return ++c->frames >= 64;       /* bound the work per input */
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    if (size < 1 || size - 1 > 64 * LSN_SIZE)
        return 0;
    int ch = channel_counts[data[0] % 3];
    static frame_acc_t acc;         /* 64 KiB: keep it off the stack */
    uint8_t sector[LSN_SIZE];
    ctx_t c = {dst_decoder_new(ch), malloc((size_t)ch * FRAME_BYTES_PER_CH), 0};
    memset(&acc, 0, sizeof(acc));
    for (size_t off = 1; off < size; off += LSN_SIZE) {
        size_t n = size - off < LSN_SIZE ? size - off : LSN_SIZE;
        memset(sector, 0, sizeof(sector));
        memcpy(sector, data + off, n);
        int rc = demux_sector(sector, &acc, ch, on_frame, &c);
        if (rc == DEMUX_STOP || rc < 0)
            break;
    }
    if (acc.started && frame_complete(&acc, ch))
        on_frame(&c, acc.tc, acc.data, acc.size, acc.dst);
    free(c.out);
    dst_decoder_free(c.dec);
    return 0;
}
