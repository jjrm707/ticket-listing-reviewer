from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.factories import make_event, make_observation
from ticket_reviewer.bootstrap import build_services
from ticket_reviewer.config import Settings
from ticket_reviewer.connectors.base import Capability, ConnectorFailure, FailureCategory
from ticket_reviewer.data.db import create_engine_and_session
from ticket_reviewer.data.repositories import EventRepository, ObservationRepository, SettingRepository
from ticket_reviewer.data.schema import (
    AlertRow,
    Base,
    ConnectorRunRow,
    EventRow,
    ObservationRow,
    OpportunityRow,
)
from ticket_reviewer.domain.enums import ObservationKind, Source, Team
from ticket_reviewer.services.alerts import AlertService, NotificationFailure
from ticket_reviewer.services.scanner import RepositoryBundle, ScanCoordinator


NOW = datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc)
FINISHED = NOW + timedelta(seconds=2)


@dataclass
class FakeConnector:
    source: Source
    events: dict[Team, list]
    observations: dict[str, object]
    capabilities: frozenset[Capability] = frozenset(
        {Capability.EVENT_SEARCH, Capability.LISTING_DETAIL}
    )

    def __post_init__(self):
        self.discover_calls = []
        self.fetch_calls = []

    def discover(self, team, starts_after, starts_before):
        self.discover_calls.append((team, starts_after, starts_before))
        value = self.events.get(team, [])
        if isinstance(value, BaseException):
            raise value
        return list(value)

    def fetch_observations(self, event):
        self.fetch_calls.append(event.external_id)
        value = self.observations.get(event.external_id, [])
        if isinstance(value, BaseException):
            raise value
        return list(value)


@pytest.fixture
def database(tmp_path):
    engine, factory = create_engine_and_session(f"sqlite:///{tmp_path / 'scanner.db'}")
    Base.metadata.create_all(engine)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'unused.db'}")


def coordinator(settings, database, connectors, *, alert_service=None):
    return ScanCoordinator(
        settings,
        database,
        RepositoryBundle,
        connectors,
        alert_service=alert_service,
        clock=lambda: FINISHED,
    )


def failure(source=Source.TICKETMASTER):
    return ConnectorFailure(
        source, FailureCategory.NETWORK, "marketplace temporarily unavailable", True
    )


def one_event(source=Source.STUBHUB, *, external_id="event-1", **overrides):
    return make_event(source=source, external_id=external_id, **overrides)


def one_observation(source=Source.STUBHUB, *, external_id="event-1", **overrides):
    observed_at = overrides.pop("observed_at", NOW)
    return make_observation(
        source=source,
        event_external_id=external_id,
        observed_at=observed_at,
        **overrides,
    )


def comparable_observation(**overrides):
    return one_observation(
        kind=ObservationKind.EVENT_AGGREGATE,
        can_buy_pair=None,
        quantity_available=None,
        listing_id=None,
        **overrides,
    )


def test_one_connector_failure_does_not_abort_scan(settings, database):
    event = one_event()
    good = FakeConnector(Source.STUBHUB, {Team.TEXANS: [event]}, {event.external_id: [one_observation()]})
    bad = FakeConnector(Source.TICKETMASTER, {Team.TEXANS: failure()}, {})

    summary = coordinator(settings, database, (good, bad)).run(NOW)

    assert summary.sources_succeeded == (good.source,)
    assert summary.sources_failed == (bad.source,)
    assert summary.observations_saved == 1
    with database() as session:
        runs = session.scalars(select(ConnectorRunRow).order_by(ConnectorRunRow.id)).all()
        assert [(row.source, row.success) for row in runs] == [
            ("stubhub", True),
            ("ticketmaster", False),
        ]


def test_event_level_observation_is_saved_but_not_actionable(settings, database):
    event = one_event()
    floor = one_observation(
        kind=ObservationKind.EVENT_FLOOR,
        pair_price=Decimal("250"),
        listing_id=None,
        can_buy_pair=None,
        quantity_available=None,
    )
    connector = FakeConnector(Source.STUBHUB, {Team.TEXANS: [event]}, {event.external_id: [floor]})

    summary = coordinator(settings, database, (connector,)).run(NOW)

    assert summary.observations_saved == 1
    assert summary.opportunities_saved == 0
    assert summary.actionable_opportunities == 0


