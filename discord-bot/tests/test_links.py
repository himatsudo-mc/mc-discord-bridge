from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.discord_bot import DiscordBridgeBot
from bridge.links import MCID_PATTERN, LinkStore


@pytest.fixture
def store(tmp_path):
    return LinkStore(tmp_path / "links.json")


def test_link_is_case_insensitive_and_persists(store, tmp_path):
    store.link("Steve", 1, 9)
    assert store.discord_id_of("steve") == 1
    assert store.mcid_of(1) == "Steve"
    assert LinkStore(tmp_path / "links.json").discord_id_of("STEVE") == 1


def test_relinking_a_user_replaces_their_old_mcid(store):
    store.link("Steve", 1, 9)
    assert store.link("Alex", 1, 9) == "Steve"
    assert store.discord_id_of("Steve") is None
    assert store.mcid_of(1) == "Alex"


def test_relinking_an_mcid_moves_it_to_the_new_user(store):
    store.link("Steve", 1, 9)
    store.link("Steve", 2, 9)
    assert store.discord_id_of("Steve") == 2
    assert store.mcid_of(1) is None


def test_unlink(store):
    store.link("Steve", 1, 9)
    assert store.unlink_user(1) == "Steve"
    assert store.unlink_user(1) is None
    store.link("Alex", 2, 9)
    assert store.unlink_mcid("alex") == 2
    assert store.discord_id_of("Alex") is None


def test_unreadable_file_refuses_to_start(tmp_path):
    path = tmp_path / "links.json"
    path.write_text("{not json")
    with pytest.raises(RuntimeError):
        LinkStore(path)


def test_mcid_pattern():
    assert MCID_PATTERN.match("Steve_01")
    assert MCID_PATTERN.match(".BedrockGuy")
    assert not MCID_PATTERN.match("bad name")
    assert not MCID_PATTERN.match("x" * 17)


def interaction(staff=True):
    perms = SimpleNamespace(manage_roles=staff, administrator=False)
    return SimpleNamespace(
        user=SimpleNamespace(id=99, guild_permissions=perms),
        response=SimpleNamespace(send_message=AsyncMock()),
    )


def bot_with(store):
    bot = DiscordBridgeBot.__new__(DiscordBridgeBot)
    bot.link_store = store
    return bot


async def test_link_command_links_and_requires_staff(store):
    bot = bot_with(store)
    user = SimpleNamespace(id=1, bot=False, mention="<@1>")
    denied = interaction(staff=False)
    await bot._link_slash(denied, user, "Steve")
    assert store.discord_id_of("Steve") is None
    assert "権限" in denied.response.send_message.call_args.args[0]

    ok = interaction()
    await bot._link_slash(ok, user, " Steve ")
    assert store.discord_id_of("Steve") == 1
    assert ok.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_link_command_rejects_bad_mcid_and_bots(store):
    bot = bot_with(store)
    await bot._link_slash(interaction(), SimpleNamespace(id=1, bot=False, mention="<@1>"), "bad name")
    await bot._link_slash(interaction(), SimpleNamespace(id=2, bot=True, mention="<@2>"), "Steve")
    assert store.discord_id_of("bad name") is None
    assert store.discord_id_of("Steve") is None


async def test_unlink_command_needs_exactly_one_target(store):
    bot = bot_with(store)
    store.link("Steve", 1, 9)
    both = interaction()
    await bot._unlink_slash(both, SimpleNamespace(id=1, mention="<@1>"), "Steve")
    assert store.discord_id_of("Steve") == 1
    await bot._unlink_slash(interaction(), None, "steve")
    assert store.discord_id_of("Steve") is None
