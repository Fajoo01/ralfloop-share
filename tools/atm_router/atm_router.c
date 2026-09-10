#include "atm_router.h"

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#define INF_TIME UINT32_MAX
#define ORIGIN_RADIUS_M 900.0
#define DEST_RADIUS_M   900.0

/*
 * Stati:
 * 0 = non abbiamo ancora preso un mezzo
 * 1 = abbiamo usato il TPL, possiamo fare un cambio a piedi
 * 2 = abbiamo appena camminato per un cambio; prima di un'altra
 *     camminata dobbiamo prendere un mezzo.
 */
#define STATE_PRE_TRANSIT 0U
#define STATE_TRANSIT     1U
#define STATE_AFTER_WALK  2U
#define STATE_COUNT       3U

#define PREV_NONE         0U
#define PREV_WALK         1U
#define PREV_TRANSIT      2U
#define PREV_ORIGIN       3U
#define PREV_LIVE_TRANSIT 4U

#define MAX_LIVE_INPUTS 64U
#define LIVE_MATCH_MAX_DELTA_S 3600U

typedef struct {
    void *mapping;
    size_t size;

    const atm_graph_header_t *header;
    const atm_stop_t *stops;
    const atm_route_t *routes;
    const atm_connection_t *connections;
    const atm_transfer_t *transfers;

    const char *strings;
} graph_t;

typedef struct {
    const char *stop_id;
    const char *line;

    uint32_t direction;
    uint32_t wait_seconds;

    uint32_t stop;
    uint32_t departure_s;

    uint32_t anchor_connection;
    uint32_t trip;
    uint32_t route;

    int64_t shift_s;

    uint8_t resolved;
    uint8_t catchable;
} live_input_t;

typedef struct {
    uint32_t node;
    uint32_t time;
} heap_item_t;

typedef struct {
    heap_item_t *items;
    size_t len;
    size_t cap;
} heap_t;

typedef struct {
    uint32_t prev_stop;
    uint32_t ref;
    uint32_t aux;
    uint32_t walk_seconds;

    uint8_t prev_state;
    uint8_t kind;
} previous_t;

typedef struct {
    uint8_t kind;

    uint32_t from_stop;
    uint32_t to_stop;

    uint32_t connection;
    uint32_t live_index;

    uint32_t walk_seconds;
    uint32_t arrival_s;
} path_step_t;

typedef struct {
    uint8_t kind;
    uint8_t live;

    uint32_t from_stop;
    uint32_t to_stop;

    uint32_t route;
    uint32_t trip;

    uint32_t departure_s;
    uint32_t arrival_s;

    uint32_t walk_seconds;
    uint32_t live_wait_seconds;
} output_leg_t;


static double deg_to_rad(double value)
{
    return value * 0.01745329251994329576923690768489;
}


static double distance_m(
    double lat1,
    double lon1,
    double lat2,
    double lon2
)
{
    const double earth = 6371000.0;

    double p1 = deg_to_rad(lat1);
    double p2 = deg_to_rad(lat2);

    double dp = deg_to_rad(lat2 - lat1);
    double dl = deg_to_rad(lon2 - lon1);

    double a =
        sin(dp / 2.0) * sin(dp / 2.0)
        + cos(p1) * cos(p2)
        * sin(dl / 2.0) * sin(dl / 2.0);

    return 2.0 * earth * asin(sqrt(a));
}


static uint32_t walk_seconds(double metres)
{
    double seconds = ceil(metres / (80.0 / 60.0));

    if (seconds < 0.0) {
        return 0U;
    }

    if (seconds > (double)UINT32_MAX) {
        return UINT32_MAX;
    }

    return (uint32_t)seconds;
}


static int parse_time(const char *text, uint32_t *result)
{
    unsigned hh;
    unsigned mm;
    unsigned ss;

    if (
        sscanf(text, "%u:%u:%u", &hh, &mm, &ss) != 3
        || mm > 59U
        || ss > 59U
        || hh > 47U
    ) {
        return 1;
    }

    *result =
        (uint32_t)(hh * 3600U + mm * 60U + ss);

    return 0;
}


static const char *graph_string(
    const graph_t *graph,
    uint32_t offset
)
{
    if ((uint64_t)offset >= graph->header->strings_size) {
        return "";
    }

    return graph->strings + offset;
}



static uint32_t find_stop_by_external_id(
    const graph_t *graph,
    const char *stop_id
)
{
    for (
        uint32_t i = 0U;
        i < graph->header->stop_count;
        ++i
    ) {
        const char *candidate = graph_string(
            graph,
            graph->stops[i].external_id_offset
        );

        if (strcmp(candidate, stop_id) == 0) {
            return i;
        }
    }

    return UINT32_MAX;
}


static int connection_matches_live(
    const graph_t *graph,
    const atm_connection_t *connection,
    uint32_t stop,
    const live_input_t *live
)
{
    if (
        connection->from_stop != stop
        || connection->route >= graph->header->route_count
    ) {
        return 0;
    }

    uint32_t direction =
        connection->flags & ATM_CONN_DIR_MASK;

    if (direction != live->direction) {
        return 0;
    }

    const char *line = graph_string(
        graph,
        graph->routes[
            connection->route
        ].short_name_offset
    );

    return strcmp(line, live->line) == 0;
}


static const live_input_t *live_for_connection(
    const graph_t *graph,
    const live_input_t *live_inputs,
    size_t live_count,
    uint32_t stop,
    const atm_connection_t *connection
)
{
    for (size_t i = 0U; i < live_count; ++i) {
        const live_input_t *live = &live_inputs[i];

        if (
            live->resolved
            && live->stop == stop
            && connection_matches_live(
                graph,
                connection,
                stop,
                live
            )
        ) {
            return live;
        }
    }

    return NULL;
}


