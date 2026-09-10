#ifndef RALF_ATM_ROUTER_H
#define RALF_ATM_ROUTER_H

#include <stdint.h>

/*
 * Su little-endian i quattro byte nel file sono letteralmente "RATM".
 */
#define ATM_GRAPH_MAGIC   0x4d544152U
#define ATM_GRAPH_VERSION 3U

/*
 * I due bit bassi di atm_connection_t.flags identificano
 * la direzione GTFS della corsa.
 */
#define ATM_CONN_DIR_0       0U
#define ATM_CONN_DIR_1       1U
#define ATM_CONN_DIR_UNKNOWN 2U
#define ATM_CONN_DIR_MASK    3U

typedef struct {
    uint32_t magic;
    uint32_t version;

    /*
     * YYYYMMDD della giornata GTFS già risolta dall'importer.
     * Il router C non deve interpretare calendar.txt/calendar_dates.txt.
     */
    uint32_t service_date_ymd;
    uint32_t reserved;

    uint32_t stop_count;
    uint32_t route_count;
    uint32_t connection_count;
    uint32_t transfer_count;

    uint64_t stops_offset;
    uint64_t routes_offset;
    uint64_t connections_offset;
    uint64_t transfers_offset;
    uint64_t strings_offset;
    uint64_t strings_size;
} atm_graph_header_t;

/*
 * Coordinate in gradi * 1e7:
 * niente double nel file e formato stabile.
 */
typedef struct {
    int32_t lat_e7;
    int32_t lon_e7;

    uint32_t name_offset;
    uint32_t external_id_offset;
} atm_stop_t;

typedef struct {
    uint32_t short_name_offset;
    uint32_t route_id_offset;
    uint16_t route_type;
    uint16_t reserved;
} atm_route_t;

/*
 * Una connessione GTFS fra due fermate consecutive.
 * I tempi sono secondi dal principio del "service day":
 * sono quindi validi anche valori > 86400 per 24:xx / 25:xx.
 */
typedef struct {
    uint32_t from_stop;
    uint32_t to_stop;

    uint32_t route;
    uint32_t trip;

    uint32_t departure_s;
    uint32_t arrival_s;

    uint32_t flags;
} atm_connection_t;

/*
 * Collegamento pedonale/interscambio locale.
 */
typedef struct {
    uint32_t from_stop;
    uint32_t to_stop;
    uint16_t walk_seconds;
    uint16_t reserved;
} atm_transfer_t;


_Static_assert(sizeof(atm_graph_header_t) == 80, "bad graph header size");
_Static_assert(sizeof(atm_stop_t) == 16, "bad stop size");
_Static_assert(sizeof(atm_route_t) == 12, "bad route size");
_Static_assert(sizeof(atm_connection_t) == 28, "bad connection size");
_Static_assert(sizeof(atm_transfer_t) == 12, "bad transfer size");

#endif
