"""Per-profile and server configuration.

A profile IS a user: one Apple ID, one device allowlist, one session
store (data dir). v1 ships with a single profile (Nate); adding another
user means adding another profile file and running the same setup
workflow for that Apple ID — no code changes. See SETUP.md.

Profile files live in the config dir (default ~/.config/findmy-sound):
  ring-<profile>.env   -> profile "<profile>"
  ring.env             -> profile "default" (legacy single-device layout)

Each file is a shell-style KEY=value file (the same format the
ring-my-device.sh wrapper sources):
  APPLE_ID="you@icloud.com"
  DEVICE_ID="abc123..."             # single device, or
  DEVICE_IDS="abc123...,def456..."  # comma-separated allowlist

DEVICE_ID and DEVICE_IDS combine into the profile's allowlist. The
service only ever rings device IDs listed in the requesting profile's
allowlist.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_DIR = "~/.config/findmy-sound"
DEFAULT_DATA_DIR = "~/.local/state/findmy-sound/sessions"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765

BEARER_TOKEN_ENV = "FINDMY_BEARER_TOKEN"
BIND_ENV = "FINDMY_BIND"
PORT_ENV = "FINDMY_PORT"
DATA_DIR_ENV = "FINDMY_DATA_DIR"


@dataclass(frozen=True)
class Profile:
    """One user: Apple ID, device allowlist, and session store."""

    name: str
    apple_id: str
    device_ids: frozenset[str]
    data_dir: str


@dataclass
class ServerConfig:
    """Everything the HTTP service needs to start."""

    host: str
    port: int
    bearer_token: str
    profiles: dict[str, Profile]
    data_dir: str


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse a shell-style KEY=value file.

    Blank lines and `#` comments are skipped; an optional `export `
    prefix is accepted; values may be single- or double-quoted. No
    variable expansion is performed — values are literal.
    """
    values: dict[str, str] = {}
    for lineno, raw in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            raise ValueError(f"{path}:{lineno}: expected KEY=value, got {raw!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key:
            raise ValueError(f"{path}:{lineno}: empty key")
        values[key] = value
    return values


def load_profiles(
    config_dir: str | Path, data_dir: str | Path
) -> dict[str, Profile]:
    """Load every profile file in config_dir.

    `ring-<name>.env` defines profile `<name>`; `ring.env` defines
    profile "default". A file missing APPLE_ID or any device ID is a
    startup error — a silently ignored profile would mean a phone that
    never rings.
    """
    config_path = Path(config_dir).expanduser()
    base_data = Path(data_dir).expanduser()
    candidates = sorted(config_path.glob("ring-*.env"))
    legacy = config_path / "ring.env"
    if legacy.is_file():
        candidates = [legacy] + candidates  # deterministic order
    profiles: dict[str, Profile] = {}
    for path in candidates:
        name = (
            "default"
            if path.name == "ring.env"
            else path.name[len("ring-"):-len(".env")]
        )
        values = parse_env_file(path)
        apple_id = values.get("APPLE_ID", "").strip()
        if not apple_id:
            raise ValueError(f"{path}: APPLE_ID is required")
        ids: set[str] = set()
        if values.get("DEVICE_ID", "").strip():
            ids.add(values["DEVICE_ID"].strip())
        for item in values.get("DEVICE_IDS", "").split(","):
            item = item.strip()
            if item:
                ids.add(item)
        if not ids:
            raise ValueError(f"{path}: set DEVICE_ID or DEVICE_IDS")
        profiles[name] = Profile(
            name=name,
            apple_id=apple_id,
            device_ids=frozenset(ids),
            data_dir=str(base_data / name),
        )
    return profiles


def load_server_config(
    *,
    config_dir: str | None = None,
    data_dir: str | None = None,
    host: str | None = None,
    port: int | None = None,
    bearer_token: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ServerConfig:
    """Build the server configuration from explicit args and env vars.

    Explicit arguments win over env vars; env vars win over defaults.
    The bearer token has no default — the server refuses to start
    without one.
    """
    env = os.environ if env is None else env
    token = (bearer_token or env.get(BEARER_TOKEN_ENV) or "").strip()
    if not token:
        raise ValueError(
            "No bearer token configured. Set FINDMY_BEARER_TOKEN "
            "(e.g. in the service env file) before starting the server."
        )
    host = host or env.get(BIND_ENV) or DEFAULT_HOST
    if port is None:
        port = int(env.get(PORT_ENV, DEFAULT_PORT))
    resolved_config_dir = Path(config_dir or DEFAULT_CONFIG_DIR).expanduser()
    resolved_data_dir = Path(
        data_dir or env.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
    ).expanduser()
    profiles = load_profiles(resolved_config_dir, resolved_data_dir)
    if not profiles:
        raise ValueError(
            f"No profiles found in {resolved_config_dir}. "
            "Create ring-<profile>.env (see SETUP.md)."
        )
    return ServerConfig(
        host=host,
        port=port,
        bearer_token=token,
        profiles=profiles,
        data_dir=str(resolved_data_dir),
    )
