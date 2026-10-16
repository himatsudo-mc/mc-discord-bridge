import asyncio
import json

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from bridge.config import BridgeSettings, Config, DiscordSettings, FormatSettings, LinkSettings
from bridge.ws_server import BridgeServer


def make_config(links, max_queue_size=50, max_queue_age_seconds=30, auth_timeout=2.0):
    return Config(
        token="x",
        discord=DiscordSettings(command_prefix="!"),
        bridge=BridgeSettings(
            host="127.0.0.1",
            port=0,
            auth_timeout=auth_timeout,
            max_queue_size=max_queue_size,
            max_queue_age_seconds=max_queue_age_seconds,
        ),
        format=FormatSettings(
            minecraft_to_discord="{player}: {message}",
            player_join="{player} joined",
            player_leave="{player} left",
        ),
        links=links,
    )


class Recorder:
    def __init__(self):
        self.chats = []
        self.starts = []
        self.stops = []
        self.joins = []
        self.leaves = []
        self.fail_next_chats = 0

    async def on_chat(self, server_id, player, message):
        if self.fail_next_chats > 0:
            self.fail_next_chats -= 1
            raise RuntimeError("boom")
        self.chats.append((server_id, player, message))

    async def on_server_start(self, server_id, name):
        self.starts.append((server_id, name))

    async def on_server_stop(self, server_id, name):
        self.stops.append((server_id, name))

    async def on_player_join(self, server_id, player):
        self.joins.append((server_id, player))

    async def on_player_leave(self, server_id, player):
        self.leaves.append((server_id, player))


@pytest.fixture
def two_links():
    return [
        LinkSettings(server_id="survival", secret="s1", chat_channel_id=1, notify_channel_id=1),
        LinkSettings(server_id="creative", secret="s2", chat_channel_id=2, notify_channel_id=2),
    ]


@pytest.fixture
async def make_bridge():
    created = []

    async def _make(links, **overrides):
        config = make_config(links, **overrides)
        recorder = Recorder()
        server = BridgeServer(
            config,
            on_chat=recorder.on_chat,
            on_server_start=recorder.on_server_start,
            on_server_stop=recorder.on_server_stop,
            on_player_join=recorder.on_player_join,
            on_player_leave=recorder.on_player_leave,
        )
        test_server = TestServer(server._app)
        client = TestClient(test_server)
        await client.start_server()
        created.append(client)
        return server, client, recorder

    yield _make

    for client in created:
        await client.close()


@pytest.fixture
async def bridge(make_bridge, two_links):
    return await make_bridge(two_links)


async def _connect(client, server_id, secret):
    ws = await client.ws_connect("/")
    await ws.send_str(json.dumps({"type": "auth", "token": secret, "server_id": server_id}))
    reply = await ws.receive_json()
    assert reply["type"] == "auth_ok", reply
    return ws


async def test_two_servers_connect_simultaneously(bridge):
    _server, client, _recorder = bridge
    ws_a = await _connect(client, "survival", "s1")
    ws_b = await _connect(client, "creative", "s2")

    assert not ws_a.closed
    assert not ws_b.closed

    await ws_a.close()
    await ws_b.close()


async def test_unknown_server_id_is_rejected(bridge):
    _server, client, _recorder = bridge
    ws = await client.ws_connect("/")
    await ws.send_str(json.dumps({"type": "auth", "token": "s1", "server_id": "nonexistent"}))
    reply = await ws.receive_json()
    assert reply["type"] == "auth_fail"


async def test_wrong_secret_is_rejected(bridge):
    _server, client, _recorder = bridge
    ws = await client.ws_connect("/")
    await ws.send_str(json.dumps({"type": "auth", "token": "wrong-secret", "server_id": "survival"}))
    reply = await ws.receive_json()
    assert reply["type"] == "auth_fail"


async def test_reconnect_same_server_id_only_closes_own_connection(bridge):
    _server, client, _recorder = bridge
    ws_a1 = await _connect(client, "survival", "s1")
    ws_b = await _connect(client, "creative", "s2")

    ws_a2 = await _connect(client, "survival", "s1")

    closing_msg = await ws_a1.receive()
    assert closing_msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)

    assert not ws_a2.closed
    assert not ws_b.closed

    await ws_a2.close()
    await ws_b.close()


