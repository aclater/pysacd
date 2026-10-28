/*
 * Sandbox for the isolated engine process (#17, docs/security.md "Isolated engine").
 *
 * pysacd._isolated calls these after loading the engine and before reading the image:
 * no_new_privs, then Landlock (filesystem), then seccomp (system calls). Nothing here runs
 * in the default in-process mode.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifdef __linux__
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/landlock.h>
#include <linux/sched.h>
#include <linux/seccomp.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#endif

int sacdconv_no_new_privs(void)
{
#ifdef __linux__
    return prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) ? -errno : 0;
#else
    return -ENOSYS;
#endif
}

#if defined(__linux__) && defined(SYS_landlock_create_ruleset)

/* Rights the kernel knows for each Landlock ABI; everything known is "handled" (denied unless
 * a rule allows it). */
static uint64_t ll_handled(int abi)
{
    uint64_t r = LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_WRITE_FILE | LANDLOCK_ACCESS_FS_READ_FILE |
                 LANDLOCK_ACCESS_FS_READ_DIR | LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_REMOVE_FILE |
                 LANDLOCK_ACCESS_FS_MAKE_CHAR | LANDLOCK_ACCESS_FS_MAKE_DIR | LANDLOCK_ACCESS_FS_MAKE_REG |
                 LANDLOCK_ACCESS_FS_MAKE_SOCK | LANDLOCK_ACCESS_FS_MAKE_FIFO | LANDLOCK_ACCESS_FS_MAKE_BLOCK |
                 LANDLOCK_ACCESS_FS_MAKE_SYM;
#ifdef LANDLOCK_ACCESS_FS_REFER
    if (abi >= 2) r |= LANDLOCK_ACCESS_FS_REFER;
#endif
#ifdef LANDLOCK_ACCESS_FS_TRUNCATE
    if (abi >= 3) r |= LANDLOCK_ACCESS_FS_TRUNCATE;
#endif
#ifdef LANDLOCK_ACCESS_FS_IOCTL_DEV
    if (abi >= 5) r |= LANDLOCK_ACCESS_FS_IOCTL_DEV;
#endif
    return r;
}

static int ll_allow(int ruleset, const char *path, uint64_t rights)
{
    struct landlock_path_beneath_attr pb = {0};
    struct stat st;
    int fd = open(path, O_PATH | O_CLOEXEC);
    if (fd < 0)
        return -errno;
    if (fstat(fd, &st) == 0 && !S_ISDIR(st.st_mode))      /* directory-only rights are EINVAL on files */
        rights &= LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE
#ifdef LANDLOCK_ACCESS_FS_TRUNCATE
                  | LANDLOCK_ACCESS_FS_TRUNCATE
#endif
            ;
    pb.allowed_access = rights;
    pb.parent_fd = fd;
    int rc = syscall(SYS_landlock_add_rule, ruleset, LANDLOCK_RULE_PATH_BENEATH, &pb, 0) ? -errno : 0;
    close(fd);
    return rc;
}

#endif

/*
 * Restrict the filesystem to `ro` (read-only files or directories) and `rw` (directories where
 * outputs are created, written and removed). Returns the Landlock ABI version applied (> 0),
 * 0 if the kernel has no Landlock (the caller warns and carries on), or -errno.
 */
