"""Tests for device listing and selection.

These verify: correct device IDs are shown, ambiguous-name lookups are
rejected, missing devices raise a clear error, and listing devices never
triggers a sound request.
"""

from __future__ import annotations

import pytest

from findmy_sound.client import FindMyClient
from findmy_sound.errors import AmbiguousDeviceNameError, DeviceNotFoundError
from tests.conftest import FakePyiCloudService


def test_list_devices_returns_normalized_models(fake_service):
    client = FindMyClient(fake_service)
    devices = client.list_devices()

    ids = {d.device_id for d in devices}
    assert ids == {"device-aaa-111", "device-bbb-222"}

    iphone = next(d for d in devices if d.device_id == "device-aaa-111")
    assert iphone.name == "Nate's iPhone"
    assert iphone.model_name == "iPhone"
    assert iphone.sound_available is True
    assert iphone.battery_label == "72%"
    assert iphone.status_label == "Online"


def test_listing_devices_never_calls_play_sound(fake_service, two_devices):
    client = FindMyClient(fake_service)
    client.list_devices()

    for device in two_devices.values():
        assert device.play_sound_calls == []


def test_get_device_by_id(fake_service):
    client = FindMyClient(fake_service)
    device = client.get_device("device-bbb-222")
    assert device.name == "Nate's MacBook"


def test_get_device_missing_id_raises(fake_service):
    client = FindMyClient(fake_service)
    with pytest.raises(DeviceNotFoundError):
        client.get_device("does-not-exist")


def test_find_by_name_ambiguous_is_rejected(duplicate_name_devices):
    service = FakePyiCloudService(duplicate_name_devices)
    client = FindMyClient(service)

    with pytest.raises(AmbiguousDeviceNameError) as excinfo:
        client.find_by_name("iPhone")

    # Must not silently pick one; must surface both candidate IDs.
    assert set(excinfo.value.matching_ids) == {"device-aaa-111", "device-ccc-333"}


def test_find_by_name_unique_match_succeeds(fake_service):
    client = FindMyClient(fake_service)
    device = client.find_by_name("Nate's iPhone")
    assert device.device_id == "device-aaa-111"


def test_find_by_name_no_match_raises(fake_service):
    client = FindMyClient(fake_service)
    with pytest.raises(DeviceNotFoundError):
        client.find_by_name("Nonexistent Device")
