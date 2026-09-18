#define _GNU_SOURCE
#define JSMN_PARENT_LINKS
#include "../../third_party/jsmn/jsmn.h"

#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#define MAX_LINE 131072
#define MAX_TOKENS 1536
#define MAX_TEXT 60000
#define MAX_INPUT 16000
#define MAX_SENTENCES 256
#define MAX_SUMMARY_SENTENCES 8

static volatile sig_atomic_t g_stop;
static const char *g_concepts_path;
static void on_signal(int sig) { (void)sig; g_stop = 1; }

static int tok_eq(const char *js, const jsmntok_t *t, const char *s) {
    size_t n = strlen(s);
    return t->type == JSMN_STRING && (size_t)(t->end - t->start) == n && memcmp(js + t->start, s, n) == 0;
}

static int tok_skip(const jsmntok_t *t, int i) {
    int j = i + 1;
    if (t[i].type == JSMN_ARRAY) for (int n = 0; n < t[i].size; ++n) j = tok_skip(t, j);
    else if (t[i].type == JSMN_OBJECT) for (int n = 0; n < t[i].size; ++n) { j = tok_skip(t, j); j = tok_skip(t, j); }
    return j;
}

static int obj_get(const char *js, const jsmntok_t *t, int obj, const char *key) {
    if (obj < 0 || t[obj].type != JSMN_OBJECT) return -1;
    int i = obj + 1;
    for (int n = 0; n < t[obj].size; ++n) {
        int k = i, v = tok_skip(t, k);
        if (tok_eq(js, &t[k], key)) return v;
        i = tok_skip(t, v);
    }
    return -1;
}

static int hex4(const char *p, unsigned *value) {
    unsigned v = 0;
    for (int i = 0; i < 4; ++i) {
        unsigned c = (unsigned char)p[i];
        unsigned d;
        if (c >= '0' && c <= '9') d = c - '0';
        else if (c >= 'a' && c <= 'f') d = c - 'a' + 10U;
        else if (c >= 'A' && c <= 'F') d = c - 'A' + 10U;
        else return -1;
        v = (v << 4) | d;
    }
    *value = v;
    return 0;
}

static int put_utf8(char *out, size_t cap, size_t *used, unsigned cp) {
    unsigned char b[4]; size_t n;
    if (cp <= 0x7FU) { b[0] = (unsigned char)cp; n = 1; }
    else if (cp <= 0x7FFU) { b[0] = 0xC0U | (cp >> 6); b[1] = 0x80U | (cp & 0x3FU); n = 2; }
    else if (cp <= 0xFFFFU) { b[0] = 0xE0U | (cp >> 12); b[1] = 0x80U | ((cp >> 6) & 0x3FU); b[2] = 0x80U | (cp & 0x3FU); n = 3; }
    else return -1;
    if (*used + n >= cap) return -1;
    memcpy(out + *used, b, n); *used += n; out[*used] = 0; return 0;
}

static int copy_json_string(const char *js, const jsmntok_t *t, char *out, size_t cap) {
    if (!t || t->type != JSMN_STRING) return -1;
    size_t used = 0;
    for (int i = t->start; i < t->end; ++i) {
        unsigned char c = (unsigned char)js[i];
        if (c != '\\') {
            if (used + 1 >= cap) return -1;
            out[used++] = (char)c; out[used] = 0; continue;
        }
        if (++i >= t->end) return -1;
        c = (unsigned char)js[i];
        if (c == 'u') {
            if (i + 4 >= t->end) return -1;
            unsigned cp;
            if (hex4(js + i + 1, &cp) != 0 || put_utf8(out, cap, &used, cp) != 0) return -1;
            i += 4; continue;
        }
        char v;
        if (c == 'n') v = '\n'; else if (c == 'r') v = '\r'; else if (c == 't') v = '\t';
        else if (c == 'b') v = '\b'; else if (c == 'f') v = '\f'; else if (c == '"') v = '"';
        else if (c == '\\') v = '\\'; else if (c == '/') v = '/'; else return -1;
        if (used + 1 >= cap) return -1;
        out[used++] = v; out[used] = 0;
    }
    return used ? 0 : -1;
}

static int copy_primitive(const char *js, const jsmntok_t *t, char *out, size_t cap) {
    if (!t || t->type != JSMN_PRIMITIVE) return -1;
    size_t n = (size_t)(t->end - t->start);
    if (!n || n >= cap) return -1;
    memcpy(out, js + t->start, n); out[n] = 0; return 0;
}

static void json_text(FILE *out, const char *s) {
    fputc('"', out);
    if (s) for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
        unsigned char c = *p;
        if (c == '"' || c == '\\') { fputc('\\', out); fputc(c, out); }
        else if (c == '\n') fputs("\\n", out); else if (c == '\r') fputs("\\r", out); else if (c == '\t') fputs("\\t", out);
        else if (c < 0x20U) fprintf(out, "\\u%04x", c); else fputc(c, out);
    }
    fputc('"', out);
}

static void raw_id(FILE *out, const char *js, const jsmntok_t *id) {
    if (!id || id->start < 0 || id->end <= id->start) { fputs("null", out); return; }
    fwrite(js + id->start, 1, (size_t)(id->end - id->start), out);
}

static void result_start(FILE *out, const char *js, const jsmntok_t *id) {
    fputs("{\"jsonrpc\":\"2.0\",\"id\":", out); raw_id(out, js, id); fputs(",\"result\":", out);
}

