"""Single-process scheduling for immediate and hourly marketplace scans."""

from collections.abc import Callable
from datetime import datetime, timezone
import inspect
import logging
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler


_JOB_ID = "hourly-market-scan"
_SCAN_INTERVAL_MINUTES = 60
_LOGGER = logging.getLogger(__name__)


def _as_utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("clock must return an aware UTC datetime")
    try:
        offset = value.utcoffset()
        normalized = value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValueError("clock must return an aware UTC datetime") from error
    if offset is None or normalized.year in {datetime.min.year, datetime.max.year}:
        raise ValueError("clock must return an aware UTC datetime")
    return normalized


def _validate_interval(settings: object) -> None:
    interval = getattr(settings, "scan_interval_minutes", None)
    if isinstance(interval, bool) or not isinstance(interval, int):
        raise TypeError("scan interval must be exactly 60 minutes")
    if interval != _SCAN_INTERVAL_MINUTES:
        raise ValueError("scan interval must be exactly 60 minutes")


def _accepts_timestamp(scan_callable: Callable[..., Any]) -> bool:
    if not callable(scan_callable):
        raise TypeError("scan callable must be callable")
    try:
        callable_signature = inspect.signature(scan_callable)
    except (TypeError, ValueError) as error:
        raise TypeError("scan callable must accept a timestamp or no arguments") from error
    marker = datetime(2000, 1, 1, tzinfo=timezone.utc)
    try:
        callable_signature.bind(marker)
    except TypeError:
        try:
            callable_signature.bind()
        except TypeError as error:
            raise TypeError(
                "scan callable must accept a timestamp or no arguments"
            ) from error
        return False
    return True


def build_scheduler(
    scan_callable: Callable[..., Any],
    settings: object,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> BackgroundScheduler:
    """Build, but do not start, one UTC scheduler with one fixed hourly job."""

    _validate_interval(settings)
    passes_timestamp = _accepts_timestamp(scan_callable)
    effective_clock = clock or (lambda: datetime.now(timezone.utc))
    first_run = _as_utc(now if now is not None else effective_clock())
    def run_scan() -> None:
        try:
            current = _as_utc(effective_clock())
            if passes_timestamp:
                scan_callable(current)
            else:
                scan_callable()
        except Exception:
            _LOGGER.error("scheduled scan failed")

    scheduler = BackgroundScheduler(timezone=timezone.utc)
    scheduler.add_job(
        run_scan,
        trigger="interval",
        minutes=_SCAN_INTERVAL_MINUTES,
        id=_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        next_run_time=first_run,
    )
    return scheduler
