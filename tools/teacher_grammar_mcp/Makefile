CC ?= cc
CFLAGS ?= -O2 -pipe
WARN = -Wall -Wextra -Werror -pedantic
ROOT := ../..
TARGET := ralf-teacher-grammar-mcp
SQLITE_LIB ?= /lib/x86_64-linux-gnu/libsqlite3.so.0

.PHONY: all clean
all: $(TARGET)

$(TARGET): main.c sqlite3_min.h $(ROOT)/third_party/jsmn/jsmn.h
	$(CC) -std=c11 $(CFLAGS) $(WARN) \
		-I$(ROOT)/third_party/jsmn -I. \
		main.c $(SQLITE_LIB) -o $@

clean:
	rm -f $(TARGET)
