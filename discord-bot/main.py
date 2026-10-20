"""Entry point: wires the Discord bot and the bridge WebSocket server together."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from bridge.config import load_config
from bridge.discord_bot import DiscordBridgeBot
from bridge.http_api import BridgeHttpApi
from bridge.links import LinkStore
from bridge.vc_manager import DynamicVcManager
from bridge.ws_server import BridgeServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("bridge.main")


async def main() -> None:
    config = load_config()
    logger.info("Configured Minecraft servers: %s", [link.server_id for link in config.links])

    bot = DiscordBridgeBot(config)
    server = BridgeServer(
        config,
        on_chat=bot.relay_minecraft_chat,
        on_server_start=bot.notify_server_start,
        on_server_stop=bot.notify_server_stop,
        on_player_join=bot.notify_player_join,
        on_player_leave=bot.notify_player_leave,
    )
    bot.bridge_server = server
    bot.vc_manager = DynamicVcManager(bot, Path(config.vc.state_file), config.vc.empty_grace_seconds)
    bot.link_store = LinkStore(Path(config.vc.links_file))
    BridgeHttpApi(
        bot, {link.server_id: link for link in config.links}, bot.vc_manager, bot.link_store
    ).add_routes(server.app)

    await server.start()
    try:
        await bot.start(config.token)
    finally:
        bot.vc_manager.stop()
        await server.stop()
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