static int graph_open(
    const char *path,
    graph_t *graph
)
{
    memset(graph, 0, sizeof(*graph));

    int fd = open(path, O_RDONLY);

    if (fd < 0) {
        fprintf(
            stderr,
            "atm-router: open(%s): %s\n",
            path,
            strerror(errno)
        );
        return 1;
    }

    struct stat st;

    if (fstat(fd, &st) != 0) {
        fprintf(
            stderr,
            "atm-router: fstat(%s): %s\n",
            path,
            strerror(errno)
        );
        close(fd);
        return 1;
    }

    if ((uint64_t)st.st_size < sizeof(atm_graph_header_t)) {
        fprintf(stderr, "atm-router: file troppo piccolo\n");
        close(fd);
        return 1;
    }

    void *mapping = mmap(
        NULL,
        (size_t)st.st_size,
        PROT_READ,
        MAP_PRIVATE,
        fd,
        0
    );

    close(fd);

    if (mapping == MAP_FAILED) {
        fprintf(
            stderr,
            "atm-router: mmap: %s\n",
            strerror(errno)
        );
        return 1;
    }

    graph->mapping = mapping;
    graph->size = (size_t)st.st_size;
    graph->header = mapping;

    const atm_graph_header_t *h = graph->header;

    if (
        h->magic != ATM_GRAPH_MAGIC
        || h->version != ATM_GRAPH_VERSION
    ) {
        fprintf(
            stderr,
            "atm-router: formato grafo non valido\n"
        );
        munmap(mapping, graph->size);
        memset(graph, 0, sizeof(*graph));
        return 1;
    }

#define CHECK_SECTION(offset, count, type)                              \
    do {                                                                \
        uint64_t section_end =                                          \
            (uint64_t)(offset)                                          \
            + (uint64_t)(count) * sizeof(type);                         \
        if (section_end > (uint64_t)graph->size) {                      \
            fprintf(stderr, "atm-router: grafo troncato\n");            \
            munmap(mapping, graph->size);                               \
            memset(graph, 0, sizeof(*graph));                           \
            return 1;                                                   \
        }                                                               \
    } while (0)

    CHECK_SECTION(
        h->stops_offset,
        h->stop_count,
        atm_stop_t
    );

    CHECK_SECTION(
        h->routes_offset,
        h->route_count,
        atm_route_t
    );

    CHECK_SECTION(
        h->connections_offset,
        h->connection_count,
        atm_connection_t
    );

    CHECK_SECTION(
        h->transfers_offset,
        h->transfer_count,
        atm_transfer_t
    );

#undef CHECK_SECTION

    if (
        h->strings_offset + h->strings_size
        > (uint64_t)graph->size
    ) {
        fprintf(
            stderr,
            "atm-router: tabella stringhe troncata\n"
        );
        munmap(mapping, graph->size);
        memset(graph, 0, sizeof(*graph));
        return 1;
    }

    graph->stops = (const atm_stop_t *)(
        (const unsigned char *)mapping
        + h->stops_offset
    );

    graph->routes = (const atm_route_t *)(
        (const unsigned char *)mapping
        + h->routes_offset
    );

    graph->connections = (const atm_connection_t *)(
        (const unsigned char *)mapping
        + h->connections_offset
    );

    graph->transfers = (const atm_transfer_t *)(
        (const unsigned char *)mapping
        + h->transfers_offset
    );

    graph->strings =
        (const char *)mapping
        + h->strings_offset;

    return 0;
}


static void graph_close(graph_t *graph)
{
    if (graph->mapping) {
        munmap(graph->mapping, graph->size);
    }

    memset(graph, 0, sizeof(*graph));
}


static void heap_free(heap_t *heap)
{
    free(heap->items);
    memset(heap, 0, sizeof(*heap));
}


static int heap_push(
    heap_t *heap,
    uint32_t node,
    uint32_t time
)
{
    if (heap->len == heap->cap) {
        size_t next_cap =
            heap->cap ? heap->cap * 2U : 1024U;

        heap_item_t *next = realloc(
            heap->items,
            next_cap * sizeof(*next)
        );

        if (!next) {
            return 1;
        }

        heap->items = next;
        heap->cap = next_cap;
    }

    size_t i = heap->len++;

    heap->items[i].node = node;
    heap->items[i].time = time;

    while (i > 0U) {
        size_t parent = (i - 1U) / 2U;

        if (
            heap->items[parent].time
            <= heap->items[i].time
        ) {
            break;
        }

        heap_item_t tmp = heap->items[parent];
        heap->items[parent] = heap->items[i];
        heap->items[i] = tmp;

        i = parent;
    }

    return 0;
}


static int heap_pop(
    heap_t *heap,
    heap_item_t *result
)
{
    if (heap->len == 0U) {
        return 1;
    }

    *result = heap->items[0];

    heap->len--;

    if (heap->len == 0U) {
        return 0;
    }

    heap->items[0] = heap->items[heap->len];

    size_t i = 0U;

    for (;;) {
        size_t left = i * 2U + 1U;
        size_t right = left + 1U;

        if (left >= heap->len) {
            break;
        }

        size_t best = left;

        if (
            right < heap->len
            && heap->items[right].time
               < heap->items[left].time
        ) {
            best = right;
        }

        if (
            heap->items[i].time
            <= heap->items[best].time
        ) {
            break;
        }

        heap_item_t tmp = heap->items[i];
        heap->items[i] = heap->items[best];
        heap->items[best] = tmp;

        i = best;
    }

    return 0;
}


static uint32_t node_id(
    uint32_t stop,
    uint32_t state,
    uint32_t stop_count
)
{
    return state * stop_count + stop;
}


static void decode_node(
    uint32_t node,
    uint32_t stop_count,
    uint32_t *stop,
    uint32_t *state
)
{
    *state = node / stop_count;
    *stop = node % stop_count;
}


static uint32_t lower_bound_connection(
    const graph_t *graph,
    const uint32_t *indices,
    uint32_t begin,
    uint32_t end,
    uint32_t time
)
{
    uint32_t lo = begin;
    uint32_t hi = end;

    while (lo < hi) {
        uint32_t mid = lo + (hi - lo) / 2U;

        const atm_connection_t *c =
            &graph->connections[indices[mid]];

        if (c->departure_s < time) {
            lo = mid + 1U;
        } else {
            hi = mid;
        }
    }

    return lo;
}


static void json_string(const char *text)
{
    putchar('"');

    for (
        const unsigned char *p =
            (const unsigned char *)text;
        *p;
        ++p
    ) {
        switch (*p) {
            case '"':
                fputs("\\\"", stdout);
                break;

            case '\\':
                fputs("\\\\", stdout);
                break;

            case '\n':
                fputs("\\n", stdout);
                break;

            case '\r':
                fputs("\\r", stdout);
                break;

            case '\t':
                fputs("\\t", stdout);
                break;

            default:
                if (*p < 32U) {
                    printf("\\u%04x", *p);
                } else {
                    putchar((int)*p);
                }
        }
    }

    putchar('"');
}