static void rpc_error(FILE *out, const char *js, const jsmntok_t *id, int code, const char *msg) {
    fputs("{\"jsonrpc\":\"2.0\",\"id\":", out); raw_id(out, js, id);
    fprintf(out, ",\"error\":{\"code\":%d,\"message\":", code); json_text(out, msg); fputs("}}\n", out); fflush(out);
}

static void tool_error(FILE *out, const char *js, const jsmntok_t *id, const char *code) {
    result_start(out, js, id);
    fputs("{\"content\":[{\"type\":\"text\",\"text\":", out); json_text(out, code);
    fputs("}],\"structuredContent\":{\"ok\":false,\"error\":", out); json_text(out, code);
    fputs(",\"writes\":0,\"external_side_effects\":0},\"isError\":true}}\n", out); fflush(out);
}

typedef struct {
    const char *p;
    int ok;
    int allow_x;
    long double x_value;
} expr_parser_t;

static void expr_ws(expr_parser_t *p) { while (isspace((unsigned char)*p->p)) ++p->p; }

static long double expr_parse_expr(expr_parser_t *p);

static long double expr_parse_factor(expr_parser_t *p) {
    expr_ws(p);
    int sign = 1;
    while (*p->p == '+' || *p->p == '-') { if (*p->p++ == '-') sign = -sign; expr_ws(p); }
    if (*p->p == '(') {
        ++p->p;
        long double value = expr_parse_expr(p);
        expr_ws(p);
        if (*p->p != ')') { p->ok = 0; return 0; }
        ++p->p;
        return (long double)sign * value;
    }
    if (p->allow_x && (*p->p == 'x' || *p->p == 'X')) {
        ++p->p;
        return (long double)sign * p->x_value;
    }
    char number[128]; size_t used = 0; int digits = 0, decimal = 0;
    while (*p->p && used + 1 < sizeof number) {
        unsigned char c = (unsigned char)*p->p;
        if (isdigit(c)) { number[used++] = (char)c; ++p->p; digits = 1; continue; }
        if ((c == '.' || c == ',') && !decimal) { number[used++] = '.'; ++p->p; decimal = 1; continue; }
        break;
    }
    if (!digits) { p->ok = 0; return 0; }
    number[used] = 0;
    char *end = NULL;
    errno = 0;
    long double value = strtold(number, &end);
    if (errno || !end || *end) { p->ok = 0; return 0; }
    if (p->allow_x && (*p->p == 'x' || *p->p == 'X')) {
        ++p->p;
        value *= p->x_value;
    }
    return (long double)sign * value;
}

static long double expr_parse_term(expr_parser_t *p) {
    long double value = expr_parse_factor(p);
    while (p->ok) {
        expr_ws(p);
        char op = *p->p;
        if (op != '*' && op != '/') break;
        ++p->p;
        long double rhs = expr_parse_factor(p);
        if (!p->ok) break;
        if (op == '/' && fabsl(rhs) < 1e-18L) { p->ok = 0; break; }
        value = op == '*' ? value * rhs : value / rhs;
        if (!isfinite((double)value)) { p->ok = 0; break; }
    }
    return value;
}

static long double expr_parse_expr(expr_parser_t *p) {
    long double value = expr_parse_term(p);
    while (p->ok) {
        expr_ws(p);
        char op = *p->p;
        if (op != '+' && op != '-') break;
        ++p->p;
        long double rhs = expr_parse_term(p);
        if (!p->ok) break;
        value = op == '+' ? value + rhs : value - rhs;
    }
    return value;
}

static int eval_expression_with_x(
    const char *text,
    int allow_x,
    long double x_value,
    long double *value
) {
    expr_parser_t p = {text, 1, allow_x, x_value};
    long double result = expr_parse_expr(&p);
    expr_ws(&p);
    if (!p.ok || *p.p) return 0;
    *value = result; return 1;
}

static int eval_expression(const char *text, long double *value) {
    return eval_expression_with_x(text, 0, 0.0L, value);
}

static int expression_char(unsigned char c) {
    return isdigit(c) || isspace(c) || c == '.' || c == ',' || c == '+' || c == '-' || c == '*' || c == '/' || c == '(' || c == ')';
}

static int extract_expression(const char *text, char *out, size_t cap) {
    size_t best = 0;
    const char *p = text;
    while (*p) {
        while (*p && !isdigit((unsigned char)*p) && *p != '(') ++p;
        if (!*p) break;
        const char *start = p;
        int operator_seen = 0;
        while (*p && expression_char((unsigned char)*p)) {
            if (*p == '+' || *p == '-' || *p == '*' || *p == '/') operator_seen = 1;
            ++p;
        }
        const char *end = p;
        while (end > start && isspace((unsigned char)end[-1])) --end;
        size_t n = (size_t)(end - start);
        if (operator_seen && n > best && n < cap) {
            char candidate[512];
            if (n >= sizeof candidate) continue;
            memcpy(candidate, start, n); candidate[n] = 0;
            long double ignored;
            if (eval_expression(candidate, &ignored)) { memcpy(out, candidate, n + 1); best = n; }
        }
    }
    return best ? 1 : 0;
}

static int linear_equation_char(unsigned char c) {
    return expression_char(c) || c == 'x' || c == 'X';
}

