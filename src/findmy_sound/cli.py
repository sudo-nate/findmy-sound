"""Command-line interface for findmy-sound.

    findmy devices
    findmy play-sound --device-id DEVICE_ID

This is CLI-only by design: no HTTP server is started, ever, from this
module. See the project README for the (not-yet-implemented) plan for a
future loopback-only local API.
"""

from __future__ import annotations

import argparse
import logging
import sys

from findmy_sound import auth
from findmy_sound.client import FindMyClient
from findmy_sound.errors import FindMySoundError
from findmy_sound.models import Device


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="findmy",
        description="List Apple devices and trigger Play Sound via iCloud Find Devices.",
    )
    parser.add_argument(
        "--apple-id",
        required=True,
        help="Apple ID email address to authenticate with.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("devices", help="List devices on this Apple Account.")

    play_sound_parser = subparsers.add_parser(
        "play-sound", help="Trigger Play Sound on a specific device."
    )
    play_sound_parser.add_argument(
        "--device-id",
        required=True,
        help="The immutable Apple device ID to play sound on (see 'findmy devices').",
    )
    play_sound_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )

    return parser


def _print_devices(devices: list[Device]) -> None:
    if not devices:
        print("No devices found.")
        return
    for device in devices:
        print(f"Name: {device.name}")
        print(f"Model: {device.model_name or device.model}")
        print(f"Device ID: {device.device_id}")
        print(f"Status: {device.status_label}")
        print(f"Battery: {device.battery_label}")
        print(f"Sound available: {'yes' if device.sound_available else 'no'}")
        print("-" * 40)


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        # No interactive stdin available (headless invocation). Default to
        # "no" so a script that forgets --yes fails safe instead of
        # silently triggering the device.
        return False
    return answer in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    try:
        service = auth.login(args.apple_id)
        client = FindMyClient(service)

        if args.command == "devices":
            _print_devices(client.list_devices())
            return 0

        if args.command == "play-sound":
            if not args.yes:
                target = client.get_device(args.device_id)
                if not _confirm(
                    f"Play sound on '{target.name}' ({target.model_name})?"
                ):
                    print("Cancelled.")
                    return 1
            client.play_sound(args.device_id)
            print(f"Play Sound triggered for device {args.device_id}.")
            return 0

    except FindMySoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.error("Unknown command")
    return 2  # pragma: no cover - argparse.error exits before this


if __name__ == "__main__":
    sys.exit(main())
