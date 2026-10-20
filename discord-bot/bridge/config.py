"""Loads config.yml and the bot token from the environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

SERVER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_SERVER_ID = "default"


@dataclass(frozen=True)
class LinkSettings:
    server_id: str
    secret: str
    chat_channel_id: int
    notify_channel_id: int


@dataclass(frozen=True)
class DiscordSettings:
    command_prefix: str


@dataclass(frozen=True)
class BridgeSettings:
    host: str
    port: int
    auth_timeout: float
    max_queue_size: int
    max_queue_age_seconds: float


@dataclass(frozen=True)
class FormatSettings:
    # Discord -> Minecraft formatting lives in the plugin's own config.yml
    # (format.discord-to-minecraft); the bot sends that direction unformatted.
    minecraft_to_discord: str
    player_join: str
    player_leave: str


@dataclass(frozen=True)
class VcSettings:
    # Where the ids of dynamically created voice channels are remembered across restarts.
    state_file: str = "dynamic_vcs.json"
    # An empty dynamic VC is deleted by the periodic sweep once it is at least this old
    # (covers channels nobody ever joined; channels that empty out are deleted immediately).
    empty_grace_seconds: float = 300.0


@dataclass(frozen=True)
class Config:
    token: str
    discord: DiscordSettings
    bridge: BridgeSettings
    format: FormatSettings
    links: list[LinkSettings]
    vc: VcSettings = VcSettings()


def _validate_secret(secret: str, where: str) -> str:
    if not secret or secret.startswith("CHANGE_ME"):
        raise RuntimeError(
            f"{where} has not been set in config.yml - it must match the "
            "corresponding minecraft-plugin instance's config.yml exactly."
        )
    return secret


def _parse_links(raw: dict) -> list[LinkSettings]:
    if "links" in raw:
        links_raw = raw.get("links")
        if not isinstance(links_raw, list) or not links_raw:
            raise RuntimeError("links must be a non-empty list of server entries.")

        links: list[LinkSettings] = []
        seen_server_ids: set[str] = set()
        seen_channel_ids: set[int] = set()
        for entry in links_raw:
            server_id = str(entry.get("server_id", "")).strip()
            if not server_id or not SERVER_ID_PATTERN.match(server_id):
                raise RuntimeError(
                    f"links[].server_id {server_id!r} is invalid - it must be non-empty and "
                    "match ^[A-Za-z0-9_-]+$."
                )
            if server_id in seen_server_ids:
                raise RuntimeError(f"Duplicate links[].server_id: {server_id!r}")
            seen_server_ids.add(server_id)

            chat_channel_id = int(entry.get("chat_channel_id", 0))
            if chat_channel_id in seen_channel_ids:
                raise RuntimeError(
                    f"Duplicate links[].chat_channel_id: {chat_channel_id} - each server "
                    "must relay to its own Discord channel."
                )
            seen_channel_ids.add(chat_channel_id)

            secret = _validate_secret(str(entry.get("secret", "")), f"links[server_id={server_id}].secret")

            links.append(
                LinkSettings(
                    server_id=server_id,
                    secret=secret,
                    chat_channel_id=chat_channel_id,
                    notify_channel_id=int(entry.get("notify_channel_id", chat_channel_id)),
                )
            )
        return links

    # Legacy single-server shape: synthesize one implicit link so existing
    # config.yml files keep working without any changes.
    discord_raw = raw.get("discord", {})
    bridge_raw = raw.get("bridge", {})
    secret = _validate_secret(str(bridge_raw.get("secret", "")), "bridge.secret")
    return [
        LinkSettings(
            server_id=DEFAULT_SERVER_ID,
            secret=secret,
            chat_channel_id=int(discord_raw.get("chat_channel_id", 0)),
            notify_channel_id=int(discord_raw.get("notify_channel_id", 0)),
        )
    ]


def load_config(config_path: str | Path = "config.yml", env_path: str | Path = ".env") -> Config:
    load_dotenv(dotenv_path=env_path)

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill in your bot token."
        )

    path = Path(config_path)
    if not path.exists():
        raise RuntimeError(
            f"{path} not found. Copy config.example.yml to {path} and fill in your settings."
        )

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    discord_raw = raw.get("discord", {})
    bridge_raw = raw.get("bridge", {})
    format_raw = raw.get("format", {})
    vc_raw = raw.get("vc", {})

    links = _parse_links(raw)

    return Config(
        token=token,
        discord=DiscordSettings(
            command_prefix=str(discord_raw.get("command_prefix", "!")),
        ),
        bridge=BridgeSettings(
            host=str(bridge_raw.get("host", "0.0.0.0")),
            port=int(bridge_raw.get("port", 8765)),
            auth_timeout=float(bridge_raw.get("auth_timeout", 10)),
            max_queue_size=int(bridge_raw.get("max_queue_size", 50)),
            max_queue_age_seconds=float(bridge_raw.get("max_queue_age_seconds", 30)),
        ),
        format=FormatSettings(
            minecraft_to_discord=str(format_raw.get("minecraft_to_discord", "**{player}**: {message}")),
            player_join=str(format_raw.get("player_join", "\N{LARGE GREEN CIRCLE} **{player}** が参加しました")),
            player_leave=str(format_raw.get("player_leave", "\N{LARGE RED CIRCLE} **{player}** が退出しました")),
        ),
        links=links,
        vc=VcSettings(
            state_file=str(vc_raw.get("state_file", "dynamic_vcs.json")),
            empty_grace_seconds=float(vc_raw.get("empty_grace_seconds", 300)),
        ),
    )
