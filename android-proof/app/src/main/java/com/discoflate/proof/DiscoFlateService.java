package com.discoflate.proof;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * Foreground service that owns DiscoFlate's Python server so it survives the
 * user leaving the app. Holds a Wi-Fi MulticastLock (Kasa discovery) + a partial
 * WakeLock (bot stays connected), and — the safety part — forces every device
 * OFF when the app is stopped or swiped away, so a pump can't get stuck on.
 */
public class DiscoFlateService extends Service {

    private static final String CHANNEL_ID = "discoflate_running";
    private static final int NOTIF_ID = 1;

    private WifiManager.MulticastLock multicastLock;
    private PowerManager.WakeLock wakeLock;

    @Override
    public void onCreate() {
        super.onCreate();
        startInForeground();

        // Locks: Kasa UDP broadcast + keep the CPU alive for the bot connection.
        try {
            WifiManager wifi = (WifiManager) getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            if (wifi != null) {
                multicastLock = wifi.createMulticastLock("discoflate-kasa");
                multicastLock.setReferenceCounted(false);
                multicastLock.acquire();
            }
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "discoflate:server");
                wakeLock.acquire();
            }
        } catch (Exception e) {
            e.printStackTrace();
        }

        // Start embedded Python + DiscoFlate's server (idempotent; the Activity
        // may have started it already).
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(getApplicationContext()));
        }
        try {
            Python.getInstance().getModule("android_boot")
                    .callAttr("start", getFilesDir().getAbsolutePath());
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;   // restart if the system kills us
    }

    /** User swiped the app away — turn devices off, then stop. */
    @Override
    public void onTaskRemoved(Intent rootIntent) {
        safetyOff();
        stopSelf();
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public void onDestroy() {
        safetyOff();
        try {
            if (multicastLock != null && multicastLock.isHeld()) multicastLock.release();
        } catch (Exception ignored) {
        }
        try {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        } catch (Exception ignored) {
        }
        super.onDestroy();
    }

    private void safetyOff() {
        try {
            if (Python.isStarted()) {
                Python.getInstance().getModule("android_boot").callAttr("force_off");
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    private void startInForeground() {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL_ID, "DiscoFlate running", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("Keeps the bot + control server alive.");
            if (nm != null) nm.createNotificationChannel(ch);
        }
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(
                this, 0, open, PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("DiscoFlate is running")
                .setContentText("Bot + control server active. Tap to open.")
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setOngoing(true)
                .setContentIntent(pi)
                .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
        } else if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
        } else {
            startForeground(NOTIF_ID, n);
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