def test_distinct_event_level_snapshots_at_same_time_are_both_persisted(
    settings, database
):
    event = one_event()
    floor = one_observation(
        kind=ObservationKind.EVENT_FLOOR,
        pair_price=Decimal("250"),
        listing_id=None,
        can_buy_pair=None,
        quantity_available=None,
    )
    aggregate = replace(
        floor,
        kind=ObservationKind.EVENT_AGGREGATE,
        pair_price=Decimal("300"),
        listing_count=120,
    )
    connector = FakeConnector(
        Source.STUBHUB,
        {Team.TEXANS: [event]},
        {event.external_id: [floor, aggregate]},
    )

    summary = coordinator(settings, database, (connector,)).run(NOW)

    assert summary.observations_saved == 2
    with database() as session:
        rows = session.scalars(select(ObservationRow)).all()
        assert {row.kind for row in rows} == {"event_floor", "event_aggregate"}


def test_both_teams_are_queried_with_deterministic_365_day_window(settings, database):
    connector = FakeConnector(Source.STUBHUB, {}, {})

    coordinator(settings, database, (connector,)).run(NOW)

    assert connector.discover_calls == [
        (Team.TEXANS, NOW, NOW + timedelta(days=365)),
        (Team.AGGIES, NOW, NOW + timedelta(days=365)),
    ]


def test_unsupported_events_are_not_persisted_or_fetched(settings, database):
    away = one_event(is_home=False)
    connector = FakeConnector(Source.STUBHUB, {Team.TEXANS: [away]}, {})

    summary = coordinator(settings, database, (connector,)).run(NOW)

    assert summary.events_seen == 1
    assert connector.fetch_calls == []
    with database() as session:
        assert session.scalars(select(ObservationRow)).all() == []


def test_stale_observations_remain_saved_but_are_excluded_from_scoring(settings, database):
    event = one_event()
    candidate = one_observation(listing_id="candidate", pair_price=Decimal("100"))
    stale = [
        comparable_observation(
            pair_price=Decimal("400"),
            observed_at=NOW - timedelta(minutes=121 + index),
        )
        for index in range(3)
    ]
    connector = FakeConnector(Source.STUBHUB, {Team.TEXANS: [event]}, {event.external_id: [candidate, *stale]})

    summary = coordinator(settings, database, (connector,)).run(NOW)

    assert summary.observations_saved == 4
    assert summary.opportunities_saved == 1
    assert summary.actionable_opportunities == 0
    with database() as session:
        assert len(session.scalars(select(ObservationRow)).all()) == 4


def test_effective_settings_are_reloaded_for_each_scan(settings, database):
    event = one_event()
    candidate = one_observation(listing_id="candidate", pair_price=Decimal("390"), buyer_fees=Decimal("0"), estimated_tax=Decimal("0"))
    comps = [comparable_observation(pair_price=Decimal("600"), section="132", row="24", observed_at=NOW - timedelta(seconds=i)) for i in range(3)]
    connector = FakeConnector(Source.STUBHUB, {Team.TEXANS: [event]}, {event.external_id: [candidate, *comps]})
    scanner = coordinator(settings, database, (connector,))

    first = scanner.run(NOW)
    with database() as session:
        SettingRepository(session).set("budget_cap", "300")
        session.commit()
    connector.observations[event.external_id] = [
        replace(item, observed_at=NOW + timedelta(minutes=1)) for item in [candidate, *comps]
    ]
    second = scanner.run(NOW + timedelta(minutes=1))

    assert first.actionable_opportunities == 1
    assert second.actionable_opportunities == 0


