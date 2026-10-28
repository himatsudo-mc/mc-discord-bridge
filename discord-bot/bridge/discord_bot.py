"""discord.py client that relays chat and server status to Discord."""

from __future__ import annotations

import logging
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config, LinkSettings
from .links import MCID_PATTERN, LinkStore

logger = logging.getLogger("bridge.discord_bot")

# Embed descriptions are capped at 4096 characters by Discord.
_EMBED_DESCRIPTION_LIMIT = 4096


_SECURITY_KINDS = {
    "banned_attempt": ("\N{NO ENTRY} アク禁中のプレイヤー/IPが接続を試みました", discord.Color.red()),
    "rapid_connections": ("\N{WARNING SIGN} 短時間に大量の接続がありました", discord.Color.orange()),
    "invalid_name": ("\N{WARNING SIGN} 不正なプレイヤー名での接続がありました", discord.Color.orange()),
    "ban_added": ("\N{HAMMER} アク禁を追加しました", discord.Color.dark_grey()),
    "ban_removed": ("\N{WHITE HEAVY CHECK MARK} アク禁を解除しました", discord.Color.dark_grey()),
}


def _clip(value: object, limit: int) -> str:
    text = "" if value is None else str(value).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _escape(text: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


class DiscordBridgeBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=config.discord.command_prefix, intents=intents)
        self._config = config
        self._links_by_id: Dict[str, LinkSettings] = {link.server_id: link for link in config.links}
        self._channel_to_server: Dict[int, str] = {link.chat_channel_id: link.server_id for link in config.links}
        # Wired up by main.py once the BridgeServer exists.
        self.bridge_server = None
        # Likewise wired up by main.py (dynamic voice channels created via the HTTP API).
        self.vc_manager = None
        # MCID <-> Discord links registered by staff with /link (wired up by main.py).
        self.link_store: Optional[LinkStore] = None
        self.tree.add_command(
            app_commands.Command(
                name="players",
                description="Minecraftサーバーの参加者一覧を表示します",
                callback=self._players_slash,
            )
        )
        self._add_link_commands()
        self._add_accessban_command()

    async def on_ready(self) -> None:
        logger.info("Logged in to Discord as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user or message.author.bot:
            return
        server_id = self._channel_to_server.get(message.channel.id)
        if server_id is None:
            return
        if message.content.startswith(self._config.discord.command_prefix):
            await self.process_commands(message)
            return

        content = message.clean_content.strip()
        if not content:
            if message.attachments:
                content = "[attachment]"
            else:
                return

        if self.bridge_server is not None:
            await self.bridge_server.broadcast_to_minecraft(server_id, message.author.display_name, content)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        if self.vc_manager is not None:
            self.vc_manager.start()

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if self.vc_manager is not None:
            await self.vc_manager.on_voice_state_update(member, before, after)

    async def _players_slash(self, interaction: discord.Interaction) -> None:
        server_id = self._channel_to_server.get(interaction.channel_id)
        if server_id is None or self.bridge_server is None:
            await interaction.response.send_message(
                "このチャンネルはMinecraftサーバーと連携されていません。", ephemeral=True
            )
            return
        await interaction.response.defer()
        result = await self.bridge_server.request_players(server_id)
        if result is None:
            await interaction.followup.send(
                "\N{WARNING SIGN} Minecraftサーバーに接続できない、または応答がないため、参加者を取得できませんでした。"
            )
            return
        players, max_players = result
        count = f"{len(players)}/{max_players}" if max_players is not None else str(len(players))
        embed = discord.Embed(
            title=f"\N{BUSTS IN SILHOUETTE} 参加者一覧 ({count})",
            description=format_player_list(players),
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    def _add_link_commands(self) -> None:
        """Staff-only /link and /unlink (needs Manage Roles; server admins can change who may use them)."""

        @app_commands.describe(user="紐づけるDiscordユーザー", mcid="そのプレイヤーのMCID（統合版は「.」などの接頭辞込み）")
        async def link(interaction: discord.Interaction, user: discord.Member, mcid: str) -> None:
            await self._link_slash(interaction, user, mcid)

        @app_commands.describe(user="紐づけを解除するDiscordユーザー", mcid="紐づけを解除するMCID")
        async def unlink(
            interaction: discord.Interaction,
            user: Optional[discord.Member] = None,
            mcid: Optional[str] = None,
        ) -> None:
            await self._unlink_slash(interaction, user, mcid)

        @app_commands.describe(user="確認するDiscordユーザー")
        async def linkinfo(interaction: discord.Interaction, user: discord.Member) -> None:
            await self._linkinfo_slash(interaction, user)

        staff = discord.Permissions(manage_roles=True)
        for name, description, callback in (
            ("link", "（運営用）DiscordユーザーとMinecraftのMCIDを紐づけます", link),
            ("unlink", "（運営用）DiscordユーザーとMCIDの紐づけを解除します", unlink),
            ("linkinfo", "（運営用）Discordユーザーに紐づいているMCIDを確認します", linkinfo),
        ):
            command = app_commands.Command(name=name, description=description, callback=callback)
            command.default_permissions = staff
            command.guild_only = True
            self.tree.add_command(command)

    def _add_accessban_command(self) -> None:
        """Staff-only /accessban, executed on the Velocity plugin (same operations as its in-game command)."""

        @app_commands.describe(
            action="追加 / 解除 / 一覧",
            target="IPアドレスまたはプレイヤー名（一覧では不要）",
            reason="アク禁の理由（追加時のみ・任意）",
        )
        @app_commands.choices(action=[
            app_commands.Choice(name="追加 (add)", value="add"),
            app_commands.Choice(name="解除 (remove)", value="remove"),
            app_commands.Choice(name="一覧 (list)", value="list"),
        ])
        async def accessban(
            interaction: discord.Interaction,
            action: app_commands.Choice[str],
            target: Optional[str] = None,
            reason: Optional[str] = None,
        ) -> None:
            await self._accessban_slash(interaction, action.value, target, reason)

        command = app_commands.Command(
            name="accessban", description="（運営用）IPアドレス/プレイヤー名をアク禁にします", callback=accessban
        )
        command.default_permissions = discord.Permissions(manage_roles=True)
        command.guild_only = True
        self.tree.add_command(command)

    def _server_for_interaction(self, interaction: discord.Interaction) -> Optional[str]:
        """The server a command targets: the only one, else the one whose channel it was run in."""
        if len(self._links_by_id) == 1:
            return next(iter(self._links_by_id))
        for link in self._links_by_id.values():
            if interaction.channel_id in (link.chat_channel_id, link.notify_channel_id, link.security_channel_id):
                return link.server_id
        return None

    async def _accessban_slash(
        self, interaction: discord.Interaction, action: str, target: Optional[str], reason: Optional[str]
    ) -> None:
        if not self._is_staff(interaction) or self.bridge_server is None:
            await interaction.response.send_message("この操作を行う権限がありません。", ephemeral=True)
            return
        server_id = self._server_for_interaction(interaction)
        if server_id is None:
            await interaction.response.send_message(
                "対象のサーバーを判別できません。そのサーバーに紐づくチャンネルで実行してください。", ephemeral=True
            )
            return
        target = (target or "").strip()
        if action != "list" and (not target or len(target.split()) != 1):
            await interaction.response.send_message(
                "target にIPアドレスまたはプレイヤー名を1つ指定してください。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        ok, message = await self.bridge_server.request_accessban(
            server_id, action, target, (reason or "").strip(), interaction.user.display_name
        )
        icon = "\N{WHITE HEAVY CHECK MARK}" if ok else "\N{CROSS MARK}"
        text = f"{icon} {discord.utils.escape_markdown(discord.utils.escape_mentions(message))}"
        await interaction.followup.send(
            text[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @staticmethod
    def _is_staff(interaction: discord.Interaction) -> bool:
        # default_permissions only hides the command; check again in case a server admin widened it.
        perms = getattr(interaction.user, "guild_permissions", None)
        return perms is not None and (perms.manage_roles or perms.administrator)

    async def _link_slash(self, interaction: discord.Interaction, user: discord.Member, mcid: str) -> None:
        if not self._is_staff(interaction) or self.link_store is None:
            await interaction.response.send_message("この操作を行う権限がありません。", ephemeral=True)
            return
        mcid = mcid.strip()
        if not MCID_PATTERN.match(mcid):
            await interaction.response.send_message(
                "MCIDの形式が正しくありません（英数字と「_」、統合版は先頭に「.」などの接頭辞）。", ephemeral=True
            )
            return
        if user.bot:
            await interaction.response.send_message("Botとは紐づけできません。", ephemeral=True)
            return
        replaced_user = self.link_store.discord_id_of(mcid)
        previous = self.link_store.link(mcid, user.id, interaction.user.id)
        safe = discord.utils.escape_markdown(mcid)
        lines = [f"\N{WHITE HEAVY CHECK MARK} {user.mention} と **{safe}** を紐づけました。"]
        if previous is not None:
            lines.append(f"（このユーザーの以前の紐づけ **{discord.utils.escape_markdown(previous)}** は解除しました）")
        if replaced_user is not None and replaced_user != user.id:
            lines.append(f"（このMCIDは <@{replaced_user}> と紐づいていたため、付け替えました）")
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _unlink_slash(
        self, interaction: discord.Interaction, user: Optional[discord.Member], mcid: Optional[str]
    ) -> None:
        if not self._is_staff(interaction) or self.link_store is None:
            await interaction.response.send_message("この操作を行う権限がありません。", ephemeral=True)
            return
        if (user is None) == (mcid is None):
            await interaction.response.send_message("user か mcid のどちらか一方を指定してください。", ephemeral=True)
            return
        if user is not None:
            removed = self.link_store.unlink_user(user.id)
            text = (f"{user.mention} と **{discord.utils.escape_markdown(removed)}** の紐づけを解除しました。"
                    if removed is not None else f"{user.mention} は紐づけされていません。")
        else:
            removed_id = self.link_store.unlink_mcid(mcid)
            safe = discord.utils.escape_markdown(mcid.strip())
            text = (f"**{safe}** と <@{removed_id}> の紐づけを解除しました。"
                    if removed_id is not None else f"**{safe}** は紐づけされていません。")
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _linkinfo_slash(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not self._is_staff(interaction) or self.link_store is None:
            await interaction.response.send_message("この操作を行う権限がありません。", ephemeral=True)
            return
        mcid = self.link_store.mcid_of(user.id)
        text = (f"{user.mention} のMCIDは **{discord.utils.escape_markdown(mcid)}** です。"
                if mcid is not None else f"{user.mention} は紐づけされていません。")
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def relay_minecraft_chat(self, server_id: str, player: str, message_text: str) -> None:
        channel = await self._channel_for(server_id, notify=False)
        if channel is None:
            return
        safe_player = discord.utils.escape_markdown(discord.utils.escape_mentions(player))
        safe_message = discord.utils.escape_markdown(discord.utils.escape_mentions(message_text))
        text = self._config.format.minecraft_to_discord.format(player=safe_player, message=safe_message)
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def notify_server_start(self, server_id: str, server_name: str) -> None:
        await self._send_status_embed(server_id, server_name, started=True)

    async def notify_server_stop(self, server_id: str, server_name: str) -> None:
        await self._send_status_embed(server_id, server_name, started=False)

    async def notify_proxy_link(self, server_id: str, status: str, backend: str) -> None:
        """Velocity's connection check result for the Minecraft server (see PROTOCOL.md "Proxy link")."""
        up = status == "up"
        if not up and not self._config.bridge.proxy_notify_down:
            return
        channel = await self._channel_for(server_id, notify=True)
        if channel is None:
            return
        embed = discord.Embed(
            title="\N{LINK SYMBOL} VelocityとMinecraftサーバーが接続出来ました" if up
            else "\N{WARNING SIGN} VelocityからMinecraftサーバーに接続出来なくなりました",
            color=discord.Color.green() if up else discord.Color.orange(),
        )
        await channel.send(embed=embed)

    async def notify_security_alert(self, server_id: str, alert: dict) -> None:
        """Velocity's unauthorized-access alert, posted to the dedicated security channel only."""
        link = self._links_by_id.get(server_id)
        if link is None:
            logger.warning("Received a security alert from unknown server_id %r; dropping.", server_id)
            return
        kind = str(alert.get("kind", ""))
        title, color = _SECURITY_KINDS.get(
            kind, (f"\N{WARNING SIGN} 不正アクセスの疑い ({_clip(kind, 40) or '不明'})", discord.Color.orange())
        )
        address = _clip(alert.get("address"), 64)
        player = _clip(alert.get("player"), 64)
        detail = _clip(alert.get("detail"), 500)
        logger.warning("Security alert from %r: kind=%s address=%s player=%s detail=%s",
                       server_id, kind, address, player, detail)
        if not link.security_channel_id:
            logger.warning("discord.security_channel_id (or links[].security_channel_id) is not set; "
                           "the security alert was not posted to Discord.")
            return
        channel = await self._get_text_channel(link.security_channel_id)
        if channel is None:
            return
        embed = discord.Embed(title=title, color=color)
        embed.add_field(name="サーバー", value=_escape(server_id), inline=True)
        if address:
            embed.add_field(name="IPアドレス", value=_escape(address), inline=True)
        if player:
            embed.add_field(name="プレイヤー名", value=_escape(player), inline=True)
        if detail:
            embed.add_field(name="詳細", value=_escape(detail), inline=False)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def notify_player_join(self, server_id: str, player: str) -> None:
        await self._send_presence_notice(server_id, player, self._config.format.player_join)

    async def notify_player_leave(self, server_id: str, player: str) -> None:
        await self._send_presence_notice(server_id, player, self._config.format.player_leave)

    async def _send_presence_notice(self, server_id: str, player: str, template: str) -> None:
        channel = await self._channel_for(server_id, notify=True)
        if channel is None:
            return
        safe_player = discord.utils.escape_markdown(discord.utils.escape_mentions(player))
        text = template.format(player=safe_player)
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def _send_status_embed(self, server_id: str, server_name: str, started: bool) -> None:
        channel = await self._channel_for(server_id, notify=True)
        if channel is None:
            return
        embed = discord.Embed(
            title=f"\N{LARGE GREEN CIRCLE} {server_name} が起動しました" if started
            else f"\N{LARGE RED CIRCLE} {server_name} が停止します",
            color=discord.Color.green() if started else discord.Color.red(),
        )
        await channel.send(embed=embed)

    async def _channel_for(self, server_id: str, notify: bool) -> Optional[discord.abc.Messageable]:
        """Resolves a server_id to its chat (or notify) channel, or None if unroutable."""
        link = self._links_by_id.get(server_id)
        if link is None:
            logger.warning("Received a message from unknown server_id %r; dropping.", server_id)
            return None
        return await self._get_text_channel(link.notify_channel_id if notify else link.chat_channel_id)

    async def _get_text_channel(self, channel_id: int) -> Optional[discord.abc.Messageable]:
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel
        # The cache can miss (e.g. right after startup); fall back to the REST API
        # and report why it failed so a misconfiguration is easy to diagnose.
        try:
            return await self.fetch_channel(channel_id)
        except discord.NotFound:
            logger.warning(
                "Configured Discord channel %s does not exist (or the bot is not in its server). "
                "Check the channel ID in config.yml.", channel_id)
        except discord.Forbidden:
            logger.warning(
                "Configured Discord channel %s exists but the bot cannot access it. "
                "Grant the bot View Channel / Send Messages permission.", channel_id)
        except discord.HTTPException:
            logger.exception("Failed to fetch Discord channel %s.", channel_id)
        return None


def format_player_list(players: list[str]) -> str:
    """Formats player names as a bulleted list that fits in an embed description.

    Names past the length limit are collapsed into a trailing "他 N 人" line.
    """
    if not players:
        return "現在オンラインのプレイヤーはいません。"
    lines = [
        f"\N{BULLET} {discord.utils.escape_markdown(discord.utils.escape_mentions(name))}"
        for name in sorted(players, key=str.lower)
    ]
    # Leave room for the "...and N more" line in case truncation is needed.
    budget = _EMBED_DESCRIPTION_LIMIT - 32
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    if len(kept) < len(lines):
        kept.append(f"…他 {len(lines) - len(kept)} 人")
    return "\n".join(kept)
