#define _GNU_SOURCE
#define JSMN_PARENT_LINKS
#include "../../third_party/jsmn/jsmn.h"
#include "sqlite3_min.h"

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#define MAX_LINE 65536
#define MAX_TOKENS 512
#define MAX_INPUT 96
#define MAX_KEY 192
#define MAX_RESULTS 4

static sqlite3 *g_db;
static volatile sig_atomic_t g_stop;

static void on_signal(int sig) { (void)sig; g_stop = 1; }

static int tok_eq(const char *js, const jsmntok_t *t, const char *s) {
    size_t n = strlen(s);
    return t->type == JSMN_STRING && (size_t)(t->end - t->start) == n
        && memcmp(js + t->start, s, n) == 0;
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

static int copy_string(const char *js, const jsmntok_t *t, char *out, size_t cap) {
    if (!t || t->type != JSMN_STRING) return -1;
    size_t n = (size_t)(t->end - t->start);
    if (n == 0 || n >= cap) return -1;
    memcpy(out, js + t->start, n); out[n] = 0;
    if (strchr(out, '\\') || strchr(out, '"')) return -1;
    return 0;
}

static unsigned latin1_casefold(unsigned cp) {
    if (cp >= 'A' && cp <= 'Z') return cp + 0x20U;
    if ((cp >= 0x00C0U && cp <= 0x00D6U) || (cp >= 0x00D8U && cp <= 0x00DEU)) return cp + 0x20U;
    return cp;
}

static unsigned compose_mark(unsigned base, unsigned mark) {
    if (mark == 0x0300U) {
        if (base == 'a') return 0x00E0U;
        if (base == 'e') return 0x00E8U;
        if (base == 'i') return 0x00ECU;
        if (base == 'o') return 0x00F2U;
        if (base == 'u') return 0x00F9U;
    }
    if (mark == 0x0301U) {
        if (base == 'a') return 0x00E1U;
        if (base == 'e') return 0x00E9U;
        if (base == 'i') return 0x00EDU;
        if (base == 'o') return 0x00F3U;
        if (base == 'u') return 0x00FAU;
    }
    return 0U;
}

static int put_utf8(char *out, size_t cap, size_t *used, unsigned cp) {
    unsigned char bytes[4]; size_t n = 0;
    if (cp <= 0x7FU) { bytes[0] = (unsigned char)cp; n = 1; }
    else if (cp <= 0x7FFU) { bytes[0] = 0xC0U | (cp >> 6); bytes[1] = 0x80U | (cp & 0x3FU); n = 2; }
    else if (cp <= 0xFFFFU) { bytes[0] = 0xE0U | (cp >> 12); bytes[1] = 0x80U | ((cp >> 6) & 0x3FU); bytes[2] = 0x80U | (cp & 0x3FU); n = 3; }
    else if (cp <= 0x10FFFFU) { bytes[0] = 0xF0U | (cp >> 18); bytes[1] = 0x80U | ((cp >> 12) & 0x3FU); bytes[2] = 0x80U | ((cp >> 6) & 0x3FU); bytes[3] = 0x80U | (cp & 0x3FU); n = 4; }
    else return -1;
    if (*used + n >= cap) return -1;
    memcpy(out + *used, bytes, n); *used += n; out[*used] = 0; return 0;
}

static int next_utf8(const unsigned char **cursor, unsigned *cp) {
    const unsigned char *p = *cursor; unsigned c = *p++;
    if (c < 0x80U) { *cp = c; *cursor = p; return 0; }
    unsigned need, value;
    if ((c & 0xE0U) == 0xC0U) { need = 1; value = c & 0x1FU; }
    else if ((c & 0xF0U) == 0xE0U) { need = 2; value = c & 0x0FU; }
    else if ((c & 0xF8U) == 0xF0U) { need = 3; value = c & 0x07U; }
    else return -1;
    for (unsigned i = 0; i < need; ++i) { if ((p[i] & 0xC0U) != 0x80U) return -1; value = (value << 6) | (p[i] & 0x3FU); }
    p += need; *cp = value; *cursor = p; return 0;
}

static int normalize_key(const char *input, char *out, size_t cap) {
    const unsigned char *p = (const unsigned char *)input; size_t used = 0, last_start = 0;
    unsigned previous = 0; out[0] = 0;
    while (*p) {
        unsigned cp; if (next_utf8(&p, &cp) != 0) return -1;
        if (cp == 0x2019U) cp = '\'';
        cp = latin1_casefold(cp);
        unsigned composed = compose_mark(previous, cp);
        if (composed) { used = last_start; if (put_utf8(out, cap, &used, composed) != 0) return -1; previous = composed; continue; }
        last_start = used; if (put_utf8(out, cap, &used, cp) != 0) return -1; previous = cp;
    }
    return used ? 0 : -1;
}

static void json_text(FILE *out, const unsigned char *s) {
    fputc('"', out);
    if (s) for (; *s; ++s) {
        unsigned char c = *s;
        if (c == '"' || c == '\\') { fputc('\\', out); fputc(c, out); }
        else if (c == '\n') fputs("\\n", out);
        else if (c == '\r') fputs("\\r", out);
        else if (c == '\t') fputs("\\t", out);
        else if (c < 0x20) fprintf(out, "\\u%04x", c);
        else fputc(c, out);
    }
    fputc('"', out);
}

static void raw_id(FILE *out, const char *js, const jsmntok_t *id) {
    if (!id || id->start < 0 || id->end <= id->start) { fputs("null", out); return; }
    fwrite(js + id->start, 1, (size_t)(id->end - id->start), out);
}

static void rpc_error(FILE *out, const char *js, const jsmntok_t *id, int code, const char *msg) {
    fputs("{\"jsonrpc\":\"2.0\",\"id\":", out); raw_id(out, js, id);
    fprintf(out, ",\"error\":{\"code\":%d,\"message\":", code); json_text(out, (const unsigned char *)msg);
    fputs("}}\n", out); fflush(out);
}

static void result_start(FILE *out, const char *js, const jsmntok_t *id) {
    fputs("{\"jsonrpc\":\"2.0\",\"id\":", out); raw_id(out, js, id); fputs(",\"result\":", out);
}

static void write_tools(FILE *out, const char *js, const jsmntok_t *id) {
    result_start(out, js, id);
    fputs("{\"tools\":[", out);
    fputs("{\"name\":\"grammar.lookup_token\",\"description\":\"Read-only Italian morphological lookup. Returns bounded approved analyses without resolving context ambiguity.\",\"inputSchema\":{\"type\":\"object\",\"additionalProperties\":false,\"properties\":{\"token\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":96}},\"required\":[\"token\"]}}", out);
    fputs(",", out);
    fputs("{\"name\":\"grammar.lookup_valency\",\"description\":\"Read-only school valency frames plus bounded T-PAS evidence for one verb lemma.\",\"inputSchema\":{\"type\":\"object\",\"additionalProperties\":false,\"properties\":{\"lemma\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":96}},\"required\":[\"lemma\"]}}", out);
    fputs(",", out);
    fputs("{\"name\":\"grammar.health\",\"description\":\"Return read-only grammar data health metadata.\",\"inputSchema\":{\"type\":\"object\",\"additionalProperties\":false}}", out);
    fputs("]}}\n", out); fflush(out);
}

