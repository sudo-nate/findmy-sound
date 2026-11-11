"""Authentication and session handling.

This module is the only place that constructs a pyicloud.PyiCloudService.
It is responsible for:
  - resolving the Apple ID password from a pluggable source (env var,
    0600 secrets file, OS keychain, or an interactive prompt)
  - driving the 2FA challenge/response loop
  - never logging or printing passwords, cookies, tokens, or full
    authentication response bodies

Session/cookie files are managed by pyicloud itself, which creates its
cookie directory with a restrictive umask (0o077) so files are not
world- or group-readable. By default pyicloud uses a temp directory for
cookies; pass an explicit data_dir (or set FINDMY_DATA_DIR) so the
trusted session survives restarts and lives in a known place.

No password is stored at rest by default. With a valid trusted session
in the data dir, login needs no password at all — pyicloud validates
the stored session token. The password is only required when the
session has expired, and then it is resolved at (re-)auth time.
"""

from __future__ import annotations

import getpass
import logging
import os
import stat
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

# Env var holding the Apple ID password, when the env credential source
# is used. Only read at (re-)auth time, never logged.
PASSWORD_ENV_VAR = "FINDMY_PASSWORD"
# Env var holding the base directory for pyicloud session/cookie data.
DATA_DIR_ENV_VAR = "FINDMY_DATA_DIR"

PASSWORD_SOURCES = ("auto", "env", "file", "keyring", "prompt")


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


def resolve_password(
    apple_id: str,
    source: str = "auto",
    *,
    password: Optional[str] = None,
    password_file: Optional[str] = None,
    env_var: str = PASSWORD_ENV_VAR,
) -> Optional[str]:
    """Resolve an Apple ID password from the configured source.

    Sources: "auto" (env var, then keychain), "env" (env var only),
    "file" (a 0600 secrets file), "keyring" (OS keychain only),
    "prompt" (no source — the caller prompts, or fails cleanly when
    non-interactive). An explicitly passed password always wins.

    Returns None when no source yields a password. Never logs it.
    """
    if password is not None:
        return password
    if source not in PASSWORD_SOURCES:
        raise ValueError(
            f"Unknown password source: {source!r} "
            f"(expected one of {', '.join(PASSWORD_SOURCES)})"
        )
    if source == "env":
        return os.environ.get(env_var) or None
    if source == "file":
        if not password_file:
            raise AuthenticationError(
                "Password source 'file' requires a password file path."
            )
        return _read_password_file(password_file)
    if source == "keyring":
        return get_password_from_keyring(apple_id)
    if source == "prompt":
        return None
    # auto: env var first, then keychain, then nothing.
    return os.environ.get(env_var) or get_password_from_keyring(apple_id)


def _read_password_file(path: str) -> str:
    """Read a password from a secrets file.

    The file should contain just the password (plus a trailing
    newline) and be mode 0600. A looser mode only earns a warning —
    the file is still read — so operators are not locked out by an
    accidental chmod, but the misconfiguration is visible in logs.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        raise AuthenticationError(
            f"Could not read password file {path}: {exc}"
        ) from exc
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        _LOGGER.warning(
            "Password file %s is readable by group/other (mode %o); "
            "it should be 0600.",
            path,
            mode,
        )
    password = content.strip()
    if not password:
        raise AuthenticationError(f"Password file {path} is empty.")
    return password


def login(
    apple_id: str,
    *,
    password: Optional[str] = None,
    save_password: Optional[bool] = None,
    code_prompt: PromptFn = default_code_prompt,
    interactive: bool = True,
    data_dir: Optional[str] = None,
    password_source: str = "auto",
    password_file: Optional[str] = None,
) -> PyiCloudService:
    """Authenticate to iCloud and return a ready-to-use PyiCloudService.

    Never logs or returns the raw password. Raises AuthenticationError on
    bad credentials and TwoFactorRequiredError when a 2FA code is needed
    but cannot be obtained (non-interactive mode never prompts — it
    fails cleanly instead).

    With a valid trusted session in the data dir, no password is needed:
    pyicloud validates the stored session token. `save_password`
    defaults to True when interactive and False otherwise, so the
    service path never stores anything at rest.
    """
    if data_dir is None:
        data_dir = os.environ.get(DATA_DIR_ENV_VAR) or None
    password = resolve_password(
        apple_id,
        password_source,
        password=password,
        password_file=password_file,
    )
    if password is None and interactive:
        password = getpass.getpass(f"Apple ID password for {apple_id}: ")
    if save_password is None:
        save_password = interactive

    kwargs: dict = {}
    if data_dir is not None:
        kwargs["cookie_directory"] = data_dir
    try:
        service = PyiCloudService(apple_id, password, **kwargs)
    except PyiCloudFailedLoginException as exc:
        raise AuthenticationError(
            "Invalid Apple ID or password. Please try again."
        ) from exc

    # PyiCloudService swallows the 2FA-required exception internally and
    # instead exposes it via the requires_2fa property, so we must check
    # for it explicitly rather than relying on a raised exception.
    if service.requires_2fa:
        if interactive:
            _handle_2fa(service, code_prompt)
        else:
            raise TwoFactorRequiredError(
                "A two-factor authentication code is required to continue. "
                "Re-authenticate interactively (see SETUP.md)."
            )

    if save_password and password is not None:
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
