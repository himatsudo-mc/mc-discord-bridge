"""discord.py client that relays chat and server status to Discord."""

from __future__ import annotations

import logging
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config, LinkSettings

logger = logging.getLogger("bridge.discord_bot")


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
        self.tree.add_command(
            app_commands.Command(
                name="players",
                description="Minecraftサーバーの参加者一覧を表示します",
                callback=self._players_slash,
            )
        )

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
                "\N{WARNING SIGN} Minecraftサーバーに接続できないため、参加者を取得できませんでした。"
            )
            return
        players, max_players = result
        count = f"{len(players)}/{max_players}" if max_players is not None else str(len(players))
        if players:
            safe = [
                discord.utils.escape_markdown(discord.utils.escape_mentions(n))
                for n in sorted(players, key=str.lower)
            ]
            description = "\n".join(f"\N{BULLET} {n}" for n in safe)
        else:
            description = "現在オンラインのプレイヤーはいません。"
        embed = discord.Embed(
            title=f"\N{BUSTS IN SILHOUETTE} 参加者一覧 ({count})",
            description=description,
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def relay_minecraft_chat(self, server_id: str, player: str, message_text: str) -> None:
        channel = self._channel_for(server_id, notify=False)
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

    async def notify_player_join(self, server_id: str, player: str) -> None:
        await self._send_presence_notice(server_id, player, self._config.format.player_join)

    async def notify_player_leave(self, server_id: str, player: str) -> None:
        await self._send_presence_notice(server_id, player, self._config.format.player_leave)

    async def _send_presence_notice(self, server_id: str, player: str, template: str) -> None:
        channel = self._channel_for(server_id, notify=True)
        if channel is None:
            return
        safe_player = discord.utils.escape_markdown(discord.utils.escape_mentions(player))
        text = template.format(player=safe_player)
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def _send_status_embed(self, server_id: str, server_name: str, started: bool) -> None:
        channel = self._channel_for(server_id, notify=True)
        if channel is None:
            return
        embed = discord.Embed(
            title=f"\N{LARGE GREEN CIRCLE} {server_name} が起動しました" if started
            else f"\N{LARGE RED CIRCLE} {server_name} が停止します",
            color=discord.Color.green() if started else discord.Color.red(),
        )
        await channel.send(embed=embed)

    def _channel_for(self, server_id: str, notify: bool) -> Optional[discord.abc.Messageable]:
        """Resolves a server_id to its chat (or notify) channel, or None if unroutable."""
        link = self._links_by_id.get(server_id)
        if link is None:
            logger.warning("Received a message from unknown server_id %r; dropping.", server_id)
            return None
        return self._get_text_channel(link.notify_channel_id if notify else link.chat_channel_id)

    def _get_text_channel(self, channel_id: int) -> Optional[discord.abc.Messageable]:
        channel = self.get_channel(channel_id)
        if channel is None:
            logger.warning("Configured Discord channel %s was not found.", channel_id)
        return channel
