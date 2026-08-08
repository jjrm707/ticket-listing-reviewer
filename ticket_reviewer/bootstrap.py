"""Application composition root without import-time services or network calls."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import httpx
from sqlalchemy.orm import Session

from ticket_reviewer.config import Settings
from ticket_reviewer.connectors.base import MarketplaceConnector
from ticket_reviewer.connectors.seatgeek import SeatGeekConnector
from ticket_reviewer.connectors.ticketmaster import TicketmasterConnector
from ticket_reviewer.data.db import create_engine_and_session
from ticket_reviewer.domain.enums import Source
from ticket_reviewer.services.scanner import (
    AlertService,
    RepositoryBundle,
    RepositoryFactory,
    ScanCoordinator,
)


class CloseableConnector(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    base_settings: Settings
    session_factory: Callable[[], Session]
    repository_factory: RepositoryFactory
    connectors: tuple[MarketplaceConnector, ...]
    scanner: ScanCoordinator
    alert_service: AlertService | None
    _owned_connectors: tuple[CloseableConnector, ...] = field(
        default=(), repr=False
    )
    _closed: bool = field(default=False, repr=False, compare=False)

    def close(self) -> None:
        """Idempotently release HTTP resources created by the composition root."""
        if self._closed:
            return
        for connector in self._owned_connectors:
            connector.close()
        object.__setattr__(self, "_closed", True)


def build_services(
    settings: Settings,
    connectors: Iterable[MarketplaceConnector] = (),
    *,
    session_factory: Callable[[], Session] | None = None,
    repository_factory: RepositoryFactory = RepositoryBundle,
    alert_service: AlertService | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ApplicationServices:
    """Wire local services and configured official public connectors."""
    if session_factory is None:
        _, session_factory = create_engine_and_session(settings.database_url)
    connector_list = list(connectors)
    owned_connectors: list[CloseableConnector] = []
    key = settings.ticketmaster_api_key
    configured_key = key.get_secret_value().strip() if key is not None else ""
    if configured_key and all(
        connector.source is not Source.TICKETMASTER for connector in connector_list
    ):
        client = httpx.Client(timeout=settings.ticketmaster_http_timeout_seconds)
        try:
            connector = TicketmasterConnector(
                settings,
                client,
                owns_client=True,
            )
        except BaseException:
            client.close()
            raise
        connector_list.append(connector)
        owned_connectors.append(connector)
    client_id = settings.seatgeek_client_id
    configured_client_id = (
        client_id.get_secret_value().strip() if client_id is not None else ""
    )
    if configured_client_id and all(
        connector.source is not Source.SEATGEEK for connector in connector_list
    ):
        try:
            client = httpx.Client(timeout=settings.seatgeek_http_timeout_seconds)
        except BaseException:
            for owned_connector in owned_connectors:
                owned_connector.close()
            raise
        try:
            connector = SeatGeekConnector(
                settings,
                client,
                owns_client=True,
            )
        except BaseException:
            client.close()
            for owned_connector in owned_connectors:
                owned_connector.close()
            raise
        connector_list.append(connector)
        owned_connectors.append(connector)
    connector_tuple = tuple(connector_list)
    try:
        scanner = ScanCoordinator(
            settings,
            session_factory,
            repository_factory,
            connector_tuple,
            alert_service=alert_service,
            clock=clock,
        )
    except BaseException:
        for connector in owned_connectors:
            connector.close()
        raise
    return ApplicationServices(
        base_settings=settings,
        session_factory=session_factory,
        repository_factory=repository_factory,
        connectors=connector_tuple,
        scanner=scanner,
        alert_service=alert_service,
        _owned_connectors=tuple(owned_connectors),
    )
