/*
 * libFuzzer harness: one or two DST frames through the decoder (#36).
 *
 * Input: byte 0 picks the channel count (2, 5 or 6); the rest is the frame.
 * The same frame is decoded twice with one decoder, so state carried between
 * frames is exercised too.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

#include "../csrc/dstdec.h"
#include "../csrc/sacd.h"

static const int channel_counts[] = {2, 5, 6};

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    if (size < 1 || size - 1 > MAX_FRAME_SIZE)
        return 0;
    int ch = channel_counts[data[0] % 3];
    dst_decoder_t *dec = dst_decoder_new(ch);
    uint8_t *out = malloc((size_t)ch * FRAME_BYTES_PER_CH);
    if (dec && out)
        for (int i = 0; i < 2; i++)
            dst_decode_frame(dec, data + 1, (int)(size - 1), out);
    free(out);
    dst_decoder_free(dec);
    return 0;
}