static int prepare(sqlite3_stmt **stmt, const char *sql) {
    int rc = sqlite3_prepare_v2(g_db, sql, -1, stmt, NULL);
    if (rc != SQLITE_OK) fprintf(stderr, "grammar_mcp sqlite prepare: %s\n", sqlite3_errmsg(g_db));
    return rc;
}

static const unsigned char *col(sqlite3_stmt *s, int i) {
    const unsigned char *v = sqlite3_column_text(s, i);
    return v ? v : (const unsigned char *)"";
}

static int write_features(FILE *out, int lexeme_id) {
    sqlite3_stmt *q = NULL;
    const char *sql = "SELECT key,value FROM features WHERE lexeme_id=? "
        "AND key IN ('lemma','genere','numero','persona','modo','tempo','grado') ORDER BY key LIMIT 8";
    if (prepare(&q, sql) != SQLITE_OK) return -1;
    sqlite3_bind_int(q, 1, lexeme_id);
    fputc('{', out); int first = 1;
    while (sqlite3_step(q) == SQLITE_ROW) {
        if (!first) fputc(',', out);
        first = 0;
        json_text(out, col(q, 0)); fputc(':', out); json_text(out, col(q, 1));
    }
    fputc('}', out); sqlite3_finalize(q); return 0;
}

