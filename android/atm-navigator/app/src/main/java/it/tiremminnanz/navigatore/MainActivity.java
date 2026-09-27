package it.tiremminnanz.navigatore;

import android.Manifest;
import android.app.*;
import android.os.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.location.*;
import android.view.*;
import android.widget.*;
import java.util.*;
import org.json.*;

public class MainActivity extends Activity {
    private static final int REQ_LOCATION = 100;
    private final ArrayList<String> destinationNames = new ArrayList<>();
    private Spinner destinations;
    private TextView state, instruction, countdown, margin, error;
    private Button start, stop;
    private String baseUrl;
    private BroadcastReceiver receiver;

    @Override public void onCreate(Bundle b) {
        super.onCreate(b);
        baseUrl = getSharedPreferences("nav", MODE_PRIVATE)
            .getString("base_url", ApiClient.DEFAULT_BASE_URL);
        buildUi();
        loadDestinations();
        receiver = new BroadcastReceiver() {
            @Override public void onReceive(Context c, Intent i) {
                if (!NavigationService.ACTION_UPDATE.equals(i.getAction())) return;
                state.setText(i.getStringExtra("state"));
                instruction.setText(i.getStringExtra("instruction"));
                countdown.setText(format("Mezzo", i.getIntExtra("seconds_to_vehicle", -1)));
                margin.setText(format("Margine", i.getIntExtra("margin_seconds", -1)));
                error.setText(i.getStringExtra("error") == null ? "" : i.getStringExtra("error"));
            }
        };
        IntentFilter filter = new IntentFilter(NavigationService.ACTION_UPDATE);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(receiver, filter, RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(receiver, filter);
        }
    }

    private String format(String label, int seconds) {
        if (seconds < 0) return "";
        return label + " " + (seconds / 60) + ":" + String.format(Locale.ITALY, "%02d", seconds % 60);
    }

    private TextView title(String text, int sp) {
        TextView v = new TextView(this);
        v.setText(text);
        v.setTextSize(sp);
        v.setPadding(0, 12, 0, 12);
        return v;
    }

    private void buildUi() {
        ScrollView scroll = new ScrollView(this);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(32, 36, 32, 36);
        scroll.addView(box);

        box.addView(title("Tiremm Navigatore", 28));
        TextView sub = title("Navigazione ATM in tempo reale", 16);
        box.addView(sub);

        destinations = new Spinner(this);
        box.addView(destinations, new LinearLayout.LayoutParams(-1, 120));

        start = new Button(this); start.setText("AVVIA NAVIGAZIONE");
        stop = new Button(this); stop.setText("FERMA");
        box.addView(start); box.addView(stop);

        state = title("Pronto", 20);
        instruction = title("Scegli una destinazione e avvia.", 24);
        countdown = title("", 20);
        margin = title("", 20);
        error = title("", 14);
        box.addView(state); box.addView(instruction); box.addView(countdown); box.addView(margin); box.addView(error);

        start.setOnClickListener(v -> startNavigation());
        stop.setOnClickListener(v -> {
            stopService(new Intent(this, NavigationService.class));
            state.setText("Fermato");
            instruction.setText("Navigazione terminata.");
        });
        setContentView(scroll);
    }

    private void loadDestinations() {
        new Thread(() -> {
            try {
                JSONObject j = ApiClient.get(baseUrl, "/destinations");
                JSONArray a = j.optJSONArray("destinations");
                ArrayList<String> labels = new ArrayList<>();
                destinationNames.clear();
                if (a != null) for (int i=0;i<a.length();i++) {
                    JSONObject d=a.getJSONObject(i);
                    destinationNames.add(d.getString("name"));
                    labels.add(d.optString("label", d.getString("name")));
                }
                runOnUiThread(() -> destinations.setAdapter(new ArrayAdapter<>(this,
                    android.R.layout.simple_spinner_dropdown_item, labels)));
            } catch (Exception e) {
                runOnUiThread(() -> error.setText("Backend non raggiungibile: " + e.getMessage()));
            }
        }).start();
    }

    private void startNavigation() {
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION}, REQ_LOCATION);
            return;
        }
        if (destinationNames.isEmpty()) { error.setText("Nessuna destinazione disponibile."); return; }
        LocationManager lm = (LocationManager)getSystemService(LOCATION_SERVICE);
        Location loc = lm.getLastKnownLocation(LocationManager.GPS_PROVIDER);
        if (loc == null) loc = lm.getLastKnownLocation(LocationManager.NETWORK_PROVIDER);
        if (loc == null) {
            error.setText("Attendo una posizione GPS...");
            lm.requestSingleUpdate(LocationManager.GPS_PROVIDER, l -> beginService(l), null);
        } else beginService(loc);
    }

    private void beginService(Location loc) {
        String destination = destinationNames.get(destinations.getSelectedItemPosition());
        Intent i = new Intent(this, NavigationService.class);
        i.putExtra("base_url", baseUrl);
        i.putExtra("destination", destination);
        i.putExtra("lat", loc.getLatitude());
        i.putExtra("lon", loc.getLongitude());
        startForegroundService(i);
        state.setText("Avvio...");
    }

    @Override public void onRequestPermissionsResult(int r, String[] p, int[] g) {
        super.onRequestPermissionsResult(r,p,g);
        if (r == REQ_LOCATION && g.length > 0 && g[0] == PackageManager.PERMISSION_GRANTED) startNavigation();
    }

    @Override protected void onDestroy() {
        if (receiver != null) unregisterReceiver(receiver);
        super.onDestroy();
    }
}
