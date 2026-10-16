package com.mcdiscordbridge.bridge;

import com.mcdiscordbridge.config.BridgeConfig;
import com.mcdiscordbridge.util.Formatting;
import com.mcdiscordbridge.util.Json;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.serializer.legacy.LegacyComponentSerializer;
import org.bukkit.Bukkit;
import org.bukkit.plugin.java.JavaPlugin;

import java.net.URI;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.logging.Logger;

/**
 * Owns the single connection to the Discord bot bridge server: connecting,
 * reconnecting with backoff, and translating between bridge protocol
 * messages and in-game chat. Every method here is safe to call from any
 * thread and never blocks the caller for a meaningful amount of time,
 * except {@link #shutdown()} which bounds its wait explicitly.
 */
public final class BridgeManager {

    private final JavaPlugin plugin;
    private final BridgeConfig config;
    private final Logger logger;

    private final AtomicReference<BridgeWebSocketClient> client = new AtomicReference<>();
    private final AtomicInteger reconnectDelaySeconds;
    private volatile boolean shuttingDown = false;
    private volatile int reconnectTaskId = -1;

    /**
     * Messages that couldn't be sent immediately (not connected/authenticated,
     * or the send itself failed), held so they can be flushed once the
     * connection to the Discord bot is (re)established instead of being lost.
     * Bounded to {@link BridgeConfig#maxQueueSize()}, oldest dropped first.
     */
    private final Deque<TimestampedMessage> outboundQueue = new ArrayDeque<>();

    /** Guards the "now queueing" notice so it is logged once per disconnect, not once per message. */
    private final AtomicBoolean queueingLogged = new AtomicBoolean(false);

    /** Serializes {@link #flushQueue()} so two flushes can't send the same queue head twice. */
    private final Object flushLock = new Object();

    private record TimestampedMessage(long enqueuedAtNanos, String json) {
    }

    public BridgeManager(JavaPlugin plugin, BridgeConfig config) {
        this.plugin = plugin;
        this.config = config;
        this.logger = plugin.getLogger();
        this.reconnectDelaySeconds = new AtomicInteger(config.reconnectInitialDelaySeconds());
    }

    public void connect() {
        if (shuttingDown) {
            return;
        }
        try {
            URI uri = new URI("ws://" + config.host() + ":" + config.port());
            BridgeWebSocketClient newClient = new BridgeWebSocketClient(uri, this, config.secret(), config.serverId(), logger);
            newClient.setConnectionLostTimeout(config.connectionLostTimeoutSeconds());
            client.set(newClient);
            newClient.connect();
        } catch (Exception e) {
            logger.warning("[MCDiscordBridge] Failed to start connection to Discord bot: " + e.getMessage());
            scheduleReconnect();
        }
    }

    void onConnected() {
        reconnectDelaySeconds.set(config.reconnectInitialDelaySeconds());
        queueingLogged.set(false);
        flushQueue();
    }

    void onDisconnected() {
        if (!shuttingDown) {
            logger.warning("[MCDiscordBridge] Disconnected from Discord bot, will retry.");
            scheduleReconnect();
        }
    }

    private void scheduleReconnect() {
        if (shuttingDown || !plugin.isEnabled()) {
            return;
        }
        int delay = reconnectDelaySeconds.get();
        reconnectTaskId = Bukkit.getScheduler().runTaskLaterAsynchronously(plugin, () -> {
            if (!shuttingDown) {
                connect();
            }
        }, delay * 20L).getTaskId();
        int next = Math.min(delay * 2, config.reconnectMaxDelaySeconds());
        reconnectDelaySeconds.set(next);
    }

    /**
     * Called when the Discord bot relays a chat message. Runs on the
     * WebSocket I/O thread, so the actual broadcast is handed to the main
     * thread as Bukkit API calls are not thread-safe.
     */
    void onDiscordChat(String user, String message) {
        String formatted = Formatting.discordToMinecraft(config.discordToMinecraftFormat(), user, message);
        Component component = LegacyComponentSerializer.legacyAmpersand().deserialize(formatted);
        Bukkit.getScheduler().runTask(plugin, () -> Bukkit.broadcast(component));
    }

    /**
     * Answers a players_request from the Discord bot. Bukkit's player list
     * must be read on the main thread, so hop there first. The reply is sent
     * directly rather than queued: a stale player list is worse than none.
     */
    void onPlayersRequest(String requestId) {
        Bukkit.getScheduler().runTask(plugin, () -> {
            String names = Bukkit.getOnlinePlayers().stream()
                    .map(org.bukkit.entity.Player::getName)
                    .collect(java.util.stream.Collectors.joining(","));
            String max = String.valueOf(Bukkit.getMaxPlayers());
            BridgeWebSocketClient current = client.get();
            if (current != null && current.isOpen() && current.isAuthenticated()) {
                try {
                    current.send(Json.object("type", "players_response", "request_id", requestId,
                            "players", names, "max", max));
                } catch (Exception e) {
                    logger.warning("[MCDiscordBridge] Failed to send players_response: " + e.getMessage());
                }
            }
        });
    }