static char *trim_in_place(char *text) {
    while (*text && isspace((unsigned char)*text)) ++text;
    char *end = text + strlen(text);
    while (end > text && isspace((unsigned char)end[-1])) --end;
    *end = 0;
    return text;
}

static int extract_linear_equation(
    const char *text,
    char *out,
    size_t cap,
    long double *solution
) {
    for (const char *eq = strchr(text, '='); eq; eq = strchr(eq + 1, '=')) {
        const char *left_start = eq;
        while (left_start > text && linear_equation_char((unsigned char)left_start[-1])) --left_start;
        const char *right_end = eq + 1;
        while (*right_end && linear_equation_char((unsigned char)*right_end)) ++right_end;

        size_t left_n = (size_t)(eq - left_start);
        size_t right_n = (size_t)(right_end - (eq + 1));
        if (!left_n || !right_n || left_n >= 512 || right_n >= 512) continue;

        char left_raw[512], right_raw[512];
        memcpy(left_raw, left_start, left_n); left_raw[left_n] = 0;
        memcpy(right_raw, eq + 1, right_n); right_raw[right_n] = 0;
        char *left = trim_in_place(left_raw);
        char *right = trim_in_place(right_raw);
        if (!*left || !*right) continue;
        if (!strchr(left, 'x') && !strchr(left, 'X') &&
            !strchr(right, 'x') && !strchr(right, 'X')) continue;

        long double l0, l1, l2, r0, r1, r2;
        if (!eval_expression_with_x(left, 1, 0.0L, &l0) ||
            !eval_expression_with_x(left, 1, 1.0L, &l1) ||
            !eval_expression_with_x(left, 1, 2.0L, &l2) ||
            !eval_expression_with_x(right, 1, 0.0L, &r0) ||
            !eval_expression_with_x(right, 1, 1.0L, &r1) ||
            !eval_expression_with_x(right, 1, 2.0L, &r2)) continue;

        long double d0 = l0 - r0;
        long double d1 = l1 - r1;
        long double d2 = l2 - r2;
        long double a = d1 - d0;
        long double scale = fmaxl(1.0L, fmaxl(fabsl(d0), fmaxl(fabsl(d1), fabsl(d2))));
        if (fabsl((d2 - d1) - a) > 1e-12L * scale || fabsl(a) <= 1e-15L * scale) continue;

        long double root = -d0 / a;
        if (!isfinite((double)root)) continue;
        int written = snprintf(out, cap, "%s=%s", left, right);
        if (written < 0 || (size_t)written >= cap) continue;
        *solution = root;
        return 1;
    }
    return 0;
}

static int eval_answer_numeric(const char *answer, long double *value) {
    if (!answer) return 0;
    const char *eq = strchr(answer, '=');
    if (eq) {
        const char *rhs = eq + 1;
        while (*rhs && isspace((unsigned char)*rhs)) ++rhs;
        return eval_expression(rhs, value);
    }
    return eval_expression(answer, value);
}

typedef struct {
    size_t words;
    size_t sentences;
    size_t chars;
    size_t letters;
    size_t long_words;
} text_profile_t;

static int word_byte(unsigned char c) {
    return isalnum(c) || c >= 0x80U || c == '\'' || c == 0xE2U;
}

static text_profile_t profile_text(const char *text) {
    text_profile_t p = {0};
    int in_word = 0;
    size_t word_chars = 0;
    int sentence_has_word = 0;
    const unsigned char *s = (const unsigned char *)text;
    for (; *s; ++s) {
        unsigned char c = *s;
        ++p.chars;
        if ((c & 0xC0U) != 0x80U && (isalpha(c) || c >= 0x80U)) ++p.letters;
        if (word_byte(c) && !isspace(c)) {
            if (!in_word) { in_word = 1; word_chars = 0; ++p.words; sentence_has_word = 1; }
            if ((c & 0xC0U) != 0x80U) ++word_chars;
        } else if (in_word) {
            if (word_chars >= 8) ++p.long_words;
            in_word = 0;
        }
        if ((c == '.' || c == '?' || c == '!' || c == '\n') && sentence_has_word) {
            ++p.sentences; sentence_has_word = 0;
        }
    }
    if (in_word && word_chars >= 8) ++p.long_words;
    if (sentence_has_word) ++p.sentences;
    return p;
}

typedef struct {
    size_t start;
    size_t end;
    double score;
    size_t index;
    int selected;
} sentence_t;

static int stopword_ascii(const char *word) {
    static const char *const words[] = {
        "anche","come","con","dalla","delle","dello","della","degli","dell","che","chi","cui","dei","del","gli","il","la","le","lo",
        "ma","nel","nella","nelle","nello","non","per","piu","poi","quale","quali","sono","sua","sue","sul","sulla","tra","una","uno","un"
    };
    for (size_t i = 0; i < sizeof words / sizeof words[0]; ++i) if (strcmp(word, words[i]) == 0) return 1;
    return 0;
}

