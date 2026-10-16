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
