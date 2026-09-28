package it.tiremminnanz.navigatore;

import android.content.Context;
import android.graphics.Color;
import android.os.Bundle;
import android.view.View;
import org.json.JSONObject;
import org.maplibre.android.MapLibre;
import org.maplibre.android.camera.CameraPosition;
import org.maplibre.android.geometry.LatLng;
import org.maplibre.android.maps.MapView;
import org.maplibre.android.maps.MapLibreMap;
import org.maplibre.android.maps.Style;
import org.maplibre.android.style.layers.CircleLayer;
import org.maplibre.android.style.sources.GeoJsonSource;
import org.maplibre.geojson.Feature;
import org.maplibre.geojson.Point;

import static org.maplibre.android.style.layers.PropertyFactory.circleColor;
import static org.maplibre.android.style.layers.PropertyFactory.circleRadius;
import static org.maplibre.android.style.layers.PropertyFactory.circleStrokeColor;
import static org.maplibre.android.style.layers.PropertyFactory.circleStrokeWidth;

public final class MapController {
    private static final String STYLE_JSON = "{\"version\":8,\"sources\":{\"osm\":{\"type\":\"raster\",\"tiles\":[\"https://tile.openstreetmap.org/{z}/{x}/{y}.png\"],\"tileSize\":256,\"attribution\":\"© OpenStreetMap contributors\"}},\"layers\":[{\"id\":\"osm\",\"type\":\"raster\",\"source\":\"osm\"}]}";

    private final MapView mapView;
    private MapLibreMap map;
    private Style style;
    public MapController(Context context, Bundle savedInstanceState) {
        MapLibre.getInstance(context);
        mapView = new MapView(context);
        mapView.onCreate(savedInstanceState);
        mapView.getMapAsync(m -> {
            map = m;
            map.setStyle(new Style.Builder().fromJson(STYLE_JSON), s -> {
                style = s;
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
        setPoint("user", lat, lon);
        if (recenter && map != null) {
            map.setCameraPosition(new CameraPosition.Builder()
                .target(new LatLng(lat, lon)).zoom(15.5).build());
        }
    }

    public void updateDestination(double lat, double lon) {
        setPoint("destination", lat, lon);
    }

    public void updatePlan(JSONObject navigation) {
        JSONObject plan = navigation.optJSONObject("plan");
        if (plan == null) return;
        JSONObject stop = plan.optJSONObject("nearest_origin_stop");
        if (stop != null && stop.has("lat") && stop.has("lon")) {
            setPoint("stop", stop.optDouble("lat"), stop.optDouble("lon"));
        }
    }

    public void onStart() { mapView.onStart(); }
    public void onResume() { mapView.onResume(); }
    public void onPause() { mapView.onPause(); }
    public void onStop() { mapView.onStop(); }
    public void onDestroy() { mapView.onDestroy(); }
    public void onLowMemory() { mapView.onLowMemory(); }
}
