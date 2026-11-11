"""Tests for per-profile and server configuration.

No real Apple Account, network, or home directory is touched: all files
live under tmp_path and env vars are passed explicitly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from findmy_sound.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    load_profiles,
    load_server_config,
    parse_env_file,
)


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_parse_env_file_quotes_export_and_comments(tmp_path):
    path = _write(
        tmp_path / "ring-me.env",
        "# a comment\n"
        "\n"
        'export APPLE_ID="me@example.com"\n'
        "DEVICE_ID='device-aaa-111'\n",
    )
    assert parse_env_file(path) == {
        "APPLE_ID": "me@example.com",
        "DEVICE_ID": "device-aaa-111",
    }


def test_parse_env_file_values_are_literal_no_expansion(tmp_path):
    path = _write(tmp_path / "ring-me.env", 'DEVICE_ID="$HOME"\n')
    assert parse_env_file(path) == {"DEVICE_ID": "$HOME"}


def test_parse_env_file_malformed_line_raises(tmp_path):
    path = _write(tmp_path / "ring-me.env", "APPLE_ID\n")
    with pytest.raises(ValueError, match="expected KEY=value"):
        parse_env_file(path)


def test_parse_env_file_empty_key_raises(tmp_path):
    path = _write(tmp_path / "ring-me.env", "=oops\n")
    with pytest.raises(ValueError, match="empty key"):
        parse_env_file(path)


def test_load_profiles_named_and_legacy_files(tmp_path):
    _write(
        tmp_path / "ring-me.env",
        'APPLE_ID="me@example.com"\nDEVICE_ID="device-aaa-111"\n',
    )
    _write(
        tmp_path / "ring-partner.env",
        'APPLE_ID="partner@example.com"\nDEVICE_IDS="device-bbb-222, device-ccc-333"\n',
    )
    _write(
        tmp_path / "ring.env",
        'APPLE_ID="old@example.com"\nDEVICE_ID="device-ddd-444"\n',
    )
    profiles = load_profiles(tmp_path, tmp_path / "sessions")

    assert set(profiles) == {"me", "partner", "default"}
    assert profiles["me"].apple_id == "me@example.com"
    assert profiles["me"].device_ids == frozenset({"device-aaa-111"})
    assert profiles["partner"].device_ids == frozenset(
        {"device-bbb-222", "device-ccc-333"}
    )
    assert profiles["default"].apple_id == "old@example.com"
    # Each profile gets its own session store.
    assert profiles["me"].data_dir == str(tmp_path / "sessions" / "me")
    assert profiles["partner"].data_dir == str(tmp_path / "sessions" / "partner")


def test_load_profiles_device_id_and_ids_combine(tmp_path):
    _write(
        tmp_path / "ring-me.env",
        'APPLE_ID="me@example.com"\n'
        'DEVICE_ID="device-aaa-111"\n'
        'DEVICE_IDS="device-aaa-111, device-bbb-222"\n',
    )
    profiles = load_profiles(tmp_path, tmp_path / "sessions")
    assert profiles["me"].device_ids == frozenset(
        {"device-aaa-111", "device-bbb-222"}
    )


def test_load_profiles_missing_apple_id_raises(tmp_path):
    _write(tmp_path / "ring-me.env", 'DEVICE_ID="device-aaa-111"\n')
    with pytest.raises(ValueError, match="APPLE_ID is required"):
        load_profiles(tmp_path, tmp_path / "sessions")


def test_load_profiles_no_device_ids_raises(tmp_path):
    _write(tmp_path / "ring-me.env", 'APPLE_ID="me@example.com"\n')
    with pytest.raises(ValueError, match="DEVICE_ID or DEVICE_IDS"):
        load_profiles(tmp_path, tmp_path / "sessions")


def test_load_server_config_requires_bearer_token(tmp_path):
    with pytest.raises(ValueError, match="FINDMY_BEARER_TOKEN"):
        load_server_config(config_dir=str(tmp_path), env={})


def test_load_server_config_from_env(tmp_path):
    _write(
        tmp_path / "ring-me.env",
        'APPLE_ID="me@example.com"\nDEVICE_ID="device-aaa-111"\n',
    )
    config = load_server_config(
        config_dir=str(tmp_path),
        env={
            "FINDMY_BEARER_TOKEN": "token-from-env",
            "FINDMY_BIND": "127.0.0.1",
            "FINDMY_PORT": "9999",
            "FINDMY_DATA_DIR": str(tmp_path / "sessions"),
        },
    )
    assert config.bearer_token == "token-from-env"
    assert config.host == "127.0.0.1"
    assert config.port == 9999
    assert config.data_dir == str(tmp_path / "sessions")
    assert set(config.profiles) == {"me"}


def test_load_server_config_defaults(tmp_path):
    _write(
        tmp_path / "ring-me.env",
        'APPLE_ID="me@example.com"\nDEVICE_ID="device-aaa-111"\n',
    )
    config = load_server_config(
        config_dir=str(tmp_path),
        data_dir=str(tmp_path / "sessions"),
        env={"FINDMY_BEARER_TOKEN": "tok"},
    )
    assert config.host == DEFAULT_HOST
    assert config.port == DEFAULT_PORT


def test_load_server_config_explicit_args_win_over_env(tmp_path):
    _write(
        tmp_path / "ring-me.env",
        'APPLE_ID="me@example.com"\nDEVICE_ID="device-aaa-111"\n',
    )
    config = load_server_config(
        config_dir=str(tmp_path),
        data_dir=str(tmp_path / "sessions"),
        host="192.0.2.1",
        port=1234,
        bearer_token="explicit-token",
        env={
            "FINDMY_BEARER_TOKEN": "env-token",
            "FINDMY_BIND": "127.0.0.1",
            "FINDMY_PORT": "9999",
        },
    )
    assert config.bearer_token == "explicit-token"
    assert config.host == "192.0.2.1"
    assert config.port == 1234


def test_load_server_config_no_profiles_raises(tmp_path):
    with pytest.raises(ValueError, match="No profiles found"):
        load_server_config(
            config_dir=str(tmp_path),
            data_dir=str(tmp_path / "sessions"),
            env={"FINDMY_BEARER_TOKEN": "tok"},
        )
