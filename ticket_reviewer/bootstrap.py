"""Application composition root without import-time services or network calls."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from ticket_reviewer.config import Settings
from ticket_reviewer.connectors.base import MarketplaceConnector
from ticket_reviewer.data.db import create_engine_and_session
from ticket_reviewer.services.scanner import (
    AlertService,
    RepositoryBundle,
    RepositoryFactory,
    ScanCoordinator,
)


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    base_settings: Settings
    session_factory: Callable[[], Session]
    repository_factory: RepositoryFactory
    connectors: tuple[MarketplaceConnector, ...]
    scanner: ScanCoordinator
    alert_service: AlertService | None


def build_services(
    settings: Settings,
    connectors: Iterable[MarketplaceConnector] = (),
    *,
    session_factory: Callable[[], Session] | None = None,
    repository_factory: RepositoryFactory = RepositoryBundle,
    alert_service: AlertService | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ApplicationServices:
    """Wire local services; real marketplace connectors are registered later."""
    if session_factory is None:
        _, session_factory = create_engine_and_session(settings.database_url)
    connector_tuple = tuple(connectors)
    scanner = ScanCoordinator(
        settings,
        session_factory,
        repository_factory,
        connector_tuple,
        alert_service=alert_service,
        clock=clock,
    )
    return ApplicationServices(
        base_settings=settings,
        session_factory=session_factory,
        repository_factory=repository_factory,
        connectors=connector_tuple,
        scanner=scanner,
        alert_service=alert_service,
    )
