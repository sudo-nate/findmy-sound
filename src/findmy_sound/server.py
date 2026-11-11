"""Stdlib-only HTTP service for findmy-sound.

Exposes a small authenticated API so Home Assistant (or any HTTP client
on the LAN) can trigger Play Sound without SSH:

  POST /ring    {"profile": "me"} or {"device_id": "..."} (Bearer required)
  GET  /healthz (no auth; service status and per-profile auth state)

Security bar (enforced, not aspirational):
  - Bearer token, from the service env file (FINDMY_BEARER_TOKEN).
    The server refuses to start without one.
  - Server-side device-ID allowlist: a request can only ring device IDs
    listed in the requesting profile's config. Anything else is rejected
    as if the device did not exist.
  - Rate limiting: max 1 ring per 10 seconds per profile, max 10 rings
    per minute overall; excess requests get 429 and are logged.
  - Binds 0.0.0.0:8765 (HA and the ESP32s are separate LAN hosts, so
    loopback-only will not work); the bearer token is what makes that
    safe. No TLS on the LAN (same posture as ESPHome's own API).
  - Logs contain redacted device IDs only — never full device IDs,
    bearer tokens, passwords, or cookies.

Run it with `findmy serve` (see cli.py) or `python -m findmy_sound.server`.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import urlsplit

from findmy_sound import auth
from findmy_sound.client import FindMyClient
from findmy_sound.config import Profile, ServerConfig, load_server_config
from findmy_sound.errors import (
    AuthenticationError,
    DeviceNotFoundError,
    FindMySoundError,
    ProfileNotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    SessionExpiredError,
    SoundUnavailableError,
    TwoFactorRequiredError,
)

_LOGGER = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
PER_PROFILE_MIN_SECONDS = 10.0
GLOBAL_MAX_PER_MINUTE = 10


def _redact(device_id: str) -> str:
    if len(device_id) <= 8:
        return "***"
    return f"{device_id[:4]}...{device_id[-4:]}"


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


class RateLimiter:
    """Per-profile + global ring rate limits. Thread-safe."""

    def __init__(
        self,
        per_profile_min_seconds: float = PER_PROFILE_MIN_SECONDS,
        global_max_per_minute: int = GLOBAL_MAX_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_profile_min_seconds = per_profile_min_seconds
        self._global_max_per_minute = global_max_per_minute
        self._clock = clock
        self._lock = threading.Lock()
        self._last_ring: dict[str, float] = {}
        self._recent: list[float] = []  # ring timestamps within the last minute

    def try_acquire(self, profile: str) -> bool:
        """Return True and record the ring if allowed, else False."""
        now = self._clock()
        with self._lock:
            last = self._last_ring.get(profile)
            if last is not None and now - last < self._per_profile_min_seconds:
                return False
            self._recent = [t for t in self._recent if now - t < 60.0]
            if len(self._recent) >= self._global_max_per_minute:
                return False
            self._last_ring[profile] = now
            self._recent.append(now)
            return True


def _default_client_factory(profile: Profile) -> FindMyClient:
    """Create a FindMyClient for one profile.

    Non-interactive: with a valid trusted session in the profile's data
    dir, no password is needed. If the session has expired, login raises
    and the server reports needs_reauth — the password is never stored
    at rest.
    """
    service = auth.login(
        profile.apple_id,
        data_dir=profile.data_dir,
        interactive=False,
        save_password=False,
    )
    return FindMyClient(service)


class RingService:
    """Profile-aware ring dispatch with auth-state tracking.

    Clients are created lazily per profile (first ring for that profile)
    and cached; a profile whose session has expired reports
    needs_reauth until a later ring succeeds.
    """

    def __init__(
        self,
        config: ServerConfig,
        client_factory: Optional[Callable[[Profile], FindMyClient]] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        self._config = config
        self._client_factory = client_factory or _default_client_factory
        self._clients: dict[str, FindMyClient] = {}
        self._lock = threading.Lock()
        self._rate_limiter = rate_limiter or RateLimiter()
        self._auth_state: dict[str, str] = {
            name: "ok" for name in config.profiles
        }
        self._last_ring: dict[str, float | None] = {
            name: None for name in config.profiles
        }

    def ring(
        self, profile: Optional[str] = None, device_id: Optional[str] = None
    ) -> dict:
        """Ring a device. Returns a small result dict on success.

        Raises ProfileNotFoundError, DeviceNotFoundError, ValueError
        (ambiguous or missing target), RateLimitedError, or the
        auth/service errors from the client layer.
        """
        target_profile, target_device = self._resolve(profile, device_id)
        if not self._rate_limiter.try_acquire(target_profile.name):
            _LOGGER.warning(
                "Rate limit exceeded for profile %s", target_profile.name
            )
            raise RateLimitedError(
                "Rate limit exceeded. Try again in a few seconds."
            )
        try:
            client = self._client_for(target_profile)
            client.play_sound(target_device)
        except (
            AuthenticationError,
            TwoFactorRequiredError,
            SessionExpiredError,
        ):
            self._set_auth_state(target_profile.name, "needs_reauth")
            raise
        self._set_auth_state(target_profile.name, "ok")
        self._last_ring[target_profile.name] = time.time()
        _LOGGER.info(
            "Play Sound triggered for profile %s device %s",
            target_profile.name,
            _redact(target_device),
        )
        return {
            "status": "ok",
            "profile": target_profile.name,
            "device_id": _redact(target_device),
        }

    def health(self) -> dict:
        """Service status for GET /healthz."""
        auth_state = (
            "needs_reauth"
            if "needs_reauth" in self._auth_state.values()
            else "ok"
        )
        profiles = {
            name: {
                "auth": self._auth_state[name],
                "last_ring": _iso(self._last_ring[name]),
            }
            for name in self._config.profiles
        }
        last = max(
            (t for t in self._last_ring.values() if t is not None),
            default=None,
        )
        return {
            "status": "ok",
            "auth": auth_state,
            "last_ring": _iso(last),
            "profiles": profiles,
        }

    def _resolve(
        self, profile: Optional[str], device_id: Optional[str]
    ) -> tuple[Profile, str]:
        if profile is not None:
            target = self._config.profiles.get(profile)
            if target is None:
                raise ProfileNotFoundError(profile)
            if device_id is not None:
                if device_id not in target.device_ids:
                    raise DeviceNotFoundError(device_id)
                return target, device_id
            if len(target.device_ids) == 1:
                return target, next(iter(target.device_ids))
            raise ValueError(
                f"Profile {profile!r} has {len(target.device_ids)} devices; "
                "specify device_id."
            )
        if device_id is not None:
            for candidate in self._config.profiles.values():
                if device_id in candidate.device_ids:
                    return candidate, device_id
            raise DeviceNotFoundError(device_id)
        raise ValueError("Specify profile or device_id.")

    def _client_for(self, profile: Profile) -> FindMyClient:
        with self._lock:
            client = self._clients.get(profile.name)
            if client is None:
                client = self._client_factory(profile)
                self._clients[profile.name] = client
            return client

    def _set_auth_state(self, profile: str, state: str) -> None:
        with self._lock:
            self._auth_state[profile] = state


class _Handler(BaseHTTPRequestHandler):
    """HTTP request handler. The server object carries the service."""

    server_version = "findmy-sound"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        _LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/healthz":
            self._send_json(200, self.server.service.health())
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/ring":
            self._send_json(404, {"error": "not_found"})
            return
        if not self._check_bearer():
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": "bad_request", "detail": str(exc)})
            return
        if not isinstance(payload, dict):
            self._send_json(
                400, {"error": "bad_request", "detail": "JSON body must be an object"}
            )
            return
        profile = payload.get("profile")
        device_id = payload.get("device_id")
        if profile is not None and not isinstance(profile, str):
            self._send_json(
                400, {"error": "bad_request", "detail": "profile must be a string"}
            )
            return
        if device_id is not None and not isinstance(device_id, str):
            self._send_json(
                400,
                {"error": "bad_request", "detail": "device_id must be a string"},
            )
            return
        try:
            result = self.server.service.ring(profile=profile, device_id=device_id)
        except RateLimitedError:
            self._send_json(429, {"error": "rate_limited"})
        except ProfileNotFoundError:
            self._send_json(404, {"error": "not_found", "detail": "unknown profile"})
        except DeviceNotFoundError:
            self._send_json(404, {"error": "not_found", "detail": "unknown device"})
        except (
            AuthenticationError,
            TwoFactorRequiredError,
            SessionExpiredError,
        ):
            self._send_json(503, {"error": "needs_reauth"})
        except (ServiceUnavailableError, SoundUnavailableError):
            self._send_json(503, {"error": "service_unavailable"})
        except ValueError as exc:
            self._send_json(400, {"error": "bad_request", "detail": str(exc)})
        else:
            self._send_json(200, result)

    def _check_bearer(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        token = header[len("Bearer "):].strip()
        if not token:
            return False
        return hmac.compare_digest(token, self.server.bearer_token)

    def _read_json(self) -> object:
        length = self.headers.get("Content-Length")
        if length is None:
            raise ValueError("missing Content-Length")
        try:
            size = int(length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if size > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(size)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("body is not valid JSON") from exc

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    """Threading HTTP server carrying the service and bearer token."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], service: RingService, bearer_token: str
    ) -> None:
        self.service = service
        self.bearer_token = bearer_token
        super().__init__(address, _Handler)


def create_server(
    config: ServerConfig,
    client_factory: Optional[Callable[[Profile], FindMyClient]] = None,
) -> _Server:
    """Create (but do not start) the HTTP server."""
    service = RingService(config, client_factory)
    return _Server((config.host, config.port), service, config.bearer_token)


def serve(
    config: ServerConfig,
    client_factory: Optional[Callable[[Profile], FindMyClient]] = None,
) -> None:
    """Serve forever (until Ctrl-C)."""
    server = create_server(config, client_factory)
    _LOGGER.info("findmy-sound listening on %s:%d", config.host, config.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m findmy_sound.server`."""
    parser = argparse.ArgumentParser(
        prog="findmy-sound-server",
        description="Authenticated HTTP service: ring Apple devices via iCloud.",
    )
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--bind", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        config = load_server_config(
            config_dir=args.config_dir,
            data_dir=args.data_dir,
            host=args.bind,
            port=args.port,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s"
    )
    serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
