"""Tests for authentication/2FA/keyring flows.

All Apple interaction is mocked. No real Apple Account is ever used here,
per the project's testing strategy (mocked tests only; live smoke tests are
manual and documented separately in the README).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pyicloud.exceptions import PyiCloudFailedLoginException

from findmy_sound import auth
from findmy_sound.errors import AuthenticationError, TwoFactorRequiredError


def _mock_service(requires_2fa: bool = False) -> MagicMock:
    service = MagicMock()
    service.requires_2fa = requires_2fa
    return service


def test_login_uses_keyring_password_when_available():
    with patch.object(
        auth, "get_password_from_keyring", return_value="stored-secret"
    ) as mock_keyring, patch.object(
        auth, "PyiCloudService", return_value=_mock_service()
    ) as mock_service_cls, patch.object(
        auth, "store_password_in_keyring"
    ) as mock_store, patch("getpass.getpass") as mock_prompt:
        auth.login("user@example.com")

    mock_keyring.assert_called_once_with("user@example.com")
    mock_prompt.assert_not_called()
    mock_service_cls.assert_called_once_with("user@example.com", "stored-secret")
    mock_store.assert_called_once()


def test_login_prompts_for_password_when_not_in_keyring():
    with patch.object(
        auth, "get_password_from_keyring", return_value=None
    ), patch(
        "getpass.getpass", return_value="typed-secret"
    ) as mock_prompt, patch.object(
        auth, "PyiCloudService", return_value=_mock_service()
    ) as mock_service_cls, patch.object(auth, "store_password_in_keyring"):
        auth.login("user@example.com")

    mock_prompt.assert_called_once()
    mock_service_cls.assert_called_once_with("user@example.com", "typed-secret")


def test_login_never_logs_the_password(caplog):
    """Regression guard: password must never appear in log output."""
    secret = "super-secret-password-xyz"
    with patch.object(
        auth, "get_password_from_keyring", return_value=secret
    ), patch.object(
        auth, "PyiCloudService", return_value=_mock_service()
    ), patch.object(auth, "store_password_in_keyring"), patch(
        "getpass.getpass"
    ):
        with caplog.at_level("DEBUG"):
            auth.login("user@example.com")

    assert secret not in caplog.text


def test_login_bad_credentials_raises_authentication_error():
    with patch.object(
        auth, "get_password_from_keyring", return_value="wrong-secret"
    ), patch.object(
        auth,
        "PyiCloudService",
        side_effect=PyiCloudFailedLoginException("bad creds"),
    ):
        with pytest.raises(AuthenticationError):
            auth.login("user@example.com")


def test_login_handles_2fa_challenge_with_valid_code():
    service = _mock_service(requires_2fa=True)
    service.validate_2fa_code.return_value = True

    with patch.object(
        auth, "get_password_from_keyring", return_value="secret"
    ), patch.object(
        auth, "PyiCloudService", return_value=service
    ), patch.object(auth, "store_password_in_keyring"):
        result = auth.login(
            "user@example.com", code_prompt=lambda _: "123456"
        )

    service.validate_2fa_code.assert_called_once_with("123456")
    service.trust_session.assert_called_once()
    assert result is service


def test_login_handles_2fa_challenge_with_invalid_code():
    service = _mock_service(requires_2fa=True)
    service.validate_2fa_code.return_value = False

    with patch.object(
        auth, "get_password_from_keyring", return_value="secret"
    ), patch.object(auth, "PyiCloudService", return_value=service):
        with pytest.raises(AuthenticationError):
            auth.login("user@example.com", code_prompt=lambda _: "000000")


def test_login_empty_2fa_code_raises_two_factor_required():
    service = _mock_service(requires_2fa=True)

    with patch.object(
        auth, "get_password_from_keyring", return_value="secret"
    ), patch.object(auth, "PyiCloudService", return_value=service):
        with pytest.raises(TwoFactorRequiredError):
            auth.login("user@example.com", code_prompt=lambda _: "")


def test_default_code_prompt_handles_no_stdin_cleanly():
    """Headless invocation (no terminal attached) must not hang or crash."""
    with patch("builtins.input", side_effect=EOFError):
        assert auth.default_code_prompt("Enter code: ") == ""


# --- Auth refactor: data dir, credential sources, non-interactive mode ----


def test_login_passes_data_dir_as_cookie_directory():
    with patch.object(
        auth, "get_password_from_keyring", return_value="stored-secret"
    ), patch.object(
        auth, "PyiCloudService", return_value=_mock_service()
    ) as mock_service_cls, patch.object(auth, "store_password_in_keyring"):
        auth.login("user@example.com", data_dir="/tmp/sessions/me")

    mock_service_cls.assert_called_once_with(
        "user@example.com", "stored-secret", cookie_directory="/tmp/sessions/me"
    )


def test_login_data_dir_from_env(monkeypatch):
    monkeypatch.setenv("FINDMY_DATA_DIR", "/tmp/env-sessions")
    with patch.object(
        auth, "get_password_from_keyring", return_value="stored-secret"
    ), patch.object(
        auth, "PyiCloudService", return_value=_mock_service()
    ) as mock_service_cls, patch.object(auth, "store_password_in_keyring"):
        auth.login("user@example.com")

    mock_service_cls.assert_called_once_with(
        "user@example.com", "stored-secret", cookie_directory="/tmp/env-sessions"
    )


def test_login_env_password_source(monkeypatch):
    monkeypatch.setenv("FINDMY_PASSWORD", "env-secret")
    with patch.object(
        auth, "get_password_from_keyring"
    ) as mock_keyring, patch.object(
        auth, "PyiCloudService", return_value=_mock_service()
    ) as mock_service_cls, patch.object(
        auth, "store_password_in_keyring"
    ), patch("getpass.getpass") as mock_prompt:
        auth.login("user@example.com", password_source="env")

    mock_keyring.assert_not_called()
    mock_prompt.assert_not_called()
    mock_service_cls.assert_called_once_with("user@example.com", "env-secret")


def test_login_file_password_source(tmp_path):
    password_file = tmp_path / "pw.txt"
    password_file.write_text("file-secret\n", encoding="utf-8")
    password_file.chmod(0o600)
    with patch.object(
        auth, "PyiCloudService", return_value=_mock_service()
    ) as mock_service_cls, patch.object(auth, "store_password_in_keyring"), patch(
        "getpass.getpass"
    ) as mock_prompt:
        auth.login(
            "user@example.com",
            password_source="file",
            password_file=str(password_file),
        )

    mock_prompt.assert_not_called()
    mock_service_cls.assert_called_once_with("user@example.com", "file-secret")


def test_login_file_password_source_warns_on_loose_mode(tmp_path, caplog):
    password_file = tmp_path / "pw.txt"
    password_file.write_text("file-secret\n", encoding="utf-8")
    password_file.chmod(0o644)
    with patch.object(
        auth, "PyiCloudService", return_value=_mock_service()
    ), patch.object(auth, "store_password_in_keyring"):
        with caplog.at_level("DEBUG"):
            auth.login(
                "user@example.com",
                password_source="file",
                password_file=str(password_file),
            )

    assert "0600" in caplog.text


def test_login_file_password_source_requires_a_path():
    with pytest.raises(auth.AuthenticationError):
        auth.login("user@example.com", password_source="file")


def test_resolve_password_unknown_source_raises():
    with pytest.raises(ValueError, match="Unknown password source"):
        auth.resolve_password("user@example.com", "bogus")


def test_resolve_password_auto_prefers_env_over_keyring(monkeypatch):
    monkeypatch.setenv("FINDMY_PASSWORD", "env-secret")
    with patch.object(
        auth, "get_password_from_keyring", return_value="keyring-secret"
    ):
        assert auth.resolve_password("user@example.com") == "env-secret"


def test_login_non_interactive_2fa_raises_without_prompting():
    service = _mock_service(requires_2fa=True)
    with patch.object(
        auth, "get_password_from_keyring", return_value="secret"
    ), patch.object(
        auth, "PyiCloudService", return_value=service
    ), patch.object(auth, "store_password_in_keyring") as mock_store, patch(
        "getpass.getpass"
    ) as mock_prompt:
        with pytest.raises(TwoFactorRequiredError):
            auth.login(
                "user@example.com",
                interactive=False,
                code_prompt=lambda _: (_ for _ in ()).throw(
                    AssertionError("must not prompt for a 2FA code")
                ),
            )

    mock_prompt.assert_not_called()
    mock_store.assert_not_called()
    service.validate_2fa_code.assert_not_called()


def test_login_non_interactive_does_not_save_password():
    with patch.object(
        auth, "get_password_from_keyring", return_value="secret"
    ), patch.object(
        auth, "PyiCloudService", return_value=_mock_service()
    ), patch.object(auth, "store_password_in_keyring") as mock_store:
        auth.login("user@example.com", interactive=False)

    mock_store.assert_not_called()
