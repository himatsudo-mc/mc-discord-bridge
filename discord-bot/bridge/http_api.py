"""HTTP API used by the himatsudoLife plugin (same listener as the bridge WebSocket).

Every request is a JSON POST carrying ``server_id`` and ``token`` (that link's
shared secret). Players are identified by their in-game name (MCID) and resolved
to Discord users through the staff-maintained LinkStore. See PROTOCOL.md, "HTTP API".
"""

from __future__ import annotations

import hmac
import logging
from typing import Dict, List, Optional

import discord
from aiohttp import web

from .config import LinkSettings
from .links import LinkStore
from .vc_manager import MAX_MEMBERS, DynamicVcManager, VcError

logger = logging.getLogger("bridge.http_api")

ROUTE_VC_CREATE = "/api/v1/vc/create"
ROUTE_AREA_ROLE = "/api/v1/area/role"


def _snowflake(data: dict, key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit():
        raise VcError(f"{key} must be a numeric Discord ID")
    return int(value)


class BridgeHttpApi:
    def __init__(
        self,
        bot: discord.Client,
        links: Dict[str, LinkSettings],
        vc_manager: DynamicVcManager,
        link_store: LinkStore,
    ) -> None:
        self._bot = bot
        self._links = links
        self._vc = vc_manager
        self._link_store = link_store

    def add_routes(self, app: web.Application) -> None:
        app.router.add_post(ROUTE_VC_CREATE, self._handle_vc_create)
        app.router.add_post(ROUTE_AREA_ROLE, self._handle_area_role)

    # ---- plumbing ----------------------------------------------------

    async def _read(self, request: web.Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise VcError("body must be JSON") from None
        if not isinstance(data, dict):
            raise VcError("body must be a JSON object")
        link = self._links.get(str(data.get("server_id", "")))
        token = data.get("token")
        if link is None or not isinstance(token, str) or not hmac.compare_digest(token, link.secret):
            raise VcError("unauthorized", status=401)
        return data

    @staticmethod
    def _error(exc: VcError) -> web.Response:
        return web.json_response({"ok": False, "error": str(exc), "code": exc.code}, status=exc.status)

    @staticmethod
    def _mcid(data: dict, key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise VcError(f"{key} must be a Minecraft name")
        return value.strip()

    def _require_link(self, mcid: str) -> int:
        discord_id = self._link_store.discord_id_of(mcid)
        if discord_id is None:
            raise VcError(f"{mcid} is not linked to a Discord user", status=404, code="not_linked")
        return discord_id

    # ---- handlers ----------------------------------------------------

    async def _handle_vc_create(self, request: web.Request) -> web.Response:
        try:
            data = await self._read(request)
            raw_members = data.get("member_mcids", [])
            if not isinstance(raw_members, list) or len(raw_members) > MAX_MEMBERS:
                raise VcError("member_mcids must be a list of Minecraft names")
            owner_id = self._require_link(self._mcid(data, "owner_mcid"))
            members: List[int] = []
            linked: List[str] = []
            unlinked: List[str] = []
            for raw in raw_members:
                mcid = self._mcid({"m": raw}, "m")
                discord_id = self._link_store.discord_id_of(mcid)
                if discord_id is None:
                    unlinked.append(mcid)
                else:
                    members.append(discord_id)
                    linked.append(mcid)
            channel = await self._vc.create(
                vc_type=str(data.get("type", "")),
                name=str(data.get("name", "")),
                category_id=_snowflake(data, "category_id"),
                owner_id=owner_id,
                member_ids=members,
            )
        except VcError as exc:
            return self._error(exc)
        except Exception:
            logger.exception("vc/create failed unexpectedly.")
            return web.json_response({"ok": False, "error": "internal error"}, status=500)
        return web.json_response(
            {
                "ok": True,
                "channel_id": str(channel.id),
                "guild_id": str(channel.guild.id),
                "url": f"https://discord.com/channels/{channel.guild.id}/{channel.id}",
                "linked_mcids": linked,
                "unlinked_mcids": unlinked,
            }
        )

    async def _handle_area_role(self, request: web.Request) -> web.Response:
        try:
            data = await self._read(request)
            action = data.get("action")
            if action not in ("add", "remove"):
                raise VcError("action must be 'add' or 'remove'")
            mcid = self._mcid(data, "mcid")
            role_id = _snowflake(data, "role_id")
            channel_id = _snowflake(data, "channel_id")

            channel = self._bot.get_channel(channel_id)
            guild: Optional[discord.Guild] = getattr(channel, "guild", None)
            if guild is None:
                raise VcError("channel_id is not a channel the bot can see", status=404)
            role = guild.get_role(role_id)
            if role is None:
                raise VcError("role_id was not found in the channel's guild", status=404)
            user_id = self._link_store.discord_id_of(mcid)
            if user_id is None:
                if action == "remove":
                    return web.json_response({"ok": True})  # nothing to take away
                raise VcError(f"{mcid} is not linked to a Discord user", status=404, code="not_linked")
            member = await DynamicVcManager._fetch_member(guild, user_id)
            if member is None:
                if action == "remove":
                    return web.json_response({"ok": True})
                raise VcError("user is not a member of the guild", status=404, code="not_in_guild")

            try:
                if action == "add":
                    await member.add_roles(role, reason="Minecraft area entered")
                else:
                    await member.remove_roles(role, reason="Minecraft area left")
            except discord.Forbidden as exc:
                raise VcError("the bot may not manage that role (check Manage Roles and role order)", status=502) from exc
            except discord.HTTPException as exc:
                raise VcError(f"Discord rejected the role change: {exc}", status=502) from exc
        except VcError as exc:
            return self._error(exc)
        except Exception:
            logger.exception("area/role failed unexpectedly.")
            return web.json_response({"ok": False, "error": "internal error"}, status=500)
        return web.json_response({"ok": True})
