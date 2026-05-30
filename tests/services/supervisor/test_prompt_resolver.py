"""
Tests for the supervisor prompt resolver.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.supervisor.prompt_resolver import _is_resolvable_commit, resolve_system_prompt

# ---------------------------------------------------------------------------
# _is_resolvable_commit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version, expected",
    [
        ("unknown", False),
        ("", False),
        ("abcdef1234567890" * 2 + "abcdef12", True),  # 40-char clean hash
        ("abc1234", True),  # short clean hash
        ("abc1234-dirty", True),  # dirty hash still resolvable
        ("abc", False),  # too short
        ("not-a-hash", False),  # non-hex chars
    ],
)
def test_is_resolvable_commit(version: str, expected: bool):
    assert _is_resolvable_commit(version) == expected


# ---------------------------------------------------------------------------
# resolve_system_prompt — fallback when git is unavailable
# ---------------------------------------------------------------------------


def test_resolve_system_prompt_falls_back_to_current_loader_for_unknown():
    with patch(
        "app.services.prompt_loader.prompt_loader.get",
        return_value="fallback system prompt",
    ):
        result = resolve_system_prompt("planner", system_version="unknown")
    assert result == "fallback system prompt"


def test_resolve_system_prompt_falls_back_when_git_show_fails():
    with (
        patch(
            "app.services.supervisor.prompt_resolver._is_resolvable_commit",
            return_value=True,
        ),
        patch(
            "app.services.supervisor.prompt_resolver._git_show_prompt",
            return_value=None,  # git show failed
        ),
        patch(
            "app.services.prompt_loader.prompt_loader.get",
            return_value="current prompt content",
        ),
    ):
        result = resolve_system_prompt("planner", system_version="abc1234")
    assert result == "current prompt content"


def test_resolve_system_prompt_uses_git_content_when_available():
    with (
        patch(
            "app.services.supervisor.prompt_resolver._is_resolvable_commit",
            return_value=True,
        ),
        patch(
            "app.services.supervisor.prompt_resolver._git_show_prompt",
            return_value="historical prompt content",
        ),
    ):
        result = resolve_system_prompt("planner", system_version="abc1234")
    assert result == "historical prompt content"


def test_resolve_system_prompt_no_version_uses_current_loader():
    with patch(
        "app.services.prompt_loader.prompt_loader.get",
        return_value="current prompt",
    ):
        result = resolve_system_prompt("planner", system_version=None)
    assert result == "current prompt"
