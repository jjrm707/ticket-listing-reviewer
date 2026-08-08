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
from ticket_reviewer.connectors.stubhub import StubHubConnector
from ticket_reviewer.connectors.ticketmaster import TicketmasterConnector
from ticket_reviewer.data.db import create_engine_and_session
from ticket_reviewer.domain.enums import Source
from ticket_reviewer.services.alerts import (
    AlertService as OpportunityAlertService,
    NtfyPublisher,
)
from ticket_reviewer.services.scanner import (
    AlertService as ScannerAlertService,
    RepositoryBundle,
    RepositoryFactory,
    ScanCoordinator,
)


class CloseableConnector(Protocol):
    def close(self) -> None: ...


def _close_all(resources: Iterable[CloseableConnector]) -> BaseException | None:
    """Attempt every close and return the first failure after all attempts."""
    first_error: BaseException | None = None
    for resource in resources:
        try:
            resource.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
    return first_error


def _cleanup_without_masking(
    original_error: BaseException, resources: Iterable[CloseableConnector]
) -> None:
    """Close every resource while preserving the in-flight construction error."""
    if _close_all(resources) is not None:
        original_error.add_note("additional owned resource cleanup failed")


def _snapshot_connector_sources(
    connectors: Iterable[MarketplaceConnector],
) -> list[Source]:
    """Read and validate each injected source once before acquiring clients."""
    sources: list[Source] = []
    for connector in connectors:
        source = connector.source
        if not isinstance(source, Source):
            raise TypeError("connector source must be a Source")
        if source in sources:
            raise ValueError(f"duplicate connector source: {source.value}")
        sources.append(source)
    return sources


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    base_settings: Settings
    session_factory: Callable[[], Session]
    repository_factory: RepositoryFactory
    connectors: tuple[MarketplaceConnector, ...]
    scanner: ScanCoordinator
    alert_service: ScannerAlertService | None
    _owned_connectors: tuple[CloseableConnector, ...] = field(
        default=(), repr=False
    )
    _closed: bool = field(default=False, repr=False, compare=False)

    def close(self) -> None:
        """Idempotently release HTTP resources created by the composition root."""
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        close_error = _close_all(self._owned_connectors)
        if close_error is not None:
            raise close_error


def build_services(
    settings: Settings,
    connectors: Iterable[MarketplaceConnector] = (),
    *,
    session_factory: Callable[[], Session] | None = None,
    repository_factory: RepositoryFactory = RepositoryBundle,
    alert_service: ScannerAlertService | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ApplicationServices:
    """Wire local services and configured official public connectors."""
    if session_factory is None:
        _, session_factory = create_engine_and_session(settings.database_url)
    connector_list = list(connectors)
    connector_sources = _snapshot_connector_sources(connector_list)
    topic_secret = settings.ntfy_topic
    configured_topic = (
        topic_secret.get_secret_value() if topic_secret is not None else ""
    )
    token_secret = settings.ntfy_access_token
    configured_token = (
        token_secret.get_secret_value() if token_secret is not None else ""
    )
    if configured_token and not configured_topic:
        raise ValueError("invalid notification configuration")
    token = configured_token or None
    if configured_topic:
        NtfyPublisher.validate_configuration(
            configured_topic, token, settings.ntfy_http_timeout_seconds
        )
    stubhub_client_id = settings.stubhub_client_id
    configured_stubhub_client_id = (
        stubhub_client_id.get_secret_value().strip()
        if stubhub_client_id is not None
        else ""
    )
    stubhub_client_secret = settings.stubhub_client_secret
    configured_stubhub_client_secret = (
        stubhub_client_secret.get_secret_value().strip()
        if stubhub_client_secret is not None
        else ""
    )
    if bool(configured_stubhub_client_id) != bool(configured_stubhub_client_secret):
        raise ValueError("StubHub OAuth credentials must be configured together")
    owned_connectors: list[CloseableConnector] = []
    effective_alert_service = alert_service
    if configured_topic and effective_alert_service is None:
        publisher = NtfyPublisher(
            configured_topic,
            token,
            settings.ntfy_http_timeout_seconds,
        )
        owned_connectors.append(publisher)
        try:
            effective_alert_service = OpportunityAlertService(
                settings, session_factory, publisher=publisher
            )
        except BaseException as error:
            _cleanup_without_masking(error, owned_connectors)
            raise
    key = settings.ticketmaster_api_key
    configured_key = key.get_secret_value().strip() if key is not None else ""
    if configured_key and Source.TICKETMASTER not in connector_sources:
        try:
            client = httpx.Client(timeout=settings.ticketmaster_http_timeout_seconds)
        except BaseException as error:
            _cleanup_without_masking(error, owned_connectors)
            raise
        try:
            connector = TicketmasterConnector(
                settings,
                client,
                owns_client=True,
            )
        except BaseException as error:
            _cleanup_without_masking(error, (client, *owned_connectors))
            raise
        connector_list.append(connector)
        connector_sources.append(Source.TICKETMASTER)
        owned_connectors.append(connector)
    client_id = settings.seatgeek_client_id
    configured_client_id = (
        client_id.get_secret_value().strip() if client_id is not None else ""
    )
    if configured_client_id and Source.SEATGEEK not in connector_sources:
        try:
            client = httpx.Client(timeout=settings.seatgeek_http_timeout_seconds)
        except BaseException as error:
            _cleanup_without_masking(error, owned_connectors)
            raise
        try:
            connector = SeatGeekConnector(
                settings,
                client,
                owns_client=True,
            )
        except BaseException as error:
            _cleanup_without_masking(error, (client, *owned_connectors))
            raise
        connector_list.append(connector)
        connector_sources.append(Source.SEATGEEK)
        owned_connectors.append(connector)
    if (
        configured_stubhub_client_id
        and configured_stubhub_client_secret
        and Source.STUBHUB not in connector_sources
    ):
        try:
            client = httpx.Client(timeout=settings.stubhub_http_timeout_seconds)
        except BaseException as error:
            _cleanup_without_masking(error, owned_connectors)
            raise
        try:
            connector = StubHubConnector(
                settings,
                client,
                owns_client=True,
            )
        except BaseException as error:
            _cleanup_without_masking(error, (client, *owned_connectors))
            raise
        connector_list.append(connector)
        connector_sources.append(Source.STUBHUB)
        owned_connectors.append(connector)
    connector_tuple = tuple(connector_list)
    try:
        scanner = ScanCoordinator(
            settings,
            session_factory,
            repository_factory,
            connector_tuple,
            connector_sources=tuple(connector_sources),
            alert_service=effective_alert_service,
            clock=clock,
        )
    except BaseException as error:
        _cleanup_without_masking(error, owned_connectors)
        raise
    return ApplicationServices(
        base_settings=settings,
        session_factory=session_factory,
        repository_factory=repository_factory,
        connectors=connector_tuple,
        scanner=scanner,
        alert_service=effective_alert_service,
        _owned_connectors=tuple(owned_connectors),
    )
