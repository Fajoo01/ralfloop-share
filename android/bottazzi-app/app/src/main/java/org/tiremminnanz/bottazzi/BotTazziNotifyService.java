package org.tiremminnanz.bottazzi;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.IBinder;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

public final class BotTazziNotifyService extends Service {
    private static final String LINK_CHANNEL = "bottazzi_link";
    private static final String TASK_CHANNEL = "bottazzi_tasks";
    private static final int LINK_NOTIFICATION_ID = 4700;
    private static final long POLL_INTERVAL_MS = 10_000L;
    private static final String PREFS = "bottazzi_gpt_notifications_v2";

    private volatile boolean running;
    private Thread worker;
    private NotificationManager notifications;
    private SharedPreferences prefs;

    @Override
    public void onCreate() {
        super.onCreate();
        notifications = getSystemService(NotificationManager.class);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        createChannels();
        startForeground(
            LINK_NOTIFICATION_ID,
            linkNotification("Connessione a Bot-tazzi…")
        );
        running = true;
        worker = new Thread(this::runLoop, "bottazzi-native-notify");
        worker.start();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) {
            worker.interrupt();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void createChannels() {
        NotificationChannel link = new NotificationChannel(
            LINK_CHANNEL,
            "Connessione Bot-tazzi",
            NotificationManager.IMPORTANCE_LOW
        );
        link.setDescription("Mantiene attive le notifiche dei lavori GPT Bot-tazzi.");
        NotificationChannel tasks = new NotificationChannel(
            TASK_CHANNEL,
            "Lavori Bot-tazzi",
            NotificationManager.IMPORTANCE_DEFAULT
        );
        tasks.setDescription("Fine lavoro, blocchi e richieste di attenzione della coda GPT.");
        notifications.createNotificationChannel(link);
        notifications.createNotificationChannel(tasks);
    }

    private void runLoop() {
        while (running) {
            try {
                JSONObject snapshot = fetchQueue();
                processSnapshot(snapshot);
                JSONArray jobs = snapshot.optJSONArray("jobs");
                int count = jobs == null ? 0 : jobs.length();
                notifications.notify(
                    LINK_NOTIFICATION_ID,
                    linkNotification("GPT attivo · " + count + " lavori")
                );
            } catch (SecurityException exc) {
                notifications.notify(
                    LINK_NOTIFICATION_ID,
                    linkNotification("Permesso notifiche da verificare")
                );
            } catch (Exception exc) {
                notifications.notify(
                    LINK_NOTIFICATION_ID,
                    linkNotification("Backend non raggiungibile")
                );
            }
            try {
                Thread.sleep(POLL_INTERVAL_MS);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
                break;
            }
        }
    }

    private JSONObject fetchQueue() throws Exception {
        URL url = new URL(gptSnapshotUrl());
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod("GET");
        connection.setConnectTimeout(5_000);
        connection.setReadTimeout(10_000);
        connection.setRequestProperty("Accept", "application/json");
        String cookie = CookieManager.getInstance().getCookie(BuildConfig.APP_URL);
        if (cookie != null && !cookie.trim().isEmpty()) {
            connection.setRequestProperty("Cookie", cookie);
        }
        int code = connection.getResponseCode();
        if (code != 200) {
            connection.disconnect();
            throw new IllegalStateException("queue_http_" + code);
        }
        try (InputStream input = connection.getInputStream()) {
            return new JSONObject(readAll(input));
        } finally {
            connection.disconnect();
        }
    }

    private String gptSnapshotUrl() {
        String base = BuildConfig.APP_URL;
        while (base.endsWith("/")) {
            base = base.substring(0, base.length() - 1);
        }
        if (base.endsWith("/assistant/v1")) {
            return base + "/gpt";
        }
        return base + "/assistant/v1/gpt";
    }

    private static String readAll(InputStream input) throws Exception {
        StringBuilder out = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
            new InputStreamReader(input, StandardCharsets.UTF_8)
        )) {
            String line;
            while ((line = reader.readLine()) != null) {
                out.append(line);
            }
        }
        return out.toString();
    }

    private void processSnapshot(JSONObject snapshot) {
        JSONArray jobs = snapshot.optJSONArray("jobs");
        if (jobs == null) {
            return;
        }
        boolean initialized = prefs.getBoolean("gpt.initialized", false);
        SharedPreferences.Editor editor = prefs.edit();
        for (int i = 0; i < jobs.length(); ++i) {
            JSONObject job = jobs.optJSONObject(i);
            if (job == null) {
                continue;
            }
            String jobId = job.optString("job_id", "").trim();
            if (jobId.isEmpty()) {
                continue;
            }
            String current = job.optString("state", "").trim();
            String previous = prefs.getString("gpt.state." + jobId, "");
            String text = job.optString("last_assistant_text", "").trim();
            String textHash = Integer.toHexString(text.hashCode());
            String previousHash = prefs.getString("gpt.answer." + jobId, "");
            String error = job.optString("last_error", "").trim();
            String previousError = prefs.getString("gpt.error." + jobId, "");
            if (initialized) {
                boolean newReview = "review".equals(current)
                    && (!"review".equals(previous) || !textHash.equals(previousHash));
                if (newReview && error.isEmpty() && !text.isEmpty()) {
                    notifyUser(
                        (jobId + ":review:" + textHash).hashCode(),
                        "Lavoro finito",
                        job.optString("title", "Bot-tazzi ha finito il lavoro")
                    );
                } else if (
                    ("failed".equals(current) || "blocked".equals(current) || ("review".equals(current) && !error.isEmpty()))
                    && (!current.equals(previous) || !error.equals(previousError))
                ) {
                    notifyUser(
                        (jobId + ":attention:" + current + ":" + error).hashCode(),
                        "Bot-tazzi richiede attenzione",
                        job.optString("title", "Controlla il lavoro")
                    );
                }
            }
            editor.putString("gpt.state." + jobId, current);
            editor.putString("gpt.answer." + jobId, textHash);
            editor.putString("gpt.error." + jobId, error);
        }
        if (!initialized) {
            editor.putBoolean("gpt.initialized", true);
        }
        editor.apply();
    }

    private int decision(JSONObject entry, JSONObject task) {
        return NativeCore.nativeNotificationDecision(
            entry.optInt("position", 0),
            task.optInt("auto_priority", 0),
            entry.optBoolean("runnable", false),
            task.optString("state", ""),
            task.optString("blocked_reason", "")
        );
    }

    private void processPriorityConflict(JSONArray conflicts) {
        if (conflicts == null || conflicts.length() == 0) {
            return;
        }
        JSONObject conflict = conflicts.optJSONObject(0);
        if (conflict == null) {
            return;
        }
        String pinned = conflict.optString("pinned_task_id", "");
        String candidate = conflict.optString("candidate_task_id", "");
        if (pinned.isEmpty() || candidate.isEmpty()) {
            return;
        }
        int position = conflict.optInt("pinned_position", 1);
        int priority = conflict.optInt("candidate_auto_priority", 0);
        int kind = NativeCore.nativeNotificationDecision(
            position,
            priority,
            false,
            "priority_conflict",
            ""
        );
        String syntheticId = "conflict:" + pinned + ":" + candidate;
        String key = NativeCore.nativeEventKey(
            syntheticId,
            "priority_conflict",
            "",
            position,
            priority
        );
        if (kind == NativeCore.NOTIFY_PRIORITY_CONFLICT && markFirst(key)) {
            notifyUser(
                key.hashCode(),
                "Conflitto di priorità",
                "JEV propone una priorità diversa da un ordine fissato da te."
            );
        }
    }

    private void notifyTaskOnce(int kind, JSONObject entry, JSONObject task) {
        String taskId = task.optString("task_id", "");
        String state = task.optString("state", "");
        String blockedReason = task.optString("blocked_reason", "");
        int position = entry.optInt("position", 0);
        int priority = task.optInt("auto_priority", 0);
        String key = NativeCore.nativeEventKey(
            taskId,
            state,
            blockedReason,
            position,
            priority
        );
        if (!markFirst(key)) {
            return;
        }

        String title;
        if (kind == NativeCore.NOTIFY_APPROVAL_REQUIRED) {
            title = "Bot-tazzi richiede conferma";
        } else if (kind == NativeCore.NOTIFY_HIGH_PRIORITY) {
            title = "Priorità JEV";
        } else {
            title = "Prossimo compito Bot-tazzi";
        }
        String body = task.optString("title", "Compito in coda");
        notifyUser(key.hashCode(), title, body);
    }

    private boolean markFirst(String key) {
        String prefKey = "event." + key;
        if (prefs.getBoolean(prefKey, false)) {
            return false;
        }
        prefs.edit().putBoolean(prefKey, true).apply();
        return true;
    }

    private PendingIntent openAppIntent() {
        Intent intent = new Intent(this, MainActivity.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        return PendingIntent.getActivity(
            this,
            0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
    }

    private Notification linkNotification(String text) {
        return new Notification.Builder(this, LINK_CHANNEL)
            .setSmallIcon(R.drawable.ic_bot_tazzi)
            .setContentTitle("Bot-tazzi")
            .setContentText(text)
            .setContentIntent(openAppIntent())
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build();
    }

    private void notifyUser(int id, String title, String text) {
        Notification notification = new Notification.Builder(this, TASK_CHANNEL)
            .setSmallIcon(R.drawable.ic_bot_tazzi)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(new Notification.BigTextStyle().bigText(text))
            .setContentIntent(openAppIntent())
            .setAutoCancel(true)
            .build();
        notifications.notify(10_000 + Math.abs(id % 20_000), notification);
    }
}
