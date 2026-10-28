package com.mcdiscordbridge.velocity;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.java_websocket.client.WebSocketClient;
import org.java_websocket.handshake.ServerHandshake;
import org.slf4j.Logger;

import java.net.URI;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * Connection to the Discord bot's bridge WebSocket server, authenticated with
 * the "proxy" role so it never displaces the Paper plugin's own connection.
 * Reconnects with backoff, and re-sends the latest status after every
 * reconnect. Nothing here blocks the proxy: all callbacks run on the
 * WebSocket or scheduler threads.
 */
final class BridgeLink {

    private final ProxyConfig config;
    private final ScheduledExecutorService scheduler;
    private final Logger logger;

    private volatile Session session;
    private volatile String lastStatus;
    private volatile boolean shuttingDown;
    private volatile int reconnectDelaySeconds;

    BridgeLink(ProxyConfig config, ScheduledExecutorService scheduler, Logger logger) {
        this.config = config;
        this.scheduler = scheduler;
        this.logger = logger;
        this.reconnectDelaySeconds = config.reconnectInitialDelaySeconds();
    }

    void connect() {
        if (shuttingDown) {
            return;
        }
        try {
            Session next = new Session(new URI("ws://" + config.host() + ":" + config.port()));
            next.setConnectionLostTimeout(30);
            session = next;
            next.connect();
        } catch (Exception e) {
            logger.warn("Failed to start connection to the Discord bot: {}", e.getMessage());
            scheduleReconnect();
        }
    }

    /** Records the backend status and sends it if connected; otherwise it goes out on (re)connect. */
    void publishStatus(String status) {
        lastStatus = status;
        sendStatus();
    }

    void shutdown() {
        shuttingDown = true;
        Session current = session;
        if (current != null) {
            current.close();
        }
    }

    private void sendStatus() {
        Session current = session;
        String status = lastStatus;
        if (status == null || current == null || !current.isOpen() || !current.authenticated) {
            return;
        }
        JsonObject msg = new JsonObject();
        msg.addProperty("type", "proxy_link");
        msg.addProperty("status", status);
        msg.addProperty("backend", config.backend());
        try {
            current.send(msg.toString());
        } catch (Exception e) {
            logger.warn("Failed to send proxy_link: {}", e.getMessage());
        }
    }

    private void scheduleReconnect() {
        if (shuttingDown) {
            return;
        }
        int delay = reconnectDelaySeconds;
        reconnectDelaySeconds = Math.min(delay * 2, config.reconnectMaxDelaySeconds());
        scheduler.schedule(this::connect, delay, TimeUnit.SECONDS);
    }

    private final class Session extends WebSocketClient {
        private volatile boolean authenticated;

        Session(URI uri) {
            super(uri);
        }

        @Override
        public void onOpen(ServerHandshake handshake) {
            JsonObject auth = new JsonObject();
            auth.addProperty("type", "auth");
            auth.addProperty("token", config.secret());
            auth.addProperty("server_id", config.serverId());
            auth.addProperty("role", "proxy");
            send(auth.toString());
        }

        @Override
        public void onMessage(String message) {
            String type;
            try {
                type = JsonParser.parseString(message).getAsJsonObject().get("type").getAsString();
            } catch (Exception e) {
                return;
            }
            if ("auth_ok".equals(type)) {
                authenticated = true;
                reconnectDelaySeconds = config.reconnectInitialDelaySeconds();
                logger.info("Connected and authenticated with the Discord bot.");
                sendStatus();
            } else if ("auth_fail".equals(type)) {
                logger.error("Authentication rejected by the Discord bot - check bridge.secret and "
                        + "bridge.server-id in config.toml.");
                close();
            }
        }

        @Override
        public void onClose(int code, String reason, boolean remote) {
            authenticated = false;
            if (!shuttingDown) {
                logger.warn("Disconnected from the Discord bot, will retry.");
                scheduleReconnect();
            }
        }

        @Override
        public void onError(Exception ex) {
            logger.warn("WebSocket error: {}", ex.getMessage());
        }
    }
}