static double sentence_score(const char *text, size_t start, size_t end, size_t index) {
    size_t words = 0, content = 0, current = 0;
    char word[64];
    for (size_t i = start; i <= end; ++i) {
        unsigned char c = i < end ? (unsigned char)text[i] : (unsigned char)' ';
        if (isalpha(c) || (c >= 0x80U && (c & 0xC0U) != 0x80U)) {
            if (current + 1 < sizeof word) word[current++] = (char)tolower(c);
        } else if (current) {
            word[current] = 0; ++words;
            if (current >= 5 && !stopword_ascii(word)) ++content;
            current = 0;
        }
    }
    if (!words) return 0.0;
    double lead = index < 3 ? (3.0 - (double)index) * 0.8 : 0.0;
    return ((double)content * 3.0 + (double)words * 0.25) / sqrt((double)words) + lead;
}

static size_t split_sentences(const char *text, sentence_t *out, size_t cap) {
    size_t len = strlen(text), count = 0, start = 0;
    while (start < len && count < cap) {
        while (start < len && isspace((unsigned char)text[start])) ++start;
        if (start >= len) break;
        size_t end = start;
        while (end < len) {
            unsigned char c = (unsigned char)text[end++];
            if (c == '.' || c == '?' || c == '!' || c == '\n') break;
        }
        size_t trimmed = end;
        while (trimmed > start && isspace((unsigned char)text[trimmed - 1])) --trimmed;
        if (trimmed > start) {
            out[count] = (sentence_t){start, trimmed, 0.0, count, 0};
            out[count].score = sentence_score(text, start, trimmed, count);
            ++count;
        }
        start = end;
    }
    return count;
}

static void write_text_profile(FILE *out, const char *text) {
    text_profile_t p = profile_text(text);
    double avg = p.words ? (double)p.letters / (double)p.words : 0.0;
    double long_ratio = p.words ? (double)p.long_words / (double)p.words : 0.0;
    int chunk = long_ratio > 0.22 || avg > 6.5 ? 650 : long_ratio > 0.12 ? 850 : 1100;
    fprintf(out, "{\"ok\":true,\"chars\":%zu,\"words\":%zu,\"sentences\":%zu,\"long_words\":%zu,\"average_word_chars\":%.3f,\"long_word_ratio\":%.5f,\"recommended_chunk_chars\":%d,\"writes\":0,\"external_side_effects\":0}",
        p.chars, p.words, p.sentences, p.long_words, avg, long_ratio, chunk);
}

static void write_extractive_summary(FILE *out, const char *text, int requested) {
    sentence_t sentences[MAX_SENTENCES];
    size_t count = split_sentences(text, sentences, MAX_SENTENCES);
    int wanted = requested < 1 ? 3 : requested > MAX_SUMMARY_SENTENCES ? MAX_SUMMARY_SENTENCES : requested;
    if ((size_t)wanted > count) wanted = (int)count;
    for (int pick = 0; pick < wanted; ++pick) {
        double best = -1.0; size_t best_i = 0;
        for (size_t i = 0; i < count; ++i) {
            if (!sentences[i].selected && sentences[i].score > best) { best = sentences[i].score; best_i = i; }
        }
        if (best < 0.0) break;
        sentences[best_i].selected = 1;
    }
    fputs("{\"ok\":true,\"summary\":\"", out);
    int first = 1, selected = 0;
    for (size_t i = 0; i < count; ++i) {
        if (!sentences[i].selected) continue;
        if (!first) fputc(' ', out);
        first = 0; ++selected;
        for (size_t j = sentences[i].start; j < sentences[i].end; ++j) {
            unsigned char c = (unsigned char)text[j];
            if (c == '"' || c == '\\') { fputc('\\', out); fputc(c, out); }
            else if (c == '\n' || c == '\r' || c == '\t') fputc(' ', out);
            else if (c < 0x20U) fputc(' ', out); else fputc(c, out);
        }
    }
    fprintf(out, "\",\"source_sentences\":%zu,\"selected_sentences\":%d,\"method\":\"bounded_extractive_lexical\",\"writes\":0,\"external_side_effects\":0}", count, selected);
}

static void write_study_plan(FILE *out, int minutes, const char *mode) {
    if (minutes < 10) minutes = 10;
    if (minutes > 240) minutes = 240;
    int a = minutes / 6;
    int d = minutes / 6;
    int c = minutes / 5;
    int b = minutes - a - c - d;
    const char *l1 = "Definisci l'obiettivo e richiama ciò che sai";
    const char *l2 = "Studio concentrato con un compito alla volta";
    const char *l3 = "Richiamo attivo senza guardare gli appunti";
    const char *l4 = "Correzione, sintesi e prossimo passo";
    if (strcmp(mode, "literacy_l2") == 0) {
        l1 = "Parole chiave e consegna orale"; l2 = "Esempio guidato e prova orale";
        l3 = "Esercizio breve con scelta o risposta"; l4 = "Ripetizione e verifica finale";
    } else if (strcmp(mode, "scholar") == 0) {
        l1 = "Domanda di ricerca, fonti e criterio di riuscita"; l2 = "Studio profondo o produzione";
        l3 = "Richiamo attivo, argomentazione o critica"; l4 = "Sintesi, lacune e prossimo passo";
    }
    fputs("{\"ok\":true,\"minutes\":", out); fprintf(out, "%d", minutes);
    fputs(",\"blocks\":[", out);
    const char *labels[4] = {l1,l2,l3,l4}; int spans[4] = {a,b,c,d};
    for (int i = 0; i < 4; ++i) {
        if (i) fputc(',', out);
        fprintf(out, "{\"order\":%d,\"minutes\":%d,\"label\":", i + 1, spans[i]); json_text(out, labels[i]); fputc('}', out);
    }
    fputs("],\"method\":\"deterministic_timeboxing\",\"writes\":0,\"external_side_effects\":0}", out);
}

