#define _GNU_SOURCE
#define JSMN_PARENT_LINKS
#include "../../third_party/jsmn/jsmn.h"

#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define MAX_LINE 65536
#define MAX_TOKENS 512
#define MAX_NODES 32
#define MAX_LEASES 128
#define PROBE_MS 180

typedef struct {
    char name[64];
    char host[128];
    int port;
    int capacity;
    int inflight;
    double ema_ms;
} node_t;

typedef struct {
    uint64_t id;
    int node;
    double expires_at;
    int active;
} lease_t;

static node_t g_nodes[MAX_NODES];
static lease_t g_leases[MAX_LEASES];
static int g_node_count;
static uint64_t g_next_lease = 1;
static volatile sig_atomic_t g_stop;
static uid_t g_allow_uid = (uid_t)-1;

static void on_signal(int sig) { (void)sig; g_stop = 1; }

static double mono_now(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0.0;
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1000000000.0;
}

static int tok_eq(const char *js, const jsmntok_t *t, const char *s) {
    size_t n = strlen(s);
    return t && t->type == JSMN_STRING &&
           (size_t)(t->end - t->start) == n &&
           memcmp(js + t->start, s, n) == 0;
}

static int tok_skip(const jsmntok_t *t, int i) {
    int j = i + 1;
    if (t[i].type == JSMN_ARRAY) {
        for (int n = 0; n < t[i].size; ++n) j = tok_skip(t, j);
    } else if (t[i].type == JSMN_OBJECT) {
        for (int n = 0; n < t[i].size; ++n) {
            j = tok_skip(t, j);
            j = tok_skip(t, j);
        }
    }
    return j;
}

static int obj_get(const char *js, const jsmntok_t *t, int obj, const char *key) {
    if (obj < 0 || t[obj].type != JSMN_OBJECT) return -1;
    int i = obj + 1;
    for (int n = 0; n < t[obj].size; ++n) {
        int k = i;
        int v = tok_skip(t, k);
        if (tok_eq(js, &t[k], key)) return v;
        i = tok_skip(t, v);
    }
    return -1;
}

static int copy_json_string(const char *js, const jsmntok_t *t, char *out, size_t cap) {
    if (!t || t->type != JSMN_STRING) return -1;
    size_t used = 0;
    for (int i = t->start; i < t->end; ++i) {
        unsigned char c = (unsigned char)js[i];
        if (c == '\\') {
            if (++i >= t->end) return -1;
            c = (unsigned char)js[i];
            if (c == 'n') c = '\n';
            else if (c == 'r') c = '\r';
            else if (c == 't') c = '\t';
            else if (c != '"' && c != '\\' && c != '/') return -1;
        }
        if (used + 1 >= cap) return -1;
        out[used++] = (char)c;
    }
    if (!used) return -1;
    out[used] = 0;
    return 0;
}

static void json_text(FILE *out, const char *s) {
    fputc('"', out);
    for (const unsigned char *p = (const unsigned char *)s; p && *p; ++p) {
        if (*p == '"' || *p == '\\') { fputc('\\', out); fputc(*p, out); }
        else if (*p == '\n') fputs("\\n", out);
        else if (*p == '\r') fputs("\\r", out);
        else if (*p == '\t') fputs("\\t", out);
        else if (*p < 0x20U) fprintf(out, "\\u%04x", *p);
        else fputc(*p, out);
    }
    fputc('"', out);
}

static void raw_id(FILE *out, const char *js, const jsmntok_t *id) {
    if (!id || id->start < 0 || id->end <= id->start) { fputs("null", out); return; }
    fwrite(js + id->start, 1, (size_t)(id->end - id->start), out);
}

static void result_start(FILE *out, const char *js, const jsmntok_t *id) {
    fputs("{\"jsonrpc\":\"2.0\",\"id\":", out);
    raw_id(out, js, id);
    fputs(",\"result\":", out);
}

static void rpc_error(FILE *out, const char *js, const jsmntok_t *id, int code, const char *msg) {
    fputs("{\"jsonrpc\":\"2.0\",\"id\":", out);
    raw_id(out, js, id);
    fprintf(out, ",\"error\":{\"code\":%d,\"message\":", code);
    json_text(out, msg);
    fputs("}}\n", out);
    fflush(out);
}

