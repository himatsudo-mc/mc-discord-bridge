package com.mcdiscordbridge.velocity;

import com.velocitypowered.api.command.CommandSource;
import com.velocitypowered.api.command.SimpleCommand;
import com.velocitypowered.api.proxy.Player;
import com.velocitypowered.api.proxy.ProxyServer;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.slf4j.Logger;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * /accessban add|remove|list - manual access bans by IP address or player name.
 * Requires the permission {@code mcdiscordbridge.accessban} (the console always has it).
 */
final class AccessBanCommand implements SimpleCommand {

    static final String PERMISSION = "mcdiscordbridge.accessban";
    private static final Pattern NAME = Pattern.compile("^[.*+A-Za-z0-9_]{1,32}$");
    private static final List<String> SUBCOMMANDS = List.of("add", "remove", "list");

    private final ProxyServer proxy;
    private final AccessBanList bans;
    private final AccessGuard guard;
    private final ProxyConfig config;
    private final Logger logger;

    AccessBanCommand(ProxyServer proxy, AccessBanList bans, AccessGuard guard, ProxyConfig config, Logger logger) {
        this.proxy = proxy;
        this.bans = bans;
        this.guard = guard;
        this.config = config;
        this.logger = logger;
    }

    @Override
    public boolean hasPermission(Invocation invocation) {
        return invocation.source().hasPermission(PERMISSION);
    }

    @Override
    public void execute(Invocation invocation) {
        CommandSource source = invocation.source();
        String[] args = invocation.arguments();
        String sub = args.length == 0 ? "" : args[0].toLowerCase(Locale.ROOT);
        switch (sub) {
            case "add" -> add(source, args);
            case "remove" -> remove(source, args);
            case "list" -> list(source);
            default -> {
                reply(source, "使い方:", NamedTextColor.YELLOW);
                reply(source, "/accessban add <IPアドレス|プレイヤー名> [理由]", NamedTextColor.GRAY);
                reply(source, "/accessban remove <IPアドレス|プレイヤー名>", NamedTextColor.GRAY);
                reply(source, "/accessban list", NamedTextColor.GRAY);
            }
        }
    }

    private void add(CommandSource source, String[] args) {
        if (args.length < 2) {
            reply(source, "使い方: /accessban add <IPアドレス|プレイヤー名> [理由]", NamedTextColor.YELLOW);
            return;
        }
        String target = normalize(args[1]);
        if (target == null) {
            reply(source, "IPアドレスまたはプレイヤー名の形式が正しくありません: " + args[1], NamedTextColor.RED);
            return;
        }
        String reason = String.join(" ", Arrays.copyOfRange(args, 2, args.length));
        String by = actor(source);
        try {
            if (!bans.add(target, reason, by)) {
                reply(source, target + " は既にアク禁です。", NamedTextColor.YELLOW);
                return;
            }
        } catch (Exception e) {
            logger.error("Failed to save access-bans.json", e);
            reply(source, "アク禁リストの保存に失敗しました。ログを確認してください。", NamedTextColor.RED);
            return;
        }
        int kicked = kickMatching(target);
        reply(source, target + " をアク禁にしました" + (kicked > 0 ? "（接続中の" + kicked + "人を切断）" : "") + "。",
                NamedTextColor.GREEN);
        guard.auditBanChange("ban_added", target,
                by + " がアク禁にしました" + (reason.isBlank() ? "" : "（理由: " + reason + "）")
                        + (kicked > 0 ? "。接続中の" + kicked + "人を切断しました。" : "。"));
    }

    private void remove(CommandSource source, String[] args) {
        if (args.length < 2) {
            reply(source, "使い方: /accessban remove <IPアドレス|プレイヤー名>", NamedTextColor.YELLOW);
            return;
        }
        String target = normalize(args[1]);
        if (target == null) {
            reply(source, "IPアドレスまたはプレイヤー名の形式が正しくありません: " + args[1], NamedTextColor.RED);
            return;
        }
        try {
            if (!bans.remove(target)) {
                reply(source, target + " はアク禁リストにありません。", NamedTextColor.YELLOW);
                return;
            }
        } catch (Exception e) {
            logger.error("Failed to save access-bans.json", e);
            reply(source, "アク禁リストの保存に失敗しました。ログを確認してください。", NamedTextColor.RED);
            return;
        }
        reply(source, target + " のアク禁を解除しました。", NamedTextColor.GREEN);
        guard.auditBanChange("ban_removed", target, actor(source) + " がアク禁を解除しました。");
    }

    private void list(CommandSource source) {
        Map<String, AccessBanList.Entry> ips = bans.ips();
        Map<String, AccessBanList.Entry> names = bans.names();
        if (ips.isEmpty() && names.isEmpty()) {
            reply(source, "アク禁リストは空です。", NamedTextColor.GRAY);
            return;
        }
        reply(source, "アク禁リスト（IP " + ips.size() + "件 / 名前 " + names.size() + "件）", NamedTextColor.YELLOW);
        ips.forEach((k, v) -> reply(source, "[IP] " + k + describe(v), NamedTextColor.GRAY));
        names.forEach((k, v) -> reply(source, "[名前] " + k + describe(v), NamedTextColor.GRAY));
    }

    @Override
    public List<String> suggest(Invocation invocation) {
        String[] args = invocation.arguments();
        List<String> out = new ArrayList<>();
        if (args.length <= 1) {
            String prefix = args.length == 0 ? "" : args[0].toLowerCase(Locale.ROOT);
            SUBCOMMANDS.stream().filter(s -> s.startsWith(prefix)).forEach(out::add);
        } else if (args.length == 2) {
            String prefix = args[1].toLowerCase(Locale.ROOT);
            if (args[0].equalsIgnoreCase("add")) {
                proxy.getAllPlayers().stream().map(Player::getUsername)
                        .filter(n -> n.toLowerCase(Locale.ROOT).startsWith(prefix)).forEach(out::add);
            } else if (args[0].equalsIgnoreCase("remove")) {
                bans.ips().keySet().stream().filter(k -> k.startsWith(prefix)).forEach(out::add);
                bans.names().keySet().stream().filter(k -> k.startsWith(prefix)).forEach(out::add);
            }
        }
        return out;
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

    private static String actor(CommandSource source) {
        return source instanceof Player p ? p.getUsername() : "コンソール";
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

    private static void reply(CommandSource source, String text, NamedTextColor color) {
        source.sendMessage(Component.text(text, color));
    }
}
