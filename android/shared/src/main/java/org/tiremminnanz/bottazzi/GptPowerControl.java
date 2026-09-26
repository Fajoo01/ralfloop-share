package org.tiremminnanz.bottazzi;

import android.app.Activity;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.webkit.CookieManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import org.json.JSONObject;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** Shared authenticated control for GPT supervision; Bot-tazzi remains usable. */
public final class GptPowerControl extends LinearLayout {
    private final Activity activity;
    private final TextView status;
    private final Button off;
    private final Button on;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean busy;
    private boolean attached;
    private final Runnable poll = () -> request(null);

    public GptPowerControl(Activity activity) {
        super(activity);
        this.activity = activity;
        setOrientation(HORIZONTAL);
        setGravity(android.view.Gravity.CENTER_VERTICAL);
        setBackgroundColor(0xff202c33);
        status = new TextView(activity);
        status.setTextColor(0xffe9edef);
        status.setText("GPT: verifica…");
        status.setTextSize(12);
        addView(status, new LayoutParams(0, LayoutParams.MATCH_PARENT, 1));
        off = new Button(activity);
        off.setText("Spegni GPT");
        off.setTextSize(12);
        off.setOnClickListener(v -> request(false));
        addView(off);
        on = new Button(activity);
        on.setText("Riattiva");
        on.setTextSize(12);
        on.setEnabled(false);
        on.setOnClickListener(v -> request(true));
        addView(on);
    }

    @Override protected void onAttachedToWindow() {
        super.onAttachedToWindow();
        attached = true;
        handler.post(poll);
    }

    @Override protected void onDetachedFromWindow() {
        attached = false;
        handler.removeCallbacks(poll);
        super.onDetachedFromWindow();
    }

    private void request(Boolean enabled) {
        if (busy || !attached) return;
        busy = true;
        handler.removeCallbacks(poll);
        off.setEnabled(false);
        on.setEnabled(false);
        String baseUrl = BuildConfig.APP_URL.replaceAll("/+$", "");
        if (!baseUrl.endsWith("/assistant/v1")) baseUrl += "/assistant/v1";
        final String powerUrl = baseUrl + "/gpt/power";
        String cookie = CookieManager.getInstance().getCookie(powerUrl);
        if (enabled != null) status.setText(enabled ? "Riattivazione…" : "Arresto…");
        new Thread(() -> {
            String message;
            Boolean active = null;
            long restSeconds = 0;
            HttpURLConnection connection = null;
            try {
                URL url = new URL(powerUrl + (enabled == null ? "" : enabled ? "/on" : "/off"));
                connection = (HttpURLConnection) url.openConnection();
                connection.setInstanceFollowRedirects(false);
                connection.setConnectTimeout(5000);
                connection.setReadTimeout(120000);
                if (cookie != null) connection.setRequestProperty("Cookie", cookie);
                if (enabled != null) {
                    connection.setRequestMethod("POST");
                    connection.setRequestProperty("Content-Type", "application/json");
                    connection.setDoOutput(true);
                    try (java.io.OutputStream out = connection.getOutputStream()) {
                        out.write("{}".getBytes(StandardCharsets.UTF_8));
                    }
                }
                int code = connection.getResponseCode();
                if (code != 200) throw new IllegalStateException(
                    code == 401 || code == 403 || code == 302 ? "Accedi per controllare GPT" : "Arresto/stato non confermato (" + code + ")");
                try (InputStream input = connection.getInputStream(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
                    byte[] buffer = new byte[4096];
                    int size;
                    while ((size = input.read(buffer)) != -1) out.write(buffer, 0, size);
                    JSONObject result = new JSONObject(out.toString("UTF-8"));
                    active = result.getBoolean("enabled");
                    restSeconds = result.optLong("rest_remaining_seconds", 0);
                    if (!active && !result.optBoolean("stop_complete", false)) {
                        active = null;
                        throw new IllegalStateException("Invii bloccati · stop da verificare");
                    }
                }
                message = restSeconds > 0 ? "Pausa GPT: " + ((restSeconds + 59) / 60) + " min" : active ? "GPT attivo · max 2 h" : "GPT spento";
            } catch (Exception exc) {
                message = exc instanceof IllegalStateException ? exc.getMessage() : "GPT: stato non verificato";
            } finally {
                if (connection != null) connection.disconnect();
            }
            final String label = message;
            final Boolean confirmed = active;
            final long remainingRest = restSeconds;
            handler.post(() -> {
                busy = false;
                if (!attached) return;
                status.setText(label);
                off.setEnabled(confirmed == null || confirmed);
                on.setEnabled(confirmed != null && !confirmed && remainingRest == 0);
                if (confirmed != null && activity.getPackageName().equals("org.tiremminnanz.gptbrowser")) {
                    activity.getSharedPreferences("gpt_power", 0).edit().putBoolean("enabled", confirmed).apply();
                    Intent service = new Intent(activity, BotTazziNotifyService.class);
                    if (!confirmed) activity.stopService(service);
                    else if (activity.hasWindowFocus()) activity.startForegroundService(service);
                }
                handler.postDelayed(poll, 10000);
            });
        }, "gpt-power-control").start();
    }
}
