/* DSD64 -> PCM conversion (see dsp.c). */
#ifndef PYSACD_DSP_H
#define PYSACD_DSP_H

#include <stdint.h>

#include "sacd.h"

#define S1_BYTES 12
#define S1_TAPS  (S1_BYTES * 8)
#define S2_TAPS  95
#define S3_TAPS  161

typedef struct {
    int    ntaps;
    float *h;
    float *buf;     /* ntaps-1 history followed by new input */
    int    cap;
} fir2_t;

typedef struct {
    int      channels;
    int      stages;                    /* 2 (176.4k) or 3 (88.2k) */
    float    s1_lut[S1_BYTES][256];
    uint8_t  s1_hist[MAX_CHANNELS_SACD][S1_BYTES - 1 + FRAME_BYTES_PER_CH];
    fir2_t   s2[MAX_CHANNELS_SACD], s3[MAX_CHANNELS_SACD];
    float    t1[FRAME_BYTES_PER_CH], t2[FRAME_BYTES_PER_CH / 2];
    float    out[MAX_CHANNELS_SACD][FRAME_BYTES_PER_CH / 2];
    int      out_per_frame;
    int      delay;                     /* group delay in output samples */
} dsd2pcm_t;

/* rate: 88200 or 176400 */
int  dsd2pcm_init(dsd2pcm_t *d, int channels, int rate);
void dsd2pcm_free(dsd2pcm_t *d);
void dsd2pcm_reset(dsd2pcm_t *d);
/* One SACD frame (channel-interleaved MSB-first bytes) -> out[c][0..out_per_frame) */
void dsd2pcm_frame(dsd2pcm_t *d, const uint8_t *frame);

#endif
