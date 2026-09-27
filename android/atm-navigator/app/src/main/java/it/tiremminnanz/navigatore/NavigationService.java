package it.tiremminnanz.navigatore;

import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.location.*;
import android.os.*;
import org.json.*;

public class NavigationService extends Service implements LocationListener {
    public static final String ACTION_UPDATE = "it.tiremminnanz.navigatore.UPDATE";
    private static final int NOTIFICATION_ID = 51;
    private LocationManager lm;
    private String baseUrl, destination, sessionId;
    private volatile boolean busy = false;

    @Override public void onCreate() {
        super.onCreate();
        NotificationChannel ch = new NotificationChannel("navigation", "Navigazione ATM",
            NotificationManager.IMPORTANCE_LOW);
        getSystemService(NotificationManager.class).createNotificationChannel(ch);
        startForeground(NOTIFICATION_ID, notification("Navigazione in avvio"));
    }

    @Override public int onStartCommand(Intent i, int flags, int startId) {
        baseUrl = i.getStringExtra("base_url");
        destination = i.getStringExtra("destination");
        double lat = i.getDoubleExtra("lat", 0), lon = i.getDoubleExtra("lon", 0);
        new Thread(() -> startSession(lat, lon)).start();
        lm = (LocationManager)getSystemService(LOCATION_SERVICE);
        if (checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) {
            lm.requestLocationUpdates(LocationManager.GPS_PROVIDER, 5000, 8f, this, Looper.getMainLooper());
            lm.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, 10000, 20f, this, Looper.getMainLooper());
        }
        return START_STICKY;
    }

    private Notification notification(String text) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, "navigation")
            .setContentTitle("Tiremm Navigatore")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_dialog_map)
            .setContentIntent(pi)
            .setOngoing(true)
            .build();
    }

    private void startSession(double lat, double lon) {
        try {
            JSONObject body = new JSONObject()
                .put("lat", lat).put("lon", lon).put("destination", destination);
            JSONObject j = ApiClient.post(baseUrl, "/navigator/start", body);
            sessionId = j.getString("session_id");
            publish(j);
        } catch (Exception e) { publishError(e); }
    }

    @Override public void onLocationChanged(Location l) {
        if (sessionId == null || busy) return;
        busy = true;
        new Thread(() -> {
            try {
                JSONObject body = new JSONObject()
                    .put("session_id", sessionId)
                    .put("lat", l.getLatitude())
                    .put("lon", l.getLongitude());
                JSONObject j = ApiClient.post(baseUrl, "/navigator/update", body);
                publish(j);
                if ("arrived".equals(j.optString("state"))) stopSelf();
            } catch (Exception e) { publishError(e); }
            finally { busy = false; }
        }).start();
    }

    private void publish(JSONObject j) {
        String s = j.optString("state", "");
        String instruction = j.optString("instruction", "");
        int seconds = j.has("seconds_to_vehicle") && !j.isNull("seconds_to_vehicle") ? j.optInt("seconds_to_vehicle", -1) : -1;
        int margin = j.has("margin_seconds") && !j.isNull("margin_seconds") ? j.optInt("margin_seconds", -1) : -1;
        Intent u = new Intent(ACTION_UPDATE);
        u.setPackage(getPackageName());
        u.putExtra("state", s);
        u.putExtra("instruction", instruction);
        u.putExtra("seconds_to_vehicle", seconds);
        u.putExtra("margin_seconds", margin);
        sendBroadcast(u);
        getSystemService(NotificationManager.class).notify(NOTIFICATION_ID,
            notification(instruction.isEmpty() ? s : instruction));
    }

    private void publishError(Exception e) {
        Intent u = new Intent(ACTION_UPDATE);
        u.setPackage(getPackageName());
        u.putExtra("error", e.getMessage());
        sendBroadcast(u);
    }

    @Override public void onDestroy() {
        if (lm != null) lm.removeUpdates(this);
        if (sessionId != null) new Thread(() -> {
            try { ApiClient.post(baseUrl, "/navigator/stop", new JSONObject().put("session_id", sessionId)); }
            catch (Exception ignored) {}
        }).start();
        super.onDestroy();
    }

    @Override public android.os.IBinder onBind(Intent i) { return null; }
}
