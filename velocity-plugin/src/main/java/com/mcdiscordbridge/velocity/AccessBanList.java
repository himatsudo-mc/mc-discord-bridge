package com.mcdiscordbridge.velocity;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.IOException;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Pattern;

/**
 * Manually maintained list of banned IP addresses and player names, persisted to
 * access-bans.json. Names are matched case-insensitively. Thread-safe.
 */
final class AccessBanList {

    record Entry(String reason, String by, String at) {}

    private static final Pattern IPV4 = Pattern.compile("^\\d{1,3}(\\.\\d{1,3}){3}$");
    private static final Gson GSON = new GsonBuilder().setPrettyPrinting().create();

    private final Path file;
    private final Map<String, Entry> ips = new TreeMap<>();
    private final Map<String, Entry> names = new TreeMap<>();

    AccessBanList(Path file) {
        this.file = file;
    }

    /** True if the target is an IP literal (IPv4, or anything containing ':' for IPv6). */
    static boolean isIp(String target) {
        return IPV4.matcher(target).matches() || target.contains(":");
    }

    /** Canonical form of an IP literal, or null if it is not one. Never performs a DNS lookup. */
    static String normalizeIp(String target) {
        if (!isIp(target)) {
            return null;
        }
        try {
            return InetAddress.getByName(target).getHostAddress();
        } catch (Exception e) {
            return null;
        }
    }

    synchronized void load() throws IOException {
        ips.clear();
        names.clear();
        if (Files.notExists(file)) {
            return;
        }
        JsonObject root = JsonParser.parseString(Files.readString(file, StandardCharsets.UTF_8)).getAsJsonObject();
        read(root, "ips", ips);
        read(root, "names", names);
    }

    private static void read(JsonObject root, String key, Map<String, Entry> into) {
        if (!root.has(key)) {
            return;
        }
        for (Map.Entry<String, com.google.gson.JsonElement> e : root.getAsJsonObject(key).entrySet()) {
            JsonObject o = e.getValue().getAsJsonObject();
            into.put(e.getKey(), new Entry(str(o, "reason"), str(o, "by"), str(o, "at")));
        }
    }

    private static String str(JsonObject o, String key) {
        return o.has(key) && !o.get(key).isJsonNull() ? o.get(key).getAsString() : "";
    }

    private void save() throws IOException {
        JsonObject root = new JsonObject();
        root.add("ips", GSON.toJsonTree(ips));
        root.add("names", GSON.toJsonTree(names));
        Path tmp = file.resolveSibling(file.getFileName() + ".tmp");
        Files.writeString(tmp, GSON.toJson(root), StandardCharsets.UTF_8);
        Files.move(tmp, file, StandardCopyOption.REPLACE_EXISTING);
    }

    /** Adds a ban. {@code target} must already be normalized. Returns false if it was already banned. */
    synchronized boolean add(String target, String reason, String by) throws IOException {
        Map<String, Entry> map = isIp(target) ? ips : names;
        String key = isIp(target) ? target : target.toLowerCase(Locale.ROOT);
        if (map.containsKey(key)) {
            return false;
        }
        map.put(key, new Entry(reason, by, Instant.now().toString()));
        save();
        return true;
    }

    /** Removes a ban. Returns false if it was not banned. */
    synchronized boolean remove(String target) throws IOException {
        Map<String, Entry> map = isIp(target) ? ips : names;
        String key = isIp(target) ? target : target.toLowerCase(Locale.ROOT);
        if (map.remove(key) == null) {
            return false;
        }
        save();
        return true;
    }

    synchronized Entry findIp(String ip) {
        return ip == null ? null : ips.get(ip);
    }

    synchronized Entry findName(String name) {
        return name == null ? null : names.get(name.toLowerCase(Locale.ROOT));
    }

    synchronized Map<String, Entry> ips() {
        return new TreeMap<>(ips);
    }

    synchronized Map<String, Entry> names() {
        return new TreeMap<>(names);
    }
}
