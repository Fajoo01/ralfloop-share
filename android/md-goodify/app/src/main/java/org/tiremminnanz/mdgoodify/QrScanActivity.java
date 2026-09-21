package org.tiremminnanz.mdgoodify;

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.widget.LinearLayout;
import android.widget.TextView;

import com.google.zxing.BarcodeFormat;
import com.google.zxing.ResultPoint;
import com.journeyapps.barcodescanner.BarcodeCallback;
import com.journeyapps.barcodescanner.BarcodeResult;
import com.journeyapps.barcodescanner.DecoratedBarcodeView;
import com.journeyapps.barcodescanner.DefaultDecoderFactory;

import org.json.JSONObject;

import java.time.LocalTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayDeque;
import java.util.Collections;
import java.util.List;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class QrScanActivity extends Activity {
    private static final DateTimeFormatter CLOCK = DateTimeFormatter.ofPattern("HH:mm:ss");
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ArrayDeque<String> recent = new ArrayDeque<>();
    private final Set<String> sessionSeenQr = new HashSet<>();

    private DecoratedBarcodeView scanner;
    private TextView statusView;
    private TextView receiptView;
    private boolean busy;
    private int sessionScans;
    private int sessionDonations;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildUi();
        scanner.getBarcodeView().setDecoderFactory(
                new DefaultDecoderFactory(Collections.singletonList(BarcodeFormat.QR_CODE)));
        scanner.decodeContinuous(callback);
        showReady();
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);

        TextView title = new TextView(this);
        title.setText("CASSA MD → TIREMM");
        title.setTextSize(22);
        title.setGravity(Gravity.CENTER);
        title.setPadding(dp(12), dp(14), dp(12), dp(10));
        root.addView(title, new LinearLayout.LayoutParams(-1, -2));

        scanner = new DecoratedBarcodeView(this);
        scanner.setStatusText("Inquadra il QR MD");
        LinearLayout.LayoutParams cameraParams = new LinearLayout.LayoutParams(-1, 0, 1f);
        root.addView(scanner, cameraParams);

        statusView = new TextView(this);
        statusView.setTextSize(21);
        statusView.setGravity(Gravity.CENTER);
        statusView.setMinHeight(dp(86));
        statusView.setPadding(dp(12), dp(12), dp(12), dp(12));
        root.addView(statusView, new LinearLayout.LayoutParams(-1, -2));

        receiptView = new TextView(this);
        receiptView.setTextSize(14);
        receiptView.setTypeface(android.graphics.Typeface.MONOSPACE);
        receiptView.setPadding(dp(16), dp(8), dp(16), dp(16));
        root.addView(receiptView, new LinearLayout.LayoutParams(-1, -2));

        setContentView(root);
    }

    private final BarcodeCallback callback = new BarcodeCallback() {
        @Override
        public void barcodeResult(BarcodeResult result) {
            if (busy || result == null || result.getText() == null) return;
            String qr = result.getText().trim();
            if (qr.isEmpty()) return;
            if (!sessionSeenQr.add(qr)) return;
            busy = true;
            scanner.pause();
            statusView.setText("QR LETTO — REGISTRAZIONE…");
            executor.execute(() -> processQr(qr));
        }

        @Override
        public void possibleResultPoints(List<ResultPoint> resultPoints) {
        }
    };

    private void processQr(String qr) {
        try {
            GoodifyApiClient.ApiResponse result = GoodifyApiClient.processQr(qr);
            runOnUiThread(() -> renderResult(result.httpCode, result.body));
        } catch (Exception ignored) {
            runOnUiThread(() -> finishOperation("ERRORE RETE", "RETE", false, 2200));
        }
    }

    private void renderResult(int httpCode, JSONObject response) {
        sessionScans++;
        String status = response.optString("status", "ERRORE");
        boolean already = response.optBoolean("already_processed", false);
        if (httpCode == 200 && "DONATED_TO_TIREMM".equals(status)) {
            if (!already) sessionDonations++;
            JSONObject win = response.optJSONObject("instant_win");
            String winStatus = win == null ? "" : win.optString("status", "");
            if ("WIN".equals(winStatus)) {
                finishOperation("DONATO A TIREMM — VINCITA", "OK+WIN", !already, 2600);
            } else if ("UNKNOWN".equals(winStatus)) {
                finishOperation("DONATO A TIREMM — PREMIO IN VERIFICA", "OK+MAIL", !already, 1800);
            } else {
                finishOperation(already ? "GIÀ REGISTRATO — TIREMM" : "DONATO A TIREMM", already ? "GIÀ" : "OK", !already, 1400);
            }
            return;
        }
        if (httpCode == 202 || "PROCESSING".equals(status)) {
            finishOperation("GIÀ IN ELABORAZIONE", "ATTESA", false, 1600);
            return;
        }
        if (status.startsWith("AMBIGUOUS")) {
            finishOperation("DA VERIFICARE — NON RIPASSARE IL QR", "VERIFICA", false, 2800);
            return;
        }
        finishOperation("ERRORE — " + status, "ERR", false, 2200);
    }

    private void finishOperation(String message, String receiptCode, boolean newDonation, long delayMs) {
        statusView.setText(message);
        addReceipt(receiptCode);
        handler.postDelayed(this::showReady, delayMs);
    }

    private void addReceipt(String code) {
        String line = LocalTime.now().format(CLOCK) + "  " + padCode(code);
        recent.addFirst(line);
        while (recent.size() > 5) recent.removeLast();
        StringBuilder text = new StringBuilder();
        text.append("SCANSIONI ").append(sessionScans)
                .append("   DONAZIONI ").append(sessionDonations).append('\n');
        for (String item : recent) text.append(item).append('\n');
        receiptView.setText(text.toString());
    }

    private String padCode(String code) {
        return code.length() >= 10 ? code : String.format("%-10s", code);
    }

    private void showReady() {
        if (isFinishing() || isDestroyed()) return;
        busy = false;
        statusView.setText("PRONTO — INQUADRA IL PROSSIMO QR");
        scanner.setStatusText("Cassa aperta — inquadra il QR MD");
        scanner.resume();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (!busy && scanner != null) scanner.resume();
    }

    @Override
    protected void onPause() {
        if (scanner != null) scanner.pause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        executor.shutdownNow();
        super.onDestroy();
    }
}
