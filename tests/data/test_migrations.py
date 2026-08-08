from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from ticket_reviewer.data.schema import Base


APPLICATION_TABLES = {
    "alerts",
    "connector_runs",
    "events",
    "manual_reviews",
    "observations",
    "opportunities",
    "outcomes",
    "settings",
    "source_events",
}


def test_initial_migration_upgrades_empty_database_to_exact_schema(tmp_path):
    database_path = tmp_path / "migration.db"
    assert not database_path.exists()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert tables - {"alembic_version"} == APPLICATION_TABLES


def test_initial_migration_creates_snapshot_and_fingerprint_uniqueness(tmp_path):
    database_path = tmp_path / "constraints.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path}")

    try:
        inspector = inspect(engine)
        observation_constraints = inspector.get_unique_constraints("observations")
        alert_constraints = inspector.get_unique_constraints("alerts")
        source_event_constraints = inspector.get_unique_constraints("source_events")
    finally:
        engine.dispose()

    assert {tuple(item["column_names"]) for item in observation_constraints} == {
        ("source", "event_external_id", "listing_identity", "observed_at")
    }
    assert {tuple(item["column_names"]) for item in alert_constraints} == {
        ("fingerprint",)
    }
    assert {tuple(item["column_names"]) for item in source_event_constraints} == {
        ("source", "external_id")
    }


def test_migrated_snapshot_unique_constraint_is_enforced(tmp_path):
    database_path = tmp_path / "enforcement.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path}")

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO events "
                "(id, team, opponent, venue, starts_at, is_home) "
                "VALUES (1, 'texans', 'Opponent', 'NRG Stadium', "
                "'2026-09-13 17:00:00', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO observations "
                "(event_id, source, event_external_id, observed_at, kind, currency, "
                "listing_identity, freshness_at) VALUES "
                "(1, 'stubhub', 'event-1', '2026-08-01 12:00:00', "
                "'listing', 'USD', 'listing:123', '2026-08-01 12:00:00')"
            )
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO observations "
                    "(event_id, source, event_external_id, observed_at, kind, currency, "
                    "listing_identity, freshness_at) VALUES "
                    "(1, 'stubhub', 'event-1', '2026-08-01 12:00:00', "
                    "'listing', 'USD', 'listing:123', '2026-08-01 12:00:00')"
                )
            )
    engine.dispose()


def test_migrated_schema_matches_orm_metadata(tmp_path):
    database_path = tmp_path / "metadata.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path}")

    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()