static int write_lookup_token(FILE *out, const char *token) {
    sqlite3_stmt *q = NULL;
    const char *sql = "SELECT id,token,categoria,source FROM lexemes "
        "WHERE normalized=? AND status='approved' "
        "ORDER BY CASE source WHEN 'proposal' THEN 0 WHEN 'bulk_morphit' THEN 1 ELSE 2 END,id LIMIT 4";
    if (prepare(&q, sql) != SQLITE_OK) return -1;
    sqlite3_bind_text(q, 1, token, -1, SQLITE_TRANSIENT);
    fputs("{\"ok\":true,\"token\":", out); json_text(out, (const unsigned char *)token);
    fputs(",\"analyses\":[", out); int first = 1;
    while (sqlite3_step(q) == SQLITE_ROW) {
        if (!first) fputc(',', out);
        first = 0;
        fputs("{\"token\":", out); json_text(out, col(q, 1));
        fputs(",\"category\":", out); json_text(out, col(q, 2));
        fputs(",\"features\":", out); if (write_features(out, sqlite3_column_int(q, 0)) != 0) { sqlite3_finalize(q); return -1; }
        fputs(",\"source\":", out); json_text(out, col(q, 3)); fputc('}', out);
    }
    fputs("],\"writes\":0,\"external_side_effects\":0}", out); sqlite3_finalize(q); return 0;
}

static int write_roles(FILE *out, int frame_id) {
    sqlite3_stmt *q = NULL;
    if (prepare(&q, "SELECT role_order,role_name,traditional_label,surface_pattern,required FROM valency_roles WHERE frame_id=? ORDER BY role_order LIMIT 8") != SQLITE_OK) return -1;
    sqlite3_bind_int(q, 1, frame_id); fputc('[', out); int first = 1;
    while (sqlite3_step(q) == SQLITE_ROW) {
        if (!first) fputc(',', out);
        first = 0;
        fprintf(out, "{\"order\":%d,\"role\":", sqlite3_column_int(q, 0)); json_text(out, col(q, 1));
        fputs(",\"traditional_label\":", out); json_text(out, col(q, 2));
        fputs(",\"surface_pattern\":", out); json_text(out, col(q, 3));
        fprintf(out, ",\"required\":%s}", sqlite3_column_int(q, 4) ? "true" : "false");
    }
    fputc(']', out); sqlite3_finalize(q); return 0;
}

