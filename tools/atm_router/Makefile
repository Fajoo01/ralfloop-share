CC ?= cc
CFLAGS ?= -O3 -std=c11 -Wall -Wextra -Werror -pedantic

all: atm-router

atm-router: atm_router.c atm_router.h
	$(CC) $(CFLAGS) -o $@ atm_router.c -lm

clean:
	rm -f atm-router

.PHONY: all clean
