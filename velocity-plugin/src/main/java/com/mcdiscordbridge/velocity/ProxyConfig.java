package com.mcdiscordbridge.velocity;

import com.moandjiezana.toml.Toml;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Typed view over config.toml, resolved once at startup.
 */
record ProxyConfig(String host, int port, String secret, String serverId,
                   int reconnectInitialDelaySeconds, int reconnectMaxDelaySeconds,
                   String backend, int intervalSeconds, int timeoutSeconds,
                   boolean securityEnabled, int rateLimitAttempts, int rateLimitWindowSeconds,
                   int alertCooldownSeconds, String namePattern, String banMessage) {

    static ProxyConfig load(Path dataDirectory) throws IOException {
        Files.createDirectories(dataDirectory);
        Path file = dataDirectory.resolve("config.toml");
        if (Files.notExists(file)) {
            try (InputStream in = ProxyConfig.class.getResourceAsStream("/config.toml")) {
                if (in == null) {
                    throw new IOException("Bundled config.toml is missing");
                }
                Files.copy(in, file);
            }
        }
        Toml toml = new Toml().read(file.toFile());
        return new ProxyConfig(
                toml.getString("bridge.host", "127.0.0.1"),
                toml.getLong("bridge.port", 8765L).intValue(),
                toml.getString("bridge.secret", ""),
                toml.getString("bridge.server-id", "default"),
                toml.getLong("bridge.reconnect.initial-delay", 5L).intValue(),
                toml.getLong("bridge.reconnect.max-delay", 60L).intValue(),
                toml.getString("check.backend", "home"),
                Math.max(1, toml.getLong("check.interval-seconds", 5L).intValue()),
                Math.max(1, toml.getLong("check.timeout-seconds", 5L).intValue()),
                toml.getBoolean("security.enabled", true),
                Math.max(2, toml.getLong("security.rate-limit.attempts", 10L).intValue()),
                Math.max(1, toml.getLong("security.rate-limit.window-seconds", 60L).intValue()),
                Math.max(0, toml.getLong("security.alert-cooldown-seconds", 300L).intValue()),
                toml.getString("security.name-pattern", "^[.*+A-Za-z0-9_]{1,17}$"),
                toml.getString("security.ban-message", "このサーバーへのアクセスは禁止されています。"));
    }
}
