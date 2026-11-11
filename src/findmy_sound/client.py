"""FindMyClient: the public interface used by cli.py.

All pyicloud-specific behavior lives behind this class. If Apple changes
its private API, or pyicloud becomes unmaintained, only this file (and
auth.py) should need to change.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloudAPIResponseException,
    PyiCloudAuthRequiredException,
    PyiCloudNoDevicesException,
    PyiCloudServiceUnavailable,
)
from requests.exceptions import RequestException

from findmy_sound.errors import (
    AmbiguousDeviceNameError,
    DeviceNotFoundError,
    MalformedResponseError,
    ServiceUnavailableError,
    SessionExpiredError,
    SoundUnavailableError,
)
from findmy_sound.models import Device

_LOGGER = logging.getLogger(__name__)


class FindMyClient:
    """Public interface for listing devices and triggering Play Sound."""

    def __init__(self, service: PyiCloudService) -> None:
        self._service = service

    def list_devices(self) -> List[Device]:
        """Return the list of devices associated with this Apple Account.

        This performs a read-only refresh; it never triggers any device
        action.
        """
        try:
            manager = self._service.devices
        except PyiCloudNoDevicesException as exc:
            raise ServiceUnavailableError(
                "No devices were returned by iCloud Find Devices."
            ) from exc
        except PyiCloudServiceUnavailable as exc:
            raise ServiceUnavailableError(
                "Find My iPhone service is not available for this account."
            ) from exc
        except PyiCloudAuthRequiredException as exc:
            raise SessionExpiredError(
                "Your session has expired. Please re-authenticate."
            ) from exc
        except RequestException as exc:
            raise ServiceUnavailableError(
                "Could not reach Apple's iCloud service (network error)."
            ) from exc

        devices: List[Device] = []
        for device_id, apple_device in manager.devices.items():
            devices.append(self._to_model(device_id, apple_device))
        return devices

    def get_device(self, device_id: str) -> Device:
        """Look up a single device by its immutable Apple device ID."""
        for device in self.list_devices():
            if device.device_id == device_id:
                return device
        raise DeviceNotFoundError(device_id)

    def find_by_name(self, name: str) -> Device:
        """Look up a device by display name.

        Refuses ambiguous matches rather than guessing. Prefer get_device()
        with an explicit device_id wherever possible.
        """
        matches = [d for d in self.list_devices() if d.name == name]
        if not matches:
            raise DeviceNotFoundError(name)
        if len(matches) > 1:
            raise AmbiguousDeviceNameError(name, [d.device_id for d in matches])
        return matches[0]

    def play_sound(self, device_id: str) -> None:
        """Trigger Play Sound on exactly the device with the given ID.

        Requires an explicit device ID (never a display name) so that
        renamed or duplicate-named devices can't cause the wrong device
        to be triggered.
        """
        try:
            manager = self._service.devices
        except (
            PyiCloudNoDevicesException,
            PyiCloudServiceUnavailable,
        ) as exc:
            raise ServiceUnavailableError(
                "Find My iPhone service is not available."
            ) from exc
        except PyiCloudAuthRequiredException as exc:
            raise SessionExpiredError(
                "Your session has expired. Please re-authenticate."
            ) from exc

        devices_map = manager.devices
        if device_id not in devices_map:
            raise DeviceNotFoundError(device_id)

        apple_device = devices_map[device_id]

        try:
            apple_device.play_sound()
        except PyiCloudServiceUnavailable as exc:
            raise SoundUnavailableError(device_id) from exc
        except PyiCloudAPIResponseException as exc:
            raise ServiceUnavailableError(
                "Apple rejected the Play Sound request."
            ) from exc
        except RequestException as exc:
            raise ServiceUnavailableError(
                "Could not reach Apple's iCloud service (network error)."
            ) from exc

        _LOGGER.info("Play Sound triggered for device %s", _redact(device_id))

    @staticmethod
    def _to_model(device_id: str, apple_device) -> Device:
        try:
            status = apple_device.status()
        except (KeyError, TypeError) as exc:
            raise MalformedResponseError(
                f"Unexpected device data from Apple for device {_redact(device_id)}"
            ) from exc

        battery = status.get("batteryLevel")
        return Device(
            device_id=device_id,
            name=status.get("name") or "",
            model=apple_device.model,
            model_name=apple_device.model_name,
            battery_level=battery if isinstance(battery, (int, float)) else None,
            device_status=status.get("deviceStatus"),
            sound_available=bool(apple_device.sound_available),
        )


def _redact(device_id: str) -> str:
    if len(device_id) <= 8:
        return "***"
    return f"{device_id[:4]}...{device_id[-4:]}"
