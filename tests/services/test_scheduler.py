import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from ticket_reviewer.config import Settings
from ticket_reviewer.services.scheduler import build_scheduler


NOW = datetime(2026, 8, 8, 15, 30, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 8, 16, 30, tzinfo=timezone(timedelta(hours=-5)))


@pytest.fixture
def settings():
    return Settings(_env_file=None)


def test_scheduler_has_one_immediate_fixed_hourly_job(settings):
    scheduler = build_scheduler(lambda _now: None, settings, now=NOW)

    jobs = scheduler.get_jobs()

    assert [job.id for job in jobs] == ["hourly-market-scan"]
    job = jobs[0]
    assert job.next_run_time == NOW
    assert job.trigger.interval == timedelta(minutes=60)
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.misfire_grace_time == 300
    assert scheduler.timezone == timezone.utc
    assert scheduler.running is False


def test_scheduler_normalizes_immediate_run_time_to_utc(settings):
    central = datetime(2026, 8, 8, 10, 30, tzinfo=timezone(timedelta(hours=-5)))

    scheduler = build_scheduler(lambda _now: None, settings, now=central)

    assert scheduler.get_job("hourly-market-scan").next_run_time == NOW


def test_scheduled_scan_reads_a_fresh_aware_utc_clock_each_time(settings):
    clock_values = iter((LATER, LATER + timedelta(hours=1)))
    calls = []
    scheduler = build_scheduler(
        calls.append,
        settings,
        now=NOW,
        clock=lambda: next(clock_values),
    )
    job = scheduler.get_job("hourly-market-scan")

    job.func()
    job.func()

    assert calls == [
        datetime(2026, 8, 8, 21, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 8, 22, 30, tzinfo=timezone.utc),
    ]
    assert all(call.tzinfo == timezone.utc for call in calls)


def test_zero_argument_callable_is_supported_without_receiving_settings(settings):
    calls = []
    scheduler = build_scheduler(lambda: calls.append("scan"), settings, now=NOW)

    scheduler.get_job("hourly-market-scan").func()

    assert calls == ["scan"]


@pytest.mark.parametrize("interval", [True, False, 60.0, "60", None, 0, 59, 61])
def test_scheduler_rejects_non_integer_or_non_hourly_settings_before_mutation(
    monkeypatch, interval
):
    mutations = []
    original = BackgroundScheduler.add_job

    def record_mutation(self, *args, **kwargs):
        mutations.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BackgroundScheduler, "add_job", record_mutation)

    with pytest.raises((TypeError, ValueError), match="scan interval"):
        build_scheduler(lambda _now: None, SimpleNamespace(scan_interval_minutes=interval))

    assert mutations == []


@pytest.mark.parametrize(
    "bad_now",
    [
        None,
        "2026-08-08T15:30:00Z",
        datetime(2026, 8, 8, 15, 30),
        datetime.min.replace(tzinfo=timezone.utc),
        datetime.max.replace(tzinfo=timezone.utc),
    ],
)
def test_invalid_construction_clock_fails_before_scheduler_mutation(
    monkeypatch, settings, bad_now
):
    mutations = []
    monkeypatch.setattr(
        BackgroundScheduler,
        "add_job",
        lambda *args, **kwargs: mutations.append((args, kwargs)),
    )

    with pytest.raises((TypeError, ValueError), match="aware UTC datetime"):
        build_scheduler(
            lambda _now: None,
            settings,
            clock=lambda: bad_now,
        )

    assert mutations == []


def test_explicit_invalid_now_fails_before_clock_is_read_or_scheduler_mutates(
    monkeypatch, settings
):
    clock_read = False
    mutations = []

    def clock():
        nonlocal clock_read
        clock_read = True
        return NOW

    monkeypatch.setattr(
        BackgroundScheduler,
        "add_job",
        lambda *args, **kwargs: mutations.append((args, kwargs)),
    )

    with pytest.raises((TypeError, ValueError), match="aware UTC datetime"):
        build_scheduler(
            lambda _now: None,
            settings,
            now=NOW.replace(tzinfo=None),
            clock=clock,
        )

    assert clock_read is False
    assert mutations == []


def test_invalid_later_clock_value_is_contained_as_a_generic_scan_failure(
    settings, caplog
):
    scheduler = build_scheduler(
        lambda _now: pytest.fail("scan must not run"),
        settings,
        now=NOW,
        clock=lambda: "secret://connector.example/private?token=credential",
    )
    job = scheduler.get_job("hourly-market-scan")

    with caplog.at_level(logging.ERROR):
        assert job.func() is None

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert rendered == "scheduled scan failed"
    assert "secret" not in rendered
    assert "credential" not in rendered
    assert scheduler.get_job("hourly-market-scan") is job


def test_scan_exception_is_contained_without_logging_private_details(settings, caplog):
    private = "secret://connector.example/private?token=credential"

    def fail(_now):
        raise RuntimeError(private, {"raw": "connector payload"})

    scheduler = build_scheduler(fail, settings, now=NOW, clock=lambda: LATER)
    job = scheduler.get_job("hourly-market-scan")

    with caplog.at_level(logging.ERROR):
        assert job.func() is None

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert rendered == "scheduled scan failed"
    assert private not in rendered
    assert "connector payload" not in rendered
    assert all(record.exc_info is None for record in caplog.records)
    assert scheduler.get_job("hourly-market-scan") is job


def test_scheduler_restores_its_generic_error_log_after_migration_logging_setup(
    monkeypatch, settings, caplog
):
    scheduler_logger = logging.getLogger("ticket_reviewer.services.scheduler")
    monkeypatch.setattr(scheduler_logger, "disabled", True)

    def fail(_now):
        raise RuntimeError("private connector detail")

    scheduler = build_scheduler(fail, settings, now=NOW, clock=lambda: LATER)

    with caplog.at_level(logging.ERROR):
        scheduler.get_job("hourly-market-scan").func()

    assert [record.getMessage() for record in caplog.records] == [
        "scheduled scan failed"
    ]


def test_factory_does_not_start_threads_and_repeated_calls_are_isolated(
    monkeypatch, settings
):
    starts = []
    monkeypatch.setattr(BackgroundScheduler, "start", lambda self: starts.append(self))

    first = build_scheduler(lambda _now: None, settings, now=NOW)
    second = build_scheduler(lambda _now: None, settings, now=NOW)

    assert starts == []
    assert first is not second
    assert [job.id for job in first.get_jobs()] == ["hourly-market-scan"]
    assert [job.id for job in second.get_jobs()] == ["hourly-market-scan"]
