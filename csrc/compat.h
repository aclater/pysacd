/*
 * Portability shims (#8): POSIX on Linux and macOS, Win32 under MinGW-w64.
 *
 * Kept to what the engine actually needs: output files opened by UTF-8 path
 * and the CPU count (scarletbook-rs reads the image, #85). Threads stay
 * pthreads (winpthreads on Windows, linked statically).
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 * (LGPL-2.1-or-later until pysacd 0.2, so that the FFmpeg-derived DST decoder could include it.)
 */
#ifndef PYSACD_COMPAT_H
#define PYSACD_COMPAT_H

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

/* printf-style checking: MinGW's plain "printf" means Microsoft's rules (no %zu) */
#if defined(__MINGW32__)
#define SC_PRINTF gnu_printf
#else
#define SC_PRINTF printf
#endif

#ifdef _WIN32

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#include <sys/stat.h>

/* Paths arrive as UTF-8 (Python's os.fsencode on Windows). Caller frees. */
static inline wchar_t *sc_widen(const char *utf8)
{
    int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, utf8, -1, NULL, 0);
    wchar_t *w = n > 0 ? malloc(sizeof(wchar_t) * (size_t)n) : NULL;
    if (w && MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, utf8, -1, w, n) != n) {
        free(w);
        w = NULL;
    }
    if (!w)
        errno = EINVAL;
    return w;
}

/* A new output file (#14): fails if anything exists at `path`, a symlink included (Windows
 * has no O_NOFOLLOW, but _O_EXCL refuses any existing name). `update` also opens it for
 * reading, as libFLAC wants. */
static inline FILE *sc_fopen_new(const char *path, int update)
{
    wchar_t *w = sc_widen(path);
    if (!w)
        return NULL;
    int fd = _wopen(w, (update ? _O_RDWR : _O_WRONLY) | _O_CREAT | _O_EXCL | _O_BINARY | _O_NOINHERIT,
                    _S_IREAD | _S_IWRITE);
    free(w);
    if (fd < 0)
        return NULL;
    FILE *fp = _fdopen(fd, update ? "w+b" : "wb");
    if (!fp)
        _close(fd);
    return fp;
}

static inline int sc_ncpus(void)
{
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    return (int)si.dwNumberOfProcessors;
}

#define fseeko fseeko64

#else   /* POSIX */

#include <fcntl.h>
#include <unistd.h>

/* A new output file (#14, as pysacd.safeio): never an existing file or a symlink. Mode 0666
 * filtered by the umask, like any user data file (shared libraries rely on umask 002), and
 * never executable. `update` also opens it for reading, as
 * libFLAC wants. */
static inline FILE *sc_fopen_new(const char *path, int update)
{
    int fd = open(path, (update ? O_RDWR : O_WRONLY) | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0666);
    if (fd < 0)
        return NULL;
    FILE *fp = fdopen(fd, update ? "w+b" : "wb");
    if (!fp) {
        int err = errno;
        close(fd);
        unlink(path);
        errno = err;
    }
    return fp;
}

static inline int sc_ncpus(void)
{
    return (int)sysconf(_SC_NPROCESSORS_ONLN);
}

#endif
static inline FILE *sc_fopen_write(const char *path)
{
    return sc_fopen_new(path, 0);
}

static inline FILE *sc_fopen_update(const char *path)
{
    return sc_fopen_new(path, 1);
}

#endif