def test_connector_transaction_rolls_back_then_failed_run_commits_safely(settings, database):
    first = one_event(external_id="event-1")
    second = one_event(external_id="event-2", opponent="Other Opponent", starts_at=first.starts_at + timedelta(days=7))
    connector = FakeConnector(
        Source.STUBHUB,
        {Team.TEXANS: [first, second]},
        {"event-1": [one_observation(external_id="event-1")], "event-2": failure(Source.STUBHUB)},
    )

    summary = coordinator(settings, database, (connector,)).run(NOW)

    assert summary.sources_failed == (Source.STUBHUB,)
    assert summary.observations_saved == 0
    with database() as session:
        assert session.scalars(select(ObservationRow)).all() == []
        runs = session.scalars(select(ConnectorRunRow)).all()
        assert len(runs) == 1
        assert runs[0].success is False
        assert runs[0].redacted_error == "marketplace temporarily unavailable"


def test_failed_run_commit_error_does_not_abort_later_connectors_or_leak(
    settings, database
):
    secret = "failed-run commit body Bearer persistence-secret"

    class CommitFailingSession:
        def __init__(self, session):
            self._session = session

        def __getattr__(self, name):
            return getattr(self._session, name)

        def commit(self):
            raise RuntimeError(secret)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    class ThirdSessionCommitFails:
        def __init__(self, factory):
            self.factory = factory
            self.calls = 0

        def __call__(self):
            self.calls += 1
            session = self.factory()
            if self.calls == 3:
                return CommitFailingSession(session)
            return session

    failing_factory = ThirdSessionCommitFails(database)
    bad = FakeConnector(
        Source.TICKETMASTER,
        {Team.TEXANS: failure(Source.TICKETMASTER)},
        {},
    )
    event = one_event()
    good = FakeConnector(
        Source.STUBHUB,
        {Team.TEXANS: [event]},
        {event.external_id: [one_observation()]},
    )
    scanner = ScanCoordinator(
        settings,
        failing_factory,
        RepositoryBundle,
        (bad, good),
        clock=lambda: FINISHED,
    )

    summary = scanner.run(NOW)

    assert summary.sources_failed == (Source.TICKETMASTER,)
    assert summary.sources_succeeded == (Source.STUBHUB,)
    assert summary.observations_saved == 1
    with database() as session:
        runs = session.scalars(select(ConnectorRunRow)).all()
        assert [(row.source, row.success) for row in runs] == [("stubhub", True)]
        assert all(secret not in (row.redacted_error or "") for row in runs)


def test_failed_connector_preserves_previous_successful_snapshot(settings, database):
    event = one_event()
    old = one_observation(observed_at=NOW - timedelta(minutes=10))
    with database() as session:
        event_id = EventRepository(session).upsert(event)
        ObservationRepository(session).add(event_id, old)
        session.commit()
    connector = FakeConnector(Source.STUBHUB, {Team.TEXANS: failure(Source.STUBHUB)}, {})

    coordinator(settings, database, (connector,)).run(NOW)

    with database() as session:
        rows = session.scalars(select(ObservationRow)).all()
        assert len(rows) == 1
        assert rows[0].observed_at == old.observed_at


def test_identical_snapshot_replay_is_not_counted_or_scored_again(settings, database):
    event = one_event()
    observation = one_observation(
        listing_id="replayed",
        pair_price=Decimal("100"),
        buyer_fees=Decimal("0"),
        estimated_tax=Decimal("0"),
    )
    connector = FakeConnector(
        Source.STUBHUB,
        {Team.TEXANS: [event]},
        {event.external_id: [observation]},
    )
    scanner = coordinator(settings, database, (connector,))

    first = scanner.run(NOW)
    second = scanner.run(NOW)

    assert (first.observations_saved, first.opportunities_saved) == (1, 1)
    assert (second.observations_saved, second.opportunities_saved) == (0, 0)
    with database() as session:
        assert len(session.scalars(select(ObservationRow)).all()) == 1
        assert len(session.scalars(select(OpportunityRow)).all()) == 1


