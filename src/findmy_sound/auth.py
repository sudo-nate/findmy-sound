"""Authentication and session handling.

This module is the only place that constructs a pyicloud.PyiCloudService.
It is responsible for:
  - prompting for a password interactively (never reading it from a
    plaintext config/.env file by default)
  - storing/retrieving the password via the OS keychain (through
    pyicloud's built-in keyring integration)
  - driving the 2FA challenge/response loop
  - never logging or printing passwords, cookies, tokens, or full
    authentication response bodies

Session/cookie files are managed by pyicloud itself, which creates its
cookie directory with a restrictive umask (0o077) so files are not
world- or group-readable.
"""

from __future__ import annotations

import getpass
import logging
from typing import Callable, Optional

from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloudAPIResponseException,
    PyiCloudFailedLoginException,
)
from pyicloud.utils import get_password_from_keyring, store_password_in_keyring

from findmy_sound.errors import AuthenticationError, TwoFactorRequiredError

_LOGGER = logging.getLogger(__name__)

# Prompt callback type: given a prompt string, return the user's answer.
PromptFn = Callable[[str], str]


def default_code_prompt(prompt: str) -> str:
    """Default 2FA code prompt using plain input().

    If there's no interactive stdin (e.g. invoked headlessly from a Siri
    Shortcut, launchd job, or a physical button), input() raises EOFError
    instead of hanging forever. Treat that the same as an empty answer so
    the caller gets a clean TwoFactorRequiredError instead of a traceback.
    """
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def login(
    apple_id: str,
    *,
    password: Optional[str] = None,
    save_password: bool = True,
    code_prompt: PromptFn = default_code_prompt,
) -> PyiCloudService:
    """Authenticate to iCloud and return a ready-to-use PyiCloudService.

    Never logs or returns the raw password. Raises AuthenticationError on
    bad credentials and TwoFactorRequiredError only if a code_prompt is not
    able to resolve the challenge (default code_prompt always attempts to
    resolve it interactively).
    """
    if password is None:
        password = get_password_from_keyring(apple_id)

    if password is None:
        password = getpass.getpass(f"Apple ID password for {apple_id}: ")

    try:
        service = PyiCloudService(apple_id, password)
    except PyiCloudFailedLoginException as exc:
        raise AuthenticationError(
            "Invalid Apple ID or password. Please try again."
        ) from exc

    # PyiCloudService swallows the 2FA-required exception internally and
    # instead exposes it via the requires_2fa property, so we must check
    # for it explicitly rather than relying on a raised exception.
    if service.requires_2fa:
        _handle_2fa(service, code_prompt)

    if save_password:
        try:
            store_password_in_keyring(apple_id, password)
        except Exception:  # pragma: no cover - keyring backend issues
            _LOGGER.debug(
                "Could not save password to OS keychain; "
                "you may be prompted again next time."
            )

    return service


def _handle_2fa(service: PyiCloudService, code_prompt: PromptFn) -> None:
    """Drive the interactive two-factor authentication challenge."""
    code = code_prompt(
        "Two-factor authentication required. "
        "Enter the code sent to your trusted device: "
    )
    if not code:
        raise TwoFactorRequiredError(
            "A two-factor authentication code is required to continue."
        )

    try:
        valid = service.validate_2fa_code(code)
    except PyiCloudAPIResponseException as exc:
        raise AuthenticationError(
            "Failed to validate the two-factor authentication code."
        ) from exc

    if not valid:
        raise AuthenticationError("Incorrect two-factor authentication code.")

    # Trust this session so subsequent runs reuse it without re-prompting.
    service.trust_session()