static int route(
    const graph_t *graph,
    double origin_lat,
    double origin_lon,
    double dest_lat,
    double dest_lon,
    uint32_t departure_time,
    const char *required_first_route,
    live_input_t *live_inputs,
    size_t live_count
)
{
    const uint32_t stop_count =
        graph->header->stop_count;

    const uint32_t connection_count =
        graph->header->connection_count;

    const uint32_t transfer_count =
        graph->header->transfer_count;

    /*
     * Indice connessioni uscenti per fermata.
     * Gli indici vengono inseriti nell'ordine globale di partenza,
     * quindi ogni bucket resta già ordinato temporalmente.
     */
    uint32_t *conn_offsets = calloc(
        (size_t)stop_count + 1U,
        sizeof(*conn_offsets)
    );

    uint32_t *conn_indices = malloc(
        (size_t)connection_count
        * sizeof(*conn_indices)
    );

    uint32_t *transfer_offsets = calloc(
        (size_t)stop_count + 1U,
        sizeof(*transfer_offsets)
    );

    uint32_t *transfer_indices = malloc(
        (size_t)transfer_count
        * sizeof(*transfer_indices)
    );

    if (
        !conn_offsets
        || !conn_indices
        || !transfer_offsets
        || !transfer_indices
    ) {
        fprintf(stderr, "atm-router: memoria insufficiente\n");
        free(conn_offsets);
        free(conn_indices);
        free(transfer_offsets);
        free(transfer_indices);
        return 1;
    }

    for (uint32_t i = 0U; i < connection_count; ++i) {
        uint32_t stop = graph->connections[i].from_stop;

        if (stop < stop_count) {
            conn_offsets[stop + 1U]++;
        }
    }

    for (uint32_t i = 1U; i <= stop_count; ++i) {
        conn_offsets[i] += conn_offsets[i - 1U];
    }

    uint32_t *cursor = malloc(
        (size_t)stop_count * sizeof(*cursor)
    );

    if (!cursor) {
        fprintf(stderr, "atm-router: memoria insufficiente\n");
        free(conn_offsets);
        free(conn_indices);
        free(transfer_offsets);
        free(transfer_indices);
        return 1;
    }

    memcpy(
        cursor,
        conn_offsets,
        (size_t)stop_count * sizeof(*cursor)
    );

    for (uint32_t i = 0U; i < connection_count; ++i) {
        uint32_t stop = graph->connections[i].from_stop;

        if (stop < stop_count) {
            conn_indices[cursor[stop]++] = i;
        }
    }

    for (uint32_t i = 0U; i < transfer_count; ++i) {
        uint32_t stop = graph->transfers[i].from_stop;

        if (stop < stop_count) {
            transfer_offsets[stop + 1U]++;
        }
    }

    for (uint32_t i = 1U; i <= stop_count; ++i) {
        transfer_offsets[i] += transfer_offsets[i - 1U];
    }

    memcpy(
        cursor,
        transfer_offsets,
        (size_t)stop_count * sizeof(*cursor)
    );

    for (uint32_t i = 0U; i < transfer_count; ++i) {
        uint32_t stop = graph->transfers[i].from_stop;

        if (stop < stop_count) {
            transfer_indices[cursor[stop]++] = i;
        }
    }

    free(cursor);

    size_t node_count =
        (size_t)stop_count * STATE_COUNT;

    uint32_t *dist = malloc(
        node_count * sizeof(*dist)
    );

    previous_t *prev = calloc(
        node_count,
        sizeof(*prev)
    );

    if (!dist || !prev) {
        fprintf(stderr, "atm-router: memoria insufficiente\n");
        free(dist);
        free(prev);
        free(conn_offsets);
        free(conn_indices);
        free(transfer_offsets);
        free(transfer_indices);
        return 1;
    }

    for (size_t i = 0U; i < node_count; ++i) {
        dist[i] = INF_TIME;
    }

    heap_t heap = {0};

    /*
     * Collegamento iniziale coordinate -> fermate.
     */
    for (uint32_t stop = 0U; stop < stop_count; ++stop) {
        double lat =
            (double)graph->stops[stop].lat_e7
            / 10000000.0;

        double lon =
            (double)graph->stops[stop].lon_e7
            / 10000000.0;

        double metres = distance_m(
            origin_lat,
            origin_lon,
            lat,
            lon
        );

        if (metres > ORIGIN_RADIUS_M) {
            continue;
        }

        uint32_t walk = walk_seconds(metres);

        if (
            departure_time > UINT32_MAX - walk
        ) {
            continue;
        }

        uint32_t arrival = departure_time + walk;

        uint32_t node = node_id(
            stop,
            STATE_PRE_TRANSIT,
            stop_count
        );

        if (arrival < dist[node]) {
            dist[node] = arrival;

            prev[node].kind = PREV_ORIGIN;
            prev[node].walk_seconds = walk;

            if (heap_push(&heap, node, arrival) != 0) {
                fprintf(
                    stderr,
                    "atm-router: memoria insufficiente\n"
                );
                heap_free(&heap);
                free(dist);
                free(prev);
                free(conn_offsets);
                free(conn_indices);
                free(transfer_offsets);
                free(transfer_indices);
                return 1;
            }
        }
    }


    /*
     * Realtime iniziale.
     *
     * ATM ci dà l'arrivo del primo mezzo alla fermata.
     * Lo associamo a una corsa GTFS della stessa
     * stop+line+direction e ne usiamo le percorrenze locali.
     *
     * Se non possiamo raggiungere la fermata prima dell'arrivo
     * live, quel mezzo non viene seminato.
     */
    for (size_t li = 0U; li < live_count; ++li) {
        live_input_t *live = &live_inputs[li];

        live->stop = find_stop_by_external_id(
            graph,
            live->stop_id
        );

        if (live->stop == UINT32_MAX) {
            continue;
        }

        if (
            departure_time
            > UINT32_MAX - live->wait_seconds
        ) {
            continue;
        }

        live->departure_s =
            departure_time + live->wait_seconds;

        uint32_t pre_node = node_id(
            live->stop,
            STATE_PRE_TRANSIT,
            stop_count
        );

        uint32_t best_connection = UINT32_MAX;
        uint32_t best_delta = UINT32_MAX;

        for (
            uint32_t ci = 0U;
            ci < connection_count;
            ++ci
        ) {
            const atm_connection_t *c =
                &graph->connections[ci];

            if (
                !connection_matches_live(
                    graph,
                    c,
                    live->stop,
                    live
                )
            ) {
                continue;
            }

            uint32_t delta =
                c->departure_s > live->departure_s
                ? c->departure_s - live->departure_s
                : live->departure_s - c->departure_s;

            if (delta < best_delta) {
                best_delta = delta;
                best_connection = ci;
            }
        }

        if (
            best_connection == UINT32_MAX
            || best_delta > LIVE_MATCH_MAX_DELTA_S
        ) {
            continue;
        }

        const atm_connection_t *anchor =
            &graph->connections[best_connection];

        live->anchor_connection = best_connection;
        live->trip = anchor->trip;
        live->route = anchor->route;
        live->shift_s =
            (int64_t)live->departure_s
            - (int64_t)anchor->departure_s;
        live->resolved = 1U;

        if (
            dist[pre_node] == INF_TIME
            || dist[pre_node] > live->departure_s
        ) {
            /*
             * Il mezzo esiste davvero ma non riusciamo
             * materialmente a raggiungerlo.
             */
            continue;
        }

        live->catchable = 1U;

        int started = 0;

        for (
            uint32_t ci = 0U;
            ci < connection_count;
            ++ci
        ) {
            const atm_connection_t *c =
                &graph->connections[ci];

            if (!started) {
                if (ci != best_connection) {
                    continue;
                }

                started = 1;
            }

            if (c->trip != live->trip) {
                continue;
            }

            if (
                c->departure_s
                < anchor->departure_s
            ) {
                continue;
            }

            int64_t shifted_arrival =
                (int64_t)c->arrival_s
                + live->shift_s;

            if (
                shifted_arrival < 0
                || shifted_arrival > UINT32_MAX
            ) {
                continue;
            }

            uint32_t next_node = node_id(
                c->to_stop,
                STATE_TRANSIT,
                stop_count
            );

            uint32_t arrival =
                (uint32_t)shifted_arrival;

            if (arrival < dist[next_node]) {
                dist[next_node] = arrival;

                prev[next_node].kind =
                    PREV_LIVE_TRANSIT;
                prev[next_node].prev_stop =
                    live->stop;
                prev[next_node].prev_state =
                    STATE_PRE_TRANSIT;
                prev[next_node].ref = ci;
                prev[next_node].aux =
                    (uint32_t)li;

                if (
                    heap_push(
                        &heap,
                        next_node,
                        arrival
                    ) != 0
                ) {
                    fprintf(
                        stderr,
                        "atm-router: memoria insufficiente\n"
                    );
                    heap_free(&heap);
                    free(dist);
                    free(prev);
                    free(conn_offsets);
                    free(conn_indices);
                    free(transfer_offsets);
                    free(transfer_indices);
                    return 1;
                }
            }
        }
    }

    uint32_t best_time = INF_TIME;
    uint32_t best_node = UINT32_MAX;
    uint32_t best_final_walk = 0U;

    /*
     * Se accesso + eventuali cambi a piedi + uscita finale
     * richiedono già almeno quanto andare direttamente a piedi,
     * quel candidato TPL non è utile.
     *
     * Usiamo lo stesso modello geometrico e la stessa velocità
     * pedonale già usati dal router per accesso e uscita.
     */
    const uint32_t direct_walk = walk_seconds(
        distance_m(
            origin_lat,
            origin_lon,
            dest_lat,
            dest_lon
        )
    );

    heap_item_t item;

    while (heap_pop(&heap, &item) == 0) {
        if (item.time != dist[item.node]) {
            continue;
        }

        if (
            best_time != INF_TIME
            && item.time >= best_time
        ) {
            break;
        }

        uint32_t stop;
        uint32_t state;

        decode_node(
            item.node,
            stop_count,
            &stop,
            &state
        );

        /*
         * Destinazione valida solo dopo almeno un tratto TPL.
         */
        if (
            state == STATE_TRANSIT
            || state == STATE_AFTER_WALK
        ) {
            double lat =
                (double)graph->stops[stop].lat_e7
                / 10000000.0;

            double lon =
                (double)graph->stops[stop].lon_e7
                / 10000000.0;

            double metres = distance_m(
                lat,
                lon,
                dest_lat,
                dest_lon
            );

            if (metres <= DEST_RADIUS_M) {
                uint32_t walk = walk_seconds(metres);

                /*
                 * Ricostruiamo solo la componente pedonale del
                 * candidato fino a questo nodo:
                 *
                 * - accesso iniziale;
                 * - eventuali interscambi a piedi;
                 * - uscita finale verso la destinazione.
                 *
                 * Importante: se il candidato viene scartato non
                 * facciamo "continue", perché questo stesso nodo
                 * può ancora portare a un percorso TPL successivo
                 * migliore.
                 */
                uint32_t candidate_walk = walk;
                uint32_t trace_node = item.node;
                int candidate_walk_known = 0;

                for (
                    size_t hops = 0U;
                    hops < node_count;
                    ++hops
                ) {
                    previous_t p = prev[trace_node];

                    if (p.kind == PREV_ORIGIN) {
                        if (
                            candidate_walk
                            > UINT32_MAX - p.walk_seconds
                        ) {
                            candidate_walk = UINT32_MAX;
                        } else {
                            candidate_walk += p.walk_seconds;
                        }

                        candidate_walk_known = 1;
                        break;
                    }

                    if (p.kind == PREV_WALK) {
                        if (
                            candidate_walk
                            > UINT32_MAX - p.walk_seconds
                        ) {
                            candidate_walk = UINT32_MAX;
                        } else {
                            candidate_walk += p.walk_seconds;
                        }
                    }

                    if (
                        p.kind != PREV_WALK
                        && p.kind != PREV_TRANSIT
                        && p.kind != PREV_LIVE_TRANSIT
                    ) {
                        break;
                    }

                    trace_node = node_id(
                        p.prev_stop,
                        p.prev_state,
                        stop_count
                    );
                }

                if (
                    (
                        !candidate_walk_known
                        || candidate_walk < direct_walk
                    )
                    && item.time <= UINT32_MAX - walk
                ) {
                    uint32_t arrival =
                        item.time + walk;

                    if (arrival < best_time) {
                        best_time = arrival;
                        best_node = item.node;
                        best_final_walk = walk;
                    }
                }
            }
        }

        /*
         * Interscambio pedonale: massimo uno consecutivo.
         */
        if (state == STATE_TRANSIT) {
            uint32_t begin = transfer_offsets[stop];
            uint32_t end = transfer_offsets[stop + 1U];

            for (uint32_t k = begin; k < end; ++k) {
                uint32_t transfer_index =
                    transfer_indices[k];

                const atm_transfer_t *t =
                    &graph->transfers[transfer_index];

                if (t->to_stop >= stop_count) {
                    continue;
                }

                if (
                    item.time
                    > UINT32_MAX - t->walk_seconds
                ) {
                    continue;
                }

                uint32_t arrival =
                    item.time + t->walk_seconds;

                uint32_t next_node = node_id(
                    t->to_stop,
                    STATE_AFTER_WALK,
                    stop_count
                );

                if (arrival < dist[next_node]) {
                    dist[next_node] = arrival;

                    prev[next_node].kind = PREV_WALK;
                    prev[next_node].prev_stop = stop;
                    prev[next_node].prev_state =
                        (uint8_t)state;
                    prev[next_node].ref =
                        transfer_index;
                    prev[next_node].walk_seconds =
                        t->walk_seconds;

                    if (
                        heap_push(
                            &heap,
                            next_node,
                            arrival
                        ) != 0
                    ) {
                        fprintf(
                            stderr,
                            "atm-router: memoria insufficiente\n"
                        );
                        heap_free(&heap);
                        free(dist);
                        free(prev);
                        free(conn_offsets);
                        free(conn_indices);
                        free(transfer_offsets);
                        free(transfer_indices);
                        return 1;
                    }
                }
            }
        }

        /*
         * REALTIME_AFTER_WALK_TRANSFER_START
         *
         * Dopo un interscambio pedonale siamo davanti a una
         * nuova salita. Se ATM conosce un mezzo live proprio
         * a questa fermata, usiamo il suo orario reale e
         * trasliamo le percorrenze GTFS del trip associato.
         *
         * Non richiediamo però che ogni fermata di cambio
         * abbia realtime: in assenza di un live locale il
         * GTFS programmato rimane utilizzabile.
         */
        if (
            state == STATE_AFTER_WALK
            && live_count > 0U
        ) {
            for (
                size_t li = 0U;
                li < live_count;
                ++li
            ) {
                const live_input_t *live =
                    &live_inputs[li];

                if (
                    !live->resolved
                    || live->stop != stop
                    || item.time > live->departure_s
                    || live->anchor_connection
                       >= connection_count
                ) {
                    continue;
                }

                const atm_connection_t *anchor =
                    &graph->connections[
                        live->anchor_connection
                    ];

                for (
                    uint32_t ci = 0U;
                    ci < connection_count;
                    ++ci
                ) {
                    const atm_connection_t *c =
                        &graph->connections[ci];

                    if (
                        c->trip != live->trip
                        || c->departure_s
                           < anchor->departure_s
                    ) {
                        continue;
                    }

                    int64_t shifted_departure =
                        (int64_t)c->departure_s
                        + live->shift_s;

                    int64_t shifted_arrival =
                        (int64_t)c->arrival_s
                        + live->shift_s;

                    if (
                        shifted_departure < 0
                        || shifted_departure
                           > UINT32_MAX
                        || shifted_arrival < 0
                        || shifted_arrival
                           > UINT32_MAX
                    ) {
                        continue;
                    }

                    /*
                     * La corsa live deve essere ancora
                     * materialmente prendibile quando
                     * arriviamo alla fermata di cambio.
                     */
                    if (
                        item.time
                        > (uint32_t)shifted_departure
                    ) {
                        continue;
                    }

                    uint32_t next_node = node_id(
                        c->to_stop,
                        STATE_TRANSIT,
                        stop_count
                    );

                    uint32_t arrival =
                        (uint32_t)shifted_arrival;

                    if (arrival >= dist[next_node]) {
                        continue;
                    }

                    dist[next_node] = arrival;

                    prev[next_node].kind =
                        PREV_LIVE_TRANSIT;

                    prev[next_node].prev_stop =
                        stop;

                    prev[next_node].prev_state =
                        (uint8_t)state;

                    prev[next_node].ref = ci;
                    prev[next_node].aux =
                        (uint32_t)li;

                    if (
                        heap_push(
                            &heap,
                            next_node,
                            arrival
                        ) != 0
                    ) {
                        fprintf(
                            stderr,
                            "atm-router: memoria "
                            "insufficiente\n"
                        );

                        heap_free(&heap);
                        free(dist);
                        free(prev);
                        free(conn_offsets);
                        free(conn_indices);
                        free(transfer_offsets);
                        free(transfer_indices);

                        return 1;
                    }
                }
            }
        }
        /* REALTIME_AFTER_WALK_TRANSFER_END */

        /*
         * Connessioni TPL prendibili da questa fermata.
         */
        uint32_t begin = conn_offsets[stop];
        uint32_t end = conn_offsets[stop + 1U];

        uint32_t first = lower_bound_connection(
            graph,
            conn_indices,
            begin,
            end,
            item.time
        );

        for (uint32_t k = first; k < end; ++k) {
            uint32_t connection_index =
                conn_indices[k];

            const atm_connection_t *c =
                &graph->connections[connection_index];

            if (
                c->to_stop >= stop_count
                || c->route >= graph->header->route_count
            ) {
                continue;
            }

            if (
                state == STATE_PRE_TRANSIT
                && required_first_route
                && *required_first_route
            ) {
                const char *route_name = graph_string(
                    graph,
                    graph->routes[c->route].short_name_offset
                );

                if (
                    strcmp(
                        route_name,
                        required_first_route
                    ) != 0
                ) {
                    continue;
                }
            }

            if (live_count > 0U) {
                const live_input_t *live =
                    live_for_connection(
                        graph,
                        live_inputs,
                        live_count,
                        stop,
                        c
                    );

                /*
                 * Per la prima salita accettiamo soltanto
                 * stop+line+direction verificati da ATM.
                 */
                if (
                    state == STATE_PRE_TRANSIT
                    && !live
                ) {
                    continue;
                }

                /*
                 * Dopo un cambio, invece, l'assenza di un
                 * live locale non deve eliminare il GTFS.
                 *
                 * Quando il live esiste, però, una vecchia
                 * partenza programmata precedente o uguale
                 * all'arrivo reale non può essere usata.
                 */
                if (
                    (
                        state == STATE_PRE_TRANSIT
                        || state == STATE_AFTER_WALK
                    )
                    && live
                    && c->departure_s
                       <= live->departure_s
                ) {
                    continue;
                }
            }

            /*
             * Se esiste già un arrivo migliore alla destinazione,
             * partenze successive non potranno migliorarlo.
             */
            if (
                best_time != INF_TIME
                && c->departure_s >= best_time
            ) {
                break;
            }

            uint32_t next_node = node_id(
                c->to_stop,
                STATE_TRANSIT,
                stop_count
            );

            if (c->arrival_s < dist[next_node]) {
                dist[next_node] = c->arrival_s;

                prev[next_node].kind = PREV_TRANSIT;
                prev[next_node].prev_stop = stop;
                prev[next_node].prev_state =
                    (uint8_t)state;
                prev[next_node].ref =
                    connection_index;

                if (
                    heap_push(
                        &heap,
                        next_node,
                        c->arrival_s
                    ) != 0
                ) {
                    fprintf(
                        stderr,
                        "atm-router: memoria insufficiente\n"
                    );
                    heap_free(&heap);
                    free(dist);
                    free(prev);
                    free(conn_offsets);
                    free(conn_indices);
                    free(transfer_offsets);
                    free(transfer_indices);
                    return 1;
                }
            }
        }
    }

    heap_free(&heap);

    if (best_node == UINT32_MAX) {
        printf(
            "{\"status\":\"no_route\","
            "\"service_date\":%" PRIu32 "}\n",
            graph->header->service_date_ymd
        );

        free(dist);
        free(prev);
        free(conn_offsets);
        free(conn_indices);
        free(transfer_offsets);
        free(transfer_indices);

        return 0;
    }

    /*
     * Ricostruzione percorso.
     */
    path_step_t *steps = calloc(
        node_count,
        sizeof(*steps)
    );

    if (!steps) {
        fprintf(stderr, "atm-router: memoria insufficiente\n");
        free(dist);
        free(prev);
        free(conn_offsets);
        free(conn_indices);
        free(transfer_offsets);
        free(transfer_indices);
        return 1;
    }

    size_t step_count = 0U;

    uint32_t current_node = best_node;
    uint32_t origin_stop = UINT32_MAX;
    uint32_t origin_walk = 0U;

    for (;;) {
        uint32_t current_stop;
        uint32_t current_state;

        decode_node(
            current_node,
            stop_count,
            &current_stop,
            &current_state
        );

        previous_t p = prev[current_node];

        if (p.kind == PREV_ORIGIN) {
            origin_stop = current_stop;
            origin_walk = p.walk_seconds;
            break;
        }

        if (
            p.kind != PREV_WALK
            && p.kind != PREV_TRANSIT
            && p.kind != PREV_LIVE_TRANSIT
        ) {
            fprintf(
                stderr,
                "atm-router: percorso non ricostruibile\n"
            );
            free(steps);
            free(dist);
            free(prev);
            free(conn_offsets);
            free(conn_indices);
            free(transfer_offsets);
            free(transfer_indices);
            return 1;
        }

        path_step_t *step = &steps[step_count++];

        step->kind = p.kind;
        step->from_stop = p.prev_stop;
        step->to_stop = current_stop;
        step->connection = p.ref;
        step->live_index = p.aux;
        step->walk_seconds = p.walk_seconds;
        step->arrival_s = dist[current_node];

        current_node = node_id(
            p.prev_stop,
            p.prev_state,
            stop_count
        );
    }

    for (
        size_t i = 0U, j = step_count ? step_count - 1U : 0U;
        i < j;
        ++i, --j
    ) {
        path_step_t tmp = steps[i];
        steps[i] = steps[j];
        steps[j] = tmp;
    }

    /*
     * Compattiamo connessioni consecutive dello stesso trip.
     */
    output_leg_t *legs = calloc(
        step_count ? step_count : 1U,
        sizeof(*legs)
    );

    if (!legs) {
        fprintf(stderr, "atm-router: memoria insufficiente\n");
        free(steps);
        free(dist);
        free(prev);
        free(conn_offsets);
        free(conn_indices);
        free(transfer_offsets);
        free(transfer_indices);
        return 1;
    }

    size_t leg_count = 0U;

    for (size_t i = 0U; i < step_count; ++i) {
        const path_step_t *step = &steps[i];

        if (step->kind == PREV_WALK) {
            output_leg_t *leg = &legs[leg_count++];

            leg->kind = PREV_WALK;
            leg->from_stop = step->from_stop;
            leg->to_stop = step->to_stop;
            leg->walk_seconds = step->walk_seconds;

            continue;
        }

        const atm_connection_t *c =
            &graph->connections[step->connection];

        if (step->kind == PREV_LIVE_TRANSIT) {
            if (step->live_index >= live_count) {
                fprintf(
                    stderr,
                    "atm-router: indice live non valido\n"
                );
                free(legs);
                free(steps);
                free(dist);
                free(prev);
                free(conn_offsets);
                free(conn_indices);
                free(transfer_offsets);
                free(transfer_indices);
                return 1;
            }

            const live_input_t *live =
                &live_inputs[step->live_index];

            output_leg_t *leg =
                &legs[leg_count++];

            leg->kind = PREV_TRANSIT;
            leg->live = 1U;

            leg->from_stop = step->from_stop;
            leg->to_stop = step->to_stop;

            leg->route = c->route;
            leg->trip = c->trip;

            leg->departure_s =
                live->departure_s;
            leg->arrival_s =
                step->arrival_s;

            leg->live_wait_seconds =
                live->wait_seconds;

            continue;
        }

        if (
            leg_count > 0U
            && legs[leg_count - 1U].kind == PREV_TRANSIT
            && !legs[leg_count - 1U].live
            && legs[leg_count - 1U].trip == c->trip
            && legs[leg_count - 1U].route == c->route
            && legs[leg_count - 1U].to_stop == c->from_stop
        ) {
            legs[leg_count - 1U].to_stop = c->to_stop;
            legs[leg_count - 1U].arrival_s = c->arrival_s;
            continue;
        }

        output_leg_t *leg = &legs[leg_count++];

        leg->kind = PREV_TRANSIT;
        leg->from_stop = c->from_stop;
        leg->to_stop = c->to_stop;
        leg->route = c->route;
        leg->trip = c->trip;
        leg->departure_s = c->departure_s;
        leg->arrival_s = c->arrival_s;
    }

    uint32_t best_stop;
    uint32_t best_state_unused;

    decode_node(
        best_node,
        stop_count,
        &best_stop,
        &best_state_unused
    );

    printf("{");
    printf("\"status\":\"ok\",");
    printf(
        "\"service_date\":%" PRIu32 ",",
        graph->header->service_date_ymd
    );
    printf(
        "\"query_departure_s\":%" PRIu32 ",",
        departure_time
    );
    printf(
        "\"arrival_s\":%" PRIu32 ",",
        best_time
    );
    printf(
        "\"total_seconds\":%" PRIu32 ",",
        best_time - departure_time
    );

    printf("\"origin_stop\":");
    json_string(
        graph_string(
            graph,
            graph->stops[origin_stop].name_offset
        )
    );
    printf(",");

    printf("\"origin_stop_id\":");
    json_string(
        graph_string(
            graph,
            graph->stops[origin_stop].external_id_offset
        )
    );
    printf(",");

    printf(
        "\"origin_walk_seconds\":%" PRIu32 ",",
        origin_walk
    );

    printf("\"destination_stop\":");
    json_string(
        graph_string(
            graph,
            graph->stops[best_stop].name_offset
        )
    );
    printf(",");

    printf("\"destination_stop_id\":");
    json_string(
        graph_string(
            graph,
            graph->stops[best_stop].external_id_offset
        )
    );
    printf(",");

    printf(
        "\"final_walk_seconds\":%" PRIu32 ",",
        best_final_walk
    );

    printf("\"legs\":[");

    for (size_t i = 0U; i < leg_count; ++i) {
        if (i) {
            putchar(',');
        }

        const output_leg_t *leg = &legs[i];

        if (leg->kind == PREV_WALK) {
            printf("{\"mode\":\"walk\",\"from\":");

            json_string(
                graph_string(
                    graph,
                    graph->stops[leg->from_stop].name_offset
                )
            );

            printf(",\"to\":");

            json_string(
                graph_string(
                    graph,
                    graph->stops[leg->to_stop].name_offset
                )
            );

            printf(
                ",\"walk_seconds\":%" PRIu32 "}",
                leg->walk_seconds
            );

            continue;
        }

        printf("{\"mode\":\"transit\",\"route\":");

        json_string(
            graph_string(
                graph,
                graph->routes[leg->route].short_name_offset
            )
        );

        printf(
            ",\"live\":%s",
            leg->live ? "true" : "false"
        );

        if (leg->live) {
            printf(
                ",\"live_wait_seconds\":%" PRIu32,
                leg->live_wait_seconds
            );
        }

        printf(",\"from\":");

        json_string(
            graph_string(
                graph,
                graph->stops[leg->from_stop].name_offset
            )
        );

        printf(",\"from_stop_id\":");

        json_string(
            graph_string(
                graph,
                graph->stops[leg->from_stop].external_id_offset
            )
        );

        printf(",\"to\":");

        json_string(
            graph_string(
                graph,
                graph->stops[leg->to_stop].name_offset
            )
        );

        printf(",\"to_stop_id\":");

        json_string(
            graph_string(
                graph,
                graph->stops[leg->to_stop].external_id_offset
            )
        );

        printf(
            ",\"departure_s\":%" PRIu32
            ",\"arrival_s\":%" PRIu32 "}",
            leg->departure_s,
            leg->arrival_s
        );
    }

    printf("]}\n");

    free(legs);
    free(steps);
    free(dist);
    free(prev);
    free(conn_offsets);
    free(conn_indices);
    free(transfer_offsets);
    free(transfer_indices);

    return 0;
}



