"""Bounded retry support for connector-owned idempotent operations."""

from collections.abc import Callable
from typing import TypeVar

from ticket_reviewer.connectors.base import ConnectorFailure, FailureCategory


T = TypeVar("T")
_RETRYABLE_CATEGORIES = frozenset(
    {FailureCategory.NETWORK, FailureCategory.RATE_LIMIT}
)


def call_with_retry(
    operation: Callable[[], T], sleep: Callable[[float], object], attempts: int = 3
) -> T:
    """Call an operation with bounded exponential backoff for retryable failures."""
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts <= 0:
        raise ValueError("attempts must be a positive non-boolean integer")

    for attempt in range(attempts):
        try:
            return operation()
        except ConnectorFailure as error:
            if (
                not error.retryable
                or error.category not in _RETRYABLE_CATEGORIES
                or attempt == attempts - 1
            ):
                raise
            sleep(0.5 * (2**attempt))
    raise AssertionError("retry loop did not return or raise")