static void tool_error(FILE *out, const char *js, const jsmntok_t *id, const char *code) {
    result_start(out, js, id);
    fputs("{\"content\":[{\"type\":\"text\",\"text\":", out);
    json_text(out, code);
    fputs("}],\"structuredContent\":{\"ok\":false,\"error\":", out);
    json_text(out, code);
    fputs(",\"writes\":0,\"external_side_effects\":0},\"isError\":true}}\n", out);
    fflush(out);
}

static int load_config(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    char line[512];
    while (fgets(line, sizeof line, f)) {
        char name[64], host[128];
        int port = 0, capacity = 0;
        char *p = line;
        while (*p == ' ' || *p == '\t') ++p;
        if (!*p || *p == '#' || *p == '\n') continue;
        if (sscanf(p, "%63s %127s %d %d", name, host, &port, &capacity) != 4) {
            fclose(f); return -1;
        }
        if (g_node_count >= MAX_NODES || port < 1 || port > 65535 ||
            capacity < 1 || capacity > 16) {
            fclose(f); return -1;
        }
        node_t *n = &g_nodes[g_node_count++];
        snprintf(n->name, sizeof n->name, "%s", name);
        snprintf(n->host, sizeof n->host, "%s", host);
        n->port = port;
        n->capacity = capacity;
    }
    fclose(f);
    return g_node_count > 0 ? 0 : -1;
}

static int probe_node(const node_t *node) {
    char port[16];
    snprintf(port, sizeof port, "%d", node->port);
    struct addrinfo hints = {0}, *res = NULL, *it = NULL;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_family = AF_UNSPEC;
    if (getaddrinfo(node->host, port, &hints, &res) != 0) return 0;
    int ok = 0;
    for (it = res; it && !ok; it = it->ai_next) {
        int fd = socket(it->ai_family, it->ai_socktype, it->ai_protocol);
        if (fd < 0) continue;
        int flags = fcntl(fd, F_GETFL, 0);
        if (flags >= 0) (void)fcntl(fd, F_SETFL, flags | O_NONBLOCK);
        int rc = connect(fd, it->ai_addr, it->ai_addrlen);
        if (rc == 0) ok = 1;
        else if (errno == EINPROGRESS) {
            struct pollfd pfd = {.fd = fd, .events = POLLOUT};
            if (poll(&pfd, 1, PROBE_MS) > 0) {
                int err = 0;
                socklen_t len = sizeof err;
                if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len) == 0 && err == 0)
                    ok = 1;
            }
        }
        close(fd);
    }
    freeaddrinfo(res);
    return ok;
}

static void reap_leases(void) {
    double now = mono_now();
    for (int i = 0; i < MAX_LEASES; ++i) {
        if (!g_leases[i].active || g_leases[i].expires_at > now) continue;
        int n = g_leases[i].node;
        if (n >= 0 && n < g_node_count && g_nodes[n].inflight > 0) --g_nodes[n].inflight;
        g_leases[i].active = 0;
    }
}

static int choose_node(void) {
    static unsigned rr;
    reap_leases();
    int best = -1;
    double best_score = 1e30;
    for (int step = 0; step < g_node_count; ++step) {
        int i = (int)((rr + (unsigned)step) % (unsigned)g_node_count);
        node_t *n = &g_nodes[i];
        if (n->inflight >= n->capacity || !probe_node(n)) continue;
        double load = (double)n->inflight / (double)n->capacity;
        double latency = n->ema_ms > 0.0 ? n->ema_ms / 100000.0 : 0.0;
        double score = load + latency;
        if (score < best_score) { best = i; best_score = score; }
    }
    if (best >= 0) rr = (unsigned)(best + 1);
    return best;
}

static lease_t *new_lease(int node) {
    reap_leases();
    for (int i = 0; i < MAX_LEASES; ++i) {
        if (g_leases[i].active) continue;
        lease_t *l = &g_leases[i];
        l->active = 1;
        l->id = g_next_lease++;
        l->node = node;
        l->expires_at = mono_now() + 240.0;
        ++g_nodes[node].inflight;
        return l;
    }
    return NULL;
}

