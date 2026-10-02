#define _POSIX_C_SOURCE 200809L
#include <dirent.h>
#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define MAX_PATH_SPECS 64

struct path_spec {
    const char *label;
    const char *path;
};

static uint64_t allocated_bytes(const struct stat *st) {
    if (st->st_blocks > 0) {
        return (uint64_t)st->st_blocks * 512ULL;
    }
    return (uint64_t)(st->st_size > 0 ? st->st_size : 0);
}

static uint64_t tree_bytes(const char *path, unsigned *errors) {
    struct stat st;
    if (lstat(path, &st) != 0) {
        if (errors) (*errors)++;
        return 0;
    }
    if (S_ISLNK(st.st_mode)) return 0;

    uint64_t total = allocated_bytes(&st);
    if (!S_ISDIR(st.st_mode)) return total;

    DIR *dir = opendir(path);
    if (!dir) {
        if (errors) (*errors)++;
        return total;
    }

    struct dirent *ent;
    while ((ent = readdir(dir)) != NULL) {
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
        size_t need = strlen(path) + strlen(ent->d_name) + 2;
        char *child = malloc(need);
        if (!child) {
            if (errors) (*errors)++;
            continue;
        }
        snprintf(child, need, "%s/%s", path, ent->d_name);
        total += tree_bytes(child, errors);
        free(child);
    }
    closedir(dir);
    return total;
}

static void json_string(const char *s) {
    putchar('"');
    for (; *s; ++s) {
        unsigned char c = (unsigned char)*s;
        switch (c) {
            case '\\': fputs("\\\\", stdout); break;
            case '"':  fputs("\\\"", stdout); break;
            case '\n': fputs("\\n", stdout); break;
            case '\r': fputs("\\r", stdout); break;
            case '\t': fputs("\\t", stdout); break;
            default:
                if (c < 0x20) printf("\\u%04x", c);
                else putchar(c);
        }
    }
    putchar('"');
}

static bool parse_u64(const char *s, uint64_t *out) {
    char *end = NULL;
    errno = 0;
    unsigned long long v = strtoull(s, &end, 10);
    if (errno || !end || *end != '\0') return false;
    *out = (uint64_t)v;
    return true;
}

static void usage(const char *argv0) {
    fprintf(stderr,
        "Usage: %s [--tmp PATH] [--age-days N] [--min-mib N] "
        "[--path LABEL=PATH]...\n", argv0);
}

int main(int argc, char **argv) {
    const char *tmp_root = "/tmp";
    uint64_t age_days = 2;
    uint64_t min_mib = 100;
    struct path_spec specs[MAX_PATH_SPECS];
    size_t spec_count = 0;

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--tmp") == 0 && i + 1 < argc) {
            tmp_root = argv[++i];
        } else if (strcmp(argv[i], "--age-days") == 0 && i + 1 < argc) {
            if (!parse_u64(argv[++i], &age_days)) {
                usage(argv[0]);
                return 2;
            }
        } else if (strcmp(argv[i], "--min-mib") == 0 && i + 1 < argc) {
            if (!parse_u64(argv[++i], &min_mib)) {
                usage(argv[0]);
                return 2;
            }
        } else if (strcmp(argv[i], "--path") == 0 && i + 1 < argc) {
            if (spec_count >= MAX_PATH_SPECS) {
                fprintf(stderr, "too many --path entries\n");
                return 2;
            }
            char *spec = argv[++i];
            char *eq = strchr(spec, '=');
            if (!eq || eq == spec || eq[1] == '\0') {
                fprintf(stderr, "invalid --path, expected LABEL=PATH\n");
                return 2;
            }
            *eq = '\0';
            specs[spec_count].label = spec;
            specs[spec_count].path = eq + 1;
            spec_count++;
        } else if (strcmp(argv[i], "--help") == 0) {
            usage(argv[0]);
            return 0;
        } else {
            usage(argv[0]);
            return 2;
        }
    }

    time_t now = time(NULL);
    uint64_t min_bytes = min_mib * 1024ULL * 1024ULL;
    uint64_t candidate_total = 0;
    unsigned errors = 0;
    size_t candidate_count = 0;

    printf("{\n");
    printf("  \"tmp_root\": "); json_string(tmp_root); printf(",\n");
    printf("  \"age_days\": %" PRIu64 ",\n", age_days);
    printf("  \"min_mib\": %" PRIu64 ",\n", min_mib);
    printf("  \"tmp_candidates\": [\n");

    DIR *dir = opendir(tmp_root);
    if (!dir) {
        fprintf(stderr, "cannot open %s: %s\n", tmp_root, strerror(errno));
        return 2;
    }

    bool first = true;
    struct dirent *ent;
    while ((ent = readdir(dir)) != NULL) {
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;

        size_t need = strlen(tmp_root) + strlen(ent->d_name) + 2;
        char *path = malloc(need);
        if (!path) {
            errors++;
            continue;
        }
        snprintf(path, need, "%s/%s", tmp_root, ent->d_name);

        struct stat st;
        if (lstat(path, &st) != 0 || !S_ISDIR(st.st_mode) || S_ISLNK(st.st_mode)) {
            free(path);
            continue;
        }

        double age_seconds = difftime(now, st.st_mtime);
        double threshold_seconds = (double)age_days * 86400.0;
        if (age_seconds < threshold_seconds) {
            free(path);
            continue;
        }

        unsigned local_errors = 0;
        uint64_t bytes = tree_bytes(path, &local_errors);
        errors += local_errors;
        if (bytes < min_bytes) {
            free(path);
            continue;
        }

        if (!first) printf(",\n");
        first = false;
        candidate_count++;
        candidate_total += bytes;
        printf("    {\"path\": "); json_string(path);
        printf(", \"size_bytes\": %" PRIu64, bytes);
        printf(", \"age_days\": %.2f", age_seconds / 86400.0);
        printf(", \"mtime_epoch\": %lld}", (long long)st.st_mtime);
        free(path);
    }
    closedir(dir);

    printf("\n  ],\n");
    printf("  \"candidate_count\": %zu,\n", candidate_count);
    printf("  \"candidate_bytes\": %" PRIu64 ",\n", candidate_total);
    printf("  \"watched_paths\": [\n");

    first = true;
    for (size_t i = 0; i < spec_count; ++i) {
        struct stat st;
        bool exists = lstat(specs[i].path, &st) == 0;
        unsigned local_errors = 0;
        uint64_t bytes = exists ? tree_bytes(specs[i].path, &local_errors) : 0;
        errors += local_errors;

        if (!first) printf(",\n");
        first = false;
        printf("    {\"label\": "); json_string(specs[i].label);
        printf(", \"path\": "); json_string(specs[i].path);
        printf(", \"exists\": %s, \"size_bytes\": %" PRIu64 "}",
               exists ? "true" : "false", bytes);
    }

    printf("\n  ],\n");
    printf("  \"errors\": %u,\n", errors);
    printf("  \"deletes_files\": false\n");
    printf("}\n");
    return 0;
}
