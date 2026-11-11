"""Command-line interface for findmy-sound.

    findmy devices
    findmy play-sound --device-id DEVICE_ID
    findmy serve

`devices` and `play-sound` are the v1 CLI. `serve` starts the
small authenticated HTTP service (see server.py) that Home Assistant
calls directly; the service itself never prompts for a password or a
2FA code — a profile whose session has expired reports needs_reauth
until someone re-authenticates it interactively (see SETUP.md).
"""

from __future__ import annotations

import argparse
import logging
import sys

from findmy_sound import auth, server
from findmy_sound.client import FindMyClient
from findmy_sound.config import load_server_config
from findmy_sound.errors import FindMySoundError
from findmy_sound.models import Device


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="findmy",
        description="List Apple devices and trigger Play Sound via iCloud Find Devices.",
    )
    parser.add_argument(
        "--apple-id",
        default=None,
        help="Apple ID email address to authenticate with (devices, play-sound).",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Base directory for pyicloud session data "
        "(default: $FINDMY_DATA_DIR or ~/.local/state/findmy-sound/sessions).",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt for a password or 2FA code; fail cleanly instead.",
    )
    parser.add_argument(
        "--password-source",
        default="auto",
        choices=auth.PASSWORD_SOURCES,
        help="Where to get the Apple ID password from "
        "(default: auto = $FINDMY_PASSWORD, then keychain).",
    )
    parser.add_argument(
        "--password-file",
        default=None,
        help="Path to a 0600 file holding the password "
        "(with --password-source file).",
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

    serve_parser = subparsers.add_parser(
        "serve", help="Start the authenticated HTTP service (see server.py)."
    )
    serve_parser.add_argument(
        "--config-dir",
        default=None,
        help="Directory holding ring-<profile>.env files "
        "(default: ~/.config/findmy-sound).",
    )
    serve_parser.add_argument(
        "--bind",
        default=None,
        help="Address to listen on (default: $FINDMY_BIND or 0.0.0.0).",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on (default: $FINDMY_PORT or 8765).",
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

    if args.command == "serve":
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
        server.serve(config)
        return 0

    if not args.apple_id:
        parser.error("--apple-id is required for this command")

    try:
        service = auth.login(
            args.apple_id,
            data_dir=args.data_dir,
            interactive=not args.non_interactive,
            password_source=args.password_source,
            password_file=args.password_file,
        )
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
