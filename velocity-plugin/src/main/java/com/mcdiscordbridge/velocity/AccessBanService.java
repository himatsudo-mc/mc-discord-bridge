package com.mcdiscordbridge.velocity;

import com.velocitypowered.api.proxy.Player;
import com.velocitypowered.api.proxy.ProxyServer;
import net.kyori.adventure.text.Component;
import org.slf4j.Logger;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * The ban operations shared by the in-game/console command and the Discord command,
 * so both behave (and audit-log) identically.
 */
final class AccessBanService {

    record Result(boolean ok, String message) {}

    private static final Pattern NAME = Pattern.compile("^[.*+A-Za-z0-9_]{1,32}$");

    private final ProxyServer proxy;
    private final AccessBanList bans;
    private final AccessGuard guard;
    private final ProxyConfig config;
    private final Logger logger;

    AccessBanService(ProxyServer proxy, AccessBanList bans, AccessGuard guard, ProxyConfig config, Logger logger) {
        this.proxy = proxy;
        this.bans = bans;
        this.guard = guard;
        this.config = config;
        this.logger = logger;
    }

    AccessBanList bans() {
        return bans;
    }

    Result add(String rawTarget, String reason, String by) {
        String target = normalize(rawTarget);
        if (target == null) {
            return new Result(false, "IPアドレスまたはプレイヤー名の形式が正しくありません: " + rawTarget);
        }
        try {
            if (!bans.add(target, reason, by)) {
                return new Result(false, target + " は既にアク禁です。");
            }
        } catch (Exception e) {
            logger.error("Failed to save access-bans.json", e);
            return new Result(false, "アク禁リストの保存に失敗しました。ログを確認してください。");
        }
        int kicked = kickMatching(target);
        guard.auditBanChange("ban_added", target,
                by + " がアク禁にしました" + (reason.isBlank() ? "" : "（理由: " + reason + "）")
                        + (kicked > 0 ? "。接続中の" + kicked + "人を切断しました。" : "。"));
        return new Result(true, target + " をアク禁にしました" + (kicked > 0 ? "（接続中の" + kicked + "人を切断）" : "") + "。");
    }

    Result remove(String rawTarget, String by) {
        String target = normalize(rawTarget);
        if (target == null) {
            return new Result(false, "IPアドレスまたはプレイヤー名の形式が正しくありません: " + rawTarget);
        }
        try {
            if (!bans.remove(target)) {
                return new Result(false, target + " はアク禁リストにありません。");
            }
        } catch (Exception e) {
            logger.error("Failed to save access-bans.json", e);
            return new Result(false, "アク禁リストの保存に失敗しました。ログを確認してください。");
        }
        guard.auditBanChange("ban_removed", target, by + " がアク禁を解除しました。");
        return new Result(true, target + " のアク禁を解除しました。");
    }

    /** One line per entry, preceded by a summary line; a single line if the list is empty. */
    List<String> list() {
        Map<String, AccessBanList.Entry> ips = bans.ips();
        Map<String, AccessBanList.Entry> names = bans.names();
        List<String> lines = new ArrayList<>();
        if (ips.isEmpty() && names.isEmpty()) {
            lines.add("アク禁リストは空です。");
            return lines;
        }
        lines.add("アク禁リスト（IP " + ips.size() + "件 / 名前 " + names.size() + "件）");
        ips.forEach((k, v) -> lines.add("[IP] " + k + describe(v)));
        names.forEach((k, v) -> lines.add("[名前] " + k + describe(v)));
        return lines;
    }

    private static String describe(AccessBanList.Entry e) {
        StringBuilder sb = new StringBuilder();
        if (!e.reason().isBlank()) {
            sb.append(" 理由: ").append(e.reason());
        }
        if (!e.by().isBlank()) {
            sb.append(" 実行者: ").append(e.by());
        }
        return sb.toString();
    }

    private static String normalize(String raw) {
        if (AccessBanList.isIp(raw)) {
            return AccessBanList.normalizeIp(raw);
        }
        return NAME.matcher(raw).matches() ? raw : null;
    }

    private int kickMatching(String target) {
        boolean ip = AccessBanList.isIp(target);
        int kicked = 0;
        for (Player p : proxy.getAllPlayers()) {
            boolean match = ip
                    ? p.getRemoteAddress().getAddress() != null
                            && p.getRemoteAddress().getAddress().getHostAddress().equals(target)
                    : p.getUsername().equalsIgnoreCase(target);
            if (match) {
                p.disconnect(Component.text(config.banMessage()));
                kicked++;
            }
        }
        return kicked;
    }
}
