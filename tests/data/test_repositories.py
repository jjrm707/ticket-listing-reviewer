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
from ticket_reviewer.data.schema import Base, EventRow, SettingRow, SourceEventRow
from ticket_reviewer.domain.enums import OpportunityStatus, Source
from ticket_reviewer.domain.models import ExitScenario


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


def test_observation_add_with_status_distinguishes_insert_from_conflicting_replay(
    session, sample_event, sample_observation
):
    repository = ObservationRepository(session)
    event_id = EventRepository(session).upsert(sample_event)

    inserted = repository.add_with_status(event_id, sample_observation)
    replayed = repository.add_with_status(
        event_id,
        replace(sample_observation, pair_price=Decimal("350.00")),
    )

    assert inserted.inserted is True
    assert replayed.inserted is False
    assert replayed.observation_id == inserted.observation_id
    assert repository.add(event_id, sample_observation) == inserted.observation_id
    assert repository.get(inserted.observation_id).pair_price == Decimal("220.00")


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


def test_source_refresh_cannot_relabel_a_shared_canonical_event(session, sample_event):
    repository = EventRepository(session)
    stub_id = repository.upsert(
        replace(sample_event, opponent="Indianapolis Colts")
    )
    ticketmaster = replace(
        sample_event,
        source=Source.TICKETMASTER,
        external_id="tm-colts",
        opponent="Colts",
    )
    assert repository.upsert(ticketmaster, canonical_event_id=stub_id) == stub_id

    remapped_id = repository.upsert(
        replace(ticketmaster, opponent="Dallas Cowboys")
    )

    assert remapped_id != stub_id
    assert session.get(EventRow, stub_id).opponent == "Indianapolis Colts"
    links = session.scalars(select(SourceEventRow).order_by(SourceEventRow.source)).all()
    assert {(row.source, row.event_id) for row in links} == {
        ("stubhub", stub_id),
        ("ticketmaster", remapped_id),
    }


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
            "comparable_count": 0,
            "comparable_observation_ids": [],
        }
    ]


def _estimate_with_comparable_ids(*ids, source=Source.STUBHUB):
    scenario = ExitScenario(
        marketplace=source,
        projected_resale_gross=Decimal("300.00"),
        seller_fee_rate=Decimal("0.15"),
        projected_proceeds=Decimal("255.00"),
        comparable_count=len(ids),
        comparable_observation_ids=tuple(ids),
    )
    return replace(make_estimate(), scenarios=(scenario,))


def test_opportunity_repository_accepts_only_exact_same_event_source_comparables(
    session, sample_event, sample_observation
):
    event_id = EventRepository(session).upsert(sample_event)
    candidate_id = ObservationRepository(session).add(event_id, sample_observation)
    first_id = ObservationRepository(session).add(
        event_id, replace(sample_observation, listing_id="first")
    )
    second_id = ObservationRepository(session).add(
        event_id, replace(sample_observation, listing_id="second")
    )

    opportunity_id = OpportunityRepository(session).save_estimate(
        event_id,
        candidate_id,
        _estimate_with_comparable_ids(first_id, second_id),
    )

    assert OpportunityRepository(session).get(opportunity_id).scenarios[0][
        "comparable_observation_ids"
    ] == [first_id, second_id]


@pytest.mark.parametrize("invalid_id", [True, 0, -1])
def test_exit_scenario_rejects_non_positive_or_bool_comparable_ids(invalid_id):
    with pytest.raises(ValueError, match="positive ints"):
        _estimate_with_comparable_ids(invalid_id)


def test_opportunity_repository_rejects_candidate_as_comparable(
    session, sample_event, sample_observation
):
    event_id = EventRepository(session).upsert(sample_event)
    candidate_id = ObservationRepository(session).add(event_id, sample_observation)

    with pytest.raises(ValueError, match="own comparable"):
        OpportunityRepository(session).save_estimate(
            event_id, candidate_id, _estimate_with_comparable_ids(candidate_id)
        )


def test_opportunity_repository_rejects_missing_foreign_or_wrong_source_ids(
    session, sample_event, sample_observation
):
    event_id = EventRepository(session).upsert(sample_event)
    candidate_id = ObservationRepository(session).add(event_id, sample_observation)
    wrong_source_id = ObservationRepository(session).add(
        event_id,
        replace(
            sample_observation,
            source=Source.SEATGEEK,
            listing_id="wrong-source",
        ),
    )
    other_event_id = EventRepository(session).upsert(
        replace(sample_event, external_id="other-event", opponent="Other")
    )
    foreign_id = ObservationRepository(session).add(
        other_event_id, replace(sample_observation, listing_id="foreign")
    )

    for invalid_id, message in (
        (999999, "does not exist"),
        (wrong_source_id, "source does not match"),
        (foreign_id, "another event"),
    ):
        with pytest.raises(ValueError, match=message):
            OpportunityRepository(session).save_estimate(
                event_id,
                candidate_id,
                _estimate_with_comparable_ids(invalid_id),
            )


def test_opportunity_repository_requires_exact_ids_for_each_new_scenario(
    session, sample_event, sample_observation
):
    event_id = EventRepository(session).upsert(sample_event)
    candidate_id = ObservationRepository(session).add(event_id, sample_observation)
    scenario = replace(
        make_estimate().scenarios[0],
        comparable_count=1,
        comparable_observation_ids=(),
    )

    with pytest.raises(ValueError, match="exact comparable observation IDs"):
        OpportunityRepository(session).save_estimate(
            event_id,
            candidate_id,
            replace(make_estimate(), scenarios=(scenario,)),
        )