def test_conflicting_snapshot_replay_uses_stored_row_and_creates_no_estimate(
    settings, database
):
    event = one_event()
    stored = one_observation(
        listing_id="same-identity",
        pair_price=Decimal("100"),
        buyer_fees=Decimal("0"),
        estimated_tax=Decimal("0"),
    )
    with database() as session:
        event_id = EventRepository(session).upsert(event)
        ObservationRepository(session).add(event_id, stored)
        session.commit()
    conflicting = replace(stored, pair_price=Decimal("350"))
    connector = FakeConnector(
        Source.STUBHUB,
        {Team.TEXANS: [event]},
        {event.external_id: [conflicting]},
    )

    summary = coordinator(settings, database, (connector,)).run(NOW)

    assert summary.observations_saved == 0
    assert summary.opportunities_saved == 0
    with database() as session:
        observations = session.scalars(select(ObservationRow)).all()
        opportunities = session.scalars(select(OpportunityRow)).all()
        assert len(observations) == 1
        assert observations[0].pair_price == Decimal("100.00")
        assert opportunities == []


def test_unexpected_connector_error_isolated_without_raw_details(settings, database):
    secret = "Bearer credential-and-response-body"
    connector = FakeConnector(Source.STUBHUB, {Team.TEXANS: RuntimeError(secret)}, {})

    summary = coordinator(settings, database, (connector,)).run(NOW)

    assert summary.sources_failed == (Source.STUBHUB,)
    with database() as session:
        row = session.scalar(select(ConnectorRunRow))
        assert row.redacted_error == "unexpected connector error"
        assert secret not in row.redacted_error


def test_duplicate_connector_sources_are_rejected(settings, database):
    first = FakeConnector(Source.STUBHUB, {}, {})
    second = FakeConnector(Source.STUBHUB, {}, {})

    with pytest.raises(ValueError, match="duplicate connector source: stubhub"):
        coordinator(settings, database, (first, second))


def test_run_requires_aware_now(settings, database):
    scanner = coordinator(settings, database, ())

    with pytest.raises(ValueError, match="now must be timezone-aware"):
        scanner.run(datetime(2026, 8, 7, 18, 0))


def test_run_normalizes_offset_now_and_finish_to_utc(settings, database):
    offset_now = NOW.astimezone(timezone(timedelta(hours=-5)))

    summary = coordinator(settings, database, ()).run(offset_now)

    assert summary.started_at == NOW
    assert summary.started_at.tzinfo is timezone.utc
    assert summary.finished_at == FINISHED


def test_same_canonical_event_comparables_can_score_across_sources(settings, database):
    stub_event = one_event(source=Source.STUBHUB, external_id="stub-event")
    stub_comps = [
        comparable_observation(source=Source.STUBHUB, external_id="stub-event", pair_price=Decimal("300"), observed_at=NOW - timedelta(seconds=i))
        for i in range(3)
    ]
    stubhub = FakeConnector(Source.STUBHUB, {Team.TEXANS: [stub_event]}, {"stub-event": stub_comps})
    tm_event = replace(
        stub_event,
        source=Source.TICKETMASTER,
        external_id="tm-event",
        opponent="OPPONENT!",
        venue="Reliant Stadium",
        starts_at=stub_event.starts_at + timedelta(minutes=90),
    )
    candidate = one_observation(source=Source.TICKETMASTER, external_id="tm-event", listing_id="candidate", pair_price=Decimal("100"), buyer_fees=Decimal("0"), estimated_tax=Decimal("0"))
    ticketmaster = FakeConnector(Source.TICKETMASTER, {Team.TEXANS: [tm_event]}, {"tm-event": [candidate]})

    summary = coordinator(settings, database, (stubhub, ticketmaster)).run(NOW)

    assert summary.actionable_opportunities == 1
    with database() as session:
        assert len(session.scalars(select(EventRow)).all()) == 1


