"""QA strategy selector.

Exposes QA_ELIGIBLE_PRODUCT_TYPES for quick eligibility checks and
select() for full strategy resolution.
"""

from __future__ import annotations

from app.services.qa.strategies.base import QAStrategy
from app.services.qa.strategies.registry import all_eligible_product_types, get_strategy

# Immutable set of product types that trigger a QA offer after project completion.
QA_ELIGIBLE_PRODUCT_TYPES: frozenset[str] = all_eligible_product_types()


def select(product_type: str) -> QAStrategy:
    """Return the QAStrategy for the given product_type.

    Raises ValueError if the product_type is not QA-eligible.
    """
    strategy = get_strategy(product_type)
    if strategy is None:
        raise ValueError(
            f"No QA strategy registered for product_type={product_type!r}. "
            f"Eligible types: {sorted(QA_ELIGIBLE_PRODUCT_TYPES)}"
        )
    return strategy


def is_qa_eligible(product_type: str | None) -> bool:
    """Return True if Aria should offer QA for this product type."""
    return product_type is not None and product_type in QA_ELIGIBLE_PRODUCT_TYPES
