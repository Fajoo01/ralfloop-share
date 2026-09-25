package org.tiremminnanz.bottazzi;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.speech.tts.TextToSpeech;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.widget.FrameLayout;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.util.Locale;

public final class MainActivity extends Activity {
    private static final int NOTIFICATION_PERMISSION_REQUEST = 4101;
    private static final int AUDIO_PERMISSION_REQUEST = 4102;
    private static final int FILE_CHOOSER_REQUEST = 4103;

    private WebView webView;
    private ValueCallback<Uri[]> fileCallback;
    private Uri pendingCameraUri;
    private TextToSpeech textToSpeech;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        FrameLayout root = new FrameLayout(this);
        webView = new WebView(this);
        root.addView(
            webView,
            new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        );
        setContentView(root);
        installSafeInsets(root);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setUserAgentString(
            settings.getUserAgentString() + " GPTBrowser/" + NativeCore.nativeVersion()
        );

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, false);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                Uri current = Uri.parse(url);
                Uri base = Uri.parse(BuildConfig.APP_URL);
                String currentPath = current.getPath() == null ? "" : current.getPath();
                String basePath = base.getPath() == null ? "" : base.getPath();
                if (
                    sameOrigin(BuildConfig.APP_URL, current)
                    && ("/".equals(currentPath) || basePath.equals(currentPath))
                    && !gptUiUrl().equals(url)
                ) {
                    view.loadUrl(gptUiUrl());
                }
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                runOnUiThread(() -> handleWebPermissionRequest(request));
            }

            @Override
            public boolean onShowFileChooser(
                WebView webView,
                ValueCallback<Uri[]> callback,
                FileChooserParams params
            ) {
                return openFileChooser(callback, params);
            }
        });
        textToSpeech = new TextToSpeech(this, status -> {
            if (status == TextToSpeech.SUCCESS) {
                textToSpeech.setLanguage(Locale.ITALIAN);
            }
        });
        webView.addJavascriptInterface(new SpeechBridge(), "BotTazziNative");
        webView.loadUrl(gptUiUrl());

        requestNotificationPermission();
        requestAudioPermission();
        startNotificationService();
    }

    private String gptUiUrl() {
        String base = BuildConfig.APP_URL;
        while (base.endsWith("/")) {
            base = base.substring(0, base.length() - 1);
        }
        return base + "/gpt-ui";
    }

    @SuppressWarnings("deprecation")
    private void installSafeInsets(FrameLayout root) {
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            int left;
            int top;
            int right;
            int bottom;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                int[] safe = Api30Insets.resolve(insets);
                left = safe[0];
                top = safe[1];
                right = safe[2];
                bottom = safe[3];
            } else {
                left = insets.getSystemWindowInsetLeft();
                top = insets.getSystemWindowInsetTop();
                right = insets.getSystemWindowInsetRight();
                bottom = insets.getSystemWindowInsetBottom();
            }
            FrameLayout.LayoutParams params = (FrameLayout.LayoutParams) webView.getLayoutParams();
            if (
                params.leftMargin != left || params.topMargin != top
                || params.rightMargin != right || params.bottomMargin != bottom
            ) {
                params.setMargins(left, top, right, bottom);
                webView.setLayoutParams(params);
            }
            return insets;
        });
        root.requestApplyInsets();
    }

    private static final class Api30Insets {
        private Api30Insets() {}

        private static int[] resolve(WindowInsets insets) {
            android.graphics.Insets system = insets.getInsets(
                WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout()
            );
            android.graphics.Insets ime = insets.getInsets(WindowInsets.Type.ime());
            return new int[] {
                system.left, system.top, system.right, Math.max(system.bottom, ime.bottom)
            };
        }
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

    private boolean openFileChooser(ValueCallback<Uri[]> callback, WebChromeClient.FileChooserParams params) {
        if (fileCallback != null) {
            fileCallback.onReceiveValue(null);
        }
        fileCallback = callback;
        boolean capture = params != null && params.isCaptureEnabled();
        String[] accept = params == null ? new String[0] : params.getAcceptTypes();
        boolean images = accept.length == 0;
        for (String type : accept) {
            if (type == null || type.isBlank() || type.startsWith("image/")) {
                images = true;
            }
        }
        try {
            if (capture && images) {
                Intent camera = cameraIntent();
                if (camera != null) {
                    startActivityForResult(camera, FILE_CHOOSER_REQUEST);
                    return true;
                }
            }
            Intent picker = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            picker.addCategory(Intent.CATEGORY_OPENABLE);
            picker.setType(images && accept.length == 1 ? "image/*" : "*/*");
            if (accept.length > 0) {
                picker.putExtra(Intent.EXTRA_MIME_TYPES, accept);
            }
            picker.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params != null && params.getMode() == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE);
            Intent chooser = Intent.createChooser(picker, images ? "Scegli foto o file" : "Scegli file");
            if (images) {
                Intent camera = cameraIntent();
                if (camera != null) {
                    chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[] {camera});
                }
            }
            startActivityForResult(chooser, FILE_CHOOSER_REQUEST);
            return true;
        } catch (RuntimeException exc) {
            fileCallback.onReceiveValue(null);
            fileCallback = null;
            return false;
        }
    }

    private Intent cameraIntent() {
        ContentValues values = new ContentValues();
        values.put(MediaStore.Images.Media.DISPLAY_NAME, "bottazzi-" + System.currentTimeMillis() + ".jpg");
        values.put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg");
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            values.put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_PICTURES + "/BotTazzi");
        }
        pendingCameraUri = getContentResolver().insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
        if (pendingCameraUri == null) {
            return null;
        }
        Intent camera = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
        camera.putExtra(MediaStore.EXTRA_OUTPUT, pendingCameraUri);
        camera.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
        return camera.resolveActivity(getPackageManager()) == null ? null : camera;
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != FILE_CHOOSER_REQUEST || fileCallback == null) {
            return;
        }
        Uri[] result = null;
        if (resultCode == RESULT_OK) {
            if (data != null && data.getClipData() != null) {
                int count = data.getClipData().getItemCount();
                result = new Uri[count];
                for (int i = 0; i < count; i++) {
                    result[i] = data.getClipData().getItemAt(i).getUri();
                }
            } else if (data != null && data.getData() != null) {
                result = new Uri[] {data.getData()};
            } else if (pendingCameraUri != null) {
                result = new Uri[] {pendingCameraUri};
            }
        } else if (pendingCameraUri != null) {
            getContentResolver().delete(pendingCameraUri, null, null);
        }
        fileCallback.onReceiveValue(result);
        fileCallback = null;
        pendingCameraUri = null;
    }

    private final class SpeechBridge {
        @JavascriptInterface
        public void speak(String text) {
            runOnUiThread(() -> {
                if (textToSpeech != null) {
                    textToSpeech.speak(String.valueOf(text), TextToSpeech.QUEUE_FLUSH, null, "bottazzi-message");
                }
            });
        }

        @JavascriptInterface
        public void stop() {
            runOnUiThread(() -> {
                if (textToSpeech != null) {
                    textToSpeech.stop();
                }
            });
        }
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
    protected void onDestroy() {
        if (fileCallback != null) {
            fileCallback.onReceiveValue(null);
            fileCallback = null;
        }
        if (textToSpeech != null) {
            textToSpeech.stop();
            textToSpeech.shutdown();
            textToSpeech = null;
        }
        if (webView != null) {
            webView.removeJavascriptInterface("BotTazziNative");
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
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