def test_ambiguous_event_matches_are_not_merged(settings, database):
    early = one_event(
        source=Source.STUBHUB,
        external_id="early",
        opponent="Jacksonville Jaguars",
        starts_at=NOW + timedelta(days=30, hours=1),
    )
    late = replace(
        early,
        source=Source.TICKETMASTER,
        external_id="late",
        starts_at=early.starts_at + timedelta(hours=2),
    )
    with database() as session:
        EventRepository(session).upsert(early)
        EventRepository(session).upsert(late)
        session.commit()
    ambiguous = replace(
        early,
        source=Source.SEATGEEK,
        external_id="ambiguous",
        opponent="JACKSONVILLE-JAGUARS",
        venue="Reliant Stadium",
        starts_at=early.starts_at + timedelta(hours=1),
    )
    connector = FakeConnector(
        Source.SEATGEEK,
        {Team.TEXANS: [ambiguous]},
        {ambiguous.external_id: []},
    )

    coordinator(settings, database, (connector,)).run(NOW)

    with database() as session:
        assert len(session.scalars(select(EventRow)).all()) == 3


def test_alert_service_receives_only_fresh_actionable_estimates(settings, database):
    class Alerts:
        def __init__(self):
            self.calls = []

        def evaluate_and_send(self, opportunity_id, now):
            self.calls.append((opportunity_id, now))

    event = one_event()
    candidate = one_observation(listing_id="candidate", pair_price=Decimal("100"), buyer_fees=Decimal("0"), estimated_tax=Decimal("0"))
    comps = [comparable_observation(pair_price=Decimal("300"), observed_at=NOW - timedelta(seconds=i)) for i in range(3)]
    connector = FakeConnector(Source.STUBHUB, {Team.TEXANS: [event]}, {"event-1": [candidate, *comps]})
    alerts = Alerts()

    summary = coordinator(settings, database, (connector,), alert_service=alerts).run(NOW)

    assert summary.actionable_opportunities == 1
    assert len(alerts.calls) == 1
    assert alerts.calls[0][1] == NOW


def test_opportunity_commits_before_alert_evaluation(settings, database):
    class Alerts:
        committed_opportunity_id = None

        def evaluate_and_send(self, opportunity_id, now):
            with database() as session:
                assert session.get(OpportunityRow, opportunity_id) is not None
            self.committed_opportunity_id = opportunity_id

    event = one_event()
    candidate = one_observation(
        listing_id="candidate",
        pair_price=Decimal("100"),
        buyer_fees=Decimal("0"),
        estimated_tax=Decimal("0"),
    )
    comps = [
        comparable_observation(
            pair_price=Decimal("300"), observed_at=NOW - timedelta(seconds=i)
        )
        for i in range(3)
    ]
    connector = FakeConnector(
        Source.STUBHUB,
        {Team.TEXANS: [event]},
        {"event-1": [candidate, *comps]},
    )
    alerts = Alerts()

    summary = coordinator(
        settings, database, (connector,), alert_service=alerts
    ).run(NOW)

    assert summary.sources_succeeded == (Source.STUBHUB,)
    assert alerts.committed_opportunity_id is not None


def test_notification_failure_does_not_fail_or_rollback_connector_scan(
    settings, database
):
    class Alerts:
        def evaluate_and_send(self, opportunity_id, now):
            raise RuntimeError("notification boundary failed")

    event = one_event()
    candidate = one_observation(
        listing_id="candidate",
        pair_price=Decimal("100"),
        buyer_fees=Decimal("0"),
        estimated_tax=Decimal("0"),
    )
    comps = [
        comparable_observation(
            pair_price=Decimal("300"), observed_at=NOW - timedelta(seconds=i)
        )
        for i in range(3)
    ]
    connector = FakeConnector(
        Source.STUBHUB,
        {Team.TEXANS: [event]},
        {"event-1": [candidate, *comps]},
    )

    summary = coordinator(
        settings, database, (connector,), alert_service=Alerts()
    ).run(NOW)

    assert summary.sources_succeeded == (Source.STUBHUB,)
    assert summary.sources_failed == ()
    with database() as session:
        assert len(session.scalars(select(OpportunityRow)).all()) == 1
        runs = session.scalars(select(ConnectorRunRow)).all()
        assert len(runs) == 1
        assert runs[0].success is True


