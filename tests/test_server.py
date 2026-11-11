"""Tests for the authenticated HTTP service (server.py).

Two layers: RingService unit tests (no sockets) and loopback HTTP tests
against a real ThreadingHTTPServer bound to 127.0.0.1:0. All iCloud
interaction is faked — no real Apple Account or network access.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

import pytest

from findmy_sound.config import Profile, ServerConfig
from findmy_sound.errors import (
    AuthenticationError,
    DeviceNotFoundError,
    ProfileNotFoundError,
    RateLimitedError,
)
from findmy_sound.server import (
    RateLimiter,
    RingService,
    _redact,
    create_server,
)

TOKEN = "test-bearer-token"
FULL_ID_ME = "device-aaa-111"
FULL_ID_PARTNER_1 = "device-bbb-222"
FULL_ID_PARTNER_2 = "device-ccc-333"


class FakeClient:
    """Stands in for FindMyClient; records rings, optionally fails."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.rang: list[str] = []
        self.fail_with = fail_with

    def play_sound(self, device_id: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.rang.append(device_id)


def _config() -> ServerConfig:
    return ServerConfig(
        host="127.0.0.1",
        port=0,
        bearer_token=TOKEN,
        profiles={
            "me": Profile(
                name="me",
                apple_id="me@example.com",
                device_ids=frozenset({FULL_ID_ME}),
                data_dir="/tmp/x/me",
            ),
            "partner": Profile(
                name="partner",
                apple_id="partner@example.com",
                device_ids=frozenset({FULL_ID_PARTNER_1, FULL_ID_PARTNER_2}),
                data_dir="/tmp/x/partner",
            ),
        },
        data_dir="/tmp/x",
    )


def _service(**kwargs) -> tuple[RingService, dict[str, FakeClient]]:
    fakes: dict[str, FakeClient] = {}

    def factory(profile: Profile) -> FakeClient:
        fake = FakeClient()
        fakes[profile.name] = fake
        return fake

    return RingService(_config(), client_factory=factory, **kwargs), fakes


# --- RingService unit tests -------------------------------------------------


def test_ring_by_profile_single_device():
    service, fakes = _service()
    result = service.ring(profile="me")

    assert result["status"] == "ok"
    assert result["profile"] == "me"
    assert result["device_id"] == _redact(FULL_ID_ME)
    assert FULL_ID_ME not in json.dumps(result)
    assert fakes["me"].rang == [FULL_ID_ME]


def test_ring_by_device_id_resolves_profile():
    service, fakes = _service()
    result = service.ring(device_id=FULL_ID_PARTNER_2)

    assert result["profile"] == "partner"
    assert fakes["partner"].rang == [FULL_ID_PARTNER_2]
    assert "me" not in fakes  # no client created for the other profile


def test_ring_unknown_profile():
    service, _ = _service()
    with pytest.raises(ProfileNotFoundError):
        service.ring(profile="nobody")


def test_ring_device_not_in_profile_allowlist():
    service, _ = _service()
    with pytest.raises(DeviceNotFoundError):
        service.ring(profile="me", device_id=FULL_ID_PARTNER_1)


def test_ring_unknown_device_id():
    service, _ = _service()
    with pytest.raises(DeviceNotFoundError):
        service.ring(device_id="device-zzz-999")


def test_ring_multi_device_profile_requires_device_id():
    service, _ = _service()
    with pytest.raises(ValueError, match="specify device_id"):
        service.ring(profile="partner")


def test_ring_with_no_target_raises():
    service, _ = _service()
    with pytest.raises(ValueError, match="Specify profile or device_id"):
        service.ring()


def test_rate_limiter_per_profile():
    limiter = RateLimiter(per_profile_min_seconds=10.0)
    assert limiter.try_acquire("me") is True
    assert limiter.try_acquire("me") is False  # too soon
    assert limiter.try_acquire("partner") is True  # other profile unaffected


def test_rate_limiter_global_cap():
    limiter = RateLimiter(per_profile_min_seconds=0, global_max_per_minute=2)
    assert limiter.try_acquire("a") is True
    assert limiter.try_acquire("b") is True
    assert limiter.try_acquire("c") is False


def test_ring_second_too_soon_is_rate_limited():
    service, _ = _service()
    service.ring(profile="me")
    with pytest.raises(RateLimitedError):
        service.ring(profile="me")


def test_health_initial_state():
    service, _ = _service()
    health = service.health()

    assert health["status"] == "ok"
    assert health["auth"] == "ok"
    assert health["last_ring"] is None
    assert health["profiles"]["me"] == {"auth": "ok", "last_ring": None}


def test_health_reports_last_ring():
    service, _ = _service()
    service.ring(profile="me")
    health = service.health()

    assert health["last_ring"] is not None
    assert health["profiles"]["me"]["last_ring"] is not None
    assert health["profiles"]["partner"]["last_ring"] is None


def test_auth_failure_marks_needs_reauth_then_recovers():
    service, fakes = _service(
        rate_limiter=RateLimiter(per_profile_min_seconds=0)
    )
    service.ring(profile="me")  # creates and caches the fake client
    fakes["me"].fail_with = AuthenticationError("session expired")

    with pytest.raises(AuthenticationError):
        service.ring(profile="me")
    assert service.health()["auth"] == "needs_reauth"
    assert service.health()["profiles"]["me"]["auth"] == "needs_reauth"

    fakes["me"].fail_with = None
    service.ring(profile="me")
    assert service.health()["auth"] == "ok"


def test_ring_logs_redacted_device_id_only(caplog):
    service, _ = _service()
    with caplog.at_level(logging.INFO, logger="findmy_sound.server"):
        service.ring(profile="me")

    assert FULL_ID_ME not in caplog.text
    assert _redact(FULL_ID_ME) in caplog.text


# --- Loopback HTTP tests ----------------------------------------------------


@pytest.fixture
def running_server():
    fakes: dict[str, FakeClient] = {}
    failures: dict[str, Exception] = {}

    def factory(profile: Profile) -> FakeClient:
        fake = FakeClient(fail_with=failures.get(profile.name))
        fakes[profile.name] = fake
        return fake

    server = create_server(_config(), client_factory=factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, fakes, failures
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(server, method, path, body=None, token=None):
    port = server.server_address[1]
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method
    )
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data=data, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), exc.headers