static void write_math_check(FILE *out, const char *text, const char *answer) {
    char expression[512]; expression[0] = 0;
    long double expected = 0.0L, observed = 0.0L;
    int equation = extract_linear_equation(text, expression, sizeof expression, &expected);
    int recognized = equation;
    if (!recognized) {
        recognized = extract_expression(text, expression, sizeof expression) &&
                     eval_expression(expression, &expected);
    }
    int answer_ok = answer && *answer && eval_answer_numeric(answer, &observed);
    int equivalent = 0;
    if (recognized && answer_ok) {
        long double scale = fmaxl(1.0L, fmaxl(fabsl(expected), fabsl(observed)));
        equivalent = fabsl(expected - observed) <= 1e-12L * scale;
    }
    fputs("{\"ok\":true,\"recognized\":", out); fputs(recognized ? "true" : "false", out);
    fputs(",\"answer_recognized\":", out); fputs(answer_ok ? "true" : "false", out);
    fputs(",\"kind\":", out); json_text(out, equation ? "linear_equation" : recognized ? "arithmetic_expression" : "unknown");
    fputs(",\"expression\":", out); json_text(out, recognized ? expression : "");
    if (recognized) fprintf(out, ",\"expected\":%.17Lg", expected); else fputs(",\"expected\":null", out);
    if (answer_ok) fprintf(out, ",\"observed\":%.17Lg", observed); else fputs(",\"observed\":null", out);
    fputs(",\"equivalent\":", out); fputs(equivalent ? "true" : "false", out);
    fputs(",\"writes\":0,\"external_side_effects\":0}", out);
}

static void write_math_hint(FILE *out, const char *text, const char *attempt) {
    char expression[512]; expression[0] = 0;
    long double expected = 0.0L;
    int equation = extract_linear_equation(text, expression, sizeof expression, &expected);
    int arithmetic = 0;
    if (!equation) {
        arithmetic = extract_expression(text, expression, sizeof expression) &&
                     eval_expression(expression, &expected);
    }
    int recognized = equation || arithmetic;
    const char *hint = "";
    if (equation) {
        char attempt_expression[512]; attempt_expression[0] = 0;
        long double attempt_expected = 0.0L;
        int attempt_equation = attempt && *attempt &&
            extract_linear_equation(attempt, attempt_expression, sizeof attempt_expression, &attempt_expected);
        if (attempt_equation) {
            hint = "Hai già scritto un'equazione con il termine in x. Il coefficiente di x non si sposta cambiando segno: quando hai kx = c, dividi entrambi i membri per lo stesso coefficiente non nullo k. Fermati prima del valore finale di x.";
        } else {
            hint = "Fai un solo passo: isola prima il termine che contiene x neutralizzando il termine costante con la stessa operazione su entrambi i membri. Fermati prima del valore finale di x.";
        }
    } else if (arithmetic && strchr(expression, '/') &&
               (strchr(expression, '+') || strchr(expression, '-'))) {
        hint = "Fai un solo passo: se stai combinando frazioni, rendi prima confrontabili le parti usando un denominatore comune. Fermati prima del risultato finale.";
    } else if (arithmetic) {
        hint = "Fai soltanto il prossimo passaggio previsto dall'ordine delle operazioni e fermati prima del risultato finale.";
    }
    fputs("{\"ok\":true,\"recognized\":", out); fputs(recognized ? "true" : "false", out);
    fputs(",\"kind\":", out); json_text(out, equation ? "linear_equation" : arithmetic ? "arithmetic_expression" : "unknown");
    fputs(",\"hint\":", out); json_text(out, hint);
    fputs(",\"help_level\":1,\"allow_final_solution\":false,\"writes\":0,\"external_side_effects\":0}", out);
}


static unsigned char ascii_fold(unsigned char c) {
    return (c >= 'A' && c <= 'Z') ? (unsigned char)(c + ('a' - 'A')) : c;
}

static int contains_ci_ascii(const char *text, const char *needle) {
    size_t n = strlen(needle);
    if (!n) return 1;
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
        size_t i = 0;
        while (i < n && p[i] && ascii_fold(p[i]) == ascii_fold((unsigned char)needle[i])) ++i;
        if (i == n) return 1;
    }
    return 0;
}

static int starts_ci_ascii(const char *text, const char *prefix) {
    while (*text && isspace((unsigned char)*text)) ++text;
    for (size_t i = 0; prefix[i]; ++i) {
        if (!text[i] || ascii_fold((unsigned char)text[i]) != ascii_fold((unsigned char)prefix[i])) return 0;
    }
    return 1;
}

static int equal_ci_ascii(const char *left, const char *right) {
    while (*left && *right) {
        if (ascii_fold((unsigned char)*left) != ascii_fold((unsigned char)*right)) return 0;
        ++left; ++right;
    }
    return *left == 0 && *right == 0;
}

static char *trim_ascii_space(char *text) {
    while (*text && isspace((unsigned char)*text)) ++text;
    char *end = text + strlen(text);
    while (end > text && isspace((unsigned char)end[-1])) --end;
    *end = 0;
    return text;
}

