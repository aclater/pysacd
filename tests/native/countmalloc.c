/* SPDX-License-Identifier: GPL-2.0-or-later
 * Counting malloc shim for the memory gate (lab standard: an exact in-process referee).
 * LD_PRELOAD it into the interpreter: every allocation through malloc, calloc, realloc,
 * memalign, posix_memalign and aligned_alloc is counted at its usable size, so the live
 * byte count is exact for the C level, including buffers handed across the ctypes boundary.
 * cm_live() returns the live bytes, cm_count() the live blocks. glibc only (uses __libc_*). */
#define _GNU_SOURCE
#include <errno.h>
#include <malloc.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>

extern void *__libc_malloc(size_t);
extern void *__libc_calloc(size_t, size_t);
extern void *__libc_realloc(void *, size_t);
extern void *__libc_memalign(size_t, size_t);
extern void __libc_free(void *);

#include <execinfo.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Optional call-site table (investigation, not the gate): cm_site_start() begins recording the
 * caller's backtrace for each new block; cm_site_dump() prints, per distinct site, the blocks and
 * bytes recorded and still live. Off by default; the counters above never depend on it. */
#define SITES 1u << 20
typedef struct { void *p; size_t n; void *bt[6]; } site_t;
static site_t *sites; static int recording; static __thread int busy;
static pthread_mutex_t site_mu = PTHREAD_MUTEX_INITIALIZER;

static atomic_llong live_bytes;
static atomic_llong live_blocks;
static atomic_llong peak_bytes;   /* high-water mark of live_bytes since the last cm_peak_reset() */

static void raise_peak(long long now) {
    long long p = atomic_load(&peak_bytes);
    while (now > p && !atomic_compare_exchange_weak(&peak_bytes, &p, now)) {
    }
}

static void site_add(void *p) {
    if (busy) return;
    busy = 1;
    void *bt[8]; int d = backtrace(bt, 8);
    pthread_mutex_lock(&site_mu);
    unsigned h = (unsigned)(((uintptr_t)p >> 4) * 2654435761u) & ((SITES) - 1);
    for (unsigned i = 0; i < (SITES); i++) {
        site_t *e = &sites[(h + i) & ((SITES) - 1)];
        if (!e->p || e->p == (void *)1) { e->p = p; e->n = malloc_usable_size(p); memset(e->bt, 0, sizeof e->bt);
            for (int k = 2; k < d && k < 8; k++) e->bt[k - 2] = bt[k];
            break; }
    }
    pthread_mutex_unlock(&site_mu);
    busy = 0;
}
static void site_del(void *p) {
    if (!recording || busy) return;
    busy = 1;
    pthread_mutex_lock(&site_mu);
    unsigned h = (unsigned)(((uintptr_t)p >> 4) * 2654435761u) & ((SITES) - 1);
    for (unsigned i = 0; i < (SITES); i++) {
        site_t *e = &sites[(h + i) & ((SITES) - 1)];
        if (!e->p) break;
        if (e->p == p) { e->p = (void *)1; break; }
    }
    pthread_mutex_unlock(&site_mu);
    busy = 0;
}
void cm_site_start(void) { sites = __libc_calloc(SITES, sizeof(site_t)); recording = 1; }
void cm_site_dump(const char *path) {
    FILE *f = fopen(path, "w");
    for (unsigned i = 0; sites && i < (SITES); i++)
        if (sites[i].p > (void *)1) {
            int d = 0; while (d < 6 && sites[i].bt[d]) d++;
            char **sym = backtrace_symbols(sites[i].bt, d);
            fprintf(f, "%zu", sites[i].n);
            for (int k = 0; k < d && sym; k++) fprintf(f, " | %s", sym[k]);
            fputc('\n', f);
            free(sym);
        }
    fclose(f);
}

static void *note(void *p) {
    if (p && recording) site_add(p);
    if (p) {
        long long n = (long long)malloc_usable_size(p);
        raise_peak(atomic_fetch_add(&live_bytes, n) + n);
        atomic_fetch_add(&live_blocks, 1);
    }
    return p;
}
long long cm_live(void) { return atomic_load(&live_bytes); }
long long cm_count(void) { return atomic_load(&live_blocks); }
long long cm_peak(void) { return atomic_load(&peak_bytes); }
void cm_peak_reset(void) { atomic_store(&peak_bytes, atomic_load(&live_bytes)); }

void *malloc(size_t n) { return note(__libc_malloc(n)); }
void *calloc(size_t a, size_t b) { return note(__libc_calloc(a, b)); }
void *memalign(size_t al, size_t n) { return note(__libc_memalign(al, n)); }
void *aligned_alloc(size_t al, size_t n) { return note(__libc_memalign(al, n)); }
int posix_memalign(void **out, size_t al, size_t n) {
    void *p = __libc_memalign(al, n);
    if (!p) return ENOMEM;
    *out = note(p);
    return 0;
}
void free(void *p) {
    if (!p) return;
    site_del(p);
    atomic_fetch_sub(&live_bytes, (long long)malloc_usable_size(p));
    atomic_fetch_sub(&live_blocks, 1);
    __libc_free(p);
}
void *realloc(void *p, size_t n) {
    if (!p) return malloc(n);
    if (!n) { free(p); return NULL; }
    long long old = (long long)malloc_usable_size(p);
    site_del(p);
    void *q = __libc_realloc(p, n);
    if (q && recording) site_add(q);
    if (q) {
        long long d = (long long)malloc_usable_size(q) - old;
        raise_peak(atomic_fetch_add(&live_bytes, d) + d);
    }
    return q;
}

/* CM_REPORT=1: print the live bytes and blocks at exit (the "bytes at exit" test) */
__attribute__((destructor)) static void cm_report(void) {
    if (getenv("CM_REPORT")) fprintf(stderr, "CM_AT_EXIT bytes=%lld blocks=%lld\n", cm_live(), cm_count());
}
