package it.tiremminnanz.navigatore;

import org.json.JSONObject;
import org.json.JSONArray;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

public final class ApiClient {
    public static final String DEFAULT_BASE_URL = "http://10.252.14.7:19126";
    private ApiClient() {}

    public static JSONObject get(String base, String path) throws Exception {
        HttpURLConnection c = open(base + path, "GET");
        return readJson(c);
    }

    public static JSONObject post(String base, String path, JSONObject body) throws Exception {
        HttpURLConnection c = open(base + path, "POST");
        c.setDoOutput(true);
        byte[] data = body.toString().getBytes(StandardCharsets.UTF_8);
        c.setRequestProperty("Content-Type", "application/json");
        c.setFixedLengthStreamingMode(data.length);
        try (OutputStream out = c.getOutputStream()) { out.write(data); }
        return readJson(c);
    }

    private static HttpURLConnection open(String url, String method) throws Exception {
        HttpURLConnection c = (HttpURLConnection)new URL(url).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(5000);
        c.setReadTimeout(25000);
        c.setRequestProperty("User-Agent", "Tiremm-Navigatore/0.1");
        return c;
    }

    private static JSONObject readJson(HttpURLConnection c) throws Exception {
        int code = c.getResponseCode();
        InputStream in = code >= 200 && code < 300 ? c.getInputStream() : c.getErrorStream();
        String text;
        try (BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            StringBuilder b = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null) b.append(line);
            text = b.toString();
        }
        if (code < 200 || code >= 300) throw new IOException("HTTP " + code + ": " + text);
        return new JSONObject(text);
    }
}