/* ATM_ROUTER_NEARBY_START */

typedef struct {
    uint32_t stop;
    double distance_m;
} nearby_stop_t;


static int nearby_stop_compare(
    const void *left_ptr,
    const void *right_ptr
)
{
    const nearby_stop_t *left = left_ptr;
    const nearby_stop_t *right = right_ptr;

    if (left->distance_m < right->distance_m) {
        return -1;
    }

    if (left->distance_m > right->distance_m) {
        return 1;
    }

    if (left->stop < right->stop) {
        return -1;
    }

    if (left->stop > right->stop) {
        return 1;
    }

    return 0;
}


static int nearby_stops(
    const graph_t *graph,
    double lat,
    double lon,
    double radius_m,
    uint32_t limit
)
{
    if (
        radius_m <= 0.0
        || radius_m > 5000.0
        || limit == 0U
        || limit > 100U
    ) {
        fprintf(
            stderr,
            "atm-router: radius/limit non validi\n"
        );
        return 2;
    }

    uint32_t stop_count = graph->header->stop_count;

    nearby_stop_t *matches = malloc(
        (size_t)stop_count * sizeof(*matches)
    );

    if (!matches) {
        fprintf(
            stderr,
            "atm-router: memoria insufficiente\n"
        );
        return 1;
    }

    size_t count = 0U;

    for (uint32_t i = 0U; i < stop_count; ++i) {
        double stop_lat =
            (double)graph->stops[i].lat_e7
            / 10000000.0;

        double stop_lon =
            (double)graph->stops[i].lon_e7
            / 10000000.0;

        double metres = distance_m(
            lat,
            lon,
            stop_lat,
            stop_lon
        );

        if (metres > radius_m) {
            continue;
        }

        matches[count].stop = i;
        matches[count].distance_m = metres;
        count++;
    }

    qsort(
        matches,
        count,
        sizeof(*matches),
        nearby_stop_compare
    );

    if (count > limit) {
        count = limit;
    }

    printf(
        "{\"status\":\"ok\","
        "\"service_date\":%" PRIu32 ","
        "\"stops\":[",
        graph->header->service_date_ymd
    );

    for (size_t i = 0U; i < count; ++i) {
        if (i) {
            putchar(',');
        }

        uint32_t stop = matches[i].stop;

        const atm_stop_t *record =
            &graph->stops[stop];

        double stop_lat =
            (double)record->lat_e7
            / 10000000.0;

        double stop_lon =
            (double)record->lon_e7
            / 10000000.0;

        uint32_t walk =
            walk_seconds(matches[i].distance_m);

        printf("{\"stop_id\":");

        json_string(
            graph_string(
                graph,
                record->external_id_offset
            )
        );

        printf(",\"name\":");

        json_string(
            graph_string(
                graph,
                record->name_offset
            )
        );

        printf(
            ",\"lat\":%.7f"
            ",\"lon\":%.7f"
            ",\"distance_m\":%.0f"
            ",\"walk_seconds\":%" PRIu32
            "}",
            stop_lat,
            stop_lon,
            matches[i].distance_m,
            walk
        );
    }

    printf("]}\n");

    free(matches);

    return 0;
}

