package it.tiremminnanz.navigatore;

import android.Manifest;
import android.app.*;
import android.os.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.location.*;
import android.net.Uri;
import android.speech.RecognizerIntent;
import android.view.*;
import android.widget.*;
import java.util.*;
import org.json.*;

public class MainActivity extends Activity {
    private static final int REQ_LOCATION = 100;
    private static final int REQ_SPEECH = 101;
    private final ArrayList<String> destinationNames = new ArrayList<>();
    private final ArrayList<Double> destinationLats = new ArrayList<>();
    private final ArrayList<Double> destinationLons = new ArrayList<>();
    private Spinner destinations;
    private EditText addressInput;
    private LinearLayout searchResults;
    private String searchedDestination;
    private double searchedLat = Double.NaN, searchedLon = Double.NaN;
    private TextView state, instruction, countdown, margin, error;
    private Button login, mic, searchAddress, start, stop;
    private BroadcastReceiver receiver;
    private MapController mapController;

    @Override public void onCreate(Bundle b) {
        super.onCreate(b);
        mapController = new MapController(this, b);
        buildUi();
        refreshAuthUi();
        if (ApiClient.isPaired(this)) loadDestinations();
        receiver = new BroadcastReceiver() {
            @Override public void onReceive(Context c, Intent i) {
                if (!NavigationService.ACTION_UPDATE.equals(i.getAction())) return;
                state.setText(i.getStringExtra("state"));
                instruction.setText(i.getStringExtra("instruction"));
                countdown.setText(format("Mezzo", i.getIntExtra("seconds_to_vehicle", -1)));
                margin.setText(format("Margine", i.getIntExtra("margin_seconds", -1)));
                if (i.hasExtra("lat") && i.hasExtra("lon")) {
                    mapController.updateLocation(i.getDoubleExtra("lat", 0), i.getDoubleExtra("lon", 0), true);
                }
                String payload = i.getStringExtra("navigation_json");
                if (payload != null && !payload.isEmpty()) {
                    try { mapController.updatePlan(new JSONObject(payload)); } catch (JSONException ignored) {}
                }
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
        box.addView(mapController.view(), new LinearLayout.LayoutParams(-1, 720));

        login = new Button(this);
        login.setOnClickListener(v -> loginOrLogout());
        box.addView(login);

        addressInput = new EditText(this);
        addressInput.setHint("Dove vuoi andare? Es. Duomo Milano");
        box.addView(addressInput, new LinearLayout.LayoutParams(-1, 120));
        LinearLayout searchBar = new LinearLayout(this);
        searchBar.setOrientation(LinearLayout.HORIZONTAL);
        mic = new Button(this); mic.setText("🎤");
        mic.setOnClickListener(v -> startVoiceInput());
        searchAddress = new Button(this); searchAddress.setText("CERCA");
        searchAddress.setOnClickListener(v -> searchAddress());
        searchBar.addView(mic, new LinearLayout.LayoutParams(0, 120, 1));
        searchBar.addView(searchAddress, new LinearLayout.LayoutParams(0, 120, 3));
        box.addView(searchBar);
        searchResults = new LinearLayout(this);
        searchResults.setOrientation(LinearLayout.VERTICAL);
        box.addView(searchResults);

        box.addView(title("Oppure scegli un preferito", 14));
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

    private void refreshAuthUi() {
        boolean paired = ApiClient.isPaired(this);
        login.setText(paired ? "ESCI DA ACCOUNT TIREMM" : "ACCEDI CON ACCOUNT TIREMM");
        start.setEnabled(paired);
        destinations.setEnabled(paired);
        if (!paired) {
            state.setText("Login richiesto");
            instruction.setText("Accedi con Account Tiremm per usare il navigatore.");
        }
    }

    private void loginOrLogout() {
        if (ApiClient.isPaired(this)) {
            stopService(new Intent(this, NavigationService.class));
            ApiClient.clearPairing(this);
            destinationNames.clear();
            destinations.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item, new ArrayList<String>()));
            refreshAuthUi();
            return;
        }
        error.setText("Avvio login Account Tiremm...");
        ApiClient.startPairing(this, new ApiClient.PairingListener() {
            @Override public void onCode(String code, String url) {
                runOnUiThread(() -> {
                    error.setText("Codice " + code + ". Completa il login nel browser.");
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                });
            }
            @Override public void onPaired() {
                runOnUiThread(() -> {
                    error.setText("");
                    refreshAuthUi();
                    loadDestinations();
                });
            }
            @Override public void onError(String message) {
                runOnUiThread(() -> error.setText(message));
            }
        });
    }

    private void startVoiceInput() {
        Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE, "it-IT");
        i.putExtra(RecognizerIntent.EXTRA_PROMPT, "Dove vuoi andare?");
        try { startActivityForResult(i, REQ_SPEECH); }
        catch (Exception e) { error.setText("Riconoscimento vocale non disponibile."); }
    }

    private void searchAddress() {
        String query = addressInput.getText().toString().trim();
        if (query.isEmpty()) { error.setText("Scrivi o detta una destinazione."); return; }
        error.setText("Cerco " + query + "...");
        new Thread(() -> {
            try {
                JSONArray rows = ApiClient.searchPlaces(query);
                runOnUiThread(() -> showSearchResults(rows));
            } catch (Exception e) {
                runOnUiThread(() -> error.setText("Ricerca non riuscita: " + e.getMessage()));
            }
        }).start();
    }

