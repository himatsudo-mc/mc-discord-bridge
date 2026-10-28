package com.mcdiscordbridge.velocity;

import com.google.inject.Inject;
import com.velocitypowered.api.event.Subscribe;
import com.velocitypowered.api.event.proxy.ProxyInitializeEvent;
import com.velocitypowered.api.event.proxy.ProxyShutdownEvent;
import com.velocitypowered.api.plugin.Plugin;
import com.velocitypowered.api.plugin.annotation.DataDirectory;
import com.velocitypowered.api.proxy.ProxyServer;
import org.slf4j.Logger;

import java.nio.file.Path;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;

@Plugin(
        id = "mcdiscordbridge-velocity",
        name = "MCDiscordBridge-Velocity",
        version = "1.0.0",
        description = "Reports the Velocity-to-backend connection status to the mc-discord-bridge Discord bot.",
        authors = {"mc-discord-bridge"}
)
public final class VelocityBridgePlugin {

    private final ProxyServer proxy;
    private final Logger logger;
    private final Path dataDirectory;

    private ScheduledExecutorService scheduler;
    private BridgeLink link;

    @Inject
    public VelocityBridgePlugin(ProxyServer proxy, Logger logger, @DataDirectory Path dataDirectory) {
        this.proxy = proxy;
        this.logger = logger;
        this.dataDirectory = dataDirectory;
    }

    @Subscribe
    public void onProxyInitialize(ProxyInitializeEvent event) {
        ProxyConfig config;
        try {
            config = ProxyConfig.load(dataDirectory);
        } catch (Exception e) {
            logger.error("Could not load config.toml; the bridge is disabled.", e);
            return;
        }
        if (config.secret().isBlank() || config.secret().equals("CHANGE_ME_SHARED_SECRET")) {
            logger.warn("bridge.secret has not been set in config.toml - set it to match the discord-bot's "
                    + "config before the bridge will authenticate.");
        }

        scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "MCDiscordBridge-Velocity");
            t.setDaemon(true);
            return t;
        });
        link = new BridgeLink(config, scheduler, logger);
        link.connect();
        new BackendMonitor(proxy, config, link, scheduler, logger).start();
        registerAccessControl(config);
    }

    private void registerAccessControl(ProxyConfig config) {
        AccessBanList bans = new AccessBanList(dataDirectory.resolve("access-bans.json"));
        try {
            bans.load();
        } catch (Exception e) {
            // Starting with an empty list would silently let banned players in, so refuse to guess.
            logger.error("Could not read access-bans.json; access control is disabled until it is fixed.", e);
            return;
        }
        AccessGuard guard = new AccessGuard(config, bans, link);
        proxy.getEventManager().register(this, guard);
        AccessBanService service = new AccessBanService(proxy, bans, guard, config, logger);
        proxy.getCommandManager().register(
                proxy.getCommandManager().metaBuilder("accessban").plugin(this).build(),
                new AccessBanCommand(proxy, service));
        link.setAccessBanHandler(service);
    }

    @Subscribe
    public void onProxyShutdown(ProxyShutdownEvent event) {
        if (link != null) {
            link.shutdown();
        }
        if (scheduler != null) {
            scheduler.shutdownNow();
        }
    }
}
