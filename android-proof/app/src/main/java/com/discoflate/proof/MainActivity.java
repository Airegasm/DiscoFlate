package com.discoflate.proof;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * Host shell: copy the bundled UI + seed config to real storage, start the
 * foreground service (which owns the Python server, Kasa MulticastLock, WakeLock,
 * and safe device-off), and show the UI in a WebView on the loopback server.
 * Handles camera permission for the Snapshot page and stamps version + copyright.
 */
public class MainActivity extends Activity {

    private static final String URL = "http://127.0.0.1:8765";
    private static final int REQ_CAM = 202;
    private static final int REQ_NOTIF = 103;
    private WebView web;
    private PermissionRequest pendingCamRequest;
    private String versionLabel = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setTitle("DiscoFlate · v1.4 by AireGasm");

        try {
            PackageInfo pi = getPackageManager().getPackageInfo(getPackageName(), 0);
            long code = (Build.VERSION.SDK_INT >= 28) ? pi.getLongVersionCode() : pi.versionCode;
            versionLabel = "v" + pi.versionName + " (build " + code + ")";
        } catch (Exception e) {
            versionLabel = "";
        }

        // Copy the bundled UI to filesDir/web so aiohttp serves it from a real path.
        File webDir = new File(getFilesDir(), "web");
        webDir.mkdirs();
        copyAsset("web/index.html", new File(webDir, "index.html"));

        // Seed the operator's config ONCE (marker-guarded); on-device edits then persist.
        File dataDir = new File(getFilesDir(), "data");
        dataDir.mkdirs();
        File seedMarker = new File(dataDir, ".seeded_1");
        if (!seedMarker.exists()) {
            copyAsset("seed/config.json", new File(dataDir, "config.json"));
            try {
                seedMarker.createNewFile();
            } catch (Exception ignored) {
            }
        }
        // Always refresh the pristine shipped default (for "Restore Default Config").
        copyAsset("seed/config.json", new File(getFilesDir(), "default_config.json"));

        // Notification permission (Android 13+) so the foreground-service notice shows.
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF);
        }

        // Start the foreground service — it owns the Python server + locks + safe-off.
        try {
            Intent svc = new Intent(this, DiscoFlateService.class);
            if (Build.VERSION.SDK_INT >= 26) {
                startForegroundService(svc);
            } else {
                startService(svc);
            }
        } catch (Exception e) {
            e.printStackTrace();
        }

        // Ensure Python is up so the WebView can poll readiness (the service also
        // starts it + the server; both calls are idempotent).
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }
        final PyObject boot = Python.getInstance().getModule("android_boot");
        boot.callAttr("start", getFilesDir().getAbsolutePath());

        // WebView.
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);   // camera preview autoplay
        web.addJavascriptInterface(new WebBridge(), "Android");   // update button → open APK url
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                stampFooter();
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> {
                    boolean wantsCam = false;
                    for (String r : request.getResources()) {
                        if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) {
                            wantsCam = true;
                            break;
                        }
                    }
                    if (!wantsCam) {
                        request.grant(request.getResources());
                        return;
                    }
                    if (checkSelfPermission(Manifest.permission.CAMERA)
                            == PackageManager.PERMISSION_GRANTED) {
                        request.grant(request.getResources());
                    } else {
                        pendingCamRequest = request;
                        requestPermissions(new String[]{Manifest.permission.CAMERA}, REQ_CAM);
                    }
                });
            }
        });
        setContentView(web);
        web.loadData("<html><body style='font-family:sans-serif;padding:24px'>"
                + "<h3>Starting DiscoFlate…</h3><p>Booting the local server.</p>"
                + "</body></html>", "text/html", "utf-8");

        final Handler ui = new Handler(Looper.getMainLooper());
        new Thread(() -> {
            final boolean up = boot.callAttr("wait_until_up").toBoolean();
            ui.post(() -> {
                if (up) {
                    web.loadUrl(URL);
                } else {
                    web.loadData("<html><body style='font-family:sans-serif;padding:24px'>"
                            + "<h3>DiscoFlate failed to start</h3>"
                            + "<p>The local server didn't come up in time. Check <code>logcat</code>.</p>"
                            + "</body></html>", "text/html", "utf-8");
                }
            });
        }, "discoflate-wait").start();
    }

    private void stampFooter() {
        String text = ("DiscoFlate " + versionLabel + " · © 2026 AireGasm").replace("'", "\\'");
        web.evaluateJavascript(
                "(function(){var f=document.getElementById('appfooter');"
                        + "if(f){f.textContent='" + text + "';}})();", null);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_CAM && pendingCamRequest != null) {
            boolean granted = grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED;
            if (granted) {
                pendingCamRequest.grant(pendingCamRequest.getResources());
            } else {
                pendingCamRequest.deny();
            }
            pendingCamRequest = null;
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    private void copyAsset(String assetPath, File dest) {
        try (InputStream in = getAssets().open(assetPath);
             OutputStream out = new FileOutputStream(dest)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    /** Exposed to the web UI as window.Android — opens the update APK url in the
     *  browser so the user can download + install it (Check for Updates). */
    public class WebBridge {
        @JavascriptInterface
        public void openUrl(final String url) {
            if (url == null || url.isEmpty()) return;
            runOnUiThread(() -> {
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url))
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
                } catch (Exception e) {
                    e.printStackTrace();
                }
            });
        }
    }
}
