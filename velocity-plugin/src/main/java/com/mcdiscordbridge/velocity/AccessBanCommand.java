package com.mcdiscordbridge.velocity;

import com.velocitypowered.api.command.CommandSource;
import com.velocitypowered.api.command.SimpleCommand;
import com.velocitypowered.api.proxy.Player;
import com.velocitypowered.api.proxy.ProxyServer;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;

/**
 * /accessban add|remove|list - manual access bans by IP address or player name.
 * Requires the permission {@code mcdiscordbridge.accessban} (the console always has it).
 * The same operations are available from Discord through {@link AccessBanService}.
 */
final class AccessBanCommand implements SimpleCommand {

    static final String PERMISSION = "mcdiscordbridge.accessban";
    private static final List<String> SUBCOMMANDS = List.of("add", "remove", "list");

    private final ProxyServer proxy;
    private final AccessBanService service;

    AccessBanCommand(ProxyServer proxy, AccessBanService service) {
        this.proxy = proxy;
        this.service = service;
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
        String by = source instanceof Player p ? p.getUsername() : "コンソール";
        switch (sub) {
            case "add" -> {
                if (args.length < 2) {
                    usage(source);
                    return;
                }
                show(source, service.add(args[1], String.join(" ", Arrays.copyOfRange(args, 2, args.length)), by));
            }
            case "remove" -> {
                if (args.length < 2) {
                    usage(source);
                    return;
                }
                show(source, service.remove(args[1], by));
            }
            case "list" -> service.list().forEach(l -> source.sendMessage(Component.text(l, NamedTextColor.GRAY)));
            default -> usage(source);
        }
    }

    private static void usage(CommandSource source) {
        source.sendMessage(Component.text("使い方:", NamedTextColor.YELLOW));
        source.sendMessage(Component.text("/accessban add <IPアドレス|プレイヤー名> [理由]", NamedTextColor.GRAY));
        source.sendMessage(Component.text("/accessban remove <IPアドレス|プレイヤー名>", NamedTextColor.GRAY));
        source.sendMessage(Component.text("/accessban list", NamedTextColor.GRAY));
    }

    private static void show(CommandSource source, AccessBanService.Result r) {
        source.sendMessage(Component.text(r.message(), r.ok() ? NamedTextColor.GREEN : NamedTextColor.RED));
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
                service.bans().ips().keySet().stream().filter(k -> k.startsWith(prefix)).forEach(out::add);
                service.bans().names().keySet().stream().filter(k -> k.startsWith(prefix)).forEach(out::add);
            }
        }
        return out;
    }
}
