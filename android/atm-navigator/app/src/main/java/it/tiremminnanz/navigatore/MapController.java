package it.tiremminnanz.navigatore;

import android.content.Context;
import android.graphics.Color;
import android.os.Bundle;
import android.view.View;
import org.json.JSONArray;
import org.json.JSONObject;
import org.maplibre.android.MapLibre;
import org.maplibre.android.camera.CameraPosition;
import org.maplibre.android.geometry.LatLng;
import org.maplibre.android.maps.MapView;
import org.maplibre.android.maps.MapLibreMap;
import org.maplibre.android.maps.Style;
import org.maplibre.android.style.layers.CircleLayer;
import org.maplibre.android.style.layers.LineLayer;
import org.maplibre.android.style.sources.GeoJsonSource;
import org.maplibre.geojson.Feature;
import org.maplibre.geojson.LineString;
import org.maplibre.geojson.Point;
import java.util.ArrayList;
import java.util.List;

import static org.maplibre.android.style.layers.PropertyFactory.circleColor;
import static org.maplibre.android.style.layers.PropertyFactory.circleRadius;
import static org.maplibre.android.style.layers.PropertyFactory.circleStrokeColor;
import static org.maplibre.android.style.layers.PropertyFactory.circleStrokeWidth;
import static org.maplibre.android.style.layers.PropertyFactory.lineColor;
import static org.maplibre.android.style.layers.PropertyFactory.lineOpacity;
import static org.maplibre.android.style.layers.PropertyFactory.lineWidth;

public final class MapController {
    private static final String STYLE_JSON = "{\"version\":8,\"sources\":{\"osm\":{\"type\":\"raster\",\"tiles\":[\"https://tile.openstreetmap.org/{z}/{x}/{y}.png\"],\"tileSize\":256,\"attribution\":\"© OpenStreetMap contributors\"}},\"layers\":[{\"id\":\"osm\",\"type\":\"raster\",\"source\":\"osm\"}]}";

    private final MapView mapView;
    private MapLibreMap map;
    private Style style;
    private boolean followMode = false;
    private double lastLat = Double.NaN, lastLon = Double.NaN;
    private float lastBearing = -1f;
    public MapController(Context context, Bundle savedInstanceState) {
        MapLibre.getInstance(context);
        mapView = new MapView(context);
        mapView.onCreate(savedInstanceState);
        mapView.getMapAsync(m -> {
            map = m;
            map.setStyle(new Style.Builder().fromJson(STYLE_JSON), s -> {
                style = s;
                GeoJsonSource routeSource = new GeoJsonSource("route-source");
                style.addSource(routeSource);
                style.addLayer(new LineLayer("route-layer", "route-source").withProperties(
                    lineColor(Color.rgb(25, 118, 210)), lineWidth(7f), lineOpacity(0.88f)));
                addPointLayer("user", Color.rgb(0, 102, 204), 9f);
                addPointLayer("stop", Color.rgb(255, 153, 0), 8f);
                addPointLayer("destination", Color.rgb(0, 140, 70), 10f);
            });
        });
    }

    public View view() { return mapView; }

    private void addPointLayer(String id, int color, float radius) {
        GeoJsonSource source = new GeoJsonSource(id + "-source");
        style.addSource(source);
        CircleLayer layer = new CircleLayer(id + "-layer", id + "-source")
            .withProperties(
                circleColor(color), circleRadius(radius),
                circleStrokeColor(Color.WHITE), circleStrokeWidth(2f));
        style.addLayer(layer);
    }

    private void setPoint(String id, double lat, double lon) {
        if (style == null) return;
        GeoJsonSource src = style.getSourceAs(id + "-source");
        if (src != null) src.setGeoJson(Feature.fromGeometry(Point.fromLngLat(lon, lat)));
    }
    public void updateLocation(double lat, double lon, boolean recenter) {
        updateLocation(lat, lon, recenter, -1f);
    }

    public void updateLocation(double lat, double lon, boolean recenter, float bearing) {
        lastLat = lat;
        lastLon = lon;
        if (bearing >= 0f) lastBearing = bearing;
        setPoint("user", lat, lon);
        if ((recenter || followMode) && map != null) applyCamera();
    }

    private void applyCamera() {
        if (map == null || Double.isNaN(lastLat) || Double.isNaN(lastLon)) return;
        CameraPosition.Builder camera = new CameraPosition.Builder()
            .target(new LatLng(lastLat, lastLon))
            .zoom(followMode ? 17.2 : 15.5)
            .tilt(followMode ? 45.0 : 0.0);
        if (lastBearing >= 0f) camera.bearing(lastBearing);
        map.setCameraPosition(camera.build());
    }

    public void setFollowMode(boolean enabled) {
        followMode = enabled;
        if (enabled) applyCamera();
    }

    public void recenter() {
        followMode = true;
        applyCamera();
    }

    public void updateDestination(double lat, double lon) {
        setPoint("destination", lat, lon);
    }

    private void addPoint(List<Point> points, double lat, double lon) {
        if (Double.isNaN(lat) || Double.isNaN(lon)) return;
        if (!points.isEmpty()) {
            Point previous = points.get(points.size() - 1);
            if (Math.abs(previous.latitude() - lat) < 0.000001 && Math.abs(previous.longitude() - lon) < 0.000001) return;
        }
        points.add(Point.fromLngLat(lon, lat));
    }

    public void updatePlan(JSONObject navigation) {
        JSONObject plan = navigation.optJSONObject("plan");
        if (plan == null) return;
        List<Point> routePoints = new ArrayList<>();
        addPoint(routePoints, lastLat, lastLon);

        JSONObject stop = plan.optJSONObject("nearest_origin_stop");
        if (stop != null && stop.has("lat") && stop.has("lon")) {
            double lat = stop.optDouble("lat", Double.NaN);
            double lon = stop.optDouble("lon", Double.NaN);
            setPoint("stop", lat, lon);
            addPoint(routePoints, lat, lon);
        }

        JSONObject route = plan.optJSONObject("local_atm_route");
        JSONArray legs = route == null ? null : route.optJSONArray("legs");
        if (legs != null) for (int i = 0; i < legs.length(); i++) {
            JSONObject leg = legs.optJSONObject(i);
            if (leg == null) continue;
            addPoint(routePoints, leg.optDouble("from_lat", Double.NaN), leg.optDouble("from_lon", Double.NaN));
            addPoint(routePoints, leg.optDouble("to_lat", Double.NaN), leg.optDouble("to_lon", Double.NaN));
        }

        JSONObject destination = plan.optJSONObject("destination");
        if (destination != null) {
            double lat = destination.optDouble("lat", Double.NaN);
            double lon = destination.optDouble("lon", Double.NaN);
            setPoint("destination", lat, lon);
            addPoint(routePoints, lat, lon);
        }

        if (style != null && routePoints.size() >= 2) {
            GeoJsonSource src = style.getSourceAs("route-source");
            if (src != null) src.setGeoJson(Feature.fromGeometry(LineString.fromLngLats(routePoints)));
        }
    }

    public void onStart() { mapView.onStart(); }
    public void onResume() { mapView.onResume(); }
    public void onPause() { mapView.onPause(); }
    public void onStop() { mapView.onStop(); }
    public void onDestroy() { mapView.onDestroy(); }
    public void onLowMemory() { mapView.onLowMemory(); }
}
