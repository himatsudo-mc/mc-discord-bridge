package com.mcdiscordbridge.velocity;

import com.velocitypowered.api.event.PostOrder;
import com.velocitypowered.api.event.ResultedEvent;
import com.velocitypowered.api.event.Subscribe;
import com.velocitypowered.api.event.connection.PreLoginEvent;
import net.kyori.adventure.text.Component;

import java.net.InetSocketAddress;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.HashMap;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * Rejects banned IPs/names at the earliest point of login and reports suspicious
 * activity to Discord. Detection never bans on its own: a false positive would
 * lock out a real player, so banning stays a manual {@code /accessban} action.
 */
final class AccessGuard {

    private final ProxyConfig config;
    private final AccessBanList bans;
    private final BridgeLink link;
    private final Pattern namePattern;

    private final Map<String, Deque<Long>> attempts = new HashMap<>();
    private final Map<String, Long> lastAlertAt = new HashMap<>();
    private long lastSweepAt;

    AccessGuard(ProxyConfig config, AccessBanList bans, BridgeLink link) {
        this.config = config;
        this.bans = bans;
        this.link = link;
        this.namePattern = Pattern.compile(config.namePattern());
    }

    @Subscribe(order = PostOrder.FIRST)
    public void onPreLogin(PreLoginEvent event) {
        String ip = ipOf(event.getConnection().getRemoteAddress());
        String name = event.getUsername();

        AccessBanList.Entry ban = bans.findIp(ip);
        String what = "IP";
        if (ban == null) {
            ban = bans.findName(name);
            what = "プレイヤー名";
        }
        if (ban != null) {
            event.setResult(PreLoginEvent.PreLoginComponentResult.denied(Component.text(config.banMessage())));
            String detail = what + "がアク禁リストに登録されています"
                    + (ban.reason().isBlank() ? "" : "（理由: " + ban.reason() + "）") + "。接続を拒否しました。";
            alert("banned_attempt", ip, name, detail, 0);
            return;
        }

        if (!config.securityEnabled()) {
            return;
        }
        int count = recordAttempt(ip);
        if (count >= config.rateLimitAttempts()) {
            alert("rapid_connections", ip, name,
                    config.rateLimitWindowSeconds() + "秒以内に" + count + "回の接続試行がありました。"
                            + "問題があれば /accessban add " + ip + " でアク禁にできます。", count);
        }
        if (name == null || !namePattern.matcher(name).matches()) {
            alert("invalid_name", ip, name == null ? "" : name,
                    "プレイヤー名が許可パターン（" + config.namePattern() + "）に一致しません。", 0);
        }
    }

    /** Reports a ban-list change made with the command (audit log). */
    void auditBanChange(String kind, String target, String detail) {
        boolean ip = AccessBanList.isIp(target);
        link.sendSecurityAlert(kind, ip ? target : "", ip ? "" : target, detail, 0);
    }

    private synchronized int recordAttempt(String ip) {
        long now = System.currentTimeMillis();
        long windowMs = config.rateLimitWindowSeconds() * 1000L;
        if (now - lastSweepAt > windowMs) {
            lastSweepAt = now;
            attempts.values().removeIf(q -> q.isEmpty() || now - q.peekLast() > windowMs);
            lastAlertAt.values().removeIf(t -> now - t > config.alertCooldownSeconds() * 1000L);
        }
        Deque<Long> q = attempts.computeIfAbsent(ip, k -> new ArrayDeque<>());
        q.addLast(now);
        while (!q.isEmpty() && now - q.peekFirst() > windowMs) {
            q.pollFirst();
        }
        return q.size();
    }

    /** Sends an alert unless the same kind for the same IP was already reported within the cooldown. */
    private synchronized void alert(String kind, String ip, String name, String detail, int count) {
        long now = System.currentTimeMillis();
        String key = kind + "|" + ip;
        Long last = lastAlertAt.get(key);
        if (last != null && now - last < config.alertCooldownSeconds() * 1000L) {
            return;
        }
        lastAlertAt.put(key, now);
        link.sendSecurityAlert(kind, ip, name == null ? "" : name, detail, count);
    }

    private static String ipOf(InetSocketAddress address) {
        if (address == null || address.getAddress() == null) {
            return address == null ? "" : address.getHostString();
        }
        return address.getAddress().getHostAddress();
    }
}
