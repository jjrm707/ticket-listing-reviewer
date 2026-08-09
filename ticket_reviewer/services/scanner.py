"""Atomic, per-connector marketplace scan orchestration."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session

from ticket_reviewer.config import RuntimeSettings, Settings
from ticket_reviewer.connectors.base import ConnectorFailure, MarketplaceConnector
from ticket_reviewer.data.repositories import (
    EventRepository,
    ObservationRepository,
    OpportunityRepository,
    RunRepository,
    SettingRepository,
)
from ticket_reviewer.domain.enums import ObservationKind, Source, Team
from ticket_reviewer.domain.matching import event_match_score, is_supported_home_game
from ticket_reviewer.domain.models import ExternalEvent, SourceObservation
from ticket_reviewer.domain.scoring import estimate_opportunity


DISCOVERY_WINDOW = timedelta(days=365)
_CLOCK_SKEW = timedelta(minutes=5)
_UNEXPECTED_ERROR = "unexpected connector error"
_EVENT_MATCH_THRESHOLD = Decimal("0.85")


class AlertService(Protocol):
    def evaluate_and_send(self, opportunity_id: int, now: datetime) -> object: ...


@dataclass(frozen=True, slots=True, init=False)
class RepositoryBundle:
    """Transaction-scoped repository instances sharing one caller-owned session."""

    events: EventRepository
    observations: ObservationRepository
    opportunities: OpportunityRepository
    runs: RunRepository
    settings: SettingRepository

    def __init__(self, session: Session) -> None:
        object.__setattr__(self, "events", EventRepository(session))
        object.__setattr__(self, "observations", ObservationRepository(session))
        object.__setattr__(self, "opportunities", OpportunityRepository(session))
        object.__setattr__(self, "runs", RunRepository(session))
        object.__setattr__(self, "settings", SettingRepository(session))


RepositoryFactory = Callable[[Session], RepositoryBundle]


@dataclass(frozen=True, slots=True)
class ScanSummary:
    started_at: datetime
    finished_at: datetime
    sources_succeeded: tuple[Source, ...]
    sources_failed: tuple[Source, ...]
    events_seen: int
    observations_saved: int
    opportunities_saved: int
    actionable_opportunities: int


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _fee_profiles(settings: RuntimeSettings) -> dict[Source, Decimal]:
    return {
        Source.STUBHUB: settings.stubhub_seller_fee_rate,
        Source.TICKETMASTER: settings.ticketmaster_seller_fee_rate,
        Source.SEATGEEK: settings.seatgeek_seller_fee_rate,
    }


def _fresh(observed_at: datetime, now: datetime, freshness: timedelta) -> bool:
    age = now - observed_at
    return -_CLOCK_SKEW <= age <= freshness


def _confirmed_listing(observation: SourceObservation) -> bool:
    return (
        observation.kind is ObservationKind.LISTING
        and observation.can_buy_pair is True
        and type(observation.quantity_available) is int
        and observation.quantity_available >= 2
        and observation.pair_price is not None
        and observation.currency == "USD"
    )


def _observation_from_row(row, canonical_external_id: str) -> SourceObservation:
    """Re-key a persisted source row to the already matched canonical event."""
    return SourceObservation(
        source=Source(row.source),
        event_external_id=canonical_external_id,
        observed_at=row.observed_at,
        kind=ObservationKind(row.kind),
        currency=row.currency,
        pair_price=row.pair_price,
        buyer_fees=row.buyer_fees,
        estimated_tax=row.estimated_tax,
        section=row.section,
        row=row.row,
        quantity_available=row.quantity_available,
        can_buy_pair=row.can_buy_pair,
        listing_id=row.listing_id,
        listing_url=row.listing_url,
        listing_count=row.listing_count,
        popularity=row.popularity,
        observation_id=row.id,
    )


def _event_from_row(row, incoming: ExternalEvent) -> ExternalEvent:
    return ExternalEvent(
        source=incoming.source,
        external_id=f"canonical:{row.id}",
        team=Team(row.team),
        opponent=row.opponent,
        venue=row.venue,
        starts_at=row.starts_at,
        is_home=row.is_home,
        is_parking=False,
        url=None,
    )


def _upsert_matched_event(
    repositories: RepositoryBundle, event: ExternalEvent
) -> int:
    if repositories.events.find_by_source(event.source, event.external_id) is not None:
        return repositories.events.upsert(event)

    qualifying = [
        row
        for row in repositories.events.list_for_team(event.team)
        if event_match_score(event, _event_from_row(row, event))
        >= _EVENT_MATCH_THRESHOLD
    ]
    if len(qualifying) == 1:
        return repositories.events.upsert(
            event, canonical_event_id=qualifying[0].id
        )
    return repositories.events.upsert(event)


class ScanCoordinator:
    """Run connectors independently so a failure cannot erase another source."""

    def __init__(
        self,
        base_settings: Settings,
        session_factory: Callable[[], Session],
        repository_factory: RepositoryFactory,
        connectors: Iterable[MarketplaceConnector] = (),
        *,
        connector_sources: Iterable[Source] | None = None,
        alert_service: AlertService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        connector_tuple = tuple(connectors)
        sources = list(
            connector_sources
            if connector_sources is not None
            else (connector.source for connector in connector_tuple)
        )
        if len(sources) != len(connector_tuple):
            raise ValueError("connector sources must match connectors")
        if any(not isinstance(source, Source) for source in sources):
            raise TypeError("connector source must be a Source")
        duplicate = next(
            (source for index, source in enumerate(sources) if source in sources[:index]),
            None,
        )
        if duplicate is not None:
            raise ValueError(f"duplicate connector source: {duplicate.value}")
        self.base_settings = base_settings
        self.session_factory = session_factory
        self.repository_factory = repository_factory
        self.connectors = connector_tuple
        self._connector_sources = tuple(sources)
        self.alert_service = alert_service
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, now: datetime) -> ScanSummary:
        started_at = _utc(now, "now")
        effective = self._effective_settings()
        succeeded: list[Source] = []
        failed: list[Source] = []
        events_seen = 0
        observations_saved = 0
        opportunities_saved = 0
        actionable_opportunities = 0

        for connector, source in zip(self.connectors, self._connector_sources):
            try:
                result = self._run_connector(connector, source, effective, started_at)
            except Exception as error:
                safe_error = (
                    error.safe_message
                    if isinstance(error, ConnectorFailure)
                    else _UNEXPECTED_ERROR
                )
                failed.append(source)
                try:
                    self._record_failed_run(source, started_at, safe_error)
                except Exception:
                    pass
                continue
            succeeded.append(source)
            events_seen += result[0]
            observations_saved += result[1]
            opportunities_saved += result[2]
            actionable_opportunities += result[3]

        return ScanSummary(
            started_at=started_at,
            finished_at=_utc(self._clock(), "finished_at"),
            sources_succeeded=tuple(succeeded),
            sources_failed=tuple(failed),
            events_seen=events_seen,
            observations_saved=observations_saved,
            opportunities_saved=opportunities_saved,
            actionable_opportunities=actionable_opportunities,
        )

    def _effective_settings(self) -> RuntimeSettings:
        with self.session_factory() as session:
            return self.repository_factory(session).settings.effective(self.base_settings)

    def _run_connector(
        self,
        connector: MarketplaceConnector,
        source: Source,
        settings: RuntimeSettings,
        now: datetime,
    ) -> tuple[int, int, int, int]:
        session = self.session_factory()
        try:
            repositories = self.repository_factory(session)
            run_id = repositories.runs.start(source, now)
            collected = self._collect(connector, source, repositories, settings, now)
            counts = collected[:4]
            repositories.runs.finish(
                run_id,
                success=True,
                observation_count=counts[1],
                finished_at=_utc(self._clock(), "finished_at"),
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        if self.alert_service is not None:
            for opportunity_id in collected[4]:
                try:
                    self.alert_service.evaluate_and_send(opportunity_id, now)
                except Exception:
                    # Notification failure cannot change an already committed scan.
                    continue
        return counts

    def _collect(
        self,
        connector: MarketplaceConnector,
        source: Source,
        repositories: RepositoryBundle,
        settings: RuntimeSettings,
        now: datetime,
    ) -> tuple[int, int, int, int, tuple[int, ...]]:
        freshness = timedelta(minutes=settings.observation_freshness_minutes)
        seen_events: set[tuple[Source, str]] = set()
        seen_observations: set[
            tuple[Source, str, ObservationKind, str | None, datetime]
        ] = set()
        events_seen = observations_saved = opportunities_saved = actionable = 0
        alert_opportunity_ids: list[int] = []

        for team in (Team.TEXANS, Team.AGGIES):
            events = connector.discover(team, now, now + DISCOVERY_WINDOW)
            for event in events:
                if not isinstance(event, ExternalEvent):
                    raise TypeError("connector returned an invalid event")
                if event.source is not source:
                    raise ValueError("connector returned an event for another source")
                event_key = (event.source, event.external_id)
                if event_key in seen_events:
                    continue
                seen_events.add(event_key)
                events_seen += 1
                if not is_supported_home_game(event):
                    continue

                event_id = _upsert_matched_event(repositories, event)
                observations = connector.fetch_observations(event)
                candidates: list[tuple[int, SourceObservation]] = []
                for observation in observations:
                    if not isinstance(observation, SourceObservation):
                        raise TypeError("connector returned an invalid observation")
                    if (
                        observation.source is not source
                        or observation.event_external_id != event.external_id
                    ):
                        raise ValueError("connector returned an observation for another event")
                    key = (
                        observation.source,
                        observation.event_external_id,
                        observation.kind,
                        observation.listing_id,
                        observation.observed_at,
                    )
                    if key in seen_observations:
                        continue
                    seen_observations.add(key)
                    saved = repositories.observations.add_with_status(
                        event_id, observation
                    )
                    if not saved.inserted:
                        continue
                    persisted_row = repositories.observations.get(
                        saved.observation_id
                    )
                    if persisted_row is None:
                        raise RuntimeError("persisted observation is unavailable")
                    persisted = _observation_from_row(
                        persisted_row, observation.event_external_id
                    )
                    observations_saved += 1
                    if _confirmed_listing(persisted) and _fresh(
                        persisted.observed_at, now, freshness
                    ):
                        candidates.append((saved.observation_id, persisted))

                rows = repositories.observations.list_for_event(event_id)
                for observation_id, candidate in candidates:
                    fresh_comparisons = tuple(
                        _observation_from_row(row, candidate.event_external_id)
                        for row in rows
                        if _fresh(row.observed_at, now, freshness)
                    )
                    estimate = estimate_opportunity(
                        candidate,
                        fresh_comparisons,
                        _fee_profiles(settings),
                        now,
                        settings.budget_cap,
                        kickoff_at=event.starts_at,
                    )
                    opportunity_id = repositories.opportunities.save_estimate(
                        event_id, observation_id, estimate
                    )
                    opportunities_saved += 1
                    if estimate.actionable:
                        actionable += 1
                        alert_opportunity_ids.append(opportunity_id)

        return (
            events_seen,
            observations_saved,
            opportunities_saved,
            actionable,
            tuple(alert_opportunity_ids),
        )

    def _record_failed_run(
        self, source: Source, started_at: datetime, safe_error: str
    ) -> None:
        session = self.session_factory()
        try:
            repositories = self.repository_factory(session)
            run_id = repositories.runs.start(source, started_at)
            repositories.runs.finish(
                run_id,
                success=False,
                observation_count=0,
                error=safe_error,
                finished_at=_utc(self._clock(), "finished_at"),
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
