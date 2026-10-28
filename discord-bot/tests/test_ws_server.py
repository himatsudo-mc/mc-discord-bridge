import asyncio
import json

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from bridge.config import BridgeSettings, Config, DiscordSettings, FormatSettings, LinkSettings
from bridge.ws_server import BridgeServer


def make_config(links, max_queue_size=50, max_queue_age_seconds=30, auth_timeout=2.0, proxy_wait_seconds=0.5):
    return Config(
        token="x",
        discord=DiscordSettings(command_prefix="!"),
        bridge=BridgeSettings(
            host="127.0.0.1",
            port=0,
            auth_timeout=auth_timeout,
            max_queue_size=max_queue_size,
            max_queue_age_seconds=max_queue_age_seconds,
            proxy_wait_seconds=proxy_wait_seconds,
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
        self.proxy_links = []
        self.events = []
        self.fail_next_chats = 0

    async def on_chat(self, server_id, player, message):
        if self.fail_next_chats > 0:
            self.fail_next_chats -= 1
            raise RuntimeError("boom")
        self.chats.append((server_id, player, message))

    async def on_server_start(self, server_id, name):
        self.starts.append((server_id, name))
        self.events.append("start")

    async def on_proxy_link(self, server_id, status, backend):
        self.proxy_links.append((server_id, status, backend))
        self.events.append("proxy_" + status)

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
            on_proxy_link=recorder.on_proxy_link,
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


async def _connect_proxy(client, server_id, secret):
    ws = await client.ws_connect("/")
    await ws.send_str(json.dumps({"type": "auth", "token": secret, "server_id": server_id, "role": "proxy"}))
    reply = await ws.receive_json()
    assert reply["type"] == "auth_ok", reply
    return ws


async def _wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_proxy_connection_does_not_replace_minecraft_connection(bridge):
    server, client, _recorder = bridge
    ws_mc = await _connect(client, "survival", "s1")
    ws_proxy = await _connect_proxy(client, "survival", "s1")

    assert not ws_mc.closed
    await server.broadcast_to_minecraft("survival", "Alice", "hi")
    assert (await ws_mc.receive_json())["message"] == "hi"

    await ws_mc.close()
    await ws_proxy.close()


async def test_proxy_requires_valid_secret(bridge):
    _server, client, _recorder = bridge
    ws = await client.ws_connect("/")
    await ws.send_str(json.dumps({"type": "auth", "token": "bad", "server_id": "survival", "role": "proxy"}))
    assert (await ws.receive_json())["type"] == "auth_fail"


async def test_proxy_link_reported_only_on_change(bridge):
    _server, client, recorder = bridge
    ws = await _connect_proxy(client, "survival", "s1")
    msg = json.dumps({"type": "proxy_link", "status": "up", "backend": "home"})
    await ws.send_str(json.dumps({"type": "proxy_link", "status": "down", "backend": "home"}))
    await ws.send_str(msg)
    await ws.send_str(msg)
    await ws.send_str(json.dumps({"type": "proxy_link", "status": "down", "backend": "home"}))
    await _wait_for(lambda: len(recorder.proxy_links) == 2)
    assert [s for _id, s, _b in recorder.proxy_links] == ["up", "down"]
    await ws.close()


async def test_server_start_is_not_delayed_without_proxy(bridge):
    _server, client, recorder = bridge
    ws = await _connect(client, "survival", "s1")
    await ws.send_str(json.dumps({"type": "server_start", "server_name": "S"}))
    await _wait_for(lambda: recorder.starts)
    await ws.close()


async def test_server_start_waits_for_proxy_link(bridge):
    _server, client, recorder = bridge
    ws_mc = await _connect(client, "survival", "s1")
    ws_proxy = await _connect_proxy(client, "survival", "s1")

    await ws_mc.send_str(json.dumps({"type": "server_start", "server_name": "S"}))
    await asyncio.sleep(0.1)
    assert recorder.starts == []

    await ws_proxy.send_str(json.dumps({"type": "proxy_link", "status": "up", "backend": "home"}))
    await _wait_for(lambda: recorder.starts)
    assert recorder.events == ["proxy_up", "start"]
    await ws_mc.close()
    await ws_proxy.close()


async def test_server_start_goes_out_after_timeout_if_proxy_silent(bridge):
    _server, client, recorder = bridge
    ws_mc = await _connect(client, "survival", "s1")
    ws_proxy = await _connect_proxy(client, "survival", "s1")
    await ws_mc.send_str(json.dumps({"type": "server_start", "server_name": "S"}))
    await _wait_for(lambda: recorder.starts, timeout=3.0)
    assert recorder.events == ["start"]
    await ws_mc.close()
    await ws_proxy.close()


async def test_server_start_not_held_when_proxy_already_up(bridge):
    _server, client, recorder = bridge
    ws_mc = await _connect(client, "survival", "s1")
    ws_proxy = await _connect_proxy(client, "survival", "s1")
    await ws_proxy.send_str(json.dumps({"type": "proxy_link", "status": "down", "backend": "home"}))
    await ws_proxy.send_str(json.dumps({"type": "proxy_link", "status": "up", "backend": "home"}))
    await _wait_for(lambda: recorder.proxy_links)
    await ws_mc.send_str(json.dumps({"type": "server_start", "server_name": "S"}))
    await _wait_for(lambda: recorder.starts)
    assert recorder.events == ["proxy_up", "start"]

    # After a stop the next start must wait for a fresh check again.
    await ws_mc.send_str(json.dumps({"type": "server_stop", "server_name": "S"}))
    await _wait_for(lambda: recorder.stops)
    await ws_mc.send_str(json.dumps({"type": "server_start", "server_name": "S"}))
    await asyncio.sleep(0.1)
    assert len(recorder.starts) == 1
    await ws_proxy.send_str(json.dumps({"type": "proxy_link", "status": "up", "backend": "home"}))
    await _wait_for(lambda: len(recorder.starts) == 2)
    await ws_mc.close()
    await ws_proxy.close()


async def test_up_reports_during_shutdown_are_not_announced(bridge):
    # Velocity reports every few seconds, and the server keeps answering pings for a
    # moment after server_stop - that must not post "connected" right after "stopping".
    _server, client, recorder = bridge
    ws_mc = await _connect(client, "survival", "s1")
    ws_proxy = await _connect_proxy(client, "survival", "s1")
    up = json.dumps({"type": "proxy_link", "status": "up", "backend": "home"})
    await ws_proxy.send_str(json.dumps({"type": "proxy_link", "status": "down", "backend": "home"}))
    await ws_proxy.send_str(up)
    await _wait_for(lambda: recorder.proxy_links)

    await ws_mc.send_str(json.dumps({"type": "server_stop", "server_name": "S"}))
    await _wait_for(lambda: recorder.stops)
    await ws_proxy.send_str(up)
    await asyncio.sleep(0.1)
    assert recorder.events == ["proxy_up"]

    # Restarted faster than Velocity noticed: the next start still gets "connected" first.
    await ws_mc.send_str(json.dumps({"type": "server_start", "server_name": "S"}))
    await asyncio.sleep(0.1)
    await ws_proxy.send_str(up)
    await _wait_for(lambda: recorder.starts)
    assert recorder.events == ["proxy_up", "proxy_up", "start"]
    await ws_mc.close()
    await ws_proxy.close()


async def test_first_report_after_bot_start_is_silent(bridge):
    # A bot restart (or Velocity starting next to an already running server) must not
    # post "connected" out of the blue.
    _server, client, recorder = bridge
    ws_proxy = await _connect_proxy(client, "survival", "s1")
    await ws_proxy.send_str(json.dumps({"type": "proxy_link", "status": "up", "backend": "home"}))
    await asyncio.sleep(0.1)
    assert recorder.proxy_links == []
    await ws_proxy.close()


async def test_security_alert_from_proxy_is_relayed(make_bridge, two_links):
    alerts = []

    async def on_alert(server_id, data):
        alerts.append((server_id, data))

    server, client, _recorder = await make_bridge(two_links)
    server._on_security_alert = on_alert
    ws = await _connect_proxy(client, "survival", "s1")
    await ws.send_str(json.dumps({"type": "security_alert", "kind": "banned_attempt", "address": "1.2.3.4"}))
    await _wait_for(lambda: len(alerts) == 1)
    assert alerts[0][0] == "survival"
    assert alerts[0][1]["kind"] == "banned_attempt"
    await ws.close()


async def test_security_alert_ignored_from_minecraft_plugin(make_bridge, two_links):
    alerts = []

    async def on_alert(server_id, data):
        alerts.append(data)

    server, client, _recorder = await make_bridge(two_links)
    server._on_security_alert = on_alert
    ws = await _connect(client, "survival", "s1")
    await ws.send_str(json.dumps({"type": "security_alert", "kind": "banned_attempt"}))
    await asyncio.sleep(0.2)
    assert alerts == []
    await ws.close()
