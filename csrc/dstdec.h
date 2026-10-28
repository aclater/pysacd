/* DST decoder, ported from FFmpeg and extended from docs/dst-bitstream.md
   (LGPL-2.1-or-later); see dstdec.c. */
#ifndef PYSACD_DSTDEC_H
#define PYSACD_DSTDEC_H

#include <stdint.h>

enum {
    DST_ERR_INVALID = -1,       /* corrupt frame */
    DST_ERR_UNSUPPORTED = -2,   /* kept for compatibility; no longer returned */
};

typedef struct dst_decoder dst_decoder_t;

dst_decoder_t *dst_decoder_new(int channels);
void dst_decoder_free(dst_decoder_t *s);
/* Decode one DST frame (DSD64) into channels*4704 bytes of channel-interleaved,
   MSB-first DSD. Returns 0 or a DST_ERR_* code. */
int dst_decode_frame(dst_decoder_t *s, const uint8_t *data, int size, uint8_t *dsd);

#endif