static lease_t *find_lease(uint64_t id) {
    reap_leases();
    for (int i = 0; i < MAX_LEASES; ++i)
        if (g_leases[i].active && g_leases[i].id == id) return &g_leases[i];
    return NULL;
}

static void write_tools(FILE *out, const char *js, const jsmntok_t *id) {
    result_start(out, js, id);
    fputs("{\"tools\":[", out);
    fputs("{\"name\":\"pool.acquire\",\"description\":\"Acquire a bounded Gemma worker lease\",", out);
    fputs("\"inputSchema\":{\"type\":\"object\",\"properties\":{\"model\":{\"type\":\"string\"}},\"required\":[\"model\"],\"additionalProperties\":false}}", out);
    fputs(",", out);
    fputs("{\"name\":\"pool.release\",\"description\":\"Release a worker lease and report latency\",", out);
    fputs("\"inputSchema\":{\"type\":\"object\",\"properties\":{\"lease\":{\"type\":\"string\"},\"latency_ms\":{\"type\":\"number\"}},\"required\":[\"lease\"],\"additionalProperties\":false}}", out);
    fputs(",", out);
    fputs("{\"name\":\"pool.health\",\"description\":\"Read worker health and in-flight load\",", out);
    fputs("\"inputSchema\":{\"type\":\"object\",\"properties\":{},\"additionalProperties\":false}}", out);
    fputs("]}}\n", out);
    fflush(out);
}

static int parse_u64_string(const char *s, uint64_t *out) {
    if (!s || s[0] != 'p') return -1;
    char *end = NULL;
    errno = 0;
    unsigned long long v = strtoull(s + 1, &end, 10);
    if (errno || !end || *end || v == 0) return -1;
    *out = (uint64_t)v;
    return 0;
}

static double primitive_double(const char *js, const jsmntok_t *t, double fallback) {
    if (!t || t->type != JSMN_PRIMITIVE) return fallback;
    char buf[64];
    size_t n = (size_t)(t->end - t->start);
    if (!n || n >= sizeof buf) return fallback;
    memcpy(buf, js + t->start, n); buf[n] = 0;
    char *end = NULL;
    errno = 0;
    double value = strtod(buf, &end);
    if (errno || !end || *end || value < 0.0 || value > 600000.0) return fallback;
    return value;
}

static void acquire_result(FILE *out, const char *js, const jsmntok_t *id) {
    int node = choose_node();
    if (node < 0) { tool_error(out, js, id, "pool_busy_or_unhealthy"); return; }
    lease_t *lease = new_lease(node);
    if (!lease) { tool_error(out, js, id, "lease_capacity_exhausted"); return; }
    node_t *n = &g_nodes[node];
    char lease_id[40], endpoint[320];
    snprintf(lease_id, sizeof lease_id, "p%llu", (unsigned long long)lease->id);
    snprintf(endpoint, sizeof endpoint, "http://%s:%d", n->host, n->port);
    result_start(out, js, id);
    fputs("{\"content\":[{\"type\":\"text\",\"text\":\"worker acquired\"}],", out);
    fputs("\"structuredContent\":{\"ok\":true,\"lease\":", out); json_text(out, lease_id);
    fputs(",\"node\":", out); json_text(out, n->name);
    fputs(",\"endpoint\":", out); json_text(out, endpoint);
    fprintf(out, ",\"inflight\":%d,\"capacity\":%d,\"writes\":0,\"external_side_effects\":0},\"isError\":false}}\n", n->inflight, n->capacity);
    fflush(out);
}

