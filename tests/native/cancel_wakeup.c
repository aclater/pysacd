/*
 * A cancel from another thread must wake the engine's threads that wait (tests/test_cancel_wakeup.py).
 *
 * Built with csrc/sacdconv.c and -DSC_TEST_HOOKS (`make build/cancel_wakeup`). The run's one
 * worker is held at the top of its loop before it takes any batch, so the writer waits for batch
 * 0 and the reader fills the pool and waits for a free batch. Once the writer waits, another
 * thread cancels the run, and only then is the worker let go: it sees the cancel and stops with
 * batches still queued. Unless the cancel itself wakes the writer and the reader, nothing else
 * will, and the run never returns. The run must return within the time limit.
 *
 * usage: cancel_wakeup IMAGE OUTDIR     exit 0: returned, cancelled; 1: error; 2: hung
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "scarletbook.h"
#include "../../csrc/sacdconv_api.h"

/* csrc/sacdconv.c, built with SC_TEST_HOOKS */
enum { SC_HOOK_WORKER_LOOP, SC_HOOK_WRITER_WAIT };
extern void (*sc_test_hook)(int point);

#define LIMIT_S 10

static atomic_int worker_held, writer_waits, released;

static void hook(int point)
{
    if (point == SC_HOOK_WRITER_WAIT) {
        atomic_store(&writer_waits, 1);
    } else if (point == SC_HOOK_WORKER_LOOP && !atomic_exchange(&worker_held, 1)) {
        while (!atomic_load(&released))     /* the first time only */
            usleep(1000);
    }
}

typedef struct {
    sc_area_t      *a;
    sc_output_t    *out;
    int             rc;
    pthread_mutex_t mu;
    pthread_cond_t  cv;
    int             done;
} run_t;

static void *run_main(void *arg)
{
    run_t *r = arg;
    int rc = sacdconv_area_run(r->a, r->out, 1);
    pthread_mutex_lock(&r->mu);
    r->rc = rc;
    r->done = 1;
    pthread_cond_signal(&r->cv);
    pthread_mutex_unlock(&r->mu);
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc != 3) {
        fprintf(stderr, "usage: cancel_wakeup IMAGE OUTDIR\n");
        return 1;
    }
    SbDisc *disc = NULL;
    SbDiscInfo di;
    SbAreaInfo ai;
    if (sb_disc_open_file(argv[1], &disc) != SB_STATUS_OK || sb_disc_info(disc, &di) != SB_STATUS_OK ||
        sb_disc_area(disc, 0, &ai) != SB_STATUS_OK) {
        fprintf(stderr, "cannot read %s\n", argv[1]);
        return 1;
    }
    sb_disc_free(disc);

    char path[4096];
    snprintf(path, sizeof path, "%s/out.dff", argv[2]);
    sc_output_t out = {.format = 3, .path = path, .tc_start = 0, .tc_end = ai.playtime_frames, .head_frame = -1};
    sc_area_t a;
    memset(&a, 0, sizeof a);
    a.iso_path = argv[1];
    a.sector_size = (int)di.sector_size;
    a.channels = (int)ai.channel_count;
    a.threads = 1;
    a.toc_lsn = ai.toc_lsn;
    a.image_sectors = di.sectors;
    a.exact = 1;
    a.seek_min_gap = -1;
    sc_test_hook = hook;

    run_t r = {.a = &a, .out = &out, .rc = 0};
    pthread_mutex_init(&r.mu, NULL);
    pthread_cond_init(&r.cv, NULL);
    pthread_t t;
    if (pthread_create(&t, NULL, run_main, &r) != 0)
        return 1;

    /* the worker is held and the writer waits for batch 0 (it decided to wait under the
       engine's mutex: the cancel below comes after that decision) */
    for (int i = 0; i < LIMIT_S * 1000 && !(atomic_load(&worker_held) && atomic_load(&writer_waits)); i++)
        usleep(1000);
    if (!atomic_load(&worker_held) || !atomic_load(&writer_waits)) {
        fprintf(stderr, "the run never reached the state under test (worker held %d, writer waits %d)\n",
                atomic_load(&worker_held), atomic_load(&writer_waits));
        atomic_store(&released, 1);
        return 1;
    }
    usleep(20000);      /* let the reader fill the pool and wait for a free batch */
    sacdconv_cancel(&a);
    atomic_store(&released, 1);

    struct timespec until;
    clock_gettime(CLOCK_REALTIME, &until);
    until.tv_sec += LIMIT_S;
    pthread_mutex_lock(&r.mu);
    int err = 0;
    while (!r.done && err != ETIMEDOUT)
        err = pthread_cond_timedwait(&r.cv, &r.mu, &until);
    int done = r.done, rc = r.rc;
    pthread_mutex_unlock(&r.mu);
    if (!done) {
        fprintf(stderr, "hung: the cancelled run did not return within %d s\n", LIMIT_S);
        fflush(stderr);
        _exit(2);       /* its threads still wait: don't join them */
    }
    pthread_join(t, NULL);
    unlink(path);
    if (rc != -1 || strcmp(a.error, "cancelled") != 0) {
        fprintf(stderr, "the run returned %d (%s), not cancelled\n", rc, a.error);
        return 1;
    }
    printf("cancelled run returned\n");
    return 0;
}