static void write_concept_evidence(FILE *out, const char *topic) {
    FILE *input = g_concepts_path ? fopen(g_concepts_path, "r") : NULL;
    if (!input) {
        fputs("{\"ok\":true,\"found\":false,\"topic\":", out); json_text(out, topic);
        fputs(",\"source\":\"curated_concept_evidence_v1\",\"writes\":0,\"external_side_effects\":0}", out);
        return;
    }
    char line[16384];
    while (fgets(line, sizeof line, input)) {
        size_t length = strlen(line);
        if (length && line[length - 1] != '\n' && !feof(input)) {
            int ch; while ((ch = fgetc(input)) != '\n' && ch != EOF) {}
            continue;
        }
        char *first = strchr(line, '\t');
        if (!first) continue;
        *first++ = 0;
        char *second = strchr(first, '\t');
        if (!second) continue;
        *second++ = 0;
        char *key = trim_ascii_space(line);
        char *evidence = trim_ascii_space(first);
        char *misconceptions = trim_ascii_space(second);
        if (!equal_ci_ascii(key, topic)) continue;
        fclose(input);
        fputs("{\"ok\":true,\"found\":true,\"topic\":", out); json_text(out, key);
        fputs(",\"evidence\":", out); json_text(out, evidence);
        fputs(",\"misconceptions\":", out); json_text(out, misconceptions);
        fputs(",\"source\":\"curated_concept_evidence_v1\",\"writes\":0,\"external_side_effects\":0}", out);
        return;
    }
    fclose(input);
    fputs("{\"ok\":true,\"found\":false,\"topic\":", out); json_text(out, topic);
    fputs(",\"source\":\"curated_concept_evidence_v1\",\"writes\":0,\"external_side_effects\":0}", out);
}

static void write_turn_classification(FILE *out, const char *text) {
    const char *move = "neutral", *signal = "none";
    double confidence = 0.55;
    if (contains_ci_ascii(text, "hai sbagliato") || contains_ci_ascii(text, "sbagliato") || contains_ci_ascii(text, "questo e falso") || contains_ci_ascii(text, "non e vero")) {
        move = "correction"; signal = "correction_cue"; confidence = 0.94;
    } else if ((starts_ci_ascii(text, "se invece ") || starts_ci_ascii(text, "e se invece ")) && strchr(text, '?')) {
        move = "question"; signal = "contrastive_question"; confidence = 0.90;
    } else if (contains_ci_ascii(text, "cosa c'entra") || contains_ci_ascii(text, "che c'entra") || contains_ci_ascii(text, "non c'entra") || contains_ci_ascii(text, "non torna") || contains_ci_ascii(text, "eppure") || contains_ci_ascii(text, "invece") || contains_ci_ascii(text, "ma allora") || contains_ci_ascii(text, "allora perche") || contains_ci_ascii(text, "però") || contains_ci_ascii(text, "pero'")) {
        move = "counterexample"; signal = "counterexample_cue"; confidence = 0.93;
    } else if (contains_ci_ascii(text, "fammi un esempio") || contains_ci_ascii(text, "fai un esempio") ||
               contains_ci_ascii(text, "con un esempio") || contains_ci_ascii(text, "esempio concreto") ||
               contains_ci_ascii(text, "fammi vedere") || contains_ci_ascii(text, "mostrami")) {
        move = "request_example"; signal = "example_request"; confidence = 0.96;
    } else if (contains_ci_ascii(text, "non capisco") || contains_ci_ascii(text, "non ho capito") || contains_ci_ascii(text, "non mi e chiaro") || contains_ci_ascii(text, "che vuol dire") || contains_ci_ascii(text, "cosa significa")) {
        move = "confusion"; signal = "confusion_cue"; confidence = 0.94;
    } else if (starts_ci_ascii(text, "ma ")) {
        move = "counterexample"; signal = "contrastive_ma"; confidence = 0.90;
    } else if (starts_ci_ascii(text, "e ") && contains_ci_ascii(text, "allora")) {
        move = "counterexample"; signal = "contrastive_followup"; confidence = 0.86;
    } else if (strchr(text, '?') || starts_ci_ascii(text, "perche ") || starts_ci_ascii(text, "come ") || starts_ci_ascii(text, "cosa ") || starts_ci_ascii(text, "quale ") || starts_ci_ascii(text, "che ")) {
        move = "question"; signal = "question_form"; confidence = 0.82;
    }
    fputs("{\"ok\":true,\"move\":", out); json_text(out, move);
    fputs(",\"signal\":", out); json_text(out, signal);
    fprintf(out, ",\"confidence\":%.2f,\"writes\":0,\"external_side_effects\":0}", confidence);
}

