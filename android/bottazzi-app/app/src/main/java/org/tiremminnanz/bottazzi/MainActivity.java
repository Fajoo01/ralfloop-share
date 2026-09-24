package org.tiremminnanz.bottazzi;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.webkit.CookieManager;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

public final class MainActivity extends Activity {
    private static final int NOTIFICATION_PERMISSION_REQUEST = 4101;
    private static final int AUDIO_PERMISSION_REQUEST = 4102;

    private WebView webView;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        webView = new WebView(this);
        setContentView(webView);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setUserAgentString(
            settings.getUserAgentString() + " BotTazzi/" + NativeCore.nativeVersion()
        );

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, false);

        webView.setWebViewClient(new WebViewClient());
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                runOnUiThread(() -> handleWebPermissionRequest(request));
            }
        });
        webView.loadUrl(BuildConfig.APP_URL);

        requestNotificationPermission();
        requestAudioPermission();
        startNotificationService();
    }

    private void requestNotificationPermission() {
        if (
            Build.VERSION.SDK_INT >= 33
            && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(
                new String[] {Manifest.permission.POST_NOTIFICATIONS},
                NOTIFICATION_PERMISSION_REQUEST
            );
        }
    }

    private void requestAudioPermission() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, AUDIO_PERMISSION_REQUEST);
        }
    }

    private void handleWebPermissionRequest(PermissionRequest request) {
        if (!sameOrigin(BuildConfig.APP_URL, request.getOrigin())) {
            request.deny();
            return;
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            request.deny();
            requestAudioPermission();
            return;
        }
        String[] resources = request.getResources();
        if (resources.length == 1 && PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resources[0])) {
            request.grant(new String[] {PermissionRequest.RESOURCE_AUDIO_CAPTURE});
        } else {
            request.deny();
        }
    }

    private boolean sameOrigin(String configuredUrl, Uri requestedOrigin) {
        Uri configured = Uri.parse(configuredUrl);
        return configured.getScheme() != null
            && configured.getScheme().equalsIgnoreCase(requestedOrigin.getScheme())
            && configured.getHost() != null
            && configured.getHost().equalsIgnoreCase(requestedOrigin.getHost())
            && configured.getPort() == requestedOrigin.getPort();
    }

    private void startNotificationService() {
        Intent intent = new Intent(this, BotTazziNotifyService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
