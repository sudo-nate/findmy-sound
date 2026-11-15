"""Stdlib-only HTTP service for findmy-sound.

Exposes a small authenticated API so Home Assistant (or any HTTP client
on the LAN) can trigger Play Sound without SSH:

  POST /ring    {"profile": "me"} or {"device_id": "..."} (Bearer required)
  GET  /healthz (no auth; service status and per-profile auth state)
  GET  / or /ring.html (no auth; home-screen button page — the page asks
         for the bearer token once, keeps it in the browser's localStorage,
         and POSTs to /ring from the same origin, so no CORS is involved.
         No token is ever baked into the page.)

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

# Home-screen button page (GET / and /ring.html). Dependency-free, no
# token baked in: the bearer token is entered once by the user and kept in
# the browser's localStorage on their own device.
RING_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Ring Phone">
<title>Ring My Phone</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: flex; flex-direction: column;
         align-items: center; justify-content: center; gap: 1.2rem;
         font-family: -apple-system, system-ui, sans-serif; background: #111;
         color: #eee; padding: 1.5rem; text-align: center; }
  h1 { font-size: 1.4rem; margin: 0; }
  #ring { width: min(22rem, 90vw); height: 6rem; font-size: 1.6rem;
          font-weight: 700; border: 0; border-radius: 1rem;
          background: #0a84ff; color: #fff; cursor: pointer; }
  #ring:disabled { background: #555; cursor: wait; }
  #status { min-height: 2.5em; font-size: 1.1rem; }
  #status.ok { color: #30d158; }
  #status.err { color: #ff453a; }
  #token-box { display: none; flex-direction: column; gap: 0.6rem;
               width: min(22rem, 90vw); }
  #token-box input { font-size: 1rem; padding: 0.7rem; border-radius: 0.6rem;
                     border: 1px solid #555; background: #222; color: #eee; }
  #token-box button { font-size: 1rem; padding: 0.7rem; border-radius: 0.6rem;
                      border: 0; background: #30d158; color: #000;
                      font-weight: 600; cursor: pointer; }
  #forget { background: none; border: 0; color: #888; font-size: 0.9rem;
            cursor: pointer; text-decoration: underline; }
  .hint { color: #888; font-size: 0.9rem; max-width: 22rem; }
</style>
</head>
<body>
<h1>🔔 Ring My Phone</h1>
<button id="ring" type="button">Ring my phone</button>
<p id="status" role="status"></p>
<div id="token-box">
  <p class="hint">First use: enter the findmy-sound bearer token. It is stored
  only in this browser (localStorage) and sent with each ring request.</p>
  <input id="token" type="password" placeholder="Bearer token"
         autocomplete="off" autocapitalize="off" spellcheck="false">
  <button id="save-token" type="button">Save token</button>
</div>
<button id="forget" type="button">Forget saved token</button>
<script>
  var KEY = "findmy-bearer-token";
  var statusEl = document.getElementById("status");
  var ringBtn = document.getElementById("ring");
  var tokenBox = document.getElementById("token-box");
  var tokenInput = document.getElementById("token");

  function setStatus(msg, kind) {
    statusEl.textContent = msg;
    statusEl.className = kind || "";
  }
  function showTokenBox() { tokenBox.style.display = "flex"; tokenInput.focus(); }

  document.getElementById("save-token").addEventListener("click", function () {
    var t = tokenInput.value.trim();
    if (!t) { setStatus("Enter the token first.", "err"); return; }
    localStorage.setItem(KEY, t);
    tokenInput.value = "";
    tokenBox.style.display = "none";
    setStatus("Token saved on this device. Tap the button to ring.", "ok");
  });

  document.getElementById("forget").addEventListener("click", function () {
    localStorage.removeItem(KEY);
    setStatus("Saved token forgotten.", "");
    showTokenBox();
  });

  ringBtn.addEventListener("click", async function () {
    var token = localStorage.getItem(KEY);
    if (!token) { setStatus("No token saved yet — enter it below.", "err"); showTokenBox(); return; }
    ringBtn.disabled = true;
    setStatus("Ringing…");
    try {
      var resp = await fetch("/ring", {
        method: "POST",
        headers: { "Content-Type": "application/json",
                   "Authorization": "Bearer " + token },
        body: JSON.stringify({ profile: "me" })
      });
      var data = await resp.json().catch(function () { return {}; });
      if (resp.ok) {
        setStatus("✅ Ringing! Listen for your phone.", "ok");
      } else if (resp.status === 401) {
        setStatus("❌ Token rejected (401). Re-enter it below.", "err");
        localStorage.removeItem(KEY);
        showTokenBox();
      } else if (resp.status === 429) {
        setStatus("⏳ Too soon — wait a few seconds and try again.", "err");
      } else if (data.error === "needs_reauth") {
        setStatus("❌ iCloud session expired — re-auth needed on the server.", "err");
      } else {
        setStatus("❌ Failed (" + resp.status + "): " + (data.error || "unknown error"), "err");
      }
    } catch (e) {
      setStatus("❌ Could not reach the service: " + e.message, "err");
    } finally {
      ringBtn.disabled = false;
    }
  });

  if (!localStorage.getItem(KEY)) { showTokenBox(); }
</script>
</body>
</html>
"""


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
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send_json(200, self.server.service.health())
        elif path in ("/", "/ring.html"):
            self._send_html(200, RING_PAGE_HTML)
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

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
