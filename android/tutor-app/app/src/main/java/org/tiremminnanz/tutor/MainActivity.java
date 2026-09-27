package org.tiremminnanz.tutor;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.webkit.CookieManager;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

public final class MainActivity extends Activity {
    private static final int MICROPHONE_REQUEST = 4201;
    private static final int FILE_REQUEST = 4202;
    private WebView webView;
    private PermissionRequest pendingMicrophone;
    private ValueCallback<Uri[]> pendingFiles;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        webView = new WebView(this);
        setContentView(webView);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setUserAgentString(settings.getUserAgentString() + " TiremmTutor/1.0");

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, false);

        webView.setWebViewClient(new TutorWebViewClient());
        webView.setWebChromeClient(new TutorChromeClient());
        webView.loadUrl(BuildConfig.APP_URL);
    }

    private static boolean sameOrigin(String left, String right) {
        try {
            Uri a = Uri.parse(left);
            Uri b = Uri.parse(right);
            int aPort = a.getPort() == -1 ? 443 : a.getPort();
            int bPort = b.getPort() == -1 ? 443 : b.getPort();
            return "https".equalsIgnoreCase(a.getScheme())
                && "https".equalsIgnoreCase(b.getScheme())
                && aPort == bPort
                && a.getHost() != null
                && a.getHost().equalsIgnoreCase(b.getHost());
        } catch (RuntimeException ignored) {
            return false;
        }
    }

    private final class TutorWebViewClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            String url = request.getUrl().toString();
            if (sameOrigin(url, BuildConfig.APP_URL) || sameOrigin(url, BuildConfig.OIDC_ORIGIN)) {
                return false;
            }
            if ("https".equalsIgnoreCase(request.getUrl().getScheme())) {
                startActivity(new Intent(Intent.ACTION_VIEW, request.getUrl()));
            }
            return true;
        }
    }

    private final class TutorChromeClient extends WebChromeClient {
        @Override
        public void onPermissionRequest(PermissionRequest request) {
            if (!sameOrigin(request.getOrigin().toString(), BuildConfig.APP_URL)) {
                request.deny();
                return;
            }
            boolean asksForMic = false;
            for (String resource : request.getResources()) {
                if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) asksForMic = true;
            }
            if (!asksForMic) {
                request.deny();
                return;
            }
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                request.grant(new String[] {PermissionRequest.RESOURCE_AUDIO_CAPTURE});
            } else {
                pendingMicrophone = request;
                requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, MICROPHONE_REQUEST);
            }
        }

        @Override
        public boolean onShowFileChooser(
            WebView view,
            ValueCallback<Uri[]> callback,
            FileChooserParams params
        ) {
            if (pendingFiles != null) pendingFiles.onReceiveValue(null);
            pendingFiles = callback;
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE);
            intent.setType("*/*");
            String[] accept = params == null ? null : params.getAcceptTypes();
            if (accept != null && accept.length > 0 && !accept[0].isBlank()) {
                intent.setType(accept[0]);
            }
            startActivityForResult(intent, FILE_REQUEST);
            return true;
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode != MICROPHONE_REQUEST || pendingMicrophone == null) return;
        PermissionRequest request = pendingMicrophone;
        pendingMicrophone = null;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            request.grant(new String[] {PermissionRequest.RESOURCE_AUDIO_CAPTURE});
        } else {
            request.deny();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != FILE_REQUEST || pendingFiles == null) return;
        ValueCallback<Uri[]> callback = pendingFiles;
        pendingFiles = null;
        callback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
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
