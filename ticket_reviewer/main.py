"""FastAPI application factory and explicitly owned runtime lifespan."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import secrets
from typing import Any, Protocol

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import make_url

from ticket_reviewer.bootstrap import ApplicationServices, build_services
from ticket_reviewer.config import Settings
from ticket_reviewer.services.scheduler import build_scheduler
from ticket_reviewer.services.instance_guard import FileInstanceGuard
from ticket_reviewer.services.keyed_locks import KeyedLockRegistry
from ticket_reviewer.services.alerts import GLOBAL_LINEAGE_LOCKS
from ticket_reviewer.services.ocr import OcrEngine, TesseractOcrEngine
from ticket_reviewer.web import router as web_router
from ticket_reviewer.web.routes import templates as web_templates


class Scheduler(Protocol):
    def start(self) -> None: ...

    def shutdown(self, *, wait: bool) -> None: ...


class InstanceGuard(Protocol):
    def acquire(self) -> None: ...

    def release(self) -> None: ...


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
    instance_guard: InstanceGuard | None = None,
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
    if instance_guard is not None:
        try:
            instance_guard.release()
        except BaseException as error:
            if first_error is None:
                first_error = error
            else:
                first_error.add_note("additional application cleanup failed")
    return first_error


def _default_instance_guard(settings: Settings) -> FileInstanceGuard:
    return FileInstanceGuard.for_database_url(settings.database_url)


def _host_is_local(raw_headers: list[tuple[bytes, bytes]], port: int) -> bool:
    hosts = [value for name, value in raw_headers if name.lower() == b"host"]
    if len(hosts) != 1:
        return False
    try:
        value = hosts[0].decode("ascii")
    except UnicodeDecodeError:
        return False
    if (
        not value
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
        or any(character in value for character in ",@/%\\?#")
    ):
        return False
    allowed = {"127.0.0.1", "localhost", "[::1]"}
    return value in allowed or value in {f"{host}:{port}" for host in allowed}


def _scan_summary(summary: object) -> dict[str, object]:
    def source_values(name: str) -> list[str]:
        values = getattr(summary, name, ())
        if not isinstance(values, tuple):
            return []
        result: list[str] = []
        for value in values:
            raw = getattr(value, "value", None)
            if isinstance(raw, str) and len(raw) <= 32 and raw.isascii():
                result.append(raw)
        return result[:8]

    def count(name: str) -> int:
        value = getattr(summary, name, 0)
        return value if type(value) is int and 0 <= value <= 1_000_000 else 0

    return {
        "sources_succeeded": source_values("sources_succeeded"),
        "sources_failed": source_values("sources_failed"),
        "events_seen": count("events_seen"),
        "observations_saved": count("observations_saved"),
        "opportunities_saved": count("opportunities_saved"),
        "actionable_opportunities": count("actionable_opportunities"),
        "purchases_attempted": 0,
    }


def create_app(
    settings: Settings | None = None,
    *,
    migration_runner: Callable[[str], None] = run_migrations,
    services_factory: Callable[[Settings], ApplicationServices] = build_services,
    scheduler_factory: Callable[..., Scheduler] = build_scheduler,
    instance_guard_factory: Callable[[Settings], InstanceGuard] = _default_instance_guard,
    clock: Callable[[], datetime] | None = None,
    ocr_engine: OcrEngine | None = None,
    testing: bool = False,
    test_scan_runner: Callable[[datetime], object] | None = None,
) -> FastAPI:
    """Create the local Ticket Listing Reviewer application."""

    effective_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services: ApplicationServices | Any | None = None
        scheduler: Scheduler | None = None
        instance_guard: InstanceGuard | None = None
        try:
            instance_guard = instance_guard_factory(effective_settings)
            instance_guard.acquire()
            migration_runner(effective_settings.database_url)
            services = services_factory(effective_settings)
            app.state.services = services
            reconcile = getattr(getattr(services, "alert_service", None), "reconcile_unattempted", None)
            if callable(reconcile):
                try:
                    reconcile()
                except Exception:
                    pass
            scheduler = scheduler_factory(
                services.scanner.run,
                effective_settings,
                clock=clock,
            )
            app.state.scheduler = scheduler
            scheduler.start()
            yield
        except BaseException as original_error:
            cleanup_error = _cleanup(scheduler, services, instance_guard)
            if cleanup_error is not None:
                original_error.add_note("additional application cleanup failed")
            raise
        else:
            cleanup_error = _cleanup(scheduler, services, instance_guard)
            if cleanup_error is not None:
                raise cleanup_error

    app = FastAPI(title="Ticket Listing Reviewer", lifespan=lifespan)
    app.state.settings = effective_settings
    app.state.testing = testing is True
    app.state.clock = clock or (lambda: datetime.now(timezone.utc))
    app.state.ocr_engine = ocr_engine if ocr_engine is not None else TesseractOcrEngine()
    app.state.manual_csrf_token = secrets.token_urlsafe(32)
    app.state.manual_confirmation_locks = KeyedLockRegistry[int]()
    app.state.outcome_locks = KeyedLockRegistry[int]()
    app.state.lineage_locks = GLOBAL_LINEAGE_LOCKS
    web_root = Path(__file__).resolve().parent / "web"
    app.mount("/static", StaticFiles(directory=str(web_root / "static")), name="static")
    app.include_router(web_router)

    @app.exception_handler(HTTPException)
    async def safe_http_error(request: Request, error: HTTPException):
        if request.url.path == "/healthz":
            return JSONResponse({"detail": "Request failed"}, status_code=error.status_code)
        message = "Event not found" if error.status_code == 404 else "Invalid dashboard request"
        return web_templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": error.status_code, "message": message},
            status_code=error.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request: Request, _error: RequestValidationError):
        if request.url.path == "/healthz":
            return JSONResponse({"detail": "Invalid request"}, status_code=422)
        return web_templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": 422, "message": "Invalid dashboard request"},
            status_code=422,
        )

    @app.middleware("http")
    async def dashboard_headers(request, call_next):
        response = await call_next(request)
        if (
            response.headers.get("content-type", "").startswith("text/html")
            or 300 <= response.status_code < 400
        ):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self'; object-src 'none'; img-src 'self' data:; "
                "style-src 'self'; script-src 'self'; "
                "connect-src 'none'"
            )
        return response

    @app.middleware("http")
    async def local_host_only(request: Request, call_next):
        if not _host_is_local(request.scope.get("headers", []), effective_settings.port):
            return JSONResponse(
                {"detail": "Invalid local request"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        return await call_next(request)

    if testing is True:

        @app.post("/internal/scan", include_in_schema=False)
        def internal_scan(request: Request) -> dict[str, object]:
            headers = [
                value
                for name, value in request.scope.get("headers", [])
                if name.lower() == b"x-dry-run-test"
            ]
            if (
                headers != [b"1"]
                or not effective_settings.dry_run
                or test_scan_runner is None
            ):
                raise HTTPException(status_code=404, detail="Not found")
            now = app.state.clock()
            if (
                not isinstance(now, datetime)
                or now.tzinfo is None
                or now.utcoffset() is None
            ):
                raise HTTPException(status_code=503, detail="Scan unavailable")
            summary = test_scan_runner(now.astimezone(timezone.utc))
            return _scan_summary(summary)

    @app.get("/healthz")
    def healthz() -> dict[str, str | bool]:
        return {"status": "ok", "dry_run": app.state.settings.dry_run}

    return app


app = create_app()