async def test_broadcast_queues_while_disconnected_and_flushes_in_order_on_reconnect(bridge):
    server, client, _recorder = bridge

    await server.broadcast_to_minecraft("survival", "Alice", "hello")
    await server.broadcast_to_minecraft("survival", "Bob", "world")

    ws = await _connect(client, "survival", "s1")

    msg1 = await ws.receive_json()
    msg2 = await ws.receive_json()
    assert (msg1["user"], msg1["message"]) == ("Alice", "hello")
    assert (msg2["user"], msg2["message"]) == ("Bob", "world")

    await ws.close()


async def test_broadcast_to_one_server_does_not_affect_another(bridge):
    server, client, _recorder = bridge

    ws_creative = await _connect(client, "creative", "s2")
    await server.broadcast_to_minecraft("survival", "Alice", "hello")

    ws_survival = await _connect(client, "survival", "s1")
    msg = await ws_survival.receive_json()
    assert (msg["user"], msg["message"]) == ("Alice", "hello")

    await ws_creative.close()
    await ws_survival.close()


async def test_queue_overflow_evicts_oldest(make_bridge, two_links):
    server, client, _recorder = await make_bridge(two_links, max_queue_size=2)

    await server.broadcast_to_minecraft("survival", "Alice", "one")
    await server.broadcast_to_minecraft("survival", "Bob", "two")
    await server.broadcast_to_minecraft("survival", "Carol", "three")

    ws = await _connect(client, "survival", "s1")
    msg1 = await ws.receive_json()
    msg2 = await ws.receive_json()
    assert (msg1["user"], msg2["user"]) == ("Bob", "Carol")

    await ws.close()


async def test_stale_queued_messages_are_dropped_at_flush(make_bridge, two_links):
    server, client, _recorder = await make_bridge(two_links, max_queue_age_seconds=0.05)

    await server.broadcast_to_minecraft("survival", "Alice", "stale")
    await asyncio.sleep(0.2)
    await server.broadcast_to_minecraft("survival", "Bob", "fresh")

    ws = await _connect(client, "survival", "s1")
    msg = await ws.receive_json()
    assert msg["user"] == "Bob"

    await ws.close()


async def test_on_chat_exception_does_not_kill_the_connection(bridge):
    _server, client, recorder = bridge
    recorder.fail_next_chats = 1

    ws = await _connect(client, "survival", "s1")
    await ws.send_str(json.dumps({"type": "chat", "player": "Steve", "message": "boom"}))
    await ws.send_str(json.dumps({"type": "chat", "player": "Steve", "message": "hello"}))

    for _ in range(20):
        if recorder.chats:
            break
        await asyncio.sleep(0.05)

    assert recorder.chats == [("survival", "Steve", "hello")]
    assert not ws.closed

    await ws.close()


async def test_request_players_round_trip(bridge):
    server, client, _recorder = bridge
    ws = await _connect(client, "survival", "s1")

    task = asyncio.ensure_future(server.request_players("survival", timeout=2))
    req = await ws.receive_json()
    assert req["type"] == "players_request"
    await ws.send_str(
        json.dumps({"type": "players_response", "request_id": req["request_id"], "players": "Steve,Alex", "max": "20"})
    )
    assert await task == (["Steve", "Alex"], 20)
    assert not server._pending_players
    await ws.close()


async def test_request_players_not_connected_or_timeout(bridge):
    server, client, _recorder = bridge
    assert await server.request_players("survival", timeout=0.1) is None
    ws = await _connect(client, "survival", "s1")
    assert await server.request_players("survival", timeout=0.1) is None
    assert not server._pending_players
    await ws.close()


async def test_players_response_from_other_server_is_ignored(bridge):
    server, client, _recorder = bridge
    ws_a = await _connect(client, "survival", "s1")
    ws_b = await _connect(client, "creative", "s2")

    task = asyncio.ensure_future(server.request_players("survival", timeout=0.5))
    req = await ws_a.receive_json()
    # A different server echoing the request_id must not resolve survival's request.
    await ws_b.send_str(
        json.dumps({"type": "players_response", "request_id": req["request_id"], "players": "Mallory", "max": "1"})
    )
    assert await task is None
    await ws_a.close()
    await ws_b.close()
