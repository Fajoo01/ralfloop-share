#ifndef RALF_SQLITE3_MIN_H
#define RALF_SQLITE3_MIN_H

#include <stdint.h>

typedef struct sqlite3 sqlite3;
typedef struct sqlite3_stmt sqlite3_stmt;
typedef void (*sqlite3_destructor_type)(void *);

#define SQLITE_OK 0
#define SQLITE_ROW 100
#define SQLITE_DONE 101
#define SQLITE_OPEN_READONLY 0x00000001
#define SQLITE_OPEN_URI 0x00000040
#define SQLITE_OPEN_NOMUTEX 0x00008000
#define SQLITE_TRANSIENT ((sqlite3_destructor_type)(intptr_t)-1)

int sqlite3_open_v2(const char *, sqlite3 **, int, const char *);
int sqlite3_close(sqlite3 *);
const char *sqlite3_errmsg(sqlite3 *);
int sqlite3_prepare_v2(sqlite3 *, const char *, int, sqlite3_stmt **, const char **);
int sqlite3_finalize(sqlite3_stmt *);
int sqlite3_reset(sqlite3_stmt *);
int sqlite3_clear_bindings(sqlite3_stmt *);
int sqlite3_bind_text(sqlite3_stmt *, int, const char *, int, sqlite3_destructor_type);
int sqlite3_bind_int(sqlite3_stmt *, int, int);
int sqlite3_step(sqlite3_stmt *);
int sqlite3_column_int(sqlite3_stmt *, int);
const unsigned char *sqlite3_column_text(sqlite3_stmt *, int);

#endif
