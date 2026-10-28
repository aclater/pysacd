/*
 * Portability shims (#8): POSIX on Linux and macOS, Win32 under MinGW-w64.
 *
 * Kept to what the engine actually needs: positional reads of the image,
 * output files opened by UTF-8 path, and aligned allocation. Threads stay
 * pthreads (winpthreads on Windows, linked statically).
 *
 * SPDX-License-Identifier: LGPL-2.1-or-later
 * (LGPL so that csrc/dstdec.c, from FFmpeg, can include it unchanged in licence.)
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

typedef HANDLE sc_file_t;
#define SC_NO_FILE INVALID_HANDLE_VALUE

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

static inline sc_file_t sc_open_read(const char *path)
{
    wchar_t *w = sc_widen(path);
    if (!w)
        return SC_NO_FILE;
    HANDLE h = CreateFileW(w, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
                           OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, NULL);
    free(w);
    if (h == INVALID_HANDLE_VALUE)
        errno = GetLastError() == ERROR_FILE_NOT_FOUND || GetLastError() == ERROR_PATH_NOT_FOUND ? ENOENT : EACCES;
    return h;
}

/* Like pread(): bytes read, 0 at the end of the file, -1 on error (errno set) */
static inline long long sc_pread(sc_file_t f, void *buf, size_t n, uint64_t off)
{
    OVERLAPPED ov = {0};
    DWORD got = 0;
    ov.Offset = (DWORD)off;
    ov.OffsetHigh = (DWORD)(off >> 32);
    if (!ReadFile(f, buf, n > (1u << 30) ? (1u << 30) : (DWORD)n, &got, &ov)) {
        if (GetLastError() == ERROR_HANDLE_EOF)
            return 0;
        errno = EIO;
        return -1;
    }
    return got;
}

static inline void sc_close(sc_file_t f)
{
    CloseHandle(f);
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

static inline void *sc_aligned_alloc(size_t align, size_t size)
{
    return _aligned_malloc(size, align);
}

static inline void sc_aligned_free(void *p)
{
    _aligned_free(p);
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

typedef int sc_file_t;
#define SC_NO_FILE (-1)

static inline sc_file_t sc_open_read(const char *path)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
#ifdef POSIX_FADV_SEQUENTIAL    /* not on macOS, whose read-ahead does this by itself */
    if (fd >= 0)
        posix_fadvise(fd, 0, 0, POSIX_FADV_SEQUENTIAL);
#endif
    return fd;
}

static inline long long sc_pread(sc_file_t f, void *buf, size_t n, uint64_t off)
{
    return pread(f, buf, n, (off_t)off);
}

static inline void sc_close(sc_file_t f)
{
    close(f);
}

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

static inline void *sc_aligned_alloc(size_t align, size_t size)
{
    void *p = NULL;
    return posix_memalign(&p, align, size) == 0 ? p : NULL;
}

static inline void sc_aligned_free(void *p)
{
    free(p);
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
