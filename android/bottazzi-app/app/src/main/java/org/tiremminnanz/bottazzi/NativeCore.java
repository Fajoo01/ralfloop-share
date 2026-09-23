package org.tiremminnanz.bottazzi;

final class NativeCore {
    static final int NOTIFY_NONE = 0;
    static final int NOTIFY_HIGH_PRIORITY = 1;
    static final int NOTIFY_APPROVAL_REQUIRED = 2;
    static final int NOTIFY_BLOCKED = 3;
    static final int NOTIFY_NEXT_TASK = 4;
    static final int NOTIFY_PRIORITY_CONFLICT = 5;

    static {
        System.loadLibrary("bottazzi_native");
    }

    private NativeCore() {}

    static native String nativeVersion();

    static native int nativeNotificationDecision(
        int position,
        int priority,
        boolean runnable,
        String state,
        String blockedReason
    );

    static native String nativeEventKey(
        String taskId,
        String state,
        String blockedReason,
        int position,
        int priority
    );
}
