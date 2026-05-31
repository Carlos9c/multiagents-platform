"""QA strategy registry.

Maps product_type strings to their QAStrategy instances.
All QA-eligible product types must have an entry here.
"""

from __future__ import annotations

from app.services.qa.strategies.api_strategy import GraphqlApiStrategy, RestApiStrategy
from app.services.qa.strategies.base import QAStrategy
from app.services.qa.strategies.cli_strategy import CliToolStrategy
from app.services.qa.strategies.desktop_strategy import DesktopAppStrategy
from app.services.qa.strategies.library_strategy import LibraryStrategy
from app.services.qa.strategies.mobile_strategy import MobileAndroidStrategy
from app.services.qa.strategies.web_app_strategy import WebAppStrategy

_REGISTRY: dict[str, QAStrategy] = {
    s.product_type: s
    for s in [
        WebAppStrategy(),
        RestApiStrategy(),
        GraphqlApiStrategy(),
        CliToolStrategy(),
        MobileAndroidStrategy(),
        LibraryStrategy(),
        DesktopAppStrategy(),
    ]
}


def get_strategy(product_type: str) -> QAStrategy | None:
    """Return the strategy for the given product_type, or None if not QA-eligible."""
    return _REGISTRY.get(product_type)


def all_eligible_product_types() -> frozenset[str]:
    """All product_type values that have a registered QA strategy."""
    return frozenset(_REGISTRY.keys())
