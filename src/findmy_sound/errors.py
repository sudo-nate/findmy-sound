"""User-friendly error types for findmy-sound.

These wrap the lower-level pyicloud/requests exceptions so that callers
(cli.py and server.py in particular) only ever need to catch a small,
stable set of exception types instead of reaching into pyicloud internals.
"""

from __future__ import annotations


class FindMySoundError(Exception):
    """Base class for all findmy-sound errors."""


class AuthenticationError(FindMySoundError):
    """Raised when Apple ID authentication fails (bad credentials)."""


class TwoFactorRequiredError(FindMySoundError):
    """Raised when the session requires a two-factor code the caller must supply."""


class SessionExpiredError(FindMySoundError):
    """Raised when a previously trusted session is no longer valid."""


class DeviceNotFoundError(FindMySoundError):
    """Raised when a requested device ID does not match any known device."""

    def __init__(self, device_id: str) -> None:
        super().__init__(f"No device found with ID: {device_id}")
        self.device_id = device_id


class AmbiguousDeviceNameError(FindMySoundError):
    """Raised when a name-based lookup matches more than one device.

    The caller must be forced to disambiguate with an explicit device ID;
    this tool never guesses which device was meant.
    """

    def __init__(self, name: str, matching_ids: list[str]) -> None:
        super().__init__(
            f"Multiple devices match name '{name}'. "
            f"Re-run with an explicit --device-id. Candidates: {', '.join(matching_ids)}"
        )
        self.name = name
        self.matching_ids = matching_ids


class SoundUnavailableError(FindMySoundError):
    """Raised when the selected device does not support Play Sound."""

    def __init__(self, device_id: str) -> None:
        super().__init__(f"Play Sound is not available for device: {device_id}")
        self.device_id = device_id


class ServiceUnavailableError(FindMySoundError):
    """Raised when Apple's Find My service is unreachable or not activated."""


class MalformedResponseError(FindMySoundError):
    """Raised when Apple returns a response this tool cannot parse safely."""


class ProfileNotFoundError(FindMySoundError):
    """Raised when a requested profile name has no configuration."""

    def __init__(self, profile: str) -> None:
        super().__init__(f"No profile configured with name: {profile}")
        self.profile = profile


class RateLimitedError(FindMySoundError):
    """Raised when a ring request exceeds the service rate limit."""
