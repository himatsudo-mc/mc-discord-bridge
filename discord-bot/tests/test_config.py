import os
import textwrap
from pathlib import Path

import pytest

from bridge.config import DEFAULT_SERVER_ID, load_config


def _write(tmp_path: Path, config_yml: str) -> tuple[Path, Path]:
    config_path = tmp_path / "config.yml"
    config_path.write_text(textwrap.dedent(config_yml), encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    return config_path, env_path


@pytest.fixture(autouse=True)
def _discord_token(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")


def test_legacy_single_server_config_synthesizes_default_link(tmp_path):
    config_path, env_path = _write(
        tmp_path,
        """
        discord:
          chat_channel_id: 111
          notify_channel_id: 222
        bridge:
          secret: "my-secret"
        """,
    )

    config = load_config(config_path, env_path)

    assert len(config.links) == 1
    link = config.links[0]
    assert link.server_id == DEFAULT_SERVER_ID
    assert link.secret == "my-secret"
    assert link.chat_channel_id == 111
    assert link.notify_channel_id == 222


def test_links_list_parses_multiple_servers(tmp_path):
    config_path, env_path = _write(
        tmp_path,
        """
        links:
          - server_id: "survival"
            secret: "survival-secret"
            chat_channel_id: 111
            notify_channel_id: 112
          - server_id: "creative"
            secret: "creative-secret"
            chat_channel_id: 222
            notify_channel_id: 223
        """,
    )

    config = load_config(config_path, env_path)

    assert len(config.links) == 2
    by_id = {link.server_id: link for link in config.links}
    assert by_id["survival"].chat_channel_id == 111
    assert by_id["survival"].secret == "survival-secret"
    assert by_id["creative"].chat_channel_id == 222
    assert by_id["creative"].secret == "creative-secret"


def test_empty_links_list_is_rejected_not_silently_ignored(tmp_path):
    # An empty links: must not fall through to the legacy single-server path,
    # which would surface a confusing "bridge.secret has not been set" error.
    config_path, env_path = _write(
        tmp_path,
        """
        links: []
        discord:
          chat_channel_id: 111
        bridge:
          secret: "my-secret"
        """,
    )

    with pytest.raises(RuntimeError, match="non-empty list"):
        load_config(config_path, env_path)


def test_links_with_duplicate_server_id_rejected(tmp_path):
    config_path, env_path = _write(
        tmp_path,
        """
        links:
          - server_id: "survival"
            secret: "secret-a"
            chat_channel_id: 111
            notify_channel_id: 111
          - server_id: "survival"
            secret: "secret-b"
            chat_channel_id: 222
            notify_channel_id: 222
        """,
    )

    with pytest.raises(RuntimeError, match="Duplicate links\\[\\].server_id"):
        load_config(config_path, env_path)


def test_links_with_duplicate_chat_channel_id_rejected(tmp_path):
    config_path, env_path = _write(
        tmp_path,
        """
        links:
          - server_id: "survival"
            secret: "secret-a"
            chat_channel_id: 111
            notify_channel_id: 111
          - server_id: "creative"
            secret: "secret-b"
            chat_channel_id: 111
            notify_channel_id: 222
        """,
    )

    with pytest.raises(RuntimeError, match="Duplicate links\\[\\].chat_channel_id"):
        load_config(config_path, env_path)


def test_links_with_placeholder_secret_rejected(tmp_path):
    config_path, env_path = _write(
        tmp_path,
        """
        links:
          - server_id: "survival"
            secret: "CHANGE_ME_SURVIVAL_SECRET"
            chat_channel_id: 111
            notify_channel_id: 111
        """,
    )

    with pytest.raises(RuntimeError, match="secret"):
        load_config(config_path, env_path)


def test_links_with_invalid_server_id_rejected(tmp_path):
    config_path, env_path = _write(
        tmp_path,
        """
        links:
          - server_id: "has a space"
            secret: "my-secret"
            chat_channel_id: 111
            notify_channel_id: 111
        """,
    )

    with pytest.raises(RuntimeError, match="server_id"):
        load_config(config_path, env_path)


def test_security_channel_id_is_parsed_for_legacy_and_links(tmp_path):
    config_path, env_path = _write(
        tmp_path,
        """
        discord:
          chat_channel_id: 1
          notify_channel_id: 2
          security_channel_id: 3
        bridge:
          secret: "real"
        """,
    )
    assert load_config(config_path, env_path).links[0].security_channel_id == 3

    config_path, env_path = _write(
        tmp_path,
        """
        links:
          - server_id: a
            secret: "real"
            chat_channel_id: 1
            security_channel_id: 7
          - server_id: b
            secret: "real"
            chat_channel_id: 2
        """,
    )
    links = load_config(config_path, env_path).links
    assert [l.security_channel_id for l in links] == [7, 0]