@pytest.mark.parametrize("candidate_id", [True, 0, -1, 999999])
def test_opportunity_repository_rejects_invalid_or_missing_candidate_ids(
    session, sample_event, candidate_id
):
    event_id = EventRepository(session).upsert(sample_event)

    with pytest.raises(ValueError, match="candidate observation"):
        OpportunityRepository(session).save_estimate(
            event_id, candidate_id, replace(make_estimate(), scenarios=())
        )


def test_opportunity_repository_rejects_candidate_from_another_event(
    session, sample_event, sample_observation
):
    event_id = EventRepository(session).upsert(sample_event)
    other_event_id = EventRepository(session).upsert(
        replace(sample_event, external_id="other-candidate-event", opponent="Other")
    )
    candidate_id = ObservationRepository(session).add(other_event_id, sample_observation)

    with pytest.raises(ValueError, match="candidate observation belongs to another event"):
        OpportunityRepository(session).save_estimate(
            event_id, candidate_id, replace(make_estimate(), scenarios=())
        )


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
        ("observation_freshness_minutes", Decimal("60.9")),
        ("scan_interval_minutes", "30"),
        ("scan_interval_minutes", "61"),
        ("scan_interval_minutes", Decimal("60.9")),
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
        ("alert_profit_threshold", "50", "50"),
        ("profit_improvement_threshold", "20", "20"),
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


@pytest.mark.parametrize("success", [0, 1, None, "yes"])
def test_connector_run_finish_requires_a_strict_bool(session, success):
    repository = RunRepository(session)
    run_id = repository.start(Source.STUBHUB, datetime(2026, 8, 1, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        repository.finish(run_id, success=success, observation_count=0)


@pytest.mark.parametrize("count", [True, -1, 1.5, Decimal("1.0")])
def test_connector_run_finish_requires_a_nonnegative_int_count(session, count):
    repository = RunRepository(session)
    run_id = repository.start(Source.STUBHUB, datetime(2026, 8, 1, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        repository.finish(run_id, success=True, observation_count=count)


def test_connector_run_finish_requires_aware_chronological_time(session):
    repository = RunRepository(session)
    started = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    run_id = repository.start(Source.STUBHUB, started)
    for finished in (started.replace(tzinfo=None), started - timedelta(microseconds=1)):
        with pytest.raises(ValueError):
            repository.finish(
                run_id,
                success=True,
                observation_count=0,
                finished_at=finished,
            )


def test_connector_run_finish_requires_a_datetime_and_normalizes_to_utc(session):
    repository = RunRepository(session)
    started = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    run_id = repository.start(Source.STUBHUB, started)
    with pytest.raises(ValueError):
        repository.finish(
            run_id,
            success=True,
            observation_count=0,
            finished_at="2026-08-01T12:01:00Z",
        )

    repository.finish(
        run_id,
        success=True,
        observation_count=0,
        finished_at=datetime(
            2026, 8, 1, 7, 1, tzinfo=timezone(timedelta(hours=-5))
        ),
    )

    assert repository.get(run_id).finished_at == datetime(
        2026, 8, 1, 12, 1, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [("alert_profit_threshold", "49.99"), ("profit_improvement_threshold", "19.99")],
)
def test_runtime_setting_repository_enforces_hard_alert_floors(session, key, value):
    with pytest.raises(ValueError):
        SettingRepository(session).set(key, value)


@pytest.mark.parametrize(
    ("raw_error", "secrets"),
    [
        ('body={"access_token": "json-secret"}', ("json-secret",)),
        ("payload={'client_secret': 'dict-secret'}", ("dict-secret",)),
        ("Authorization: Basic basic-secret", ("basic-secret",)),
        (
            'Authorization: Digest username="digest-user", realm="digest-realm", '
            'response="digest-response"',
            ("digest-user", "digest-realm", "digest-response"),
        ),
        (
            "headers={'Authorization': 'Digest username=\"quoted-user\", "
            "response=\"quoted-response\"'}",
            ("quoted-user", "quoted-response"),
        ),
        (
            '{"Authorization":"Digest username=\\"json-user-secret\\", '
            'response=\\"json-response-secret\\""}',
            ("json-user-secret", "json-response-secret"),
        ),
        ("headers={'Authorization': 'Basic python-auth-secret'}", ("python-auth-secret",)),
        ('headers={"Authorization": "Basic json-auth-secret"}', ("json-auth-secret",)),
        ("url=https://user:url-secret@example.test/path", ("url-secret",)),
        ("url=https://example.test/path?api_key=query-secret", ("query-secret",)),
        (
            "Cookie: session=bare-cookie-one; csrf=bare-cookie-two",
            ("bare-cookie-one", "bare-cookie-two"),
        ),
        (
            "Set-Cookie: session=set-cookie-one; Path=/, csrf=set-cookie-two; Secure",
            ("set-cookie-one", "set-cookie-two"),
        ),
        (
            "headers={'Cookie': 'session=python-cookie-secret; "
            "csrf=python-csrf-secret'}",
            ("python-cookie-secret", "python-csrf-secret"),
        ),
        (
            'headers={"Set-Cookie": "session=json-cookie-secret; Path=/, '
            'csrf=json-csrf-secret"}',
            ("json-cookie-secret", "json-csrf-secret"),
        ),
        (
            '{"Cookie":"session=\\"escaped-cookie-secret\\"; '
            'csrf=\\"escaped-csrf-secret\\""}',
            ("escaped-cookie-secret", "escaped-csrf-secret"),
        ),
        (
            '{"Set-Cookie":"session=\\"escaped-set-cookie-secret\\"; Path=/, '
            'csrf=\\"escaped-set-csrf-secret\\""}',
            ("escaped-set-cookie-secret", "escaped-set-csrf-secret"),
        ),
    ],
)
def test_connector_run_never_persists_common_raw_secret_forms(
    session, raw_error, secrets
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
    assert all(secret not in stored for secret in secrets)


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
