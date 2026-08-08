from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.factories import make_estimate, make_event, make_observation
from ticket_reviewer.config import Settings
from ticket_reviewer.data.db import create_engine_and_session
from ticket_reviewer.data.repositories import (
    AlertRepository,
    EventRepository,
    ObservationRepository,
    OpportunityRepository,
    OutcomeRepository,
    RunRepository,
    SettingRepository,
)
from ticket_reviewer.data.schema import Base, SettingRow
from ticket_reviewer.domain.enums import OpportunityStatus, Source


@pytest.fixture
def session_factory(tmp_path):
    engine, factory = create_engine_and_session(
        f"sqlite:///{tmp_path / 'repositories.db'}"
    )
    Base.metadata.create_all(engine)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def session(session_factory):
    with session_factory() as current:
        yield current


@pytest.fixture
def sample_event():
    return make_event()


@pytest.fixture
def sample_observation():
    return make_observation()


def _saved_opportunity(session, sample_event, sample_observation):
    event_id = EventRepository(session).upsert(sample_event)
    observation_id = ObservationRepository(session).add(event_id, sample_observation)
    opportunity_id = OpportunityRepository(session).save_estimate(
        event_id, observation_id, make_estimate()
    )
    return event_id, observation_id, opportunity_id


def test_observation_round_trip(session, sample_event, sample_observation):
    event_id = EventRepository(session).upsert(sample_event)
    observation_id = ObservationRepository(session).add(event_id, sample_observation)

    loaded = ObservationRepository(session).get(observation_id)

    assert loaded.event_id == event_id
    assert loaded.pair_price == Decimal("220.00")
    assert loaded.buyer_fees == Decimal("25.00")
    assert loaded.listing_id == "listing-123"


def test_duplicate_source_snapshot_is_idempotent(
    session, sample_event, sample_observation
):
    event_id = EventRepository(session).upsert(sample_event)

    first = ObservationRepository(session).add(event_id, sample_observation)
    second = ObservationRepository(session).add(event_id, sample_observation)

    assert second == first
    assert len(ObservationRepository(session).list_for_event(event_id)) == 1


def test_missing_listing_id_uses_stable_snapshot_identity(
    session, sample_event, sample_observation
):
    event_id = EventRepository(session).upsert(sample_event)
    observation = replace(sample_observation, listing_id=None)

    first = ObservationRepository(session).add(event_id, observation)
    second = ObservationRepository(session).add(event_id, observation)

    assert second == first


def test_event_upsert_updates_source_metadata_without_duplicating_event(
    session, sample_event
):
    repository = EventRepository(session)
    first = repository.upsert(sample_event)
    updated = replace(sample_event, url="https://example.test/events/updated")

    second = repository.upsert(updated)

    assert second == first
    assert repository.find_by_source(Source.STUBHUB, sample_event.external_id).url == (
        "https://example.test/events/updated"
    )


def test_repository_writes_disappear_when_caller_rolls_back(
    session_factory, sample_event
):
    with session_factory() as first_session:
        event_id = EventRepository(first_session).upsert(sample_event)
        first_session.rollback()

    with session_factory() as second_session:
        assert EventRepository(second_session).get(event_id) is None


def test_repository_writes_survive_when_caller_commits(session_factory, sample_event):
    with session_factory() as first_session:
        event_id = EventRepository(first_session).upsert(sample_event)
        first_session.commit()

    with session_factory() as second_session:
        assert EventRepository(second_session).get(event_id).opponent == "Opponent"


def test_datetimes_round_trip_as_aware_utc_after_reopening_database(session_factory):
    offset = timezone(timedelta(hours=5, minutes=30))
    starts_at = datetime(2026, 9, 13, 22, 30, tzinfo=offset)
    observed_at = datetime(2026, 8, 1, 18, 45, tzinfo=offset)
    event = make_event(starts_at=starts_at)
    observation = make_observation(observed_at=observed_at)

    with session_factory() as session:
        event_id = EventRepository(session).upsert(event)
        observation_id = ObservationRepository(session).add(event_id, observation)
        session.commit()

    with session_factory() as session:
        loaded_event = EventRepository(session).get(event_id)
        loaded_observation = ObservationRepository(session).get(observation_id)
        assert loaded_event.starts_at == datetime(
            2026, 9, 13, 17, 0, tzinfo=timezone.utc
        )
        assert loaded_event.starts_at.tzinfo is timezone.utc
        assert loaded_observation.observed_at == datetime(
            2026, 8, 1, 13, 15, tzinfo=timezone.utc
        )
        assert loaded_observation.observed_at.tzinfo is timezone.utc


