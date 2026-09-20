package org.tiremminnanz.mdgoodify;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;

import com.google.zxing.BarcodeFormat;
import com.google.zxing.ResultPoint;
import com.journeyapps.barcodescanner.BarcodeCallback;
import com.journeyapps.barcodescanner.BarcodeResult;
import com.journeyapps.barcodescanner.DecoratedBarcodeView;
import com.journeyapps.barcodescanner.DefaultDecoderFactory;

import java.util.Collections;
import java.util.List;

public final class QrScanActivity extends Activity {
    public static final String EXTRA_QR_TEXT = "qr_text";
    private DecoratedBarcodeView scanner;
    private boolean delivered;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        scanner = new DecoratedBarcodeView(this);
        scanner.setStatusText("Inquadra il QR MD");
        scanner.getBarcodeView().setDecoderFactory(
                new DefaultDecoderFactory(Collections.singletonList(BarcodeFormat.QR_CODE)));
        setContentView(scanner);
        scanner.decodeSingle(callback);
    }

    private final BarcodeCallback callback = new BarcodeCallback() {
        @Override
        public void barcodeResult(BarcodeResult result) {
            if (delivered || result == null || result.getText() == null) return;
            delivered = true;
            Intent data = new Intent().putExtra(EXTRA_QR_TEXT, result.getText());
            setResult(RESULT_OK, data);
            finish();
        }

        @Override
        public void possibleResultPoints(List<ResultPoint> resultPoints) {
        }
    };

    @Override
    protected void onResume() {
        super.onResume();
        scanner.resume();
    }

    @Override
    protected void onPause() {
        scanner.pause();
        super.onPause();
    }
}