int sacdconv_landlock(const char *const *ro, int nro, const char *const *rw, int nrw)
{
#if defined(__linux__) && defined(SYS_landlock_create_ruleset)
    int abi = (int)syscall(SYS_landlock_create_ruleset, NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
    if (abi < 0)
        return (errno == ENOSYS || errno == EOPNOTSUPP) ? 0 : -errno;
    uint64_t handled = ll_handled(abi);
    struct landlock_ruleset_attr attr = {.handled_access_fs = handled};
    int rs = (int)syscall(SYS_landlock_create_ruleset, &attr, sizeof attr, 0);
    if (rs < 0)
        return -errno;
    uint64_t read = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR;
    uint64_t write = read | LANDLOCK_ACCESS_FS_WRITE_FILE | LANDLOCK_ACCESS_FS_MAKE_REG |
                     LANDLOCK_ACCESS_FS_REMOVE_FILE;
#ifdef LANDLOCK_ACCESS_FS_TRUNCATE
    if (abi >= 3) write |= LANDLOCK_ACCESS_FS_TRUNCATE;
#endif
    int rc = 0;
    for (int i = 0; i < nro && !rc; i++)
        rc = ll_allow(rs, ro[i], read & handled);
    for (int i = 0; i < nrw && !rc; i++)
        rc = ll_allow(rs, rw[i], write & handled);
    if (!rc && syscall(SYS_landlock_restrict_self, rs, 0))
        rc = -errno;
    close(rs);
    return rc ? rc : abi;
#else
    (void)ro; (void)nro; (void)rw; (void)nrw;
    return 0;
#endif
}

#if defined(__linux__) && (defined(__x86_64__) || defined(__aarch64__))
#if defined(__x86_64__)
#define SB_ARCH AUDIT_ARCH_X86_64
#else
#define SB_ARCH AUDIT_ARCH_AARCH64
#endif
#define SB_DENY(nr) BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (nr), 0, 1), \
                    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM)
#endif

/*
 * Deny the system calls the engine never needs, so code running in a compromised engine can't
 * run programs, reach the network, debug other processes or change namespaces and mounts.
 * Denied calls fail with EPERM, except clone3, which fails with ENOSYS so that glibc falls
 * back to clone, whose namespace flags are checked here. A foreign syscall ABI (x32, or a
 * 32-bit ABI on a 64-bit kernel) kills the process. Returns 0, -ENOSYS on an unsupported
 * architecture, or -errno.
 */
int sacdconv_seccomp(void)
{
#if defined(__linux__) && (defined(__x86_64__) || defined(__aarch64__))
    const uint32_t newns = CLONE_NEWNS | CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET | CLONE_NEWUTS |
                           CLONE_NEWIPC | CLONE_NEWCGROUP;
    struct sock_filter f[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SB_ARCH, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
#if defined(__x86_64__)
        BPF_JUMP(BPF_JMP | BPF_JGE | BPF_K, 0x40000000, 0, 1),         /* x32 */
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
#endif
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_clone3, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_clone, 0, 4),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, newns, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        SB_DENY(__NR_execve), SB_DENY(__NR_execveat), SB_DENY(__NR_ptrace),
        SB_DENY(__NR_process_vm_readv), SB_DENY(__NR_process_vm_writev),
        SB_DENY(__NR_socket), SB_DENY(__NR_socketpair), SB_DENY(__NR_connect), SB_DENY(__NR_bind),
        SB_DENY(__NR_listen), SB_DENY(__NR_accept), SB_DENY(__NR_accept4),
        SB_DENY(__NR_mount), SB_DENY(__NR_umount2), SB_DENY(__NR_pivot_root), SB_DENY(__NR_chroot),
        SB_DENY(__NR_unshare), SB_DENY(__NR_setns), SB_DENY(__NR_keyctl), SB_DENY(__NR_add_key),
        SB_DENY(__NR_request_key), SB_DENY(__NR_bpf), SB_DENY(__NR_perf_event_open),
        SB_DENY(__NR_init_module), SB_DENY(__NR_finit_module), SB_DENY(__NR_delete_module),
        SB_DENY(__NR_kexec_load), SB_DENY(__NR_open_by_handle_at), SB_DENY(__NR_userfaultfd),
        SB_DENY(__NR_io_uring_setup), SB_DENY(__NR_personality),
#ifdef __NR_fork
        SB_DENY(__NR_fork), SB_DENY(__NR_vfork),
#endif
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog prog = {.len = sizeof f / sizeof f[0], .filter = f};
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0))
        return -errno;
    return syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC, &prog) ? -errno : 0;
#else
    return -ENOSYS;
#endif
}
