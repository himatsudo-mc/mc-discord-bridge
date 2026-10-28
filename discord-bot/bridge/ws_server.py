"""WebSocket server the Minecraft plugin(s) connect to.

Each configured link (see bridge.config.LinkSettings) gets its own
independent connection slot, keyed by the server_id the plugin presents at
auth time. Multiple Minecraft servers can be connected simultaneously, each
bridging to its own Discord channel. If a plugin reconnects, only the
previous connection for that *same* server_id is replaced - other servers'
connections are never touched.

While a server_id's connection is down (initial connect, mid-reconnect, or
a transient send failure), outgoing messages for it are held in a small
bounded queue and flushed in order once it (re)connects, rather than being
silently dropped. All handlers here are defensive: a malformed message or a
send failure is logged and swallowed rather than raised, so a flaky
Minecraft-side connection can never take the bot down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Awaitable, Callable, Deque, Dict, List, Optional, Tuple

from aiohttp import web

from . import protocol
from .config import Config, LinkSettings

logger = logging.getLogger("bridge.ws_server")

ChatCallback = Callable[[str, str, str], Awaitable[None]]
StatusCallback = Callable[[str, str], Awaitable[None]]
PlayerCallback = Callable[[str, str], Awaitable[None]]
ProxyLinkCallback = Callable[[str, str, str], Awaitable[None]]


class BridgeServer:
    def __init__(
        self,
        config: Config,
        on_chat: ChatCallback,
        on_server_start: StatusCallback,
        on_server_stop: StatusCallback,
        on_player_join: PlayerCallback,
        on_player_leave: PlayerCallback,
        on_proxy_link: Optional[ProxyLinkCallback] = None,
    ) -> None:
        self._config = config
        self._on_chat = on_chat
        self._on_server_start = on_server_start
        self._on_server_stop = on_server_stop
        self._on_player_join = on_player_join
        self._on_player_leave = on_player_leave
        self._on_proxy_link = on_proxy_link
        self._app = web.Application()
        self._app.router.add_get("/", self._handle_ws)
        self._runner: Optional[web.AppRunner] = None

        self._links_by_id: Dict[str, LinkSettings] = {link.server_id: link for link in config.links}
        self._connections: Dict[str, web.WebSocketResponse] = {}
        self._queues: Dict[str, Deque[Tuple[float, str]]] = defaultdict(
            lambda: deque(maxlen=self._config.bridge.max_queue_size)
        )
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # In-flight players_request calls, keyed by (server_id, request_id) so
        # only the server that was asked can answer.
        self._pending_players: Dict[Tuple[str, str], asyncio.Future] = {}

        # Velocity plugin connections (role "proxy"), one per server_id. They never take part in
        # chat relaying, so they live apart from self._connections.
        self._proxy_connections: Dict[str, web.WebSocketResponse] = {}
        # Last backend state the Velocity plugin reported: "up" / "down" (absent = unknown).
        self._proxy_state: Dict[str, str] = {}
        # server_ids that sent server_stop and haven't been seen "down" by Velocity since. Their
        # "up" reports are stale (the server answers pings while shutting down) until it restarts.
        self._proxy_restarting: set = set()
        # server_start notices waiting for the proxy to report "up", keyed by server_id.
        self._proxy_waiters: Dict[str, List[asyncio.Future]] = defaultdict(list)
        self._background_tasks: set = set()

    @property
    def app(self) -> web.Application:
        """The aiohttp application; extra routes must be added before ``start()``."""
        return self._app

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._config.bridge.host, self._config.bridge.port)
        await site.start()
        logger.info(
            "Bridge WebSocket server listening on %s:%s", self._config.bridge.host, self._config.bridge.port
        )

    async def stop(self) -> None:
        for ws in [*self._connections.values(), *self._proxy_connections.values()]:
            if not ws.closed:
                await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    async def broadcast_to_minecraft(self, server_id: str, user: str, message: str) -> None:
        """Relays a Discord message to the connected Minecraft server.

        If that server isn't currently connected (or the send fails), the
        message is queued (bounded, oldest-dropped-first) and flushed once
        the server (re)connects, rather than being dropped outright.
        """
        payload = json.dumps({"type": protocol.TYPE_CHAT, "user": user, "message": message})
        async with self._locks[server_id]:
            ws = self._connections.get(server_id)
            if ws is not None and not ws.closed:
                try:
                    await ws.send_str(payload)
                    return
                except Exception:
                    logger.exception("Failed to send message to Minecraft server %r; queueing.", server_id)
            queue = self._queues[server_id]
            queue.append((time.monotonic(), payload))
            logger.info(
                "Minecraft server %r not connected; queued Discord message (queue size=%d).",
                server_id,
                len(queue),
            )

    async def request_players(
        self, server_id: str, timeout: float = 5.0
    ) -> Optional[Tuple[List[str], Optional[int]]]:
        """Asks the Minecraft server for its online players.

        Returns (player names, max players or None), or None if the server is
        not connected or doesn't answer within ``timeout`` seconds. Unlike
        chat, this is never queued: a stale answer would be misleading.
        """
        ws = self._connections.get(server_id)
        if ws is None or ws.closed:
            return None
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        key = (server_id, request_id)
        self._pending_players[key] = future
        try:
            await ws.send_str(json.dumps({"type": protocol.TYPE_PLAYERS_REQUEST, "request_id": request_id}))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Minecraft server %r did not answer players_request in time.", server_id)
            return None
        except Exception:
            logger.exception("Failed to request player list from %r.", server_id)
            return None
        finally:
            self._pending_players.pop(key, None)

    async def _flush_queue_locked(self, server_id: str, ws: web.WebSocketResponse) -> None:
        """Flushes server_id's queue to ws. Must be called while holding self._locks[server_id]."""
        queue = self._queues.get(server_id)
        if not queue:
            return
        max_age = self._config.bridge.max_queue_age_seconds
        now = time.monotonic()
        remaining: Deque[Tuple[float, str]] = deque(maxlen=self._config.bridge.max_queue_size)
        while queue:
            queued_at, payload = queue.popleft()
            if now - queued_at > max_age:
                logger.info("Dropping stale queued message for %r (age=%.1fs).", server_id, now - queued_at)
                continue
            try:
                await ws.send_str(payload)
            except Exception:
                logger.exception("Failed to flush queued message to %r; re-queueing remainder.", server_id)
                remaining.append((queued_at, payload))
                remaining.extend(queue)
                break
        self._queues[server_id] = remaining

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        auth = await self._authenticate(ws)
        if auth is None:
            await ws.close(code=4001, message=b"unauthorized")
            return ws
        server_id, role = auth

        if role == protocol.ROLE_PROXY:
            return await self._serve_proxy(server_id, ws)

        async with self._locks[server_id]:
            old_ws = self._connections.get(server_id)
            if old_ws is not None and not old_ws.closed:
                logger.warning(
                    "A new connection for Minecraft server %r replaced a previously active one.", server_id
                )
                await old_ws.close()
            self._connections[server_id] = ws
            await self._flush_queue_locked(server_id, ws)
        logger.info("Minecraft plugin %r connected and authenticated.", server_id)

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._handle_message(server_id, msg.data)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.warning("Bridge WebSocket connection error for %r: %s", server_id, ws.exception())
        finally:
            if self._connections.get(server_id) is ws:
                self._connections.pop(server_id, None)
            logger.info("Minecraft plugin %r disconnected.", server_id)

        return ws

    async def _serve_proxy(self, server_id: str, ws: web.WebSocketResponse) -> web.WebSocketResponse:
        old_ws = self._proxy_connections.get(server_id)
        if old_ws is not None and not old_ws.closed:
            await old_ws.close()
        self._proxy_connections[server_id] = ws
        logger.info("Velocity plugin for %r connected and authenticated.", server_id)
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._handle_proxy_message(server_id, msg.data)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.warning("Velocity connection error for %r: %s", server_id, ws.exception())
        finally:
            if self._proxy_connections.get(server_id) is ws:
                self._proxy_connections.pop(server_id, None)
                self._release_proxy_waiters(server_id)
            logger.info("Velocity plugin for %r disconnected.", server_id)
        return ws

    async def _handle_proxy_message(self, server_id: str, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Received malformed Velocity message from %r: %r", server_id, raw)
            return
        if data.get("type") != protocol.TYPE_PROXY_LINK:
            return
        status = str(data.get("status", ""))
        if status not in (protocol.PROXY_STATUS_UP, protocol.PROXY_STATUS_DOWN):
            return
        previous = self._proxy_state.get(server_id)
        self._proxy_state[server_id] = status
        waiting = bool(self._proxy_waiters.get(server_id))
        # Velocity reports every few seconds; only real transitions are posted. The first report
        # (bot just started) is silent unless a server_start is waiting on it.
        if status == protocol.PROXY_STATUS_DOWN:
            self._proxy_restarting.discard(server_id)
            announce = previous == protocol.PROXY_STATUS_UP
        elif server_id in self._proxy_restarting:
            # Stopped but Velocity never saw it go down: announce once the restart's
            # server_start is waiting, ignore the shutdown-time reports before that.
            announce = waiting
            if waiting:
                self._proxy_restarting.discard(server_id)
        else:
            announce = previous == protocol.PROXY_STATUS_DOWN or (previous is None and waiting)
        try:
            if announce and self._on_proxy_link is not None:
                await self._on_proxy_link(server_id, status, str(data.get("backend", "")))
        except Exception:
            logger.exception("Failed to relay a proxy_link message from %r to Discord; dropping it.", server_id)
        finally:
            if status == protocol.PROXY_STATUS_UP:
                self._release_proxy_waiters(server_id)

    def _release_proxy_waiters(self, server_id: str) -> None:
        for future in self._proxy_waiters.pop(server_id, []):
            if not future.done():
                future.set_result(None)

    def _should_hold_server_start(self, server_id: str) -> bool:
        """True while a connected Velocity plugin has yet to report the backend as reachable."""
        if self._config.bridge.proxy_wait_seconds <= 0:
            return False
        ws = self._proxy_connections.get(server_id)
        if ws is None or ws.closed:
            return False
        return (
            self._proxy_state.get(server_id) != protocol.PROXY_STATUS_UP
            or server_id in self._proxy_restarting
        )

    async def _deliver_server_start(self, server_id: str, server_name: str) -> None:
        """Posts server_start, first giving Velocity a moment to report its connection check."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._proxy_waiters[server_id].append(future)
        try:
            await asyncio.wait_for(future, timeout=self._config.bridge.proxy_wait_seconds)
        except asyncio.TimeoutError:
            logger.info("Velocity did not report %r reachable in time; posting server start anyway.", server_id)
        finally:
            waiters = self._proxy_waiters.get(server_id)
            if waiters and future in waiters:
                waiters.remove(future)
        try:
            await self._on_server_start(server_id, server_name)
        except Exception:
            logger.exception("Failed to relay a server_start message from %r to Discord; dropping it.", server_id)

    async def _authenticate(self, ws: web.WebSocketResponse) -> Optional[Tuple[str, str]]:
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=self._config.bridge.auth_timeout)
        except asyncio.TimeoutError:
            logger.warning("Bridge connection timed out waiting for auth.")
            return None

        if msg.type != web.WSMsgType.TEXT:
            return None

        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            return None

        if data.get("type") != protocol.TYPE_AUTH:
            return None

        token = data.get("token")
        raw_server_id = data.get("server_id")

        if raw_server_id is not None:
            server_id = str(raw_server_id)
            link = self._links_by_id.get(server_id)
            if link is None or token != link.secret:
                await ws.send_str(json.dumps({"type": protocol.TYPE_AUTH_FAIL}))
                logger.warning("Rejected bridge connection: unknown server_id or invalid token (%r).", server_id)
                return None
        else:
            if len(self._links_by_id) != 1:
                await ws.send_str(json.dumps({"type": protocol.TYPE_AUTH_FAIL}))
                logger.warning(
                    "Rejected bridge connection with no server_id: %d links are configured, so the "
                    "target server is ambiguous. Set server.id in the plugin's config.yml.",
                    len(self._links_by_id),
                )
                return None
            (link,) = self._links_by_id.values()
            if token != link.secret:
                await ws.send_str(json.dumps({"type": protocol.TYPE_AUTH_FAIL}))
                logger.warning("Rejected bridge connection with an invalid or missing auth token.")
                return None
            server_id = link.server_id

        await ws.send_str(json.dumps({"type": protocol.TYPE_AUTH_OK}))
        role = protocol.ROLE_PROXY if data.get("role") == protocol.ROLE_PROXY else ""
        return server_id, role

    async def _handle_message(self, server_id: str, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Received malformed bridge message from %r: %r", server_id, raw)
            return

        msg_type = data.get("type")
        try:
            if msg_type == protocol.TYPE_CHAT:
                await self._on_chat(server_id, str(data.get("player", "?")), str(data.get("message", "")))
            elif msg_type == protocol.TYPE_SERVER_START:
                server_name = str(data.get("server_name", "Minecraft Server"))
                if self._should_hold_server_start(server_id):
                    # Run apart from the receive loop so chat from this server isn't stalled meanwhile.
                    task = asyncio.create_task(self._deliver_server_start(server_id, server_name))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                else:
                    await self._on_server_start(server_id, server_name)
            elif msg_type == protocol.TYPE_SERVER_STOP:
                # The backend is going away; the next start must wait for a fresh check.
                self._proxy_restarting.add(server_id)
                await self._on_server_stop(server_id, str(data.get("server_name", "Minecraft Server")))
            elif msg_type == protocol.TYPE_PLAYER_JOIN:
                await self._on_player_join(server_id, str(data.get("player", "?")))
            elif msg_type == protocol.TYPE_PLAYER_LEAVE:
                await self._on_player_leave(server_id, str(data.get("player", "?")))
            elif msg_type == protocol.TYPE_PLAYERS_RESPONSE:
                future = self._pending_players.get((server_id, str(data.get("request_id", ""))))
                if future is not None and not future.done():
                    raw_players = str(data.get("players", ""))
                    players = [name for name in raw_players.split(",") if name]
                    try:
                        max_players: Optional[int] = int(data["max"])
                    except (KeyError, TypeError, ValueError):
                        max_players = None
                    future.set_result((players, max_players))
            else:
                logger.debug("Ignoring unknown bridge message type from %r: %s", server_id, msg_type)
        except Exception:
            logger.exception(
                "Failed to relay a %s message from %r to Discord; dropping just this message.",
                msg_type,
                server_id,
            )
