"""FastAPI application factory and explicitly owned runtime lifespan."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy.engine import make_url

from ticket_reviewer.bootstrap import ApplicationServices, build_services
from ticket_reviewer.config import Settings
from ticket_reviewer.services.scheduler import build_scheduler


class Scheduler(Protocol):
    def start(self) -> None: ...

    def shutdown(self, *, wait: bool) -> None: ...


def run_migrations(database_url: str) -> None:
    """Upgrade the configured local database before runtime services exist."""

    database = make_url(database_url)
    if database.get_backend_name() == "sqlite" and database.database not in {
        None,
        "",
        ":memory:",
    }:
        Path(database.database).parent.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(root / "ticket_reviewer" / "data" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def _cleanup(
    scheduler: Scheduler | None,
    services: ApplicationServices | Any | None,
) -> BaseException | None:
    first_error: BaseException | None = None
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except BaseException as error:
            first_error = error
    if services is not None:
        try:
            services.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
            else:
                first_error.add_note("additional application cleanup failed")
    return first_error


def create_app(
    settings: Settings | None = None,
    *,
    migration_runner: Callable[[str], None] = run_migrations,
    services_factory: Callable[[Settings], ApplicationServices] = build_services,
    scheduler_factory: Callable[..., Scheduler] = build_scheduler,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Create the local Ticket Listing Reviewer application."""

    effective_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services: ApplicationServices | Any | None = None
        scheduler: Scheduler | None = None
        try:
            migration_runner(effective_settings.database_url)
            services = services_factory(effective_settings)
            app.state.services = services
            scheduler = scheduler_factory(
                services.scanner.run,
                effective_settings,
                clock=clock,
            )
            app.state.scheduler = scheduler
            scheduler.start()
            yield
        except BaseException as original_error:
            cleanup_error = _cleanup(scheduler, services)
            if cleanup_error is not None:
                original_error.add_note("additional application cleanup failed")
            raise
        else:
            cleanup_error = _cleanup(scheduler, services)
            if cleanup_error is not None:
                raise cleanup_error

    app = FastAPI(title="Ticket Listing Reviewer", lifespan=lifespan)
    app.state.settings = effective_settings

    @app.get("/healthz")
    def healthz() -> dict[str, str | bool]:
        return {"status": "ok", "dry_run": app.state.settings.dry_run}

    return app


app = create_app()
