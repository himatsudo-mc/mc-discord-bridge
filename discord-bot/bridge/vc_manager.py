"""Dynamic voice channels created on request from the Minecraft side.

The plugin asks the bot (via bridge.http_api) to create a voice channel for a
friend / party / public group. The bot remembers every channel it created in a
small JSON file so it can delete each one as soon as it is empty - including
after a bot restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, Iterable, Optional

import discord

logger = logging.getLogger("bridge.vc_manager")

VC_TYPES = ("friend", "party", "public")
_MAX_NAME_LENGTH = 100
# Discord allows at most 100 permission overwrites per channel; keep room for @everyone and the bot.
MAX_MEMBERS = 90
_SWEEP_INTERVAL_SECONDS = 60


class VcError(Exception):
    """A request that cannot be fulfilled; ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request") -> None:
        super().__init__(message)
        self.status = status
        # Machine-readable reason for the plugin (e.g. "not_linked").
        self.code = code


def sanitize_channel_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name).strip()
    return cleaned[:_MAX_NAME_LENGTH]


class DynamicVcManager:
    def __init__(self, bot: discord.Client, state_file: Path, empty_grace_seconds: float) -> None:
        self._bot = bot
        self._state_file = state_file
        self._empty_grace_seconds = empty_grace_seconds
        # channel id -> creation time (epoch seconds)
        self._channels: Dict[int, float] = {}
        self._sweep_task: Optional[asyncio.Task] = None
        self._load()

    # ---- persistence -------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._channels = {int(k): float(v) for k, v in raw.items()}
        except FileNotFoundError:
            self._channels = {}
        except (ValueError, TypeError, OSError):
            logger.exception("Could not read %s; starting with no tracked voice channels.", self._state_file)
            self._channels = {}

    def _save(self) -> None:
        try:
            self._state_file.write_text(
                json.dumps({str(k): v for k, v in self._channels.items()}), encoding="utf-8"
            )
        except OSError:
            logger.exception("Could not write %s.", self._state_file)

    # ---- lifecycle ---------------------------------------------------

    def start(self) -> None:
        if self._sweep_task is None:
            self._sweep_task = asyncio.get_running_loop().create_task(self._sweep_loop())

    def stop(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            self._sweep_task = None

    def is_tracked(self, channel_id: int) -> bool:
        return channel_id in self._channels

    # ---- creation ----------------------------------------------------

    async def create(
        self,
        vc_type: str,
        name: str,
        category_id: int,
        owner_id: int,
        member_ids: Iterable[int],
    ) -> discord.VoiceChannel:
        if vc_type not in VC_TYPES:
            raise VcError(f"unknown vc type {vc_type!r}")
        name = sanitize_channel_name(name)
        if not name:
            raise VcError("name must not be empty")
        category = self._bot.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            raise VcError("category_id is not a category the bot can see", status=404)
        guild = category.guild

        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        if vc_type != "public":
            allowed_ids = {owner_id, *member_ids}
            if len(allowed_ids) > MAX_MEMBERS:
                raise VcError("too many members")
            overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False, connect=False)
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True, connect=True, manage_channels=True
            )
            for user_id in allowed_ids:
                member = await self._fetch_member(guild, user_id)
                if member is None:
                    if user_id == owner_id:
                        raise VcError("owner is not a member of the guild", status=404, code="not_in_guild")
                    continue
                overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)

        try:
            channel = await guild.create_voice_channel(
                name, category=category, overwrites=overwrites, reason="Minecraft /hl vcc"
            )
        except discord.Forbidden as exc:
            raise VcError("the bot lacks permission to create channels", status=502) from exc
        except discord.HTTPException as exc:
            raise VcError(f"Discord rejected the channel: {exc}", status=502) from exc

        self._channels[channel.id] = time.time()
        self._save()
        return channel

    @staticmethod
    async def _fetch_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            logger.exception("Could not fetch member %s.", user_id)
            return None

    # ---- deletion ----------------------------------------------------

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        channel = before.channel
        if channel is None or channel == after.channel or channel.id not in self._channels:
            return
        if len(channel.members) == 0:
            await self._delete(channel)

    async def _delete(self, channel: discord.abc.GuildChannel) -> None:
        try:
            await channel.delete(reason="Minecraft /hl vcc: channel is empty")
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logger.exception("Could not delete voice channel %s; will retry on the next sweep.", channel.id)
            return
        self._forget(channel.id)

    def _forget(self, channel_id: int) -> None:
        if self._channels.pop(channel_id, None) is not None:
            self._save()

    async def sweep(self) -> None:
        """Deletes tracked channels that are empty and old enough, and forgets vanished ones.

        Voice events only fire when someone leaves, so this catches channels nobody ever
        joined and channels that emptied while the bot was offline.
        """
        now = time.time()
        for channel_id, created_at in list(self._channels.items()):
            channel = self._bot.get_channel(channel_id)
            if channel is None:
                if self._bot.is_ready():
                    self._forget(channel_id)
                continue
            if len(getattr(channel, "members", [])) == 0 and now - created_at >= self._empty_grace_seconds:
                await self._delete(channel)

    async def _sweep_loop(self) -> None:
        await self._bot.wait_until_ready()
        while True:
            try:
                await self.sweep()
            except Exception:
                logger.exception("Dynamic voice channel sweep failed.")
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