static void write_tools(FILE *out, const char *js, const jsmntok_t *id) {
    result_start(out, js, id);
    fputs("{\"tools\":[", out);
    fputs("{\"name\":\"core.text_profile\",\"description\":\"Read-only bounded text metrics for reading/accessibility routing.\",\"inputSchema\":{\"type\":\"object\",\"additionalProperties\":false,\"properties\":{\"text\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":60000}},\"required\":[\"text\"]}}", out);
    fputs(",", out);
    fputs("{\"name\":\"core.extractive_summary\",\"description\":\"Source-faithful extractive summary with bounded sentence selection.\",\"inputSchema\":{\"type\":\"object\",\"additionalProperties\":false,\"properties\":{\"text\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":60000},\"max_sentences\":{\"type\":\"integer\",\"minimum\":1,\"maximum\":8}},\"required\":[\"text\"]}}", out);
    fputs(",", out);
    fputs("{\"name\":\"core.study_plan\",\"description\":\"Deterministic pedagogical timeboxing without model inference.\",\"inputSchema\":{\"type\":\"object\",\"additionalProperties\":false,\"properties\":{\"minutes\":{\"type\":\"integer\",\"minimum\":10,\"maximum\":240},\"mode\":{\"type\":\"string\",\"enum\":[\"standard\",\"literacy_l2\",\"scholar\"]}},\"required\":[\"minutes\",\"mode\"]}}", out);
    fputs(",", out);
    fputs("{\"name\":\"core.math_check\",\"description\":\"Bounded arithmetic and linear-equation answer verification.\",\"inputSchema\":{\"type\":\"object\",\"additionalProperties\":false,\"properties\":{\"text\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":16000},\"answer\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":256}},\"required\":[\"text\",\"answer\"]}}", out);
    fputs(",", out);
    fputs("{\"name\":\"core.math_hint\",\"description\":\"Deterministic one-step math scaffolding with a hard no-final-answer contract for recognized arithmetic and linear equations.\",\"inputSchema\":{\"type\":\"object\",\"additionalProperties\":false,\"properties\":{\"text\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":16000},\"attempt\":{\"type\":\"string\",\"maxLength\":512}},\"required\":[\"text\"]}}", out);
    fputs(",", out);
    fputs("{\"name\":\"core.classify_turn\",\"description\":\"Deterministic discourse classification for objections, counterexamples, confusion and questions.\",\"inputSchema\":{\"type\":\"object\",\"additionalProperties\":false,\"properties\":{\"text\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":6000}},\"required\":[\"text\"]}}", out);
    fputs(",", out);
    fputs("{\"name\":\"core.concept_evidence\",\"description\":\"Read-only curated concept evidence for factual guarding before model inference.\",\"inputSchema\":{\"type\":\"object\",\"additionalProperties\":false,\"properties\":{\"topic\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":300}},\"required\":[\"topic\"]}}", out);
    fputs("]}}\n", out); fflush(out);
}

static int parse_int_token(const char *js, const jsmntok_t *t, int *value) {
    char raw[32];
    if (copy_primitive(js, t, raw, sizeof raw) != 0) return -1;
    char *end = NULL; errno = 0; long parsed = strtol(raw, &end, 10);
    if (errno || !end || *end || parsed < -2147483647L || parsed > 2147483647L) return -1;
    *value = (int)parsed; return 0;
}

static void tool_result(FILE *out, const char *js, const jsmntok_t *id, const char *name,
                        const char *text, const char *answer, const char *mode, int number) {
    result_start(out, js, id);
    fputs("{\"content\":[{\"type\":\"text\",\"text\":\"deterministic teacher core\"}],\"structuredContent\":", out);
    if (strcmp(name, "core.text_profile") == 0) write_text_profile(out, text);
    else if (strcmp(name, "core.extractive_summary") == 0) write_extractive_summary(out, text, number);
    else if (strcmp(name, "core.study_plan") == 0) write_study_plan(out, number, mode);
    else if (strcmp(name, "core.math_check") == 0) write_math_check(out, text, answer);
    else if (strcmp(name, "core.math_hint") == 0) write_math_hint(out, text, answer);
    else if (strcmp(name, "core.classify_turn") == 0) write_turn_classification(out, text);
    else if (strcmp(name, "core.concept_evidence") == 0) write_concept_evidence(out, text);
    else { fputs("{\"ok\":false,\"error\":\"POLICY_DENIED\",\"writes\":0,\"external_side_effects\":0}", out); }
    fputs(",\"isError\":false}}\n", out); fflush(out);
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
    char method[80];
    if (method_i < 0 || copy_json_string(line, &toks[method_i], method, sizeof method) != 0) { rpc_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, -32600, "invalid_request"); return; }
    if (strcmp(method, "notifications/initialized") == 0) return;
    if (strcmp(method, "initialize") == 0) {
        result_start(out, line, id_i >= 0 ? &toks[id_i] : NULL);
        fputs("{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"ralf-teacher-core\",\"version\":\"1\"}}}\n", out); fflush(out); return;
    }
    if (strcmp(method, "tools/list") == 0) { write_tools(out, line, id_i >= 0 ? &toks[id_i] : NULL); return; }
    if (strcmp(method, "ping") == 0) { result_start(out, line, id_i >= 0 ? &toks[id_i] : NULL); fputs("{}\n", out); fflush(out); return; }
    if (strcmp(method, "tools/call") != 0) { rpc_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, -32601, "method_not_found"); return; }

    int params_i = obj_get(line, toks, 0, "params");
    int name_i = obj_get(line, toks, params_i, "name");
    int args_i = obj_get(line, toks, params_i, "arguments");
    char name[96], text[MAX_TEXT + 1], answer[512], mode[64];
    text[0] = answer[0] = mode[0] = 0;
    int number = 0;
    if (name_i < 0 || copy_json_string(line, &toks[name_i], name, sizeof name) != 0 || args_i < 0 || toks[args_i].type != JSMN_OBJECT) {
        tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
    }
    if (strcmp(name, "core.concept_evidence") == 0) {
        int topic_i = obj_get(line, toks, args_i, "topic");
        if (topic_i < 0 || copy_json_string(line, &toks[topic_i], text, sizeof text) != 0 || strlen(text) > 300) {
            tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
        }
    } else if (strcmp(name, "core.text_profile") == 0 || strcmp(name, "core.extractive_summary") == 0 || strcmp(name, "core.classify_turn") == 0) {
        int text_i = obj_get(line, toks, args_i, "text");
        if (text_i < 0 || copy_json_string(line, &toks[text_i], text, sizeof text) != 0) {
            tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
        }
        if (strcmp(name, "core.classify_turn") == 0 && strlen(text) > 6000) {
            tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
        }
        if (strcmp(name, "core.extractive_summary") == 0) {
            int n_i = obj_get(line, toks, args_i, "max_sentences");
            number = 3;
            if (n_i >= 0 && parse_int_token(line, &toks[n_i], &number) != 0) {
                tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
            }
            if (number < 1 || number > 8) { tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return; }
        }
    } else if (strcmp(name, "core.study_plan") == 0) {
        int n_i = obj_get(line, toks, args_i, "minutes");
        int mode_i = obj_get(line, toks, args_i, "mode");
        if (n_i < 0 || mode_i < 0 || parse_int_token(line, &toks[n_i], &number) != 0 || copy_json_string(line, &toks[mode_i], mode, sizeof mode) != 0) {
            tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
        }
        if (number < 10 || number > 240 || (strcmp(mode, "standard") && strcmp(mode, "literacy_l2") && strcmp(mode, "scholar"))) {
            tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
        }
    } else if (strcmp(name, "core.math_check") == 0 || strcmp(name, "core.math_hint") == 0) {
        int text_i = obj_get(line, toks, args_i, "text");
        if (text_i < 0 || copy_json_string(line, &toks[text_i], text, sizeof text) != 0 || strlen(text) > 16000) {
            tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
        }
        if (strcmp(name, "core.math_check") == 0) {
            int answer_i = obj_get(line, toks, args_i, "answer");
            if (answer_i < 0 || copy_json_string(line, &toks[answer_i], answer, sizeof answer) != 0) {
                tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
            }
        } else {
            int attempt_i = obj_get(line, toks, args_i, "attempt");
            if (attempt_i >= 0 && copy_json_string(line, &toks[attempt_i], answer, sizeof answer) != 0) {
                tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "INVALID_INPUT"); return;
            }
        }
    } else {
        tool_error(out, line, id_i >= 0 ? &toks[id_i] : NULL, "POLICY_DENIED"); return;
    }
    tool_result(out, line, id_i >= 0 ? &toks[id_i] : NULL, name, text, answer, mode, number);
}