def test_real_alert_service_commits_dedupes_and_retries_without_failing_scan(
    settings, database
):
    event = one_event()
    first_candidate = one_observation(
        listing_id="candidate-1",
        pair_price=Decimal("100"),
        buyer_fees=Decimal("0"),
        estimated_tax=Decimal("0"),
    )
    comps = [
        comparable_observation(
            pair_price=Decimal("300"), observed_at=NOW - timedelta(seconds=i)
        )
        for i in range(3)
    ]
    first_connector = FakeConnector(
        Source.STUBHUB,
        {Team.TEXANS: [event]},
        {"event-1": [first_candidate, *comps]},
    )

    first_summary = coordinator(
        settings,
        database,
        (first_connector,),
        alert_service=AlertService(settings, database),
    ).run(NOW)

    assert first_summary.sources_succeeded == (Source.STUBHUB,)
    with database() as session:
        first_opportunity = session.scalars(
            select(OpportunityRow).order_by(OpportunityRow.id)
        ).one()
        first_alert = session.scalars(select(AlertRow)).one()
        assert first_alert.opportunity_id == first_opportunity.id
        assert first_alert.provider_message_id == "dry-run"
        assert session.scalars(select(ConnectorRunRow)).one().success is True

    restarted = AlertService(settings, database).evaluate_and_send(
        first_opportunity.id, NOW
    )
    assert restarted.should_send is False
    assert restarted.reason == "already sent"

    class TimeoutPublisher:
        def __init__(self):
            self.calls = 0

        def publish(self, message):
            self.calls += 1
            raise NotificationFailure(retryable=True)

    live_settings = settings.model_copy(update={"dry_run": False})
    timeout = TimeoutPublisher()
    second_candidate = one_observation(
        listing_id="candidate-2",
        observed_at=NOW + timedelta(minutes=1),
        pair_price=Decimal("100"),
        buyer_fees=Decimal("0"),
        estimated_tax=Decimal("0"),
    )
    second_connector = FakeConnector(
        Source.STUBHUB,
        {Team.TEXANS: [event]},
        {"event-1": [second_candidate]},
    )

    second_summary = coordinator(
        live_settings,
        database,
        (second_connector,),
        alert_service=AlertService(
            live_settings, database, publisher=timeout
        ),
    ).run(NOW + timedelta(minutes=1))

    assert second_summary.sources_succeeded == (Source.STUBHUB,)
    assert second_summary.sources_failed == ()
    assert timeout.calls == 1
    with database() as session:
        opportunities = session.scalars(
            select(OpportunityRow).order_by(OpportunityRow.id)
        ).all()
        assert len(opportunities) == 2
        second_opportunity_id = opportunities[-1].id
        assert len(session.scalars(select(AlertRow)).all()) == 1
        runs = session.scalars(
            select(ConnectorRunRow).order_by(ConnectorRunRow.id)
        ).all()
        assert len(runs) == 2
        assert all(run.success is True for run in runs)

    class SuccessPublisher:
        def publish(self, message):
            return "provider-retry"

    retry = AlertService(
        live_settings, database, publisher=SuccessPublisher()
    ).evaluate_and_send(second_opportunity_id, NOW + timedelta(minutes=1))

    assert retry.should_send is True
    with database() as session:
        alerts = session.scalars(select(AlertRow).order_by(AlertRow.id)).all()
        assert [alert.provider_message_id for alert in alerts] == [
            "dry-run",
            "provider-retry",
        ]


def test_build_services_accepts_injected_fakes_without_calling_connectors(settings, database):
    connector = FakeConnector(Source.STUBHUB, {}, {})

    services = build_services(
        settings,
        connectors=(connector,),
        session_factory=database,
        repository_factory=RepositoryBundle,
        clock=lambda: FINISHED,
    )

    assert services.base_settings is settings
    assert services.session_factory is database
    assert services.repository_factory is RepositoryBundle
    assert services.connectors == (connector,)
    assert services.scanner.connectors == (connector,)
    assert connector.discover_calls == []
    assert services.alert_service is None
