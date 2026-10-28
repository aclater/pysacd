/* Shared Scarletbook constants for the native helper. */
#ifndef PYSACD_SACD_H
#define PYSACD_SACD_H

#define LSN_SIZE            2048
#define FRAME_BYTES_PER_CH  4704            /* 588 samples * 64 / 8 */
#define FRAME_RATE          75
#define MAX_CHANNELS_SACD   6
#define MAX_FRAME_SIZE      (64 * 1024)
#define DSD_SILENCE         0x69

#endif
