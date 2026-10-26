/*
 * LD_PRELOAD shim for NVIDIA's own tools.
 *
 * nvidia-smi deliberately chmods /dev/nvidia* to the values in
 * /proc/driver/nvidia/params on every invocation (0666 root:root by default)
 * and chmods /dev/nvidia-caps/nvidia-cap* to 0400/0444.  Those modes defeat
 * the broker-only 0660 root:citespec-gpu policy.  The kernel module parameters are
 * read-only after load and reloading them would disrupt the active display
 * session, so the wrapper for nvidia-smi preloads this shim and makes the
 * tool's own chmod calls no-ops.  The real permissions are still installed by
 * fix_gpu_permissions.sh and enforced by the device ownership.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int is_nvidia_device(const char *path) {
    return path != NULL && strncmp(path, "/dev/nvidia", 11) == 0;
}

int chmod(const char *path, mode_t mode) {
    static int (*real_chmod)(const char *, mode_t) = NULL;
    if (is_nvidia_device(path)) {
        return 0;
    }
    if (real_chmod == NULL) {
        real_chmod = dlsym(RTLD_NEXT, "chmod");
    }
    if (real_chmod == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return real_chmod(path, mode);
}

int fchmodat(int dirfd, const char *path, mode_t mode, int flags) {
    static int (*real_fchmodat)(int, const char *, mode_t, int) = NULL;
    if (is_nvidia_device(path)) {
        return 0;
    }
    if (real_fchmodat == NULL) {
        real_fchmodat = dlsym(RTLD_NEXT, "fchmodat");
    }
    if (real_fchmodat == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return real_fchmodat(dirfd, path, mode, flags);
}

int chown(const char *path, uid_t owner, gid_t group) {
    static int (*real_chown)(const char *, uid_t, gid_t) = NULL;
    if (is_nvidia_device(path)) {
        return 0;
    }
    if (real_chown == NULL) {
        real_chown = dlsym(RTLD_NEXT, "chown");
    }
    if (real_chown == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return real_chown(path, owner, group);
}

int fchownat(int dirfd, const char *path, uid_t owner, gid_t group, int flags) {
    static int (*real_fchownat)(int, const char *, uid_t, gid_t, int) = NULL;
    if (is_nvidia_device(path)) {
        return 0;
    }
    if (real_fchownat == NULL) {
        real_fchownat = dlsym(RTLD_NEXT, "fchownat");
    }
    if (real_fchownat == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return real_fchownat(dirfd, path, owner, group, flags);
}
