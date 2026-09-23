#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "../app/src/main/cpp/bottazzi_core.h"

int main(void) {
    uint64_t a;
    uint64_t b;

    assert(bt_notification_decision(1, 85, 1, "queued", "") == BT_NOTIFY_HIGH_PRIORITY);
    assert(bt_notification_decision(1, 50, 1, "queued", "") == BT_NOTIFY_NEXT_TASK);
    assert(bt_notification_decision(2, 100, 1, "queued", "") == BT_NOTIFY_NONE);
    assert(bt_notification_decision(1, 90, 0, "blocked", "awaiting_approval") == BT_NOTIFY_APPROVAL_REQUIRED);
    assert(bt_notification_decision(1, 90, 0, "blocked", "attesa conferma") == BT_NOTIFY_APPROVAL_REQUIRED);
    assert(bt_notification_decision(1, 90, 0, "blocked", "rete non disponibile") == BT_NOTIFY_NONE);
    assert(bt_notification_decision(1, 90, 0, "priority_conflict", "") == BT_NOTIFY_PRIORITY_CONFLICT);

    a = bt_event_hash("task-1", "queued", "", 1, 85);
    b = bt_event_hash("task-1", "queued", "", 1, 85);
    assert(a == b);
    assert(a != bt_event_hash("task-1", "queued", "", 2, 85));
    assert(a != bt_event_hash("task-2", "queued", "", 1, 85));

    puts("bottazzi_core_test: ok");
    return 0;
}
