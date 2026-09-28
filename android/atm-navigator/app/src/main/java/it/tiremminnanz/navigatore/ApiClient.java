package it.tiremminnanz.navigatore;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Base64;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;

public final class ApiClient {
    public static final String DEFAULT_BASE_URL = "https://remote.tiremminnanz.com/account/remote";
    private static final String PREFS = "tiremm_navigator_auth";
    private ApiClient() {}

    public interface PairingListener {
        void onCode(String userCode, String verificationUrl);
        void onPaired();
        void onError(String message);
    }

    public static boolean isPaired(Context context) {
        SharedPreferences p = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        return !p.getString("device_id", "").isEmpty() && !p.getString("token", "").isEmpty();
    }

    public static void clearPairing(Context context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear().apply();
    }

    public static void startPairing(Context context, PairingListener listener) {
        new Thread(() -> {
            try {
                byte[] raw = new byte[32];
                new SecureRandom().nextBytes(raw);
                String verifier = b64(raw);
                String challenge = b64(MessageDigest.getInstance("SHA-256").digest(verifier.getBytes(StandardCharsets.UTF_8)));
                JSONObject request = new JSONObject()
                    .put("code_challenge", challenge)
                    .put("code_challenge_method", "S256")
                    .put("device_name", "Tiremm Navigatore");
                JSONObject started = postPublic("/device/start", request);
                String deviceCode = started.getString("device_code");
                String userCode = started.getString("user_code");
                String verify = started.optString("verification_uri_complete", started.getString("verification_uri"));
                listener.onCode(userCode, verify);
                long deadline = System.currentTimeMillis() + Math.max(60, started.optLong("expires_in", 600)) * 1000L;
                long interval = Math.max(2, started.optLong("interval", 3));
                while (System.currentTimeMillis() < deadline) {
                    Thread.sleep(interval * 1000L);
                    JSONObject poll = new JSONObject().put("device_code", deviceCode).put("code_verifier", verifier);
                    try {
                        JSONObject paired = postPublic("/device/poll", poll);
                        String token = paired.optString("access_token");
                        String deviceId = paired.optString("device_id");
                        if (!token.isEmpty() && !deviceId.isEmpty()) {
                            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                                .putString("device_id", deviceId).putString("token", token).apply();
                            listener.onPaired();
                            return;
                        }
                    } catch (HttpError e) {
                        if (e.code == 400 && e.body.contains("authorization_pending")) continue;
                        throw e;
                    }
                }
                listener.onError("Login scaduto: riprova.");
            } catch (Exception e) {
                listener.onError(e.getMessage() == null ? "Errore login" : e.getMessage());
            }
        }).start();
    }

    public static JSONObject get(Context context, String path) throws Exception {
        if (path.startsWith("/device/navigator/")) {
            return deviceNavigator(context, path.substring("/device/navigator/".length()), new JSONObject());
        }
        return readJson(openAuthenticated(context, path, "GET", null));
    }

    public static JSONObject post(Context context, String path, JSONObject body) throws Exception {
        if (path.startsWith("/device/navigator/")) {
            return deviceNavigator(context, path.substring("/device/navigator/".length()), body);
        }
        byte[] data = body.toString().getBytes(StandardCharsets.UTF_8);
        return readJson(openAuthenticated(context, path, "POST", data));
    }

    private static JSONObject deviceNavigator(Context context, String action, JSONObject payload) throws Exception {
        SharedPreferences p = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String deviceId = p.getString("device_id", "");
        String token = p.getString("token", "");
        if (deviceId.isEmpty() || token.isEmpty()) throw new IOException("Login Account Tiremm richiesto");
        JSONObject envelope = new JSONObject()
            .put("navigator_action", action)
            .put("device_id", deviceId)
            .put("access_token", token)
            .put("payload", payload == null ? new JSONObject() : payload);
        return postPublic("/device/poll", envelope);
    }

    public static JSONArray searchPlaces(String query) throws Exception {
        String q = URLEncoder.encode(query, StandardCharsets.UTF_8);
        HttpURLConnection c = open(
            "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&countrycodes=it&q=" + q,
            "GET"
        );
        c.setRequestProperty("Accept-Language", "it");
        int code = c.getResponseCode();
        InputStream in = code >= 200 && code < 300 ? c.getInputStream() : c.getErrorStream();
        String text = readText(in);
        if (code < 200 || code >= 300) throw new IOException("Geocoding HTTP " + code);
        return new JSONArray(text);
    }

    private static HttpURLConnection openAuthenticated(Context context, String path, String method, byte[] body) throws Exception {
        SharedPreferences p = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String deviceId = p.getString("device_id", "");
        String token = p.getString("token", "");
        if (deviceId.isEmpty() || token.isEmpty()) throw new IOException("Login Account Tiremm richiesto");
        HttpURLConnection c = open(DEFAULT_BASE_URL + path, method);
        c.setRequestProperty("Authorization", "Bearer " + token);
        c.setRequestProperty("X-Tiremm-Device-Id", deviceId);
        if (body != null) writeBody(c, body);
        return c;
    }

    private static JSONObject postPublic(String path, JSONObject body) throws Exception {
        HttpURLConnection c = open(DEFAULT_BASE_URL + path, "POST");
        writeBody(c, body.toString().getBytes(StandardCharsets.UTF_8));
        return readJson(c);
    }

    private static HttpURLConnection open(String url, String method) throws Exception {
        HttpURLConnection c = (HttpURLConnection)new URL(url).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(8000);
        c.setReadTimeout(30000);
        c.setRequestProperty("User-Agent", "Tiremm-Navigatore/0.2");
        return c;
    }

    private static void writeBody(HttpURLConnection c, byte[] data) throws Exception {
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        c.setFixedLengthStreamingMode(data.length);
        try (OutputStream out = c.getOutputStream()) { out.write(data); }
    }

    private static JSONObject readJson(HttpURLConnection c) throws Exception {
        int code = c.getResponseCode();
        InputStream in = code >= 200 && code < 300 ? c.getInputStream() : c.getErrorStream();
        String text = readText(in);
        if (code < 200 || code >= 300) throw new HttpError(code, text);
        return new JSONObject(text);
    }

    private static String readText(InputStream in) throws Exception {
        if (in == null) return "";
        try (BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            StringBuilder b = new StringBuilder(); String line;
            while ((line = r.readLine()) != null) b.append(line);
            return b.toString();
        }
    }

    private static String b64(byte[] bytes) {
        return Base64.encodeToString(bytes, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING);
    }

    private static final class HttpError extends IOException {
        final int code; final String body;
        HttpError(int code, String body) { super("HTTP " + code); this.code = code; this.body = body == null ? "" : body; }
    }
}
