#include "bottazzi_core.h"

#include <ctype.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

static int ascii_contains_casefold(const char *haystack, const char *needle) {
    size_t i;
    size_t j;
    size_t hay_len;
    size_t needle_len;

    if (haystack == NULL || needle == NULL) {
        return 0;
    }
    hay_len = strlen(haystack);
    needle_len = strlen(needle);
    if (needle_len == 0 || needle_len > hay_len) {
        return 0;
    }
    for (i = 0; i + needle_len <= hay_len; ++i) {
        for (j = 0; j < needle_len; ++j) {
            unsigned char a = (unsigned char)haystack[i + j];
            unsigned char b = (unsigned char)needle[j];
            if (tolower(a) != tolower(b)) {
                break;
            }
        }
        if (j == needle_len) {
            return 1;
        }
    }
    return 0;
}

static int reason_requires_human(const char *reason) {
    static const char *const markers[] = {
        "approval",
        "approv",
        "conferm",
        "confirm",
        "otp",
        "firma",
        "sign",
        "pin"
    };
    size_t i;

    if (reason == NULL || reason[0] == '\0') {
        return 0;
    }
    for (i = 0; i < sizeof(markers) / sizeof(markers[0]); ++i) {
        if (ascii_contains_casefold(reason, markers[i])) {
            return 1;
        }
    }
    return 0;
}

int bt_notification_decision(
    int position,
    int priority,
    int runnable,
    const char *state,
    const char *blocked_reason
) {
    if (state != NULL && strcmp(state, "priority_conflict") == 0) {
        return BT_NOTIFY_PRIORITY_CONFLICT;
    }
    if (state != NULL && strcmp(state, "blocked") == 0) {
        if (reason_requires_human(blocked_reason)) {
            return BT_NOTIFY_APPROVAL_REQUIRED;
        }
        return BT_NOTIFY_NONE;
    }
    if (state == NULL || strcmp(state, "queued") != 0 || !runnable || position != 1) {
        return BT_NOTIFY_NONE;
    }
    if (priority >= 85) {
        return BT_NOTIFY_HIGH_PRIORITY;
    }
    return BT_NOTIFY_NEXT_TASK;
}

static uint64_t fnv1a_bytes(uint64_t hash, const unsigned char *data, size_t size) {
    size_t i;
    for (i = 0; i < size; ++i) {
        hash ^= (uint64_t)data[i];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}

static uint64_t fnv1a_text(uint64_t hash, const char *text) {
    static const unsigned char separator = 0xff;
    if (text != NULL) {
        hash = fnv1a_bytes(hash, (const unsigned char *)text, strlen(text));
    }
    return fnv1a_bytes(hash, &separator, 1);
}

uint64_t bt_event_hash(
    const char *task_id,
    const char *state,
    const char *blocked_reason,
    int position,
    int priority
) {
    char numbers[64];
    uint64_t hash = UINT64_C(1469598103934665603);

    hash = fnv1a_text(hash, task_id);
    hash = fnv1a_text(hash, state);
    hash = fnv1a_text(hash, blocked_reason);
    (void)snprintf(numbers, sizeof(numbers), "%d:%d", position, priority);
    hash = fnv1a_text(hash, numbers);
    return hash;
}
