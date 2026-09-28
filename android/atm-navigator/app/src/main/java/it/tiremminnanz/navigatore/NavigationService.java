package it.tiremminnanz.navigatore;

import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.location.*;
import android.os.*;
import android.speech.tts.TextToSpeech;
import java.util.Locale;
import org.json.*;

public class NavigationService extends Service implements LocationListener {
    public static final String ACTION_UPDATE = "it.tiremminnanz.navigatore.UPDATE";
    private static final int NOTIFICATION_ID = 51;
    private LocationManager lm;
    private String destination, destinationLabel, sessionId;
    private double destinationLat = Double.NaN, destinationLon = Double.NaN;
    private volatile boolean busy = false;
    private volatile double lastLat = Double.NaN, lastLon = Double.NaN;
    private volatile float lastBearing = -1f;
    private TextToSpeech tts;
    private volatile boolean ttsReady = false;
    private String lastSpokenInstruction = "";

    @Override public void onCreate() {
        super.onCreate();
        NotificationChannel ch = new NotificationChannel("navigation", "Navigazione ATM",
            NotificationManager.IMPORTANCE_LOW);
        getSystemService(NotificationManager.class).createNotificationChannel(ch);
        startForeground(NOTIFICATION_ID, notification("Navigazione in avvio"));
        tts = new TextToSpeech(this, status -> {
            if (status == TextToSpeech.SUCCESS) {
                int result = tts.setLanguage(Locale.ITALIAN);
                ttsReady = result != TextToSpeech.LANG_MISSING_DATA && result != TextToSpeech.LANG_NOT_SUPPORTED;
            }
        });
    }

    @Override public int onStartCommand(Intent i, int flags, int startId) {
        if (i == null) return START_NOT_STICKY;
        destination = i.getStringExtra("destination");
        destinationLabel = i.getStringExtra("destination_label");
        destinationLat = i.hasExtra("destination_lat") ? i.getDoubleExtra("destination_lat", Double.NaN) : Double.NaN;
        destinationLon = i.hasExtra("destination_lon") ? i.getDoubleExtra("destination_lon", Double.NaN) : Double.NaN;
        double lat = i.getDoubleExtra("lat", 0), lon = i.getDoubleExtra("lon", 0);
        lastLat = lat; lastLon = lon;
        new Thread(() -> startSession(lat, lon)).start();
        lm = (LocationManager)getSystemService(LOCATION_SERVICE);
        if (checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) {
            lm.requestLocationUpdates(LocationManager.GPS_PROVIDER, 2500, 4f, this, Looper.getMainLooper());
            lm.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, 8000, 15f, this, Looper.getMainLooper());
        }
        return START_NOT_STICKY;
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
            if (!Double.isNaN(destinationLat) && !Double.isNaN(destinationLon)) {
                body.put("destination_lat", destinationLat)
                    .put("destination_lon", destinationLon)
                    .put("destination_label", destinationLabel == null ? destination : destinationLabel);
            }
            JSONObject j = ApiClient.post(this, "/device/navigator/start", body);
            sessionId = j.getString("session_id");
            publish(j);
        } catch (Exception e) { publishError(e); }
    }

    @Override public void onLocationChanged(Location l) {
        lastLat = l.getLatitude(); lastLon = l.getLongitude();
        if (l.hasBearing()) lastBearing = l.getBearing();
        if (sessionId == null || busy) return;
        busy = true;
        new Thread(() -> {
            try {
                JSONObject body = new JSONObject()
                    .put("session_id", sessionId)
                    .put("lat", l.getLatitude())
                    .put("lon", l.getLongitude());
                JSONObject j = ApiClient.post(this, "/device/navigator/update", body);
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
        if (!Double.isNaN(lastLat) && !Double.isNaN(lastLon)) {
            u.putExtra("lat", lastLat);
            u.putExtra("lon", lastLon);
        }
        if (lastBearing >= 0f) u.putExtra("bearing", lastBearing);
        u.putExtra("navigation_json", j.toString());
        sendBroadcast(u);
        getSystemService(NotificationManager.class).notify(NOTIFICATION_ID,
            notification(instruction.isEmpty() ? s : instruction));
        if (ttsReady && tts != null && !instruction.isEmpty() && !instruction.equals(lastSpokenInstruction)) {
            lastSpokenInstruction = instruction;
            tts.speak(instruction, TextToSpeech.QUEUE_FLUSH, null, "tiremm-nav-instruction");
        }
    }

    private void publishError(Exception e) {
        Intent u = new Intent(ACTION_UPDATE);
        u.setPackage(getPackageName());
        u.putExtra("error", e.getMessage());
        sendBroadcast(u);
    }

    @Override public void onDestroy() {
        if (lm != null) lm.removeUpdates(this);
        if (tts != null) {
            tts.stop();
            tts.shutdown();
            tts = null;
        }
        if (sessionId != null) new Thread(() -> {
            try { ApiClient.post(this, "/device/navigator/stop", new JSONObject().put("session_id", sessionId)); }
            catch (Exception ignored) {}
        }).start();
        super.onDestroy();
    }

    @Override public android.os.IBinder onBind(Intent i) { return null; }
}
