"""
Conftest for integration tests that require a live Docker daemon.

Run integration tests explicitly:
    poetry run pytest -m integration -v
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless -m integration (or -m '') is passed."""
    skip_marker = pytest.mark.skip(reason="requires Docker daemon — run with -m integration")
    for item in items:
        if "integration" in item.keywords:
            # Only skip when the user has NOT selected the integration mark explicitly
            expression = config.option.markexpr
            if "integration" not in (expression or ""):
                item.add_marker(skip_marker)
