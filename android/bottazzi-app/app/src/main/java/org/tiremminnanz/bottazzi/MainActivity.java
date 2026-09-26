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
    private GptPowerControl powerControl;
    private ValueCallback<Uri[]> fileCallback;
    private Uri pendingCameraUri;
    private TextToSpeech textToSpeech;
    private volatile boolean speechReady;
    private PermissionRequest pendingAudioRequest;

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
        powerControl = new GptPowerControl(this);
        root.addView(powerControl, new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            (int) (56 * getResources().getDisplayMetrics().density)
        ));
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

            @Override
            public void onPermissionRequestCanceled(PermissionRequest request) {
                if (pendingAudioRequest == request) pendingAudioRequest = null;
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
            if (status == TextToSpeech.SUCCESS && textToSpeech != null) {
                int language = textToSpeech.setLanguage(Locale.ITALIAN);
                speechReady = language != TextToSpeech.LANG_MISSING_DATA && language != TextToSpeech.LANG_NOT_SUPPORTED;
            }
        });
        webView.addJavascriptInterface(new SpeechBridge(), "BotTazziNative");
        webView.loadUrl(BuildConfig.APP_URL);

        requestNotificationPermission();
        startNotificationService();
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
            powerControl.setTranslationY(top);
            powerControl.setPadding(left, 0, right, 0);
            top += (int) (56 * getResources().getDisplayMetrics().density);
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
        String[] resources = request.getResources();
        if (resources.length == 1 && PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resources[0])) {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                request.grant(new String[] {PermissionRequest.RESOURCE_AUDIO_CAPTURE});
            } else {
                if (pendingAudioRequest != null) pendingAudioRequest.deny();
                pendingAudioRequest = request;
                requestAudioPermission();
            }
        } else {
            request.deny();
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != AUDIO_PERMISSION_REQUEST || pendingAudioRequest == null) return;
        PermissionRequest request = pendingAudioRequest;
        pendingAudioRequest = null;
        if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED
                && sameOrigin(BuildConfig.APP_URL, request.getOrigin())
                && webView.getUrl() != null && sameOrigin(BuildConfig.APP_URL, Uri.parse(webView.getUrl()))) {
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
        public boolean speak(String text) {
            if (!speechReady || text == null || text.trim().isEmpty()) return false;
            runOnUiThread(() -> {
                if (textToSpeech != null) {
                    int size = Math.min(3500, TextToSpeech.getMaxSpeechInputLength() - 1);
                    for (int i = 0; i < text.length(); i += size) {
                        textToSpeech.speak(text.substring(i, Math.min(text.length(), i + size)),
                            i == 0 ? TextToSpeech.QUEUE_FLUSH : TextToSpeech.QUEUE_ADD, null, "bottazzi-message-" + i);
                    }
                }
            });
            return true;
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
        if (pendingAudioRequest != null) {
            pendingAudioRequest.deny();
            pendingAudioRequest = null;
        }
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
