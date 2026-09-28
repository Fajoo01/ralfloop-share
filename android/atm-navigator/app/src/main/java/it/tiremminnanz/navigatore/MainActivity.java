package it.tiremminnanz.navigatore;

import android.Manifest;
import android.app.*;
import android.os.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.location.*;
import android.net.Uri;
import android.speech.RecognizerIntent;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
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
    private Button login, mic, searchAddress, start, stop, recenter;
    private View planningPanel;
    private LinearLayout navigationPanel;
    private boolean navigationMode = false;
    private BroadcastReceiver receiver;
    private MapController mapController;

    @Override public void onCreate(Bundle b) {
        super.onCreate(b);
        try {
            mapController = new MapController(this, b);
        } catch (Throwable ignored) {
            mapController = null;
        }
        buildUi();
        refreshAuthUi();
        if (ApiClient.isPaired(this)) loadDestinations();
        receiver = new BroadcastReceiver() {
            @Override public void onReceive(Context c, Intent i) {
                if (!NavigationService.ACTION_UPDATE.equals(i.getAction())) return;
                String navState = i.getStringExtra("state");
                String navInstruction = i.getStringExtra("instruction");
                if (navState != null && !navState.isEmpty()) enterNavigationMode();
                state.setText(navState == null ? "" : stateLabel(navState));
                instruction.setText(navInstruction == null ? "" : navInstruction);
                countdown.setText(format("Mezzo", i.getIntExtra("seconds_to_vehicle", -1)));
                margin.setText(format("Margine", i.getIntExtra("margin_seconds", -1)));
                if (mapController != null && i.hasExtra("lat") && i.hasExtra("lon")) {
                    mapController.updateLocation(
                        i.getDoubleExtra("lat", 0), i.getDoubleExtra("lon", 0), navigationMode,
                        i.getFloatExtra("bearing", -1f));
                }
                String payload = i.getStringExtra("navigation_json");
                if (mapController != null && payload != null && !payload.isEmpty()) {
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
        v.setTextColor(Color.rgb(24, 24, 24));
        v.setPadding(0, dp(6), 0, dp(6));
        return v;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private GradientDrawable card(int alpha) {
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Color.argb(alpha, 255, 255, 255));
        bg.setCornerRadius(dp(18));
        bg.setStroke(dp(1), Color.argb(40, 0, 0, 0));
        return bg;
    }

    private String stateLabel(String value) {
        if ("walking_to_stop".equals(value)) return "🚶 A PIEDI";
        if ("waiting".equals(value)) return "⏳ IN ATTESA";
        if ("onboard".equals(value)) return "🚌 A BORDO";
        if ("transfer".equals(value)) return "🔁 CAMBIO";
        if ("final_walk".equals(value)) return "🚶 ULTIMO TRATTO";
        if ("arrived".equals(value)) return "🏁 ARRIVATO";
        if ("missed".equals(value)) return "⏭ RICALCOLO";
        if ("replanning".equals(value)) return "↻ RICALCOLO";
        return value == null ? "" : value.toUpperCase(Locale.ITALY);
    }

    private void enterNavigationMode() {
        navigationMode = true;
        if (planningPanel != null) planningPanel.setVisibility(View.GONE);
        if (navigationPanel != null) navigationPanel.setVisibility(View.VISIBLE);
        if (recenter != null) recenter.setVisibility(View.VISIBLE);
        if (mapController != null) mapController.setFollowMode(true);
    }

    private void exitNavigationMode() {
        navigationMode = false;
        if (planningPanel != null) planningPanel.setVisibility(View.VISIBLE);
        if (navigationPanel != null) navigationPanel.setVisibility(View.GONE);
        if (recenter != null) recenter.setVisibility(View.GONE);
        if (mapController != null) mapController.setFollowMode(false);
    }

    private void buildUi() {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(235, 238, 240));

        if (mapController != null) {
            root.addView(mapController.view(), new FrameLayout.LayoutParams(-1, -1));
        }

        LinearLayout planner = new LinearLayout(this);
        planner.setOrientation(LinearLayout.VERTICAL);
        planner.setPadding(dp(16), dp(14), dp(16), dp(14));
        planner.setBackground(card(242));
        planningPanel = planner;
        FrameLayout.LayoutParams plannerLp = new FrameLayout.LayoutParams(-1, -2, Gravity.TOP);
        plannerLp.setMargins(dp(12), dp(18), dp(12), 0);
        root.addView(planner, plannerLp);

        TextView appTitle = title("Tiremm Navigatore", 22);
        appTitle.setTypeface(null, android.graphics.Typeface.BOLD);
        planner.addView(appTitle);
        login = new Button(this);
        login.setOnClickListener(v -> loginOrLogout());
        planner.addView(login, new LinearLayout.LayoutParams(-1, dp(48)));

        addressInput = new EditText(this);
        addressInput.setSingleLine(true);
        addressInput.setHint("Dove vuoi andare?");
        planner.addView(addressInput, new LinearLayout.LayoutParams(-1, dp(54)));

        LinearLayout searchBar = new LinearLayout(this);
        searchBar.setOrientation(LinearLayout.HORIZONTAL);
        mic = new Button(this); mic.setText("🎤");
        mic.setContentDescription("Detta destinazione");
        mic.setOnClickListener(v -> startVoiceInput());
        searchAddress = new Button(this); searchAddress.setText("CERCA");
        searchAddress.setOnClickListener(v -> searchAddress());
        searchBar.addView(mic, new LinearLayout.LayoutParams(0, dp(50), 1));
        searchBar.addView(searchAddress, new LinearLayout.LayoutParams(0, dp(50), 3));
        planner.addView(searchBar);

        searchResults = new LinearLayout(this);
        searchResults.setOrientation(LinearLayout.VERTICAL);
        planner.addView(searchResults);

        destinations = new Spinner(this);
        planner.addView(destinations, new LinearLayout.LayoutParams(-1, dp(48)));
        start = new Button(this); start.setText("AVVIA");
        planner.addView(start, new LinearLayout.LayoutParams(-1, dp(54)));
        start.setOnClickListener(v -> startNavigation());

        navigationPanel = new LinearLayout(this);
        navigationPanel.setOrientation(LinearLayout.VERTICAL);
        navigationPanel.setPadding(dp(18), dp(14), dp(18), dp(14));
        navigationPanel.setBackground(card(248));
        navigationPanel.setVisibility(View.GONE);
        FrameLayout.LayoutParams navLp = new FrameLayout.LayoutParams(-1, -2, Gravity.TOP);
        navLp.setMargins(dp(12), dp(18), dp(12), 0);
        root.addView(navigationPanel, navLp);

        state = title("PRONTO", 15);
        state.setTypeface(null, android.graphics.Typeface.BOLD);
        instruction = title("Scegli una destinazione e avvia.", 26);
        instruction.setTypeface(null, android.graphics.Typeface.BOLD);
        navigationPanel.addView(state);
        navigationPanel.addView(instruction);

        LinearLayout stats = new LinearLayout(this);
        stats.setOrientation(LinearLayout.HORIZONTAL);
        countdown = title("", 18);
        margin = title("", 18);
        stats.addView(countdown, new LinearLayout.LayoutParams(0, -2, 1));
        stats.addView(margin, new LinearLayout.LayoutParams(0, -2, 1));
        navigationPanel.addView(stats);

        stop = new Button(this); stop.setText("TERMINA NAVIGAZIONE");
        navigationPanel.addView(stop, new LinearLayout.LayoutParams(-1, dp(48)));
        stop.setOnClickListener(v -> {
            stopService(new Intent(this, NavigationService.class));
            state.setText("FERMATO");
            instruction.setText("Navigazione terminata.");
            exitNavigationMode();
        });

        recenter = new Button(this);
        recenter.setText("◎");
        recenter.setTextSize(26);
        recenter.setContentDescription("Ricentra sulla posizione");
        recenter.setVisibility(View.GONE);
        recenter.setOnClickListener(v -> { if (mapController != null) mapController.recenter(); });
        FrameLayout.LayoutParams recenterLp = new FrameLayout.LayoutParams(dp(64), dp(64), Gravity.END | Gravity.BOTTOM);
        recenterLp.setMargins(0, 0, dp(18), dp(82));
        root.addView(recenter, recenterLp);

        error = title("", 14);
        error.setPadding(dp(12), dp(8), dp(12), dp(8));
        error.setBackground(card(230));
        FrameLayout.LayoutParams errorLp = new FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM);
        errorLp.setMargins(dp(12), 0, dp(92), dp(18));
        root.addView(error, errorLp);

        if (mapController == null) {
            TextView fallback = title("Mappa non disponibile. Il motore di navigazione resta attivo.", 18);
            FrameLayout.LayoutParams fallbackLp = new FrameLayout.LayoutParams(-1, -2, Gravity.CENTER);
            fallbackLp.setMargins(dp(24), 0, dp(24), 0);
            root.addView(fallback, fallbackLp);
        }
        setContentView(root);
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
                if (mapController != null) mapController.updateDestination(lat, lon);
                error.setText("Destinazione selezionata.");
            });
            searchResults.addView(b);
        }
    }

    private void loadDestinations() {
        new Thread(() -> {
            try {
                JSONObject j = ApiClient.get(this, "/device/navigator/destinations");
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
                    if (mapController != null && !destinationLats.isEmpty()) mapController.updateDestination(destinationLats.get(0), destinationLons.get(0));
                    destinations.setOnItemSelectedListener(new android.widget.AdapterView.OnItemSelectedListener() {
                        public void onItemSelected(android.widget.AdapterView<?> p, View v, int pos, long id) {
                            if (mapController != null && pos >= 0 && pos < destinationLats.size()) mapController.updateDestination(destinationLats.get(pos), destinationLons.get(pos));
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
        enterNavigationMode();
        if (mapController != null) mapController.updateLocation(loc.getLatitude(), loc.getLongitude(), true);
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

    @Override protected void onStart() { super.onStart(); if (mapController != null) mapController.onStart(); }
    @Override protected void onResume() { super.onResume(); if (mapController != null) mapController.onResume(); }
    @Override protected void onPause() { if (mapController != null) mapController.onPause(); super.onPause(); }
    @Override protected void onStop() { if (mapController != null) mapController.onStop(); super.onStop(); }
    @Override public void onLowMemory() { super.onLowMemory(); if (mapController != null) mapController.onLowMemory(); }

    @Override protected void onDestroy() {
        if (receiver != null) unregisterReceiver(receiver);
        if (mapController != null) mapController.onDestroy();
        super.onDestroy();
    }
}