static int write_valency(FILE *out, const char *lemma) {
    sqlite3_stmt *q = NULL;
    const char *sql = "SELECT id,lemma,sense,valency,predicate_type,notes,source FROM valency_frames WHERE lemma=? AND status='approved' ORDER BY id LIMIT 4";
    if (prepare(&q, sql) != SQLITE_OK) return -1;
    sqlite3_bind_text(q, 1, lemma, -1, SQLITE_TRANSIENT);
    fputs("{\"ok\":true,\"lemma\":", out); json_text(out, (const unsigned char *)lemma); fputs(",\"frames\":[", out);
    int first = 1;
    while (sqlite3_step(q) == SQLITE_ROW) {
        if (!first) fputc(',', out);
        first = 0;
        fputs("{\"lemma\":", out); json_text(out, col(q, 1)); fputs(",\"sense\":", out); json_text(out, col(q, 2));
        fputs(",\"valency\":", out); json_text(out, col(q, 3)); fputs(",\"predicate_type\":", out); json_text(out, col(q, 4));
        fputs(",\"notes\":", out); json_text(out, col(q, 5)); fputs(",\"source\":", out); json_text(out, col(q, 6));
        fputs(",\"roles\":", out); if (write_roles(out, sqlite3_column_int(q, 0)) != 0) { sqlite3_finalize(q); return -1; } fputc('}', out);
    }
    sqlite3_finalize(q); fputs("],\"tpas\":[", out);
    if (prepare(&q, "SELECT lemma,label,pattern_string,sense,freq,ratio FROM tpas_patterns WHERE lemma=? ORDER BY COALESCE(freq,0) DESC,id LIMIT 4") != SQLITE_OK) return -1;
    sqlite3_bind_text(q, 1, lemma, -1, SQLITE_TRANSIENT); first = 1;
    while (sqlite3_step(q) == SQLITE_ROW) {
        if (!first) fputc(',', out);
        first = 0;
        fputs("{\"lemma\":", out); json_text(out, col(q, 0));
        fputs(",\"label\":", out); json_text(out, col(q, 1));
        fputs(",\"pattern\":", out); json_text(out, col(q, 2));
        fputs(",\"sense\":", out); json_text(out, col(q, 3));
        fprintf(out, ",\"freq\":%d,\"ratio\":", sqlite3_column_int(q, 4));
        json_text(out, col(q, 5)); fputc('}', out);
    }
    sqlite3_finalize(q);
    fputs("],\"writes\":0,\"external_side_effects\":0}", out);
    return 0;
}

static void write_health(FILE *out) {
    sqlite3_stmt *q = NULL;
    int lexemes = 0, frames = 0, patterns = 0;
    if (prepare(&q, "SELECT count(*) FROM lexemes WHERE status='approved'") == SQLITE_OK && sqlite3_step(q) == SQLITE_ROW) lexemes = sqlite3_column_int(q, 0);
    sqlite3_finalize(q); q = NULL;
    if (prepare(&q, "SELECT count(*) FROM valency_frames WHERE status='approved'") == SQLITE_OK && sqlite3_step(q) == SQLITE_ROW) frames = sqlite3_column_int(q, 0);
    sqlite3_finalize(q); q = NULL;
    if (prepare(&q, "SELECT count(*) FROM tpas_patterns") == SQLITE_OK && sqlite3_step(q) == SQLITE_ROW) patterns = sqlite3_column_int(q, 0);
    sqlite3_finalize(q);
    fprintf(out, "{\"ok\":true,\"lexemes\":%d,\"approved_valency_frames\":%d,\"tpas_patterns\":%d,\"writes\":0,\"external_side_effects\":0}", lexemes, frames, patterns);
}

static void tool_error(FILE *out, const char *js, const jsmntok_t *id, const char *code) {
    result_start(out, js, id);
    fputs("{\"content\":[{\"type\":\"text\",\"text\":", out); json_text(out, (const unsigned char *)code);
    fputs("}],\"structuredContent\":{\"ok\":false,\"error\":", out); json_text(out, (const unsigned char *)code);
    fputs(",\"writes\":0,\"external_side_effects\":0},\"isError\":true}}\n", out); fflush(out);
}

static void tool_result(FILE *out, const char *js, const jsmntok_t *id, const char *name, const char *value) {
    result_start(out, js, id);
    fputs("{\"content\":[{\"type\":\"text\",\"text\":\"read-only grammar evidence\"}],\"structuredContent\":", out);
    int rc = 0;
    if (strcmp(name, "grammar.lookup_token") == 0) rc = write_lookup_token(out, value);
    else if (strcmp(name, "grammar.lookup_valency") == 0) rc = write_valency(out, value);
    else if (strcmp(name, "grammar.health") == 0) write_health(out);
    else rc = -2;
    if (rc == -2) { fputs("{\"ok\":false,\"error\":\"POLICY_DENIED\"}", out); }
    else if (rc != 0) { fputs("{\"ok\":false,\"error\":\"SOURCE_UNAVAILABLE\"}", out); }
    fprintf(out, ",\"isError\":%s}}\n", rc == 0 ? "false" : "true"); fflush(out);
}

