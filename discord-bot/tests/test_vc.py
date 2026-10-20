import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bridge.config import LinkSettings
from bridge.http_api import ROUTE_AREA_ROLE, ROUTE_VC_CREATE, BridgeHttpApi
from bridge.links import LinkStore
from bridge.vc_manager import DynamicVcManager, VcError, sanitize_channel_name


class FakeMember:
    def __init__(self, uid):
        self.id = uid
        self.add_roles = AsyncMock()
        self.remove_roles = AsyncMock()


def make_guild():
    guild = MagicMock(spec=discord.Guild)
    guild.id = 10
    guild.default_role = MagicMock(name="everyone")
    guild.me = MagicMock(name="me")
    guild.get_member.side_effect = lambda uid: FakeMember(uid) if uid in (1, 2) else None
    guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "nope"))
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = 555
    channel.guild = guild
    guild.create_voice_channel = AsyncMock(return_value=channel)
    return guild, channel


def make_bot(guild):
    category = MagicMock(spec=discord.CategoryChannel)
    category.guild = guild
    bot = MagicMock()
    bot.get_channel.side_effect = lambda cid: category if cid == 99 else None
    return bot


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "vcs.json"


async def test_create_friend_vc_is_private_and_tracked(state_file):
    guild, channel = make_guild()
    manager = DynamicVcManager(make_bot(guild), state_file, 300)
    result = await manager.create("friend", "Friend-Steve", 99, 1, [2, 3])
    assert result is channel
    _, kwargs = guild.create_voice_channel.call_args
    overwrites = kwargs["overwrites"]
    assert overwrites[guild.default_role].view_channel is False
    allowed = [t for t in overwrites if getattr(t, "id", None) in (1, 2)]
    assert len(allowed) == 2  # user 3 is not in the guild and is skipped
    assert manager.is_tracked(555)
    assert json.loads(state_file.read_text())["555"] > 0


async def test_create_public_vc_has_no_overwrites(state_file):
    guild, _ = make_guild()
    manager = DynamicVcManager(make_bot(guild), state_file, 300)
    await manager.create("public", "Public-Steve", 99, 1, [])
    assert guild.create_voice_channel.call_args.kwargs["overwrites"] == {}


async def test_create_rejects_bad_input(state_file):
    guild, _ = make_guild()
    manager = DynamicVcManager(make_bot(guild), state_file, 300)
    with pytest.raises(VcError):
        await manager.create("bogus", "x", 99, 1, [])
    with pytest.raises(VcError):
        await manager.create("party", "   ", 99, 1, [])
    with pytest.raises(VcError) as missing:
        await manager.create("party", "x", 12345, 1, [])
    assert missing.value.status == 404
    with pytest.raises(VcError):  # owner not in guild
        await manager.create("party", "x", 99, 7, [])


def voice_channel(channel_id, members):
    ch = MagicMock()
    ch.id = channel_id
    ch.members = members
    ch.delete = AsyncMock()
    return ch


async def test_deleted_when_last_member_leaves(state_file):
    manager = DynamicVcManager(MagicMock(), state_file, 300)
    manager._channels[5] = time.time()
    ch = voice_channel(5, [])
    await manager.on_voice_state_update(None, SimpleNamespace(channel=ch), SimpleNamespace(channel=None))
    ch.delete.assert_awaited_once()
    assert not manager.is_tracked(5)


async def test_not_deleted_while_occupied_or_untracked_or_moved_within(state_file):
    manager = DynamicVcManager(MagicMock(), state_file, 300)
    manager._channels[5] = time.time()
    occupied = voice_channel(5, [object()])
    await manager.on_voice_state_update(None, SimpleNamespace(channel=occupied), SimpleNamespace(channel=None))
    occupied.delete.assert_not_awaited()
    other = voice_channel(6, [])
    await manager.on_voice_state_update(None, SimpleNamespace(channel=other), SimpleNamespace(channel=None))
    other.delete.assert_not_awaited()
    # mute/deafen toggles keep the same channel and must never delete it
    empty = voice_channel(5, [])
    await manager.on_voice_state_update(None, SimpleNamespace(channel=empty), SimpleNamespace(channel=empty))
    empty.delete.assert_not_awaited()


async def test_sweep_respects_grace_and_forgets_vanished(state_file):
    bot = MagicMock()
    old, fresh = voice_channel(1, []), voice_channel(2, [])
    bot.get_channel.side_effect = {1: old, 2: fresh}.get
    bot.is_ready.return_value = True
    manager = DynamicVcManager(bot, state_file, 300)
    manager._channels.update({1: time.time() - 1000, 2: time.time(), 3: time.time()})
    await manager.sweep()
    old.delete.assert_awaited_once()
    fresh.delete.assert_not_awaited()
    assert not manager.is_tracked(3)
    assert manager.is_tracked(2)


