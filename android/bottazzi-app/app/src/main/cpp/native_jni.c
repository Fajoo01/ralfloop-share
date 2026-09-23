#include <jni.h>
#include <stdint.h>
#include <stdio.h>

#include "bottazzi_core.h"

static const char *get_utf(JNIEnv *env, jstring value) {
    if (value == NULL) {
        return NULL;
    }
    return (*env)->GetStringUTFChars(env, value, NULL);
}

static void release_utf(JNIEnv *env, jstring value, const char *chars) {
    if (value != NULL && chars != NULL) {
        (*env)->ReleaseStringUTFChars(env, value, chars);
    }
}

JNIEXPORT jstring JNICALL
Java_org_tiremminnanz_bottazzi_NativeCore_nativeVersion(JNIEnv *env, jclass clazz) {
    (void)clazz;
    return (*env)->NewStringUTF(env, "bottazzi-native-c/1.1");
}

JNIEXPORT jint JNICALL
Java_org_tiremminnanz_bottazzi_NativeCore_nativeNotificationDecision(
    JNIEnv *env,
    jclass clazz,
    jint position,
    jint priority,
    jboolean runnable,
    jstring state,
    jstring blocked_reason
) {
    const char *state_chars;
    const char *reason_chars;
    int result;

    (void)clazz;
    state_chars = get_utf(env, state);
    reason_chars = get_utf(env, blocked_reason);
    result = bt_notification_decision(
        (int)position,
        (int)priority,
        runnable == JNI_TRUE,
        state_chars,
        reason_chars
    );
    release_utf(env, blocked_reason, reason_chars);
    release_utf(env, state, state_chars);
    return (jint)result;
}

JNIEXPORT jstring JNICALL
Java_org_tiremminnanz_bottazzi_NativeCore_nativeEventKey(
    JNIEnv *env,
    jclass clazz,
    jstring task_id,
    jstring state,
    jstring blocked_reason,
    jint position,
    jint priority
) {
    const char *task_chars;
    const char *state_chars;
    const char *reason_chars;
    uint64_t hash;
    char key[32];

    (void)clazz;
    task_chars = get_utf(env, task_id);
    state_chars = get_utf(env, state);
    reason_chars = get_utf(env, blocked_reason);
    hash = bt_event_hash(
        task_chars,
        state_chars,
        reason_chars,
        (int)position,
        (int)priority
    );
    release_utf(env, blocked_reason, reason_chars);
    release_utf(env, state, state_chars);
    release_utf(env, task_id, task_chars);
    (void)snprintf(key, sizeof(key), "%016llx", (unsigned long long)hash);
    return (*env)->NewStringUTF(env, key);
}