static int parse_line(const char *line, jsmntok_t *toks, int cap) {
    jsmn_parser p; jsmn_init(&p);
    return jsmn_parse(&p, line, strlen(line), toks, (unsigned)cap);
}

static void dispatch(FILE *out, const char *line) {
    jsmntok_t toks[MAX_TOKENS];
    int count = parse_line(line, toks, MAX_TOKENS);
    if (count < 1 || toks[0].type != JSMN_OBJECT) { rpc_error(out, line, NULL, -32700, "parse_error"); return; }
    int id_i = obj_get(line, toks, 0, "id");
    int method_i = obj_get(line, toks, 0, "method");
    if (method_i < 0 || toks[method_i].type != JSMN_STRING) { rpc_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, -32600, "invalid_request"); return; }
    char method[80];
    if (copy_string(line, &toks[method_i], method, sizeof method) != 0) { rpc_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, -32600, "invalid_method"); return; }
    if (strcmp(method, "notifications/initialized") == 0) return;
    if (strcmp(method, "initialize") == 0) {
        result_start(out, line, id_i >= 0 ? &toks[id_i] : NULL);
        fputs("{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"ralf-teacher-grammar\",\"version\":\"1\"}}}\n", out); fflush(out); return;
    }
    if (strcmp(method, "tools/list") == 0) { write_tools(out, line, id_i >= 0 ? &toks[id_i] : NULL); return; }
    if (strcmp(method, "ping") == 0) { result_start(out, line, id_i >= 0 ? &toks[id_i] : NULL); fputs("{}\n", out); fflush(out); return; }
    if (strcmp(method, "tools/call") != 0) { rpc_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, -32601, "method_not_found"); return; }

    int params_i = obj_get(line, toks, 0, "params");
    int name_i = obj_get(line, toks, params_i, "name");
    int args_i = obj_get(line, toks, params_i, "arguments");
    char name[96], value[MAX_INPUT], key[MAX_KEY]; value[0] = 0; key[0] = 0;
    if (name_i < 0 || copy_string(line, &toks[name_i], name, sizeof name) != 0 || args_i < 0 || toks[args_i].type != JSMN_OBJECT) {
        tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
    }
    if (strcmp(name, "grammar.lookup_token") == 0) {
        int value_i = obj_get(line, toks, args_i, "token");
        if (value_i < 0 || copy_string(line, &toks[value_i], value, sizeof value) != 0) {
            tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
        }
    } else if (strcmp(name, "grammar.lookup_valency") == 0) {
        int value_i = obj_get(line, toks, args_i, "lemma");
        if (value_i < 0 || copy_string(line, &toks[value_i], value, sizeof value) != 0) {
            tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
        }
    } else if (strcmp(name, "grammar.health") != 0) {
        tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "POLICY_DENIED"); return;
    }
    if (strcmp(name, "grammar.health") != 0) {
        if (normalize_key(value, key, sizeof key) != 0) {
            tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
        }
    }
    tool_result(out, line, id_i >= 0 ? &toks[id_i] : NULL, name, key);
}

static int serve_stream(FILE *in, FILE *out) {
    char *line = NULL; size_t cap = 0;
    while (!g_stop && getline(&line, &cap, in) >= 0) {
        size_t n = strlen(line);
        if (n == 0 || n > MAX_LINE) { rpc_error(out, "", NULL, -32700, "message_too_large"); continue; }
        while (n && (line[n-1] == '\n' || line[n-1] == '\r')) line[--n] = 0;
        if (n) dispatch(out, line);
    }
    free(line); return 0;
}

