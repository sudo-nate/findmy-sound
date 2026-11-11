"""Shared test fakes.

These are lightweight stand-ins for pyicloud objects, implementing only the
surface area that findmy_sound.client.FindMyClient actually touches. No real
Apple Account or network access is ever used in tests.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from pyicloud.exceptions import PyiCloudServiceUnavailable


class FakeAppleDevice:
    def __init__(
        self,
        device_id: str,
        name: str,
        model: str = "iPhone14,2",
        model_name: str = "iPhone",
        battery_level: Optional[float] = 0.72,
        device_status: str = "200",
        sound_available: bool = True,
    ) -> None:
        self.id = device_id
        self.model = model
        self.model_name = model_name
        self.sound_available = sound_available
        self._status = {
            "name": name,
            "deviceDisplayName": model_name,
            "deviceStatus": device_status,
            "batteryLevel": battery_level,
        }
        self.play_sound_calls: list[str] = []

    def status(self, additional: Any = None) -> dict:
        return dict(self._status)

    def play_sound(self, subject: str = "Find My iPhone Alert") -> None:
        if not self.sound_available:
            raise PyiCloudServiceUnavailable("Sound is not available for this device")
        self.play_sound_calls.append(self.id)


class FakeDevicesManager:
    """Stands in for pyicloud's FindMyiPhoneServiceManager."""

    def __init__(self, devices: dict[str, FakeAppleDevice]) -> None:
        self._devices = devices

    @property
    def devices(self) -> dict[str, FakeAppleDevice]:
        return self._devices


class FakePyiCloudService:
    """Stands in for pyicloud.PyiCloudService."""

    def __init__(self, devices: dict[str, FakeAppleDevice]) -> None:
        self.devices = FakeDevicesManager(devices)
        self.requires_2fa = False


@pytest.fixture
def two_devices() -> dict[str, FakeAppleDevice]:
    return {
        "device-aaa-111": FakeAppleDevice(
            "device-aaa-111", name="Nate's iPhone", model_name="iPhone"
        ),
        "device-bbb-222": FakeAppleDevice(
            "device-bbb-222", name="Nate's MacBook", model_name="MacBook Pro",
            sound_available=False,
        ),
    }


@pytest.fixture
def duplicate_name_devices() -> dict[str, FakeAppleDevice]:
    return {
        "device-aaa-111": FakeAppleDevice("device-aaa-111", name="iPhone"),
        "device-ccc-333": FakeAppleDevice("device-ccc-333", name="iPhone"),
    }


@pytest.fixture
def fake_service(two_devices):
    return FakePyiCloudService(two_devices)
