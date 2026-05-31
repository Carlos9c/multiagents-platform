"""Tests for QA strategy selector — Phase 3."""

from __future__ import annotations

import pytest

from app.services.qa.strategies.base import QAStrategy
from app.services.qa.strategy_selector import (
    QA_ELIGIBLE_PRODUCT_TYPES,
    is_qa_eligible,
    select,
)

# ── QA_ELIGIBLE_PRODUCT_TYPES ─────────────────────────────────────────────────


def test_eligible_types_contains_all_software_types():
    expected = {
        "web_app",
        "rest_api",
        "graphql_api",
        "cli_tool",
        "mobile_android",
        "library",
        "desktop_app",
    }
    assert expected <= QA_ELIGIBLE_PRODUCT_TYPES


def test_eligible_types_excludes_non_software():
    non_software = {"written_content", "music", "visual_content", "data_product", "game_assets"}
    for t in non_software:
        assert t not in QA_ELIGIBLE_PRODUCT_TYPES


def test_eligible_types_is_frozenset():
    assert isinstance(QA_ELIGIBLE_PRODUCT_TYPES, frozenset)


# ── is_qa_eligible ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "product_type",
    ["web_app", "rest_api", "graphql_api", "cli_tool", "mobile_android", "library", "desktop_app"],
)
def test_is_qa_eligible_true_for_software(product_type: str):
    assert is_qa_eligible(product_type) is True


@pytest.mark.parametrize(
    "product_type",
    ["written_content", "music", "visual_content", "data_product", "game_assets"],
)
def test_is_qa_eligible_false_for_non_software(product_type: str):
    assert is_qa_eligible(product_type) is False


def test_is_qa_eligible_false_for_none():
    assert is_qa_eligible(None) is False


def test_is_qa_eligible_false_for_unknown():
    assert is_qa_eligible("unknown_type") is False


# ── select ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "product_type",
    ["web_app", "rest_api", "graphql_api", "cli_tool", "mobile_android", "library", "desktop_app"],
)
def test_select_returns_strategy_for_eligible_type(product_type: str):
    strategy = select(product_type)
    assert isinstance(strategy, QAStrategy)
    assert strategy.product_type == product_type


def test_select_raises_for_non_eligible():
    with pytest.raises(ValueError, match="No QA strategy registered"):
        select("music")


def test_select_raises_for_unknown():
    with pytest.raises(ValueError, match="No QA strategy registered"):
        select("completely_unknown")


# ── Per-strategy properties ───────────────────────────────────────────────────


def test_mobile_android_requires_compiled_artifact():
    strategy = select("mobile_android")
    assert strategy.requires_compiled_artifact is True


def test_web_app_does_not_require_compiled_artifact():
    strategy = select("web_app")
    assert strategy.requires_compiled_artifact is False


def test_rest_api_does_not_require_compiled_artifact():
    strategy = select("rest_api")
    assert strategy.requires_compiled_artifact is False


def test_all_strategies_have_synthesis_agent():
    """Every strategy must allow synthesis_agent — it always runs last."""
    for product_type in QA_ELIGIBLE_PRODUCT_TYPES:
        strategy = select(product_type)
        assert (
            "synthesis_agent" in strategy.allowed_agents
        ), f"Strategy for {product_type!r} is missing synthesis_agent"


def test_all_strategies_have_at_least_two_agents():
    """Every strategy must allow at least one probing agent plus synthesis."""
    for product_type in QA_ELIGIBLE_PRODUCT_TYPES:
        strategy = select(product_type)
        assert (
            len(strategy.allowed_agents) >= 2
        ), f"Strategy for {product_type!r} has fewer than 2 agents"


def test_is_agent_allowed_true_for_listed_agent():
    strategy = select("web_app")
    assert strategy.is_agent_allowed("functional_tester") is True


def test_is_agent_allowed_false_for_unlisted_agent():
    strategy = select("cli_tool")
    assert strategy.is_agent_allowed("apk_installer") is False


def test_mobile_android_includes_apk_installer():
    strategy = select("mobile_android")
    assert strategy.is_agent_allowed("apk_installer") is True
