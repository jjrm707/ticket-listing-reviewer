"""Capability-aware marketplace connector contracts."""

from .base import Capability, ConnectorFailure, FailureCategory, MarketplaceConnector

__all__ = [
    "Capability",
    "ConnectorFailure",
    "FailureCategory",
    "MarketplaceConnector",
]