static void release_result(FILE *out, const char *js, const jsmntok_t *id,
                           const char *lease_name, double latency_ms) {
    uint64_t lease_id = 0;
    if (parse_u64_string(lease_name, &lease_id) != 0) {
        tool_error(out, js, id, "invalid_lease"); return;
    }
    lease_t *lease = find_lease(lease_id);
    if (!lease) { tool_error(out, js, id, "lease_not_found"); return; }
    node_t *n = &g_nodes[lease->node];
    if (n->inflight > 0) --n->inflight;
    if (latency_ms >= 0.0) {
        n->ema_ms = n->ema_ms > 0.0 ? n->ema_ms * 0.8 + latency_ms * 0.2 : latency_ms;
    }
    lease->active = 0;
    result_start(out, js, id);
    fputs("{\"content\":[{\"type\":\"text\",\"text\":\"worker released\"}],", out);
    fputs("\"structuredContent\":{\"ok\":true,\"node\":", out); json_text(out, n->name);
    fprintf(out, ",\"inflight\":%d,\"ema_ms\":%.3f,\"writes\":0,\"external_side_effects\":0},\"isError\":false}}\n", n->inflight, n->ema_ms);
    fflush(out);
}

static void health_result(FILE *out, const char *js, const jsmntok_t *id) {
    reap_leases();
    result_start(out, js, id);
    fputs("{\"content\":[{\"type\":\"text\",\"text\":\"pool health\"}],", out);
    fputs("\"structuredContent\":{\"ok\":true,\"nodes\":[", out);
    for (int i = 0; i < g_node_count; ++i) {
        node_t *n = &g_nodes[i];
        if (i) fputc(',', out);
        fputs("{\"name\":", out); json_text(out, n->name);
        fprintf(out, ",\"healthy\":%s,\"inflight\":%d,\"capacity\":%d,\"ema_ms\":%.3f}",
                probe_node(n) ? "true" : "false", n->inflight, n->capacity, n->ema_ms);
    }
    fputs("],\"writes\":0,\"external_side_effects\":0},\"isError\":false}}\n", out);
    fflush(out);
}

static int parse_line(const char *line, jsmntok_t *toks, int cap) {
    jsmn_parser parser;
    jsmn_init(&parser);
    return jsmn_parse(&parser, line, strlen(line), toks, (unsigned)cap);
}

static void handle_line(FILE *out, const char *line) {
    jsmntok_t toks[MAX_TOKENS];
    int count = parse_line(line, toks, MAX_TOKENS);
    if (count < 1 || toks[0].type != JSMN_OBJECT) {
        rpc_error(out, line, NULL, -32700, "parse_error"); return;
    }
    int id_i = obj_get(line, toks, 0, "id");
    int method_i = obj_get(line, toks, 0, "method");
    char method[64];
    if (method_i < 0 || copy_json_string(line, &toks[method_i], method, sizeof method) != 0) {
        rpc_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, -32600, "invalid_request"); return;
    }
    if (strcmp(method, "initialize") == 0) {
        result_start(out, line, id_i >= 0 ? &toks[id_i] : NULL);
        fputs("{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{\"tools\":{}},", out);
        fputs("\"serverInfo\":{\"name\":\"ralf-teacher-model-pool\",\"version\":\"1\"}}}\n", out);
        fflush(out); return;
    }
    if (strcmp(method, "notifications/initialized") == 0) return;
    if (strcmp(method, "tools/list") == 0) {
        write_tools(out, line, id_i >= 0 ? &toks[id_i] : NULL); return;
    }
    if (strcmp(method, "ping") == 0) {
        result_start(out, line, id_i >= 0 ? &toks[id_i] : NULL);
        fputs("{}\n", out); fflush(out); return;
    }
    if (strcmp(method, "tools/call") != 0) {
        rpc_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, -32601, "method_not_found"); return;
    }
    int params_i = obj_get(line, toks, 0, "params");
    int name_i = obj_get(line, toks, params_i, "name");
    int args_i = obj_get(line, toks, params_i, "arguments");
    char name[64];
    if (name_i < 0 || args_i < 0 || toks[args_i].type != JSMN_OBJECT ||
        copy_json_string(line, &toks[name_i], name, sizeof name) != 0) {
        rpc_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, -32602, "invalid_params"); return;
    }
    if (strcmp(name, "pool.health") == 0) {
        health_result(out, line, id_i >= 0 ? &toks[id_i] : NULL); return;
    }
    if (strcmp(name, "pool.acquire") == 0) {
        int model_i = obj_get(line, toks, args_i, "model");
        char model[64];
        if (model_i < 0 || copy_json_string(line, &toks[model_i], model, sizeof model) != 0 ||
            strcmp(model, "gemma3:4b") != 0) {
            tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "model_not_allowed"); return;
        }
        acquire_result(out, line, id_i >= 0 ? &toks[id_i] : NULL); return;
    }
    if (strcmp(name, "pool.release") == 0) {
        int lease_i = obj_get(line, toks, args_i, "lease");
        int latency_i = obj_get(line, toks, args_i, "latency_ms");
        char lease_name[64];
        if (lease_i < 0 || copy_json_string(line, &toks[lease_i], lease_name, sizeof lease_name) != 0) {
            tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "invalid_lease"); return;
        }
        double latency = latency_i >= 0 ? primitive_double(line, &toks[latency_i], -1.0) : -1.0;
        release_result(out, line, id_i >= 0 ? &toks[id_i] : NULL, lease_name, latency);
        return;
    }
    tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "unknown_tool");
}

