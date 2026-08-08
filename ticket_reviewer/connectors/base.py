"""Shared contract for official marketplace API adapters."""

from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from ticket_reviewer.domain.enums import Source, Team
from ticket_reviewer.domain.models import ExternalEvent, SourceObservation


class Capability(str, Enum):
    EVENT_SEARCH = "event_search"
    EVENT_PRICE = "event_price"
    LISTING_DETAIL = "listing_detail"


class FailureCategory(str, Enum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    PARSE = "parse"
    UNSUPPORTED = "unsupported"


class ConnectorFailure(Exception):
    """A connector failure carrying only a caller-approved safe message."""

    def __init__(
        self,
        source: Source,
        category: FailureCategory,
        safe_message: str,
        retryable: bool,
    ) -> None:
        super().__init__(safe_message)
        self.source = source
        self.category = category
        self.safe_message = safe_message
        self.retryable = retryable

    def __str__(self) -> str:
        return self.safe_message

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.safe_message!r})"


@runtime_checkable
class MarketplaceConnector(Protocol):
    source: Source
    capabilities: frozenset[Capability]

    def discover(
        self, team: Team, starts_after: datetime, starts_before: datetime
    ) -> list[ExternalEvent]: ...

    def fetch_observations(self, event: ExternalEvent) -> list[SourceObservation]: ...