def test_healthz_needs_no_auth(running_server):
    server, _, _ = running_server
    status, body, _ = _request(server, "GET", "/healthz")
    assert status == 200
    assert body["status"] == "ok"
    assert body["auth"] == "ok"


def test_unknown_path_404(running_server):
    server, _, _ = running_server
    status, body, _ = _request(server, "GET", "/nope")
    assert status == 404
    assert body["error"] == "not_found"


def test_ring_requires_bearer_token(running_server):
    server, _, _ = running_server
    status, body, _ = _request(server, "POST", "/ring", {"profile": "me"})
    assert status == 401
    assert body["error"] == "unauthorized"

    status, _, _ = _request(
        server, "POST", "/ring", {"profile": "me"}, token="wrong-token"
    )
    assert status == 401


def test_ring_success_over_http(running_server):
    server, fakes, _ = running_server
    status, body, _ = _request(
        server, "POST", "/ring", {"profile": "me"}, token=TOKEN
    )
    assert status == 200
    assert body["status"] == "ok"
    assert body["profile"] == "me"
    assert FULL_ID_ME not in json.dumps(body)
    assert fakes["me"].rang == [FULL_ID_ME]


def test_ring_unknown_device_404_over_http(running_server):
    server, _, _ = running_server
    status, body, _ = _request(
        server, "POST", "/ring", {"device_id": "device-zzz-999"}, token=TOKEN
    )
    assert status == 404
    assert body["error"] == "not_found"


def test_ring_unknown_profile_404_over_http(running_server):
    server, _, _ = running_server
    status, body, _ = _request(
        server, "POST", "/ring", {"profile": "nobody"}, token=TOKEN
    )
    assert status == 404
    assert body["detail"] == "unknown profile"


def test_ring_malformed_json_400(running_server):
    server, _, _ = running_server
    status, body, _ = _request(
        server, "POST", "/ring", b"{not json", token=TOKEN
    )
    assert status == 400
    assert body["error"] == "bad_request"


def test_ring_empty_body_400(running_server):
    server, _, _ = running_server
    status, body, _ = _request(server, "POST", "/ring", {}, token=TOKEN)
    assert status == 400
    assert body["error"] == "bad_request"


def test_ring_rate_limited_429_over_http(running_server):
    server, _, _ = running_server
    status, _, _ = _request(
        server, "POST", "/ring", {"profile": "me"}, token=TOKEN
    )
    assert status == 200
    status, body, _ = _request(
        server, "POST", "/ring", {"profile": "me"}, token=TOKEN
    )
    assert status == 429
    assert body["error"] == "rate_limited"


def test_ring_needs_reauth_503_over_http(running_server):
    server, _, failures = running_server
    failures["partner"] = AuthenticationError("session expired")
    status, body, _ = _request(
        server,
        "POST",
        "/ring",
        {"profile": "partner", "device_id": FULL_ID_PARTNER_1},
        token=TOKEN,
    )
    assert status == 503
    assert body["error"] == "needs_reauth"

    status, body, _ = _request(server, "GET", "/healthz")
    assert body["auth"] == "needs_reauth"
    assert body["profiles"]["partner"]["auth"] == "needs_reauth"


def test_http_logs_and_headers_never_leak_secrets(running_server, caplog):
    server, _, _ = running_server
    with caplog.at_level(logging.INFO, logger="findmy_sound.server"):
        status, _, headers = _request(
            server, "POST", "/ring", {"profile": "me"}, token=TOKEN
        )
    assert status == 200
    assert FULL_ID_ME not in caplog.text
    assert TOKEN not in caplog.text
    assert "Python" not in headers.get("Server", "")
