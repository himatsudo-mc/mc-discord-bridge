from bridge.discord_bot import _EMBED_DESCRIPTION_LIMIT, format_player_list


def test_format_player_list_empty():
    assert format_player_list([]) == "現在オンラインのプレイヤーはいません。"


def test_format_player_list_sorts_and_escapes():
    assert format_player_list(["steve", "Alex_1"]) == "\N{BULLET} Alex\\_1\n\N{BULLET} steve"


def test_format_player_list_truncates_to_embed_limit():
    players = [f"player{i:010d}" for i in range(500)]
    text = format_player_list(players)
    assert len(text) <= _EMBED_DESCRIPTION_LIMIT
    shown = text.count("\N{BULLET}")
    assert text.endswith(f"…他 {500 - shown} 人")


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


def _make_security_bot(security_channel_id):
    from bridge.config import BridgeSettings, Config, DiscordSettings, FormatSettings, LinkSettings
    from bridge.discord_bot import DiscordBridgeBot

    config = Config(
        token="x",
        discord=DiscordSettings(command_prefix="!"),
        bridge=BridgeSettings(host="127.0.0.1", port=0, auth_timeout=1, max_queue_size=1, max_queue_age_seconds=1),
        format=FormatSettings(minecraft_to_discord="{player}: {message}", player_join="{player}", player_leave="{player}"),
        links=[LinkSettings("default", "s", 1, 2, security_channel_id)],
    )
    bot = DiscordBridgeBot(config)
    channels = {}

    async def fake_get(channel_id):
        return channels.setdefault(channel_id, _FakeChannel())

    bot._get_text_channel = fake_get
    return bot, channels


async def test_security_alert_goes_only_to_security_channel():
    bot, channels = _make_security_bot(99)
    await bot.notify_security_alert(
        "default", {"kind": "banned_attempt", "address": "1.2.3.4", "player": "@everyone", "detail": "x"}
    )
    assert list(channels) == [99]
    embed = channels[99].sent[0]["embed"]
    assert "アク禁" in embed.title
    values = {f.name: f.value for f in embed.fields}
    assert values["IPアドレス"] == "1.2.3.4"
    assert "@everyone" not in values["プレイヤー名"].replace("@​everyone", "")


async def test_security_alert_not_posted_without_security_channel():
    bot, channels = _make_security_bot(0)
    await bot.notify_security_alert("default", {"kind": "rapid_connections", "address": "1.2.3.4"})
    assert channels == {}


def test_accessban_server_selection_is_never_ambiguous():
    from types import SimpleNamespace

    from bridge.config import BridgeSettings, Config, DiscordSettings, FormatSettings, LinkSettings
    from bridge.discord_bot import DiscordBridgeBot

    config = Config(
        token="x",
        discord=DiscordSettings(command_prefix="!"),
        bridge=BridgeSettings(host="127.0.0.1", port=0, auth_timeout=1, max_queue_size=1, max_queue_age_seconds=1),
        format=FormatSettings(minecraft_to_discord="{player}: {message}", player_join="{player}", player_leave="{player}"),
        # Both servers share security channel 9.
        links=[LinkSettings("a", "s", 1, 1, 9), LinkSettings("b", "s", 2, 2, 9)],
    )
    bot = DiscordBridgeBot(config)
    at = lambda channel_id: SimpleNamespace(channel_id=channel_id)
    assert bot._server_for_interaction(at(2), None) == "b"
    assert bot._server_for_interaction(at(9), None) is None
    assert bot._server_for_interaction(at(9), "a") == "a"
    assert bot._server_for_interaction(at(1), "nope") is None