    /** Forwards an in-game chat message to Discord. Fire-and-forget. */
    public void sendChat(String player, String message) {
        sendBestEffort(Json.object("type", "chat", "player", player, "message", message));
    }

    /** Notifies Discord that this server has finished starting up. */
    public void sendServerStart() {
        sendBestEffort(Json.object("type", "server_start", "server_name", config.serverName()));
    }

    /** Notifies Discord that a player has joined the server. */
    public void sendPlayerJoin(String player) {
        sendBestEffort(Json.object("type", "player_join", "player", player));
    }

    /** Notifies Discord that a player has left the server. */
    public void sendPlayerLeave(String player) {
        sendBestEffort(Json.object("type", "player_leave", "player", player));
    }

    /**
     * Sends a message if it can go out immediately, otherwise queues it so it
     * is not lost, to be flushed once the connection to the Discord bot is
     * (re)established. A message is only sent directly when the queue is
     * empty; while a flush is still draining, new messages queue behind it so
     * they can never overtake older ones.
     */
    private void sendBestEffort(String json) {
        BridgeWebSocketClient current = client.get();
        if (current != null && current.isOpen() && current.isAuthenticated()) {
            synchronized (outboundQueue) {
                if (!outboundQueue.isEmpty()) {
                    enqueueLocked(json);
                    return;
                }
            }
            try {
                current.send(json);
                return;
            } catch (Exception e) {
                logger.warning("[MCDiscordBridge] Failed to send bridge message, queueing: " + e.getMessage());
            }
        }
        synchronized (outboundQueue) {
            enqueueLocked(json);
        }
    }

    /** Appends to the queue, evicting the oldest entry if it is full. Caller must hold outboundQueue. */
    private void enqueueLocked(String json) {
        if (outboundQueue.size() >= config.maxQueueSize()) {
            outboundQueue.pollFirst();
        }
        outboundQueue.addLast(new TimestampedMessage(System.nanoTime(), json));
        // Only log the first message queued per disconnect, so a busy server
        // with the bot offline doesn't flood the console.
        if (queueingLogged.compareAndSet(false, true)) {
            logger.info("[MCDiscordBridge] Discord bot unavailable; queueing messages (up to "
                    + config.maxQueueSize() + ") until it reconnects.");
        }
    }

    /**
     * Flushes queued messages in order, dropping any that are too stale.
     * Called on successful auth.
     *
     * <p>Each message stays at the head of the queue until it has actually
     * been sent, so the queue never looks empty while a send is still in
     * flight - that is what lets {@link #sendBestEffort(String)} decide to
     * queue behind an in-progress flush instead of overtaking it. The
     * {@code flushLock} keeps two flushes from sending the same head twice.
     */
    private void flushQueue() {
        synchronized (flushLock) {
            long maxAgeNanos = (long) (config.maxQueueAgeSeconds() * 1_000_000_000L);
            int sent = 0;
            int dropped = 0;
            for (;;) {
                TimestampedMessage next;
                synchronized (outboundQueue) {
                    next = outboundQueue.peekFirst();
                }
                if (next == null) {
                    break;
                }
                if (System.nanoTime() - next.enqueuedAtNanos() > maxAgeNanos) {
                    synchronized (outboundQueue) {
                        outboundQueue.pollFirst();
                    }
                    dropped++;
                    continue;
                }
                BridgeWebSocketClient current = client.get();
                if (current == null || !current.isOpen() || !current.isAuthenticated()) {
                    break;
                }
                try {
                    current.send(next.json());
                } catch (Exception e) {
                    logger.warning("[MCDiscordBridge] Failed to flush queued message, will retry: "
                            + e.getMessage());
                    break;
                }
                synchronized (outboundQueue) {
                    outboundQueue.pollFirst();
                }
                sent++;
            }
            if (sent > 0 || dropped > 0) {
                logger.info("[MCDiscordBridge] Flushed " + sent + " queued message(s), dropped "
                        + dropped + " stale one(s).");
            }
        }
    }

    /**
     * Sends the server_stop notice and closes the connection, bounded by
     * {@code bridge.shutdown-flush-timeout-ms} so a stuck or unreachable
     * bot never delays server shutdown.
     */
    public void shutdown() {
        shuttingDown = true;
        if (reconnectTaskId != -1) {
            Bukkit.getScheduler().cancelTask(reconnectTaskId);
        }
        BridgeWebSocketClient current = client.get();
        if (current == null) {
            return;
        }
        Thread flushThread = new Thread(() -> {
            try {
                if (current.isOpen() && current.isAuthenticated()) {
                    current.send(Json.object("type", "server_stop", "server_name", config.serverName()));
                }
                current.closeBlocking();
            } catch (Exception e) {
                logger.fine("[MCDiscordBridge] Error while closing bridge connection: " + e.getMessage());
            }
        }, "MCDiscordBridge-Shutdown");
        flushThread.setDaemon(true);
        flushThread.start();
        try {
            flushThread.join(config.shutdownFlushTimeoutMs());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
