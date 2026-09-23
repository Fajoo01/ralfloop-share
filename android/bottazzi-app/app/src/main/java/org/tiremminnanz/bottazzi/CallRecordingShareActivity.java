package org.tiremminnanz.bottazzi;

import android.app.Activity;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.OpenableColumns;
import android.view.Gravity;
import android.widget.TextView;
import android.widget.Toast;
import android.webkit.CookieManager;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

public final class CallRecordingShareActivity extends Activity {
    private static final int BUFFER_SIZE = 64 * 1024;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        TextView status = new TextView(this);
        status.setText("Importo la registrazione in Bot-tazzi…");
        status.setGravity(Gravity.CENTER);
        status.setTextSize(18f);
        status.setPadding(48, 48, 48, 48);
        setContentView(status);

        Uri uri = sharedUri(getIntent());
        if (uri == null) {
            finishWithMessage("Nessuna registrazione ricevuta");
            return;
        }
        Thread worker = new Thread(() -> upload(uri), "bottazzi-call-recording-upload");
        worker.start();
    }

    private Uri sharedUri(Intent intent) {
        if (!Intent.ACTION_SEND.equals(intent.getAction())) {
            return null;
        }
        if (Build.VERSION.SDK_INT >= 33) {
            return intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri.class);
        }
        @SuppressWarnings("deprecation")
        Uri value = intent.getParcelableExtra(Intent.EXTRA_STREAM);
        return value;
    }

    private void upload(Uri uri) {
        HttpURLConnection connection = null;
        try {
            String contentType = getContentResolver().getType(uri);
            if (contentType == null || contentType.isBlank()) {
                contentType = getIntent().getType();
            }
            if (contentType == null || contentType.isBlank()) {
                contentType = "application/octet-stream";
            }
            String filename = displayName(uri);
            URL endpoint = new URL(callRecordingsUrl());
            connection = (HttpURLConnection) endpoint.openConnection();
            connection.setRequestMethod("POST");
            connection.setConnectTimeout(15_000);
            connection.setReadTimeout(120_000);
            connection.setDoOutput(true);
            connection.setChunkedStreamingMode(BUFFER_SIZE);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Content-Type", contentType);
            connection.setRequestProperty(
                "X-Bottazzi-Filename",
                URLEncoder.encode(filename, StandardCharsets.UTF_8.name())
            );
            connection.setRequestProperty("X-Bottazzi-Transport", "android_share");
            String cookie = CookieManager.getInstance().getCookie(BuildConfig.APP_URL);
            if (cookie != null && !cookie.isBlank()) {
                connection.setRequestProperty("Cookie", cookie);
            }

            try (
                InputStream input = getContentResolver().openInputStream(uri);
                OutputStream output = connection.getOutputStream()
            ) {
                if (input == null) {
                    throw new IllegalStateException("shared_recording_unreadable");
                }
                byte[] buffer = new byte[BUFFER_SIZE];
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    if (read > 0) {
                        output.write(buffer, 0, read);
                    }
                }
            }

            int code = connection.getResponseCode();
            if (code >= 200 && code < 300) {
                finishWithMessage("Registrazione consegnata a Bot-tazzi");
            } else if (code == 401) {
                finishWithMessage("Apri Bot-tazzi ed effettua l'accesso, poi condividi di nuovo");
            } else {
                finishWithMessage("Importazione fallita: HTTP " + code);
            }
        } catch (Exception exc) {
            finishWithMessage("Importazione fallita: " + exc.getClass().getSimpleName());
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private String displayName(Uri uri) {
        try (Cursor cursor = getContentResolver().query(
            uri,
            new String[] {OpenableColumns.DISPLAY_NAME},
            null,
            null,
            null
        )) {
            if (cursor != null && cursor.moveToFirst()) {
                int column = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (column >= 0) {
                    String value = cursor.getString(column);
                    if (value != null && !value.isBlank()) {
                        return value;
                    }
                }
            }
        } catch (Exception ignored) {
        }
        String fallback = uri.getLastPathSegment();
        return fallback == null || fallback.isBlank() ? "call-recording" : fallback;
    }

    private String callRecordingsUrl() {
        String base = BuildConfig.APP_URL;
        while (base.endsWith("/")) {
            base = base.substring(0, base.length() - 1);
        }
        if (base.endsWith("/assistant/v1")) {
            return base + "/call-recordings";
        }
        return base + "/assistant/v1/call-recordings";
    }

    private void finishWithMessage(String message) {
        runOnUiThread(() -> {
            Toast.makeText(this, message, Toast.LENGTH_LONG).show();
            finish();
        });
    }
}
