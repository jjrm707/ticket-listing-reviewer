"""FastAPI application factory and explicitly owned runtime lifespan."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import secrets
from threading import Lock
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
from ticket_reviewer.services.ocr import OcrEngine, TesseractOcrEngine
from ticket_reviewer.web import router as web_router
from ticket_reviewer.web.routes import templates as web_templates


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
    ocr_engine: OcrEngine | None = None,
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
    app.state.clock = clock or (lambda: datetime.now().astimezone())
    app.state.ocr_engine = ocr_engine if ocr_engine is not None else TesseractOcrEngine()
    app.state.manual_csrf_token = secrets.token_urlsafe(32)
    app.state.manual_confirmation_guard = Lock()
    app.state.manual_confirmation_locks = {}
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
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self'; object-src 'none'; img-src 'self' data:; "
                "style-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
                "connect-src 'none'"
            )
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str | bool]:
        return {"status": "ok", "dry_run": app.state.settings.dry_run}

    return app


app = create_app()
