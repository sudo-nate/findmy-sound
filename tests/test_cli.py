"""Tests for cli.py behavior, particularly headless invocation safety."""

from __future__ import annotations

from unittest.mock import patch

from findmy_sound.cli import _confirm


def test_confirm_yes():
    with patch("builtins.input", return_value="y"):
        assert _confirm("Play sound?") is True


def test_confirm_no():
    with patch("builtins.input", return_value="n"):
        assert _confirm("Play sound?") is False


def test_confirm_default_is_no():
    with patch("builtins.input", return_value=""):
        assert _confirm("Play sound?") is False


def test_confirm_fails_safe_with_no_stdin():
    """Headless invocation (no terminal attached) must default to 'no',
    not hang or raise. --yes is the intended way to skip confirmation
    entirely in that context."""
    with patch("builtins.input", side_effect=EOFError):
        assert _confirm("Play sound?") is False