def test_naive_datetime_is_rejected_at_database_boundary(session):
    with pytest.raises(ValueError, match="timezone-aware"):
        RunRepository(session).start(Source.STUBHUB, datetime(2026, 8, 1, 12, 0))


def test_opportunity_serializes_decimal_scenarios_losslessly(
    session, sample_event, sample_observation
):
    event_id = EventRepository(session).upsert(sample_event)
    observation_id = ObservationRepository(session).add(event_id, sample_observation)
    estimate = make_estimate()

    opportunity_id = OpportunityRepository(session).save_estimate(
        event_id, observation_id, estimate
    )
    loaded = OpportunityRepository(session).get(opportunity_id)

    assert loaded.seller_fee_rate == Decimal("0.1500")
    assert loaded.status == OpportunityStatus.NEW.value
    assert loaded.risk_reasons == ["limited comparables"]
    assert loaded.actionable is False
    assert loaded.scenarios == [
        {
            "marketplace": "stubhub",
            "projected_resale_gross": "300.00",
            "seller_fee_rate": "0.15",
            "projected_proceeds": "255.00",
            "comparable_count": 3,
        }
    ]


def test_setting_overrides_merge_over_base_nonsecret_defaults(session):
    base = Settings(
        _env_file=None,
        budget_cap=Decimal("400.00"),
        scan_interval_minutes=60,
        ticketmaster_api_key="environment-secret",
    )
    repository = SettingRepository(session)
    repository.set("budget_cap", "375.50")
    repository.set("observation_freshness_minutes", "180")

    effective = repository.effective(base)

    assert effective.budget_cap == Decimal("375.50")
    assert effective.observation_freshness_minutes == 180
    assert effective.alert_profit_threshold == Decimal("50.00")
    stored = session.scalars(select(SettingRow)).all()
    assert {(row.key, row.value) for row in stored} == {
        ("budget_cap", "375.50"),
        ("observation_freshness_minutes", "180"),
    }
    assert all("secret" not in row.value for row in stored)


@pytest.mark.parametrize(
    "key",
    ["database_url", "ticketmaster_api_key", "ntfy_topic", "unknown_setting"],
)
def test_setting_rejects_unknown_and_secret_keys(session, key):
    with pytest.raises(ValueError, match="not an allowed runtime setting"):
        SettingRepository(session).set(key, "must-not-persist")

    assert session.scalars(select(SettingRow)).all() == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("budget_cap", "not-money"),
        ("budget_cap", "0"),
        ("budget_cap", "400.01"),
        ("alert_profit_threshold", "-0.01"),
        ("profit_improvement_threshold", "-0.01"),
        ("observation_freshness_minutes", "59"),
        ("observation_freshness_minutes", "1441"),
        ("scan_interval_minutes", "30"),
        ("scan_interval_minutes", "61"),
        ("stubhub_seller_fee_rate", "0.5001"),
        ("ticketmaster_seller_fee_rate", "-0.01"),
        ("seatgeek_seller_fee_rate", "0.51"),
    ],
)
def test_setting_rejects_invalid_values(session, key, value):
    with pytest.raises(ValueError, match="invalid value"):
        SettingRepository(session).set(key, value)


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("budget_cap", "1", "1"),
        ("budget_cap", "400", "400"),
        ("alert_profit_threshold", "0", "0"),
        ("profit_improvement_threshold", "0", "0"),
        ("observation_freshness_minutes", "60", "60"),
        ("observation_freshness_minutes", "1440", "1440"),
        ("scan_interval_minutes", "60", "60"),
        ("stubhub_seller_fee_rate", "0", "0"),
        ("stubhub_seller_fee_rate", "0.50", "0.50"),
        ("ticketmaster_seller_fee_rate", "0.50", "0.50"),
        ("seatgeek_seller_fee_rate", "0.50", "0.50"),
    ],
)
def test_setting_accepts_documented_safety_boundaries(session, key, value, expected):
    repository = SettingRepository(session)

    repository.set(key, value)

    assert repository.get(key) == expected


def test_effective_revalidates_complete_merged_runtime_settings(session):
    session.add(SettingRow(key="budget_cap", value="400.01"))
    session.flush()

    with pytest.raises(ValueError):
        SettingRepository(session).effective(Settings(_env_file=None))


