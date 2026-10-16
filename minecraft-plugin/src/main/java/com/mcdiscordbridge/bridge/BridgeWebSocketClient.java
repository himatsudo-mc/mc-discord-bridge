package com.mcdiscordbridge.bridge;

import com.mcdiscordbridge.util.Json;
import org.java_websocket.client.WebSocketClient;
import org.java_websocket.handshake.ServerHandshake;

import java.net.URI;
import java.util.Map;
import java.util.logging.Logger;

/**
 * A single WebSocket connection to the Discord bot bridge server.
 * All lifecycle callbacks run on Java-WebSocket's own I/O thread, never on
 * the server main thread, so a slow or unreachable bot cannot stall the
 * Minecraft server.
 */
final class BridgeWebSocketClient extends WebSocketClient {

    private final BridgeManager manager;
    private final String secret;
    private final String serverId;
    private final Logger logger;
    private volatile boolean authenticated = false;

    BridgeWebSocketClient(URI serverUri, BridgeManager manager, String secret, String serverId, Logger logger) {
        super(serverUri);
        this.manager = manager;
        this.secret = secret;
        this.serverId = serverId;
        this.logger = logger;
    }

    @Override
    public void onOpen(ServerHandshake handshakeData) {
        authenticated = false;
        send(Json.object("type", "auth", "token", secret, "server_id", serverId));
    }

    @Override
    public void onMessage(String message) {
        Map<String, String> fields = Json.parseFlatObject(message);
        String type = fields.getOrDefault("type", "");
        switch (type) {
            case "auth_ok" -> {
                authenticated = true;
                logger.info("[MCDiscordBridge] Connected and authenticated with the Discord bot.");
                manager.onConnected();
            }
            case "auth_fail" -> {
                authenticated = false;
                logger.severe("[MCDiscordBridge] Authentication rejected by the Discord bot - check that "
                        + "bridge.secret matches on both sides.");
                close();
            }
            case "chat" -> {
                if (authenticated) {
                    manager.onDiscordChat(fields.getOrDefault("user", "Discord"), fields.getOrDefault("message", ""));
                }
            }
            case "players_request" -> {
                if (authenticated) {
                    manager.onPlayersRequest(fields.getOrDefault("request_id", ""));
                }
            }
            default -> {
                // Unknown/ignored message type.
            }
        }
    }

    @Override
    public void onClose(int code, String reason, boolean remote) {
        authenticated = false;
        manager.onDisconnected();
    }

    @Override
    public void onError(Exception ex) {
        logger.warning("[MCDiscordBridge] WebSocket error: " + ex.getMessage());
    }

    boolean isAuthenticated() {
        return authenticated;
    }
}
