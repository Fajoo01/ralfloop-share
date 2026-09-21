package org.tiremminnanz.mdgoodify;

import android.Manifest;
import android.app.Activity;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

import com.google.zxing.BinaryBitmap;
import com.google.zxing.MultiFormatReader;
import com.google.zxing.RGBLuminanceSource;
import com.google.zxing.Result;
import com.google.zxing.common.HybridBinarizer;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private static final int CAMERA_REQUEST = 41;
    private static final int NOTIFICATION_REQUEST = 42;
    private static final String CHANNEL_ID = "md_goodify_wins";

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private TextView stateView;
    private TextView statsView;
    private ProgressBar progress;
    private Button scanButton;
    private Button connectButton;
    private LinearLayout loginBox;
    private EditText emailView;
    private EditText passwordView;
    private boolean enrolled;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildUi();
        createNotificationChannel();
        requestNotificationPermissionOnce();
        checkBackend();
        handleIncomingIntent(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIncomingIntent(intent);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (enrolled) loadStats();
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER_HORIZONTAL);
        int pad = dp(24);
        root.setPadding(pad, dp(40), pad, pad);

        TextView brand = new TextView(this);
        brand.setText("TIREMM INNANZ APS");
        brand.setTextSize(28);
        brand.setGravity(Gravity.CENTER);
        brand.setPadding(0, 0, 0, dp(8));
        root.addView(brand, new LinearLayout.LayoutParams(-1, -2));

        TextView subtitle = new TextView(this);
        subtitle.setText("MD → Goodify → Tiremm");
        subtitle.setTextSize(17);
        subtitle.setGravity(Gravity.CENTER);
        subtitle.setPadding(0, 0, 0, dp(18));
        root.addView(subtitle, new LinearLayout.LayoutParams(-1, -2));

        statsView = new TextView(this);
        statsView.setText("Totale MD — …\nTiremm via Bot-tazzi — …");
        statsView.setTextSize(20);
        statsView.setGravity(Gravity.CENTER);
        statsView.setPadding(dp(12), dp(16), dp(12), dp(16));
        statsView.setVisibility(View.GONE);
        root.addView(statsView, new LinearLayout.LayoutParams(-1, -2));

        loginBox = new LinearLayout(this);
        loginBox.setOrientation(LinearLayout.VERTICAL);
        loginBox.setPadding(0, 0, 0, dp(24));

        TextView loginHint = new TextView(this);
        loginHint.setText("Collega una volta il tuo account MD. La password non viene salvata.");
        loginHint.setTextSize(16);
        loginHint.setGravity(Gravity.CENTER);
        loginHint.setPadding(0, 0, 0, dp(12));
        loginBox.addView(loginHint, new LinearLayout.LayoutParams(-1, -2));

        emailView = new EditText(this);
        emailView.setHint("Email MD");
        emailView.setSingleLine(true);
        emailView.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS);
        loginBox.addView(emailView, new LinearLayout.LayoutParams(-1, -2));

        passwordView = new EditText(this);
        passwordView.setHint("Password MD");
        passwordView.setSingleLine(true);
        passwordView.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        loginBox.addView(passwordView, new LinearLayout.LayoutParams(-1, -2));

        connectButton = new Button(this);
        connectButton.setText("Collega account MD");
        connectButton.setOnClickListener(v -> enrollMd());
        loginBox.addView(connectButton, new LinearLayout.LayoutParams(-1, -2));
        root.addView(loginBox, new LinearLayout.LayoutParams(-1, -2));

        scanButton = new Button(this);
        scanButton.setText("Scansiona QR MD");
        scanButton.setTextSize(20);
        scanButton.setMinHeight(dp(64));
        scanButton.setEnabled(false);
        scanButton.setOnClickListener(v -> startScanner());
        root.addView(scanButton, new LinearLayout.LayoutParams(-1, -2));

        progress = new ProgressBar(this);
        progress.setIndeterminate(true);
        progress.setVisibility(View.GONE);
        LinearLayout.LayoutParams progressParams = new LinearLayout.LayoutParams(dp(48), dp(48));
        progressParams.topMargin = dp(28);
        root.addView(progress, progressParams);

        stateView = new TextView(this);
        stateView.setText("Verifica collegamento…");
        stateView.setTextSize(18);
        stateView.setGravity(Gravity.CENTER);
        stateView.setPadding(0, dp(24), 0, 0);
        root.addView(stateView, new LinearLayout.LayoutParams(-1, -2));

        setContentView(root);
    }

    private void checkBackend() {
        setBusy(true, "Verifica collegamento…");
        executor.execute(() -> {
            HttpURLConnection connection = null;
            try {
                String base = BuildConfig.API_BASE_URL.replaceAll("/+$", "");
                connection = (HttpURLConnection) new URL(base + "/health").openConnection();
                connection.setRequestMethod("GET");
                connection.setConnectTimeout(8000);
                connection.setReadTimeout(8000);
                connection.setRequestProperty("Accept", "application/json");
                connection.setRequestProperty("Authorization", "Bearer " + BuildConfig.API_TOKEN);
                int code = connection.getResponseCode();
                InputStream stream = code >= 400 ? connection.getErrorStream() : connection.getInputStream();
                JSONObject response = readJson(stream);
                if (code == 401) { runOnUiThread(() -> setBusy(false, "App non autorizzata — aggiorna l’app")); return; }
                boolean linked = response.optBoolean("enrolled", false);
                runOnUiThread(() -> setEnrollmentState(linked));
            } catch (Exception ignored) {
                runOnUiThread(() -> setBusy(false, "Errore rete: servizio Tiremm non raggiungibile"));
            } finally {
                if (connection != null) connection.disconnect();
            }
        });
    }

    private void enrollMd() {
        String email = emailView.getText().toString().trim();
        String password = passwordView.getText().toString();
        if (email.isEmpty() || password.isEmpty()) {
            stateView.setText("Inserisci email e password MD");
            return;
        }
        setBusy(true, "Collegamento account MD…");
        executor.execute(() -> callEnroll(email, password));
    }

    private void callEnroll(String email, String password) {
        HttpURLConnection connection = null;
        try {
            String base = BuildConfig.API_BASE_URL.replaceAll("/+$", "");
            connection = (HttpURLConnection) new URL(base + "/v1/enroll").openConnection();
            connection.setRequestMethod("POST");
            connection.setConnectTimeout(8000);
            connection.setReadTimeout(20000);
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json");
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Authorization", "Bearer " + BuildConfig.API_TOKEN);
            byte[] body = new JSONObject().put("email", email).put("password", password)
                    .toString().getBytes(StandardCharsets.UTF_8);
            connection.setFixedLengthStreamingMode(body.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(body);
            }
            int code = connection.getResponseCode();
            InputStream stream = code >= 400 ? connection.getErrorStream() : connection.getInputStream();
            JSONObject response = readJson(stream);
            boolean ok = code == 200 && "ENROLLED".equals(response.optString("status"));
            runOnUiThread(() -> {
                passwordView.setText("");
                if (ok) setEnrollmentState(true);
                else setBusy(false, code == 401 ? "Email o password MD non corretti" : "Accesso MD non riuscito");
            });
        } catch (Exception ignored) {
            runOnUiThread(() -> {
                passwordView.setText("");
                setBusy(false, "Errore rete: servizio Tiremm non raggiungibile");
            });
        } finally {
            password = "";
            if (connection != null) connection.disconnect();
        }
    }

    private void loadStats() {
        executor.execute(() -> {
            try {
                GoodifyApiClient.ApiResponse result = GoodifyApiClient.getStats();
                if (result.httpCode != 200) return;
                JSONObject body = result.body;
                String md = body.optString("md_total_eur", "");
                String mdMonth = body.optString("md_month_total_eur", "");
                String tiremm = body.optString("tiremm_total_eur", "");
                String tiremmMonth = body.optString("tiremm_month_total_eur", "");
                int mdMonthCount = body.optInt("md_month_count", 0);
                int count = body.optInt("tiremm_completed_count", 0);
                int tiremmMonthCount = body.optInt("tiremm_month_count", 0);
                runOnUiThread(() -> {
                    String mdLine = md.isEmpty() ? "Totale MD — non disponibile" : "Totale MD — € " + euro(md);
                    String mdMonthLine = mdMonth.isEmpty()
                            ? "MD questo mese — " + mdMonthCount + " donazioni"
                            : "MD questo mese — € " + euro(mdMonth) + "  (" + mdMonthCount + ")";
                    String tiremmLine = tiremm.isEmpty()
                            ? "Tiremm via Bot-tazzi — " + count + " donazioni"
                            : "Tiremm via Bot-tazzi — € " + euro(tiremm) + "  (" + count + ")";
                    String tiremmMonthLine = tiremmMonth.isEmpty()
                            ? "Tiremm questo mese — " + tiremmMonthCount + " donazioni"
                            : "Tiremm questo mese — € " + euro(tiremmMonth) + "  (" + tiremmMonthCount + ")";
                    statsView.setText(mdLine + "\n" + mdMonthLine + "\n" + tiremmLine + "\n" + tiremmMonthLine);
                });
            } catch (Exception ignored) {
            }
        });
    }

    private String euro(String value) {
        return value.replace('.', ',');
    }

    private void setEnrollmentState(boolean linked) {
        enrolled = linked;
        loginBox.setVisibility(linked ? View.GONE : View.VISIBLE);
        statsView.setVisibility(linked ? View.VISIBLE : View.GONE);
        setBusy(false, linked ? "Pronto" : "Collega il tuo account MD");
        if (linked) loadStats();
    }

    private void startScanner() {
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, CAMERA_REQUEST);
            return;
        }
        startActivity(new Intent(this, QrScanActivity.class));
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == CAMERA_REQUEST && grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            startScanner();
        }
    }


    private void handleIncomingIntent(Intent intent) {
        if (intent == null || !Intent.ACTION_SEND.equals(intent.getAction())) {
            return;
        }
        String type = intent.getType();
        if (type != null && type.startsWith("text/")) {
            String text = intent.getStringExtra(Intent.EXTRA_TEXT);
            if (text != null && !text.trim().isEmpty()) {
                processQr(text.trim());
            }
            return;
        }
        if (type != null && type.startsWith("image/")) {
            @SuppressWarnings("deprecation") Uri uri = intent.getParcelableExtra(Intent.EXTRA_STREAM);
            if (uri != null) {
                decodeSharedImage(uri);
            }
        }
    }

    private void decodeSharedImage(Uri uri) {
        setBusy(true, "QR acquisito — lettura immagine…");
        executor.execute(() -> {
            try (InputStream input = getContentResolver().openInputStream(uri)) {
                Bitmap bitmap = BitmapFactory.decodeStream(input);
                if (bitmap == null) throw new IllegalArgumentException("image_decode_failed");
                int width = bitmap.getWidth();
                int height = bitmap.getHeight();
                int[] pixels = new int[width * height];
                bitmap.getPixels(pixels, 0, width, 0, 0, width, height);
                RGBLuminanceSource source = new RGBLuminanceSource(width, height, pixels);
                Result result = new MultiFormatReader().decode(new BinaryBitmap(new HybridBinarizer(source)));
                runOnUiThread(() -> processQr(result.getText()));
            } catch (Exception ignored) {
                runOnUiThread(() -> setBusy(false, "Errore: QR non leggibile nell’immagine"));
            }
        });
    }

    private void processQr(String qr) {
        if (!enrolled) {
            setBusy(false, "Collega prima il tuo account MD");
            return;
        }
        if (qr == null || qr.trim().isEmpty() || qr.length() > 4096) {
            setBusy(false, "Errore: QR non valido");
            return;
        }
        setBusy(true, "QR acquisito — donazione in corso…");
        executor.execute(() -> callBackend(qr.trim()));
    }

    private void callBackend(String qr) {
        try {
            GoodifyApiClient.ApiResponse result = GoodifyApiClient.processQr(qr);
            runOnUiThread(() -> renderResponse(result.httpCode, result.body));
        } catch (Exception ignored) {
            runOnUiThread(() -> setBusy(false, "Errore rete: servizio Tiremm non raggiungibile"));
        }
    }

    private JSONObject readJson(InputStream stream) throws Exception {
        if (stream == null) throw new IllegalStateException("empty_response");
        StringBuilder text = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null && text.length() < 65536) text.append(line);
        }
        return new JSONObject(text.toString());
    }

    private void renderResponse(int httpCode, JSONObject response) {
        String status = response.optString("status", "ERRORE");
        boolean already = response.optBoolean("already_processed", false);
        if (httpCode == 200 && "DONATED_TO_TIREMM".equals(status)) {
            setBusy(false, already ? "Già processato — donato a Tiremm Innanz APS" : "Donato a Tiremm Innanz APS");
            JSONObject win = response.optJSONObject("instant_win");
            if (win != null && "WIN".equals(win.optString("status"))) {
                Object amount = win.opt("amount");
                notifyWin(amount == null || amount == JSONObject.NULL ? "premio rilevato" : String.valueOf(amount));
            }
            return;
        }
        if (httpCode == 202 || "PROCESSING".equals(status)) {
            setBusy(false, "Già in elaborazione");
            return;
        }
        if (status.startsWith("AMBIGUOUS")) {
            setBusy(false, "Stato incerto — QR bloccato per evitare doppie donazioni");
            return;
        }
        setBusy(false, "Errore: " + status);
    }

    private void setBusy(boolean busy, String message) {
        progress.setVisibility(busy ? View.VISIBLE : View.GONE);
        scanButton.setEnabled(!busy && enrolled);
        if (connectButton != null) connectButton.setEnabled(!busy);
        stateView.setText(message);
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(CHANNEL_ID, "Vincite MD / Goodify", NotificationManager.IMPORTANCE_HIGH);
            channel.setDescription("Notifiche quando Goodify segnala una vincita");
            getSystemService(NotificationManager.class).createNotificationChannel(channel);
        }
    }

    private void requestNotificationPermissionOnce() {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATION_REQUEST);
        }
    }

    private void notifyWin(String amount) {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return;
        Intent intent = new Intent(this, MainActivity.class);
        PendingIntent pending = PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        android.app.Notification notification = new android.app.Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.star_big_on)
                .setContentTitle("Hai vinto con MD / Goodify")
                .setContentText("Premio rilevato: " + amount)
                .setAutoCancel(true)
                .setContentIntent(pending)
                .build();
        getSystemService(NotificationManager.class).notify(8801, notification);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    @Override
    protected void onDestroy() {
        executor.shutdownNow();
        super.onDestroy();
    }
}
