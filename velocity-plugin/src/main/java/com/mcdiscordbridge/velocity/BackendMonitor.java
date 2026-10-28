package com.mcdiscordbridge.velocity;

import com.velocitypowered.api.proxy.ProxyServer;
import com.velocitypowered.api.proxy.server.RegisteredServer;
import org.slf4j.Logger;

import java.util.Optional;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Periodically pings the configured backend through Velocity itself, so a
 * success means Velocity can really reach and talk to the Minecraft server.
 * Reports every result to {@link BridgeLink}; the bot only posts on changes.
 */
final class BackendMonitor {

    private final ProxyServer proxy;
    private final ProxyConfig config;
    private final BridgeLink link;
    private final ScheduledExecutorService scheduler;
    private final Logger logger;

    private final AtomicBoolean inFlight = new AtomicBoolean(false);
    private volatile Boolean lastUp;
    private boolean warnedMissing;

    BackendMonitor(ProxyServer proxy, ProxyConfig config, BridgeLink link,
                   ScheduledExecutorService scheduler, Logger logger) {
        this.proxy = proxy;
        this.config = config;
        this.link = link;
        this.scheduler = scheduler;
        this.logger = logger;
    }

    void start() {
        scheduler.scheduleWithFixedDelay(this::check, 0, config.intervalSeconds(), TimeUnit.SECONDS);
    }

    private void check() {
        Optional<RegisteredServer> server = proxy.getServer(config.backend());
        if (server.isEmpty()) {
            if (!warnedMissing) {
                warnedMissing = true;
                logger.warn("check.backend \"{}\" is not defined under [servers] in velocity.toml.", config.backend());
            }
            return;
        }
        if (!inFlight.compareAndSet(false, true)) {
            return;
        }
        try {
            server.get().ping()
                    .orTimeout(config.timeoutSeconds(), TimeUnit.SECONDS)
                    .whenComplete((ping, error) -> {
                        try {
                            report(error == null);
                        } finally {
                            inFlight.set(false);
                        }
                    });
        } catch (RuntimeException e) {
            inFlight.set(false);
            logger.warn("Backend check failed to start: {}", e.getMessage());
        }
    }

    private void report(boolean up) {
        if (lastUp == null || lastUp != up) {
            logger.info("Backend \"{}\" is {}.", config.backend(), up ? "reachable" : "unreachable");
            lastUp = up;
        }
        link.publishStatus(up ? "up" : "down");
    }
}