def test_alert_fingerprint_is_restart_safe_and_idempotent(
    session_factory, sample_event, sample_observation
):
    sent_at = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    with session_factory() as session:
        _, _, opportunity_id = _saved_opportunity(
            session, sample_event, sample_observation
        )
        first = AlertRepository(session).record(
            opportunity_id,
            "stable-fingerprint",
            sent_at,
            Decimal("55.25"),
            "provider-123",
        )
        session.commit()

    with session_factory() as session:
        repository = AlertRepository(session)
        second = repository.record(
            opportunity_id,
            "stable-fingerprint",
            sent_at,
            Decimal("55.25"),
            "provider-duplicate",
        )
        assert second == first
        assert repository.find_by_fingerprint(
            "stable-fingerprint"
        ).provider_message_id == "provider-123"


def test_outcome_save_updates_one_record_per_opportunity(
    session, sample_event, sample_observation
):
    _, _, opportunity_id = _saved_opportunity(
        session, sample_event, sample_observation
    )
    repository = OutcomeRepository(session)
    first = repository.save(
        opportunity_id,
        OpportunityStatus.PURCHASED,
        actual_acquisition=Decimal("260.00"),
        notes="Bought two",
    )
    second = repository.save(
        opportunity_id,
        OpportunityStatus.SOLD,
        actual_acquisition=Decimal("260.00"),
        actual_proceeds=Decimal("340.00"),
        actual_fees=Decimal("51.00"),
        notes="Sold both",
    )

    loaded = repository.get(opportunity_id)
    assert second == first
    assert loaded.status == OpportunityStatus.SOLD.value
    assert loaded.actual_acquisition == Decimal("260.00")
    assert loaded.actual_proceeds == Decimal("340.00")
    assert loaded.actual_fees == Decimal("51.00")
    assert loaded.notes == "Sold both"
    assert OpportunityRepository(session).get(opportunity_id).status == (
        OpportunityStatus.SOLD.value
    )


def test_connector_run_finish_records_result_and_redacts_error(session):
    repository = RunRepository(session)
    started_at = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 8, 1, 12, 1, tzinfo=timezone.utc)
    run_id = repository.start(Source.TICKETMASTER, started_at)

    repository.finish(
        run_id,
        success=False,
        observation_count=7,
        error="request failed: api_key=super-secret-value",
        finished_at=finished_at,
    )

    loaded = repository.get(run_id)
    assert loaded.started_at == started_at
    assert loaded.finished_at == finished_at
    assert loaded.success is False
    assert loaded.observation_count == 7
    assert loaded.redacted_error == "request failed: api_key=[REDACTED]"
    assert "super-secret-value" not in loaded.redacted_error


@pytest.mark.parametrize(
    ("raw_error", "secret"),
    [
        ('body={"access_token": "json-secret"}', "json-secret"),
        ("payload={'client_secret': 'dict-secret'}", "dict-secret"),
        ("Authorization: Basic basic-secret", "basic-secret"),
        ("headers={'Authorization': 'Basic python-auth-secret'}", "python-auth-secret"),
        ('headers={"Authorization": "Basic json-auth-secret"}', "json-auth-secret"),
        ("url=https://user:url-secret@example.test/path", "url-secret"),
        ("url=https://example.test/path?api_key=query-secret", "query-secret"),
        ("headers={'Cookie': 'session=python-cookie-secret'}", "python-cookie-secret"),
        ('headers={"Set-Cookie": "session=json-cookie-secret"}', "json-cookie-secret"),
    ],
)
def test_connector_run_never_persists_common_raw_secret_forms(
    session, raw_error, secret
):
    repository = RunRepository(session)
    run_id = repository.start(Source.TICKETMASTER)

    repository.finish(
        run_id,
        success=False,
        observation_count=0,
        error=raw_error,
    )

    stored = repository.get(run_id).redacted_error
    assert "[REDACTED]" in stored
    assert secret not in stored


def test_missing_listing_identity_cannot_collide_with_real_listing_id(
    session, sample_event, sample_observation
):
    event_id = EventRepository(session).upsert(sample_event)

    missing_id = ObservationRepository(session).add(
        event_id, replace(sample_observation, listing_id=None)
    )
    sentinel_shaped_id = ObservationRepository(session).add(
        event_id, replace(sample_observation, listing_id="__missing_listing_id__")
    )

    assert sentinel_shaped_id != missing_id