    private void showSearchResults(JSONArray rows) {
        searchResults.removeAllViews();
        if (rows.length() == 0) { error.setText("Nessun risultato."); return; }
        error.setText("Scegli il risultato corretto.");
        for (int i = 0; i < rows.length(); i++) {
            JSONObject row = rows.optJSONObject(i);
            if (row == null) continue;
            String label = row.optString("display_name", "Destinazione");
            double lat = row.optDouble("lat", Double.NaN);
            double lon = row.optDouble("lon", Double.NaN);
            Button b = new Button(this); b.setText(label);
            b.setOnClickListener(v -> {
                searchedDestination = label; searchedLat = lat; searchedLon = lon;
                addressInput.setText(label); searchResults.removeAllViews();
                mapController.updateDestination(lat, lon);
                error.setText("Destinazione selezionata.");
            });
            searchResults.addView(b);
        }
    }

    private void loadDestinations() {
        new Thread(() -> {
            try {
                JSONObject j = ApiClient.get(this, "/navigator/destinations");
                JSONArray a = j.optJSONArray("destinations");
                ArrayList<String> labels = new ArrayList<>();
                destinationNames.clear(); destinationLats.clear(); destinationLons.clear();
                if (a != null) for (int i=0;i<a.length();i++) {
                    JSONObject d=a.getJSONObject(i);
                    destinationNames.add(d.getString("name"));
                    destinationLats.add(d.optDouble("lat"));
                    destinationLons.add(d.optDouble("lon"));
                    labels.add(d.optString("label", d.getString("name")));
                }
                runOnUiThread(() -> {
                    destinations.setAdapter(new ArrayAdapter<>(this,
                        android.R.layout.simple_spinner_dropdown_item, labels));
                    if (!destinationLats.isEmpty()) mapController.updateDestination(destinationLats.get(0), destinationLons.get(0));
                    destinations.setOnItemSelectedListener(new android.widget.AdapterView.OnItemSelectedListener() {
                        public void onItemSelected(android.widget.AdapterView<?> p, View v, int pos, long id) {
                            if (pos >= 0 && pos < destinationLats.size()) mapController.updateDestination(destinationLats.get(pos), destinationLons.get(pos));
                        }
                        public void onNothingSelected(android.widget.AdapterView<?> p) {}
                    });
                });
            } catch (Exception e) {
                runOnUiThread(() -> error.setText("Backend non raggiungibile: " + e.getMessage()));
            }
        }).start();
    }

    private void startNavigation() {
        if (!ApiClient.isPaired(this)) {
            error.setText("Accedi prima con Account Tiremm.");
            refreshAuthUi();
            return;
        }
        if (!addressInput.getText().toString().trim().isEmpty() && searchedDestination == null) {
            error.setText("Prima cerca e scegli la destinazione proposta.");
            searchAddress();
            return;
        }
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION}, REQ_LOCATION);
            return;
        }
        if (searchedDestination == null && destinationNames.isEmpty()) {
            error.setText("Nessuna destinazione disponibile."); return;
        }
        LocationManager lm = (LocationManager)getSystemService(LOCATION_SERVICE);
        Location loc = lm.getLastKnownLocation(LocationManager.GPS_PROVIDER);
        if (loc == null) loc = lm.getLastKnownLocation(LocationManager.NETWORK_PROVIDER);
        if (loc == null) {
            error.setText("Attendo una posizione GPS...");
            lm.requestSingleUpdate(LocationManager.GPS_PROVIDER, l -> beginService(l), null);
        } else beginService(loc);
    }

    private void beginService(Location loc) {
        mapController.updateLocation(loc.getLatitude(), loc.getLongitude(), true);
        String destination;
        Intent i = new Intent(this, NavigationService.class);
        if (searchedDestination != null && !Double.isNaN(searchedLat) && !Double.isNaN(searchedLon)) {
            destination = searchedDestination;
            i.putExtra("destination_lat", searchedLat);
            i.putExtra("destination_lon", searchedLon);
            i.putExtra("destination_label", searchedDestination);
        } else {
            destination = destinationNames.get(destinations.getSelectedItemPosition());
        }
        i.putExtra("destination", destination);
        i.putExtra("lat", loc.getLatitude());
        i.putExtra("lon", loc.getLongitude());
        startForegroundService(i);
        state.setText("Avvio...");
    }

    @Override protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_SPEECH && resultCode == RESULT_OK && data != null) {
            ArrayList<String> rows = data.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS);
            if (rows != null && !rows.isEmpty()) {
                addressInput.setText(rows.get(0));
                searchedDestination = null;
                searchedLat = Double.NaN; searchedLon = Double.NaN;
                searchAddress();
            }
        }
    }

    @Override public void onRequestPermissionsResult(int r, String[] p, int[] g) {
        super.onRequestPermissionsResult(r,p,g);
        if (r == REQ_LOCATION && g.length > 0 && g[0] == PackageManager.PERMISSION_GRANTED) startNavigation();
    }

    @Override protected void onStart() { super.onStart(); mapController.onStart(); }
    @Override protected void onResume() { super.onResume(); mapController.onResume(); }
    @Override protected void onPause() { mapController.onPause(); super.onPause(); }
    @Override protected void onStop() { mapController.onStop(); super.onStop(); }
    @Override public void onLowMemory() { super.onLowMemory(); mapController.onLowMemory(); }

    @Override protected void onDestroy() {
        if (receiver != null) unregisterReceiver(receiver);
        mapController.onDestroy();
        super.onDestroy();
    }
}
