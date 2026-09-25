#ifndef BOTTAZZI_CORE_H
#define BOTTAZZI_CORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum bt_notification_kind {
    BT_NOTIFY_NONE = 0,
    BT_NOTIFY_HIGH_PRIORITY = 1,
    BT_NOTIFY_APPROVAL_REQUIRED = 2,
    BT_NOTIFY_BLOCKED = 3,
    BT_NOTIFY_NEXT_TASK = 4,
    BT_NOTIFY_PRIORITY_CONFLICT = 5
};

int bt_notification_decision(
    int position,
    int priority,
    int runnable,
    const char *state,
    const char *blocked_reason
);

uint64_t bt_event_hash(
    const char *task_id,
    const char *state,
    const char *blocked_reason,
    int position,
    int priority
);

#ifdef __cplusplus
}
#endif

#endif