/* ATM_ROUTER_NEARBY_END */


static int inspect_graph(const char *path)
{
    graph_t graph;

    if (graph_open(path, &graph) != 0) {
        return 1;
    }

    const atm_graph_header_t *h = graph.header;

    printf("magic=RATM\n");
    printf("version=%" PRIu32 "\n", h->version);
    printf(
        "service_date=%" PRIu32 "\n",
        h->service_date_ymd
    );
    printf("stops=%" PRIu32 "\n", h->stop_count);
    printf("routes=%" PRIu32 "\n", h->route_count);
    printf(
        "connections=%" PRIu32 "\n",
        h->connection_count
    );
    printf(
        "transfers=%" PRIu32 "\n",
        h->transfer_count
    );

    graph_close(&graph);

    return 0;
}


int main(int argc, char **argv)
{
    if (
        argc == 3
        && strcmp(argv[1], "--inspect") == 0
    ) {
        return inspect_graph(argv[2]);
    }

    if (
        argc == 7
        && strcmp(argv[1], "--nearby") == 0
    ) {
        graph_t graph;

        if (graph_open(argv[2], &graph) != 0) {
            return 1;
        }

        char *end = NULL;

        double lat = strtod(argv[3], &end);

        if (!end || *end) {
            fprintf(stderr, "lat non valida\n");
            graph_close(&graph);
            return 2;
        }

        end = NULL;

        double lon = strtod(argv[4], &end);

        if (!end || *end) {
            fprintf(stderr, "lon non valida\n");
            graph_close(&graph);
            return 2;
        }

        end = NULL;

        double radius_m = strtod(argv[5], &end);

        if (!end || *end) {
            fprintf(stderr, "radius non valido\n");
            graph_close(&graph);
            return 2;
        }

        end = NULL;

        unsigned long raw_limit = strtoul(
            argv[6],
            &end,
            10
        );

        if (
            !end
            || *end
            || raw_limit == 0UL
            || raw_limit > 100UL
        ) {
            fprintf(stderr, "limit non valido\n");
            graph_close(&graph);
            return 2;
        }

        int result = nearby_stops(
            &graph,
            lat,
            lon,
            radius_m,
            (uint32_t)raw_limit
        );

        graph_close(&graph);

        return result;
    }

    if (
        argc >= 8
        && strcmp(argv[1], "--route") == 0
    ) {
        graph_t graph;

        if (graph_open(argv[2], &graph) != 0) {
            return 1;
        }

        char *end = NULL;

        double origin_lat = strtod(argv[3], &end);
        if (!end || *end) {
            fprintf(stderr, "origine lat non valida\n");
            graph_close(&graph);
            return 2;
        }

        double origin_lon = strtod(argv[4], &end);
        if (!end || *end) {
            fprintf(stderr, "origine lon non valida\n");
            graph_close(&graph);
            return 2;
        }

        double dest_lat = strtod(argv[5], &end);
        if (!end || *end) {
            fprintf(stderr, "destinazione lat non valida\n");
            graph_close(&graph);
            return 2;
        }

        double dest_lon = strtod(argv[6], &end);
        if (!end || *end) {
            fprintf(stderr, "destinazione lon non valida\n");
            graph_close(&graph);
            return 2;
        }

        uint32_t departure_time;

        if (parse_time(argv[7], &departure_time) != 0) {
            fprintf(
                stderr,
                "ora non valida; usare HH:MM:SS\n"
            );
            graph_close(&graph);
            return 2;
        }

        const char *required_first_route = NULL;

        live_input_t live_inputs[MAX_LIVE_INPUTS];
        memset(
            live_inputs,
            0,
            sizeof(live_inputs)
        );

        size_t live_count = 0U;

        int argi = 8;

        while (argi < argc) {
            if (
                strcmp(argv[argi], "--first-route") == 0
            ) {
                if (argi + 1 >= argc) {
                    fprintf(
                        stderr,
                        "--first-route richiede LINE\n"
                    );
                    graph_close(&graph);
                    return 2;
                }

                required_first_route =
                    argv[argi + 1];

                argi += 2;
                continue;
            }

            if (strcmp(argv[argi], "--live") == 0) {
                if (
                    argi + 4 >= argc
                    || live_count >= MAX_LIVE_INPUTS
                ) {
                    fprintf(
                        stderr,
                        "--live richiede "
                        "STOP LINE DIRECTION WAIT_SECONDS\n"
                    );
                    graph_close(&graph);
                    return 2;
                }

                char *endp = NULL;

                unsigned long direction = strtoul(
                    argv[argi + 3],
                    &endp,
                    10
                );

                if (
                    !endp
                    || *endp
                    || direction > 1UL
                ) {
                    fprintf(
                        stderr,
                        "direction live non valida\n"
                    );
                    graph_close(&graph);
                    return 2;
                }

                endp = NULL;

                unsigned long wait = strtoul(
                    argv[argi + 4],
                    &endp,
                    10
                );

                if (
                    !endp
                    || *endp
                    || wait > 86400UL
                ) {
                    fprintf(
                        stderr,
                        "wait live non valido\n"
                    );
                    graph_close(&graph);
                    return 2;
                }

                live_input_t *live =
                    &live_inputs[live_count++];

                live->stop_id = argv[argi + 1];
                live->line = argv[argi + 2];
                live->direction =
                    (uint32_t)direction;
                live->wait_seconds =
                    (uint32_t)wait;

                argi += 5;
                continue;
            }

            fprintf(
                stderr,
                "argomento route sconosciuto: %s\n",
                argv[argi]
            );
            graph_close(&graph);
            return 2;
        }

        int result = route(
            &graph,
            origin_lat,
            origin_lon,
            dest_lat,
            dest_lon,
            departure_time,
            required_first_route,
            live_inputs,
            live_count
        );

        graph_close(&graph);

        return result;
    }

    fprintf(
        stderr,
        "uso:\n"
        "  %s --inspect GRAPH.bin\n"
        "  %s --nearby GRAPH.bin LAT LON RADIUS_M LIMIT\n"
        "  %s --route GRAPH.bin "
        "ORIGIN_LAT ORIGIN_LON DEST_LAT DEST_LON HH:MM:SS "
        "[--first-route LINE] "
        "[--live STOP LINE DIRECTION WAIT_SECONDS]...\n",
        argv[0],
        argv[0],
        argv[0]
    );

    return 2;
}