static int serve_stream(FILE *in, FILE *out) {
    char *line = malloc(MAX_LINE + 2U);
    if (!line) return 1;
    while (!g_stop && fgets(line, MAX_LINE + 2, in)) {
        size_t n = strlen(line);
        if (n > MAX_LINE || (n == MAX_LINE && line[n - 1] != '\n')) {
            free(line); return 1;
        }
        handle_line(out, line);
    }
    free(line);
    return 0;
}

static int peer_allowed(int fd) {
    if (g_allow_uid == (uid_t)-1) return 1;
    struct ucred cred;
    socklen_t len = sizeof cred;
    return getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &cred, &len) == 0 && cred.uid == g_allow_uid;
}

static int serve_socket(const char *path) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return 1;
    struct sockaddr_un addr = {0};
    addr.sun_family = AF_UNIX;
    if (strlen(path) >= sizeof addr.sun_path) { close(fd); return 1; }
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", path);
    unlink(path);
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) != 0 || listen(fd, 16) != 0) {
        close(fd); return 1;
    }
    (void)chmod(path, 0600);
    while (!g_stop) {
        struct pollfd pfd = {.fd = fd, .events = POLLIN};
        int ready = poll(&pfd, 1, 250);
        if (ready < 0) { if (errno == EINTR) continue; break; }
        if (ready == 0) continue;
        int client = accept(fd, NULL, NULL);
        if (client < 0) { if (errno == EINTR) continue; break; }
        if (!peer_allowed(client)) { close(client); continue; }
        FILE *io = fdopen(client, "r+");
        if (!io) { close(client); continue; }
        (void)serve_stream(io, io);
        fclose(io);
    }
    close(fd);
    unlink(path);
    return 0;
}

static void usage(const char *argv0) {
    fprintf(stderr, "usage: %s --config FILE [--stdio | --socket PATH] [--allow-uid UID]\n", argv0);
}

int main(int argc, char **argv) {
    const char *config = NULL;
    const char *socket_path = NULL;
    int use_stdio = 0;
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--config") == 0 && i + 1 < argc) config = argv[++i];
        else if (strcmp(argv[i], "--socket") == 0 && i + 1 < argc) socket_path = argv[++i];
        else if (strcmp(argv[i], "--stdio") == 0) use_stdio = 1;
        else if (strcmp(argv[i], "--allow-uid") == 0 && i + 1 < argc) {
            char *end = NULL;
            unsigned long value = strtoul(argv[++i], &end, 10);
            if (!end || *end || value > 0xffffffffUL) { usage(argv[0]); return 2; }
            g_allow_uid = (uid_t)value;
        } else { usage(argv[0]); return 2; }
    }
    if (!config || (use_stdio == (socket_path != NULL))) { usage(argv[0]); return 2; }
    if (load_config(config) != 0) {
        fprintf(stderr, "invalid pool config: %s\n", config); return 2;
    }
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);
    if (use_stdio) return serve_stream(stdin, stdout);
    return serve_socket(socket_path);
}
