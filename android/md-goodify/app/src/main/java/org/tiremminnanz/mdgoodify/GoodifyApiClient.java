package org.tiremminnanz.mdgoodify;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

final class GoodifyApiClient {
    static final class ApiResponse {
        final int httpCode;
        final JSONObject body;

        ApiResponse(int httpCode, JSONObject body) {
            this.httpCode = httpCode;
            this.body = body;
        }
    }

    private GoodifyApiClient() {
    }

    static ApiResponse processQr(String qr) throws Exception {
        String base = BuildConfig.API_BASE_URL.replaceAll("/+$", "");
        HttpURLConnection connection = (HttpURLConnection) new URL(base + "/v1/process-qr").openConnection();
        try {
            connection.setRequestMethod("POST");
            connection.setConnectTimeout(8000);
            connection.setReadTimeout(65000);
            connection.setDoOutput(true);
            connection.setUseCaches(false);
            connection.setRequestProperty("Content-Type", "application/json");
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Authorization", "Bearer " + BuildConfig.API_TOKEN);

            byte[] body = new JSONObject().put("qr_code", qr)
                    .toString().getBytes(StandardCharsets.UTF_8);
            connection.setFixedLengthStreamingMode(body.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(body);
            }
            int code = connection.getResponseCode();
            InputStream stream = code >= 400 ? connection.getErrorStream() : connection.getInputStream();
            return new ApiResponse(code, readJson(stream));
        } finally {
            connection.disconnect();
        }
    }

    private static JSONObject readJson(InputStream stream) throws Exception {
        if (stream == null) throw new IllegalStateException("empty_response");
        StringBuilder text = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null && text.length() < 65536) {
                text.append(line);
            }
        }
        return new JSONObject(text.toString());
    }
}