async def test_state_survives_restart(state_file):
    state_file.write_text(json.dumps({"7": 123.0}))
    assert DynamicVcManager(MagicMock(), state_file, 1).is_tracked(7)


def test_sanitize_channel_name():
    assert sanitize_channel_name("  a \n b  ") == "a b"
    assert len(sanitize_channel_name("x" * 500)) == 100


@pytest.fixture
async def api_client(state_file):
    guild, channel = make_guild()
    role = MagicMock()
    guild.get_role.side_effect = lambda rid: role if rid == 77 else None
    member = FakeMember(1)
    guild.get_member.side_effect = lambda uid: member if uid == 1 else FakeMember(uid)
    bot = MagicMock()
    category = MagicMock(spec=discord.CategoryChannel)
    category.guild = guild
    vc = SimpleNamespace(guild=guild)
    bot.get_channel.side_effect = {99: category, 200: vc}.get
    manager = DynamicVcManager(bot, state_file, 300)
    store = LinkStore(state_file.with_name("links.json"))
    store.link("Steve", 1, 0)
    store.link("Alex", 2, 0)
    app = web.Application()
    BridgeHttpApi(bot, {"default": LinkSettings("default", "s3cret", 1, 1)}, manager, store).add_routes(app)
    async with TestClient(TestServer(app)) as client:
        client.member, client.role, client.guild = member, role, guild
        yield client


async def test_api_requires_valid_token(api_client):
    body = {"server_id": "default", "token": "wrong"}
    for route in (ROUTE_VC_CREATE, ROUTE_AREA_ROLE):
        resp = await api_client.post(route, json=body)
        assert resp.status == 401
    resp = await api_client.post(ROUTE_VC_CREATE, json={"server_id": "other", "token": "s3cret"})
    assert resp.status == 401


async def test_api_vc_create(api_client):
    resp = await api_client.post(ROUTE_VC_CREATE, json={
        "server_id": "default", "token": "s3cret", "type": "party", "name": "Party-Steve",
        "category_id": "99", "owner_mcid": "steve", "member_mcids": ["Alex", "Nobody"],
    })
    data = await resp.json()
    assert resp.status == 200 and data["ok"] is True
    assert data["url"] == "https://discord.com/channels/10/555"
    assert data["linked_mcids"] == ["Alex"]
    assert data["unlinked_mcids"] == ["Nobody"]


async def test_api_vc_create_requires_linked_owner(api_client):
    resp = await api_client.post(ROUTE_VC_CREATE, json={
        "server_id": "default", "token": "s3cret", "type": "public", "name": "x",
        "category_id": "99", "owner_mcid": "Nobody",
    })
    assert resp.status == 404
    assert (await resp.json())["code"] == "not_linked"


async def test_api_vc_create_rejects_non_numeric_ids(api_client):
    resp = await api_client.post(ROUTE_VC_CREATE, json={
        "server_id": "default", "token": "s3cret", "type": "party", "name": "x",
        "category_id": "abc", "owner_mcid": "Steve",
    })
    assert resp.status == 400


async def test_api_area_role_add_and_remove(api_client):
    base = {"server_id": "default", "token": "s3cret", "mcid": "Steve", "role_id": "77", "channel_id": "200"}
    assert (await api_client.post(ROUTE_AREA_ROLE, json={**base, "action": "add"})).status == 200
    api_client.member.add_roles.assert_awaited_once()
    assert (await api_client.post(ROUTE_AREA_ROLE, json={**base, "action": "remove"})).status == 200
    api_client.member.remove_roles.assert_awaited_once()
    assert (await api_client.post(ROUTE_AREA_ROLE, json={**base, "action": "zap"})).status == 400
    assert (await api_client.post(ROUTE_AREA_ROLE, json={**base, "action": "add", "role_id": "1"})).status == 404
    assert (await api_client.post(ROUTE_AREA_ROLE, json={**base, "action": "add", "channel_id": "5"})).status == 404


async def test_api_area_role_unlinked(api_client):
    base = {"server_id": "default", "token": "s3cret", "mcid": "Nobody", "role_id": "77", "channel_id": "200"}
    resp = await api_client.post(ROUTE_AREA_ROLE, json={**base, "action": "add"})
    assert resp.status == 404 and (await resp.json())["code"] == "not_linked"
    # Removing from someone who is not linked has nothing to do and is not an error.
    assert (await api_client.post(ROUTE_AREA_ROLE, json={**base, "action": "remove"})).status == 200