static int serve_unix(const char *path, uid_t allow_uid) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) { perror("socket"); return 1; }
    struct sockaddr_un addr; memset(&addr, 0, sizeof addr); addr.sun_family = AF_UNIX;
    if (strlen(path) >= sizeof addr.sun_path) { fprintf(stderr, "grammar_mcp socket path too long\n"); close(fd); return 1; }
    strcpy(addr.sun_path, path); unlink(path);
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) != 0) { perror("bind"); close(fd); return 1; }
    if (chmod(path, 0660) != 0) { perror("chmod"); close(fd); unlink(path); return 1; }
    if (listen(fd, 16) != 0) { perror("listen"); close(fd); unlink(path); return 1; }
    while (!g_stop) {
        int client = accept(fd, NULL, NULL);
        if (client < 0) { if (errno == EINTR) continue; perror("accept"); break; }
        struct timeval io_timeout = {.tv_sec = 5, .tv_usec = 0};
        (void)setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &io_timeout, sizeof io_timeout);
        (void)setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &io_timeout, sizeof io_timeout);
#ifdef SO_PEERCRED
        struct ucred cred; socklen_t len = sizeof cred;
        if (getsockopt(client, SOL_SOCKET, SO_PEERCRED, &cred, &len) != 0 || (allow_uid != (uid_t)-1 && cred.uid != allow_uid)) { close(client); continue; }
#else
        (void)allow_uid;
#endif
        FILE *in = fdopen(dup(client), "r"); FILE *out = fdopen(dup(client), "w");
        if (in && out) serve_stream(in, out);
        if (in) fclose(in);
        if (out) fclose(out);
        close(client);
    }
    close(fd); unlink(path); return 0;
}

static int open_db(const char *path) {
    if (!path || path[0] != '/' || strchr(path, '?') || strchr(path, '#')) return 1;
    char uri[4096];
    if (snprintf(uri, sizeof uri, "file:%s?mode=ro&immutable=1", path) >= (int)sizeof uri) return 1;
    int flags = SQLITE_OPEN_READONLY | SQLITE_OPEN_URI | SQLITE_OPEN_NOMUTEX;
    int rc = sqlite3_open_v2(uri, &g_db, flags, NULL);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "grammar_mcp open failed: %s\n", g_db ? sqlite3_errmsg(g_db) : "no handle");
        if (g_db) sqlite3_close(g_db);
        g_db = NULL;
        return 1;
    }
    sqlite3_stmt *q = NULL;
    if (prepare(&q, "PRAGMA query_only=ON") != SQLITE_OK || sqlite3_step(q) != SQLITE_DONE) {
        if (q) sqlite3_finalize(q);
        sqlite3_close(g_db);
        g_db = NULL;
        return 1;
    }
    sqlite3_finalize(q); return 0;
}

static void usage(const char *argv0) {
    fprintf(stderr, "usage: %s --db ABSOLUTE.sqlite [--stdio | --socket PATH [--allow-uid UID]]\n", argv0);
}

int main(int argc, char **argv) {
    const char *db = NULL, *sock = NULL; int stdio_mode = 0; uid_t allow_uid = (uid_t)-1;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--db") && i + 1 < argc) db = argv[++i];
        else if (!strcmp(argv[i], "--socket") && i + 1 < argc) sock = argv[++i];
        else if (!strcmp(argv[i], "--allow-uid") && i + 1 < argc) allow_uid = (uid_t)strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--stdio")) stdio_mode = 1;
        else { usage(argv[0]); return 2; }
    }
    if (!db || (stdio_mode == 0 && !sock) || (stdio_mode && sock)) { usage(argv[0]); return 2; }
    signal(SIGTERM, on_signal); signal(SIGINT, on_signal); signal(SIGPIPE, SIG_IGN);
    if (open_db(db) != 0) return 1;
    int rc = stdio_mode ? serve_stream(stdin, stdout) : serve_unix(sock, allow_uid);
    sqlite3_close(g_db); g_db = NULL;
    return rc;
}
