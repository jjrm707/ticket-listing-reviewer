from datetime import datetime

from ticket_reviewer.connectors.base import (
    Capability,
    ConnectorFailure,
    FailureCategory,
    MarketplaceConnector,
)
from ticket_reviewer.domain.enums import Source, Team


class CompleteConnector:
    source = Source.STUBHUB
    capabilities = frozenset({Capability.EVENT_SEARCH, Capability.LISTING_DETAIL})

    def discover(
        self, team: Team, starts_after: datetime, starts_before: datetime
    ) -> list:
        return []

    def fetch_observations(self, event) -> list:
        return []


class IncompleteConnector:
    source = Source.STUBHUB
    capabilities = frozenset({Capability.EVENT_SEARCH})


def test_contract_has_stable_capabilities_and_is_runtime_checkable():
    assert [item.value for item in Capability] == [
        "event_search",
        "event_price",
        "listing_detail",
    ]
    assert isinstance(CompleteConnector(), MarketplaceConnector)
    assert not isinstance(IncompleteConnector(), MarketplaceConnector)
    assert isinstance(CompleteConnector.capabilities, frozenset)


def test_failure_categories_have_stable_values():
    assert [item.value for item in FailureCategory] == [
        "auth",
        "rate_limit",
        "network",
        "parse",
        "unsupported",
    ]


def test_connector_failure_string_and_repr_expose_only_safe_message():
    failure = ConnectorFailure(
        Source.STUBHUB,
        FailureCategory.NETWORK,
        "marketplace temporarily unavailable",
        retryable=True,
    )

    assert str(failure) == "marketplace temporarily unavailable"
    assert repr(failure) == "ConnectorFailure('marketplace temporarily unavailable')"
    assert failure.args == ("marketplace temporarily unavailable",)
