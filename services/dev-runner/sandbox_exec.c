#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/landlock.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef LANDLOCK_ACCESS_FS_REFER
#define LANDLOCK_ACCESS_FS_REFER (1ULL << 13)
#endif
#ifndef LANDLOCK_ACCESS_FS_TRUNCATE
#define LANDLOCK_ACCESS_FS_TRUNCATE (1ULL << 14)
#endif

static __u64 read_access(void) {
    return LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE |
           LANDLOCK_ACCESS_FS_READ_DIR;
}

static __u64 write_access(void) {
    return read_access() | LANDLOCK_ACCESS_FS_WRITE_FILE |
           LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_REMOVE_FILE |
           LANDLOCK_ACCESS_FS_MAKE_CHAR | LANDLOCK_ACCESS_FS_MAKE_DIR |
           LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_MAKE_SOCK |
           LANDLOCK_ACCESS_FS_MAKE_FIFO | LANDLOCK_ACCESS_FS_MAKE_BLOCK |
           LANDLOCK_ACCESS_FS_MAKE_SYM | LANDLOCK_ACCESS_FS_REFER |
           LANDLOCK_ACCESS_FS_TRUNCATE;
}

static int add_path_rule(int ruleset_fd, const char *path, __u64 access) {
    int path_fd = open(path, O_PATH | O_CLOEXEC);
    if (path_fd < 0) {
        fprintf(stderr, "sandbox path unavailable: %s\n", path);
        return -1;
    }
    struct landlock_path_beneath_attr rule = {
        .allowed_access = access,
        .parent_fd = path_fd,
    };
    int result = syscall(SYS_landlock_add_rule, ruleset_fd,
                         LANDLOCK_RULE_PATH_BENEATH, &rule, 0);
    if (result < 0) {
        fprintf(stderr, "sandbox rule rejected for %s: %s\n", path,
                strerror(errno));
    }
    close(path_fd);
    return result;
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fputs("usage: forge-sandbox-exec WORKSPACE COMMAND [ARG...]\n", stderr);
        return 64;
    }

    char workspace[PATH_MAX];
    if (realpath(argv[1], workspace) == NULL ||
        (strncmp(workspace, "/workspaces/", 12) != 0 &&
         strncmp(workspace, "/sandboxes/", 11) != 0)) {
        fputs("sandbox workspace is invalid\n", stderr);
        return 65;
    }

    struct landlock_ruleset_attr ruleset = {
        .handled_access_fs = write_access(),
    };
    int ruleset_fd = syscall(SYS_landlock_create_ruleset, &ruleset,
                             sizeof(ruleset), 0);
    if (ruleset_fd < 0) {
        perror("Landlock is unavailable; refusing untrusted execution");
        return 126;
    }

    const char *readonly_paths[] = {
        "/usr", "/bin", "/lib", "/lib64", "/etc",
#ifdef FORGE_BROWSER_SANDBOX
        /* Chromium requires process/CPU metadata. This build is used only in the
           separate browser container: no network, credentials, or host PID namespace. */
        "/proc",
#endif
        NULL,
    };
    for (size_t index = 0; readonly_paths[index] != NULL; index++) {
        if (add_path_rule(ruleset_fd, readonly_paths[index], read_access()) < 0) {
            perror("failed to add sandbox read rule");
            close(ruleset_fd);
            return 126;
        }
    }
    if (add_path_rule(ruleset_fd, workspace, write_access()) < 0 ||
        add_path_rule(ruleset_fd, "/tmp", write_access()) < 0 ||
        add_path_rule(ruleset_fd, "/dev/null",
                      LANDLOCK_ACCESS_FS_READ_FILE |
                          LANDLOCK_ACCESS_FS_WRITE_FILE) < 0 ||
        add_path_rule(ruleset_fd, "/dev/urandom", LANDLOCK_ACCESS_FS_READ_FILE) < 0 ||
        add_path_rule(ruleset_fd, "/dev/random", LANDLOCK_ACCESS_FS_READ_FILE) < 0) {
        perror("failed to add sandbox write rule");
        close(ruleset_fd);
        return 126;
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0 ||
        syscall(SYS_landlock_restrict_self, ruleset_fd, 0) < 0) {
        perror("failed to enforce Landlock sandbox");
        close(ruleset_fd);
        return 126;
    }
    close(ruleset_fd);
    execvp(argv[2], &argv[2]);
    perror("sandbox command execution failed");
    return errno == ENOENT ? 127 : 126;
}
