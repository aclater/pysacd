/*
 * CET shadow stacks forced on, independent of glibc's policy (tests/test_pcm_dsd2pcm.py,
 * ShadowStacksForced): converts DSD to PCM through the engine library (with dsd2pcm). main()
 * turns them on with a raw arch_prctl, as the kernel's selftests do, and never returns: from then on every call and return in this
 * process, the engine's worker threads included (the kernel gives each new thread a shadow
 * stack), is checked by the CPU. A mismatch is a #CP fault: SIGSEGV with si_code SEGV_CPERR.
 *
 * usage: shstk_run on|off JOBS     run the jobs (one per line, below), with or without
 *        shstk_run on|off --bad    a deliberately wrong return (the control: faults when on)
 * JOBS lines: iso sector_size data_offset channels toc_lsn image_sectors check_frame rate filter
 *             cutoff threads frames out.wav
 * exit: 0 all jobs ran; 1 error; 3 #CP fault (SEGV_CPERR); 4 another SIGSEGV; 5 shstk not on
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "../../csrc/sacdconv_api.h"

#ifndef SEGV_CPERR
#define SEGV_CPERR 10
#endif
#define ARCH_SHSTK_ENABLE 0x5001
#define ARCH_SHSTK_SHSTK  (1ULL << 0)

static void on_segv(int sig, siginfo_t *si, void *uc)
{
    (void)sig; (void)uc;
    static const char cp[] = "SIGSEGV: SEGV_CPERR (shadow-stack violation)\n", other[] = "SIGSEGV: other\n";
    if (si->si_code == SEGV_CPERR) {
        write(2, cp, sizeof cp - 1);
        _exit(3);
    }
    write(2, other, sizeof other - 1);
    _exit(4);
}

static int shstk_on(const char *who)
{
    char line[256];
    FILE *f = fopen("/proc/thread-self/status", "r");
    int on = 0;
    while (f && fgets(line, sizeof line, f))
        if (!strncmp(line, "x86_Thread_features:", 20))
            on = strstr(line + 20, "shstk") != NULL;
    if (f)
        fclose(f);
    printf("%s: shstk %s\n", who, on ? "on" : "off");
    return on;
}

static void *thread_probe(void *arg)
{
    *(int *)arg = shstk_on("new thread");
    return NULL;
}

static __attribute__((noinline)) void bad_return(void)
{
    /* return to the label through a pushed address: the shadow stack holds main's call site */
    __asm__ volatile("lea 1f(%%rip), %%rax\n\tpush %%rax\n\tret\n1:" ::: "rax", "memory");
}

static int run_jobs(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) {
        perror(path);
        return 1;
    }
    char iso[4096], out[4096];
    int ss, doff, ch, rate, filt, cut, thr;
    unsigned long long toc, sectors, check, frames;
    int n = 0;
    while (fscanf(f, "%4095s %d %d %d %llu %llu %llu %d %d %d %d %llu %4095s", iso, &ss, &doff, &ch, &toc,
                  &sectors, &check, &rate, &filt, &cut, &thr, &frames, out) == 13) {
        sc_output_t o;
        memset(&o, 0, sizeof o);
        o.format = 2;       /* wav */
        o.path = out;
        o.tc_start = 0;
        o.tc_end = (uint32_t)frames;
        o.head_frame = -1;
        sc_area_t a;
        memset(&a, 0, sizeof a);
        a.iso_path = iso;
        a.sector_size = ss;
        a.data_offset = doff;
        a.channels = ch;
        a.toc_lsn = (uint32_t)toc;
        a.image_sectors = sectors;
        a.check_frame = (uint32_t)check;
        a.exact = 1;
        a.seek_min_gap = -1;
        a.pcm_rate = rate;
        a.pcm_filter = filt;
        a.pcm_cutoff = cut;
        a.threads = thr;
        if (sacdconv_area_run(&a, &o, 1) != 0) {
            fprintf(stderr, "job %d: %s / %s\n", n, a.error, o.error);
            fclose(f);
            return 1;
        }
        n++;
    }
    fclose(f);
    printf("jobs: %d\n", n);
    return n ? 0 : 1;
}

int main(int argc, char **argv)
{
    if (argc != 3 || (strcmp(argv[1], "on") && strcmp(argv[1], "off"))) {
        fprintf(stderr, "usage: shstk_run on|off JOBS|--bad\n");
        return 1;
    }
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = on_segv;
    sa.sa_flags = SA_SIGINFO;
    sigaction(SIGSEGV, &sa, NULL);
    setvbuf(stdout, NULL, _IOLBF, 0);
    printf("engine: %s\n", sacdconv_build_info());

    int want = !strcmp(argv[1], "on");
    if (want) {
        long rc;
        /* inline, not through a libc call: a function that turned them on could not return */
        __asm__ volatile("syscall" : "=a"(rc) : "0"(158L /* SYS_arch_prctl */), "D"((long)ARCH_SHSTK_ENABLE),
                         "S"((long)ARCH_SHSTK_SHSTK) : "rcx", "r11", "memory");
        if (rc) {
            fprintf(stderr, "arch_prctl(ARCH_SHSTK_ENABLE): %ld\n", rc);
            _exit(5);
        }
    }
    int main_on = shstk_on("main thread"), thr_on = 0;
    pthread_t t;
    if (pthread_create(&t, NULL, thread_probe, &thr_on) || pthread_join(t, NULL))
        _exit(1);
    if (want && !(main_on && thr_on))
        _exit(5);
    int rc;
    if (!strcmp(argv[2], "--bad")) {
        bad_return();
        printf("bad return went through\n");
        rc = 0;
    } else {
        rc = run_jobs(argv[2]);
    }
    fflush(NULL);
    _exit(rc);      /* never return past the point where shadow stacks went on */
}
