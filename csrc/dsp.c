/*
 * DSD64 -> PCM conversion: three linear-phase Kaiser-windowed FIR stages.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "dsp.h"

/*
 * Three linear-phase Kaiser-windowed FIR stages:
 *   1. 2.8224 MHz -> 352.8 kHz (/8), byte-wise lookup tables
 *   2. 352.8 kHz  -> 176.4 kHz (/2), pass <60 kHz, stop >88.2 kHz
 *   3. 176.4 kHz  ->  88.2 kHz (/2), pass <40 kHz, stop >48 kHz (88.2k output only)
 * Stage 1 must keep everything stage 2 passes from aliasing: stop at
 * 352.8 - 88.2 = 264.6 kHz.  All stages reach ~105-110 dB stopband.
 */

static double bessel_i0(double x)
{
    double sum = 1, term = 1;
    for (int k = 1; k < 50; k++) {
        term *= (x / (2 * k)) * (x / (2 * k));
        sum += term;
        if (term < 1e-12 * sum)
            break;
    }
    return sum;
}

/* fc: cutoff in cycles/sample; normalized to unity DC gain */
static void kaiser_lowpass(double *h, int n, double fc, double atten_db)
{
    double beta = 0.1102 * (atten_db - 8.7), m = (n - 1) / 2.0, sum = 0;
    for (int i = 0; i < n; i++) {
        double x = i - m;
        double s = x == 0 ? 2 * fc : sin(2 * M_PI * fc * x) / (M_PI * x);
        double r = x / m;
        h[i] = s * bessel_i0(beta * sqrt(1 - r * r)) / bessel_i0(beta);
        sum += h[i];
    }
    for (int i = 0; i < n; i++)
        h[i] /= sum;
}


static int fir2_init(fir2_t *f, int ntaps, double fc, int max_in)
{
    double tmp[S3_TAPS];
    kaiser_lowpass(tmp, ntaps, fc, 110);
    f->ntaps = ntaps;
    f->cap = ntaps - 1 + max_in;
    f->h = malloc(sizeof(float) * ntaps);
    f->buf = calloc(f->cap, sizeof(float));
    if (!f->h || !f->buf)
        return -1;
    for (int i = 0; i < ntaps; i++)
        f->h[i] = (float)tmp[i];
    return 0;
}

static void fir2_free(fir2_t *f)
{
    free(f->h);
    free(f->buf);
}

/* n even; writes n/2 outputs */
static void fir2_run(fir2_t *f, const float *in, int n, float *out)
{
    int hist = f->ntaps - 1;
    memcpy(f->buf + hist, in, sizeof(float) * n);
    for (int o = 0; o < n / 2; o++) {
        const float *x = f->buf + 2 * o + 1;
        float acc = 0;
        for (int k = 0; k < f->ntaps; k++)
            acc += f->h[k] * x[k];
        out[o] = acc;
    }
    memmove(f->buf, f->buf + n, sizeof(float) * hist);
}


int dsd2pcm_init(dsd2pcm_t *d, int channels, int rate)
{
    double h1[S1_TAPS];
    memset(d, 0, sizeof(*d));
    d->channels = channels;
    d->stages = rate == 88200 ? 3 : 2;
    d->out_per_frame = rate / FRAME_RATE;

    /* stage 1: pass 60k, stop 264.6k at fs 2822.4k -> centre 162.3k */
    kaiser_lowpass(h1, S1_TAPS, 162300.0 / 2822400.0, 110);
    /* lut[j][v]: contribution of byte v at window position j (oldest first),
       bits MSB first, 1 -> +1, 0 -> -1 */
    for (int j = 0; j < S1_BYTES; j++)
        for (int v = 0; v < 256; v++) {
            double acc = 0;
            for (int i = 0; i < 8; i++)
                acc += h1[j * 8 + i] * ((v >> (7 - i)) & 1 ? 1.0 : -1.0);
            d->s1_lut[j][v] = (float)acc;
        }

    for (int c = 0; c < channels; c++) {
        memset(d->s1_hist[c], DSD_SILENCE, S1_BYTES - 1);
        /* stage 2: pass 60k, stop 88.2k at fs 352.8k */
        if (fir2_init(&d->s2[c], S2_TAPS, 74100.0 / 352800.0, FRAME_BYTES_PER_CH) < 0)
            return -1;
        /* stage 3: pass 40k, stop 48k at fs 176.4k */
        if (d->stages == 3 &&
            fir2_init(&d->s3[c], S3_TAPS, 44000.0 / 176400.0, FRAME_BYTES_PER_CH / 2) < 0)
            return -1;
    }

    /* Group delay: stage 1 output for byte t is centred (S1_TAPS-1)/2 - 7 bits
       before the end of that byte; each /2 stage (window ending at input 2o+1)
       adds (taps-1)/2 - 1 input samples and halves. */
    double delay = ((S1_TAPS - 1) / 2.0 - 7) / 8;
    delay = (delay + (S2_TAPS - 1) / 2.0 - 1) / 2;
    if (d->stages == 3)
        delay = (delay + (S3_TAPS - 1) / 2.0 - 1) / 2;
    d->delay = (int)lround(delay);
    return 0;
}

void dsd2pcm_free(dsd2pcm_t *d)
{
    for (int c = 0; c < d->channels; c++) {
        fir2_free(&d->s2[c]);
        if (d->stages == 3)
            fir2_free(&d->s3[c]);
    }
}

/* One SACD frame (interleaved MSB-first bytes) -> out[c][0..out_per_frame) */
void dsd2pcm_frame(dsd2pcm_t *d, const uint8_t *frame)
{
    int ch = d->channels;
    for (int c = 0; c < ch; c++) {
        uint8_t *hb = d->s1_hist[c];
        for (int i = 0; i < FRAME_BYTES_PER_CH; i++)
            hb[S1_BYTES - 1 + i] = frame[i * ch + c];
        for (int t = 0; t < FRAME_BYTES_PER_CH; t++) {
            float acc = 0;
            for (int j = 0; j < S1_BYTES; j++)
                acc += d->s1_lut[j][hb[t + j]];
            d->t1[t] = acc;
        }
        memmove(hb, hb + FRAME_BYTES_PER_CH, S1_BYTES - 1);

        if (d->stages == 3) {
            fir2_run(&d->s2[c], d->t1, FRAME_BYTES_PER_CH, d->t2);
            fir2_run(&d->s3[c], d->t2, FRAME_BYTES_PER_CH / 2, d->out[c]);
        } else {
            fir2_run(&d->s2[c], d->t1, FRAME_BYTES_PER_CH, d->out[c]);
        }
    }
}

/* Restore the power-on filter state (DSD silence in, zeros in the FIRs) */
void dsd2pcm_reset(dsd2pcm_t *d)
{
    for (int c = 0; c < d->channels; c++) {
        memset(d->s1_hist[c], DSD_SILENCE, S1_BYTES - 1);
        memset(d->s2[c].buf, 0, sizeof(float) * (d->s2[c].ntaps - 1));
        if (d->stages == 3)
            memset(d->s3[c].buf, 0, sizeof(float) * (d->s3[c].ntaps - 1));
    }
}
