CC ?= cc
CFLAGS ?= -O2 -pipe
WARN = -Wall -Wextra -Werror -pedantic
ROOT := ../..
TARGET := ralf-teacher-core-mcp

.PHONY: all clean
all: $(TARGET)

$(TARGET): main.c $(ROOT)/third_party/jsmn/jsmn.h
	$(CC) -std=c11 $(CFLAGS) $(WARN) -I$(ROOT)/third_party/jsmn main.c -lm -o $@

clean:
	rm -f $(TARGET)
