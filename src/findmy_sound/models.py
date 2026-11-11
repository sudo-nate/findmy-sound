"""Normalized device data model.

Nothing outside this module (and client.py, which builds these) should ever
touch a raw pyicloud AppleDevice object. That keeps the pyicloud dependency
fully isolated behind client.py, per the adapter-pattern requirement in the
project outline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Device:
    """A single Apple device as reported by iCloud Find Devices."""

    device_id: str
    name: str
    model: str
    model_name: str
    battery_level: Optional[float]
    device_status: Optional[str]
    sound_available: bool

    @property
    def status_label(self) -> str:
        """Human-readable online/offline-ish status for display purposes.

        Apple's deviceStatus codes aren't officially documented; we only
        map the common ones and fall back to the raw value rather than
        guessing.
        """
        mapping = {
            "200": "Online",
            "201": "Offline",
            "203": "Pending",
            "204": "Unregistered",
        }
        if self.device_status is None:
            return "Unknown"
        return mapping.get(str(self.device_status), f"Unknown ({self.device_status})")

    @property
    def battery_label(self) -> str:
        if self.battery_level is None:
            return "N/A"
        return f"{round(self.battery_level * 100)}%"

    def redacted_id(self) -> str:
        """Return a redacted form of the device ID, safe for logs."""
        if len(self.device_id) <= 8:
            return "***"
        return f"{self.device_id[:4]}...{self.device_id[-4:]}"