static int serve_stream(FILE *in, FILE *out) {
    char *line = NULL; size_t cap = 0;
    while (!g_stop && getline(&line, &cap, in) >= 0) {
        size_t n = strlen(line);
        if (!n || n > MAX_LINE) { rpc_error(out, "", NULL, -32700, "message_too_large"); continue; }
        while (n && (line[n - 1] == '\n' || line[n - 1] == '\r')) line[--n] = 0;
        if (n) dispatch(out, line);
    }
    free(line); return 0;
}

static int serve_unix(const char *path, uid_t allow_uid) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) { perror("socket"); return 1; }
    struct sockaddr_un addr; memset(&addr, 0, sizeof addr); addr.sun_family = AF_UNIX;
    if (strlen(path) >= sizeof addr.sun_path) { fprintf(stderr, "teacher_core socket path too long\n"); close(fd); return 1; }
    strcpy(addr.sun_path, path); unlink(path);
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) != 0) { perror("bind"); close(fd); return 1; }
    if (chmod(path, 0660) != 0) { perror("chmod"); close(fd); unlink(path); return 1; }
    if (listen(fd, 16) != 0) { perror("listen"); close(fd); unlink(path); return 1; }
    while (!g_stop) {
        struct pollfd listener = {.fd = fd, .events = POLLIN, .revents = 0};
        int ready = poll(&listener, 1, 250);
        if (ready < 0) { if (errno == EINTR) continue; perror("poll"); break; }
        if (ready == 0) continue;
        if (!(listener.revents & POLLIN)) {
            if (listener.revents & (POLLERR | POLLHUP | POLLNVAL)) break;
            continue;
        }
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

static void usage(const char *argv0) {
    fprintf(stderr, "usage: %s [--stdio | --socket PATH [--allow-uid UID]] [--concepts PATH]\n", argv0);
}

int main(int argc, char **argv) {
    const char *sock = NULL; int stdio_mode = 0; uid_t allow_uid = (uid_t)-1;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--socket") && i + 1 < argc) sock = argv[++i];
        else if (!strcmp(argv[i], "--allow-uid") && i + 1 < argc) allow_uid = (uid_t)strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--concepts") && i + 1 < argc) g_concepts_path = argv[++i];
        else if (!strcmp(argv[i], "--stdio")) stdio_mode = 1;
        else { usage(argv[0]); return 2; }
    }
    if ((stdio_mode == 0 && !sock) || (stdio_mode && sock)) { usage(argv[0]); return 2; }
    signal(SIGTERM, on_signal); signal(SIGINT, on_signal); signal(SIGPIPE, SIG_IGN);
    return stdio_mode ? serve_stream(stdin, stdout) : serve_unix(sock, allow_uid);
}
