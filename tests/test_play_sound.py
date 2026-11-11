"""Tests for the play_sound path.

Verify: the correct (and only the correct) device ID is sent, invalid IDs
are rejected instead of falling back to any device, and devices without
sound support raise a clear error instead of silently doing nothing.
"""

from __future__ import annotations

import pytest

from findmy_sound.client import FindMyClient
from findmy_sound.errors import DeviceNotFoundError, SoundUnavailableError


def test_play_sound_triggers_only_requested_device(fake_service, two_devices):
    client = FindMyClient(fake_service)
    client.play_sound("device-aaa-111")

    assert two_devices["device-aaa-111"].play_sound_calls == ["device-aaa-111"]
    assert two_devices["device-bbb-222"].play_sound_calls == []


def test_play_sound_invalid_device_id_raises_and_does_not_fallback(
    fake_service, two_devices
):
    client = FindMyClient(fake_service)

    with pytest.raises(DeviceNotFoundError):
        client.play_sound("totally-unknown-id")

    # Must not have silently triggered sound on any real device instead.
    for device in two_devices.values():
        assert device.play_sound_calls == []


def test_play_sound_unavailable_raises_clear_error(fake_service):
    client = FindMyClient(fake_service)

    with pytest.raises(SoundUnavailableError):
        client.play_sound("device-bbb-222")  # sound_available=False in fixture


def test_play_sound_requires_explicit_device_id_argument():
    """play_sound has no name-based or default-device overload."""
    import inspect

    sig = inspect.signature(FindMyClient.play_sound)
    params = list(sig.parameters)
    assert params == ["self", "device_id"]
