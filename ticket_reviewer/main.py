"""FastAPI application factory."""

from fastapi import FastAPI

from ticket_reviewer.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the local Ticket Listing Reviewer application."""

    app = FastAPI(title="Ticket Listing Reviewer")
    app.state.settings = settings or Settings()

    @app.get("/healthz")
    def healthz() -> dict[str, str | bool]:
        return {"status": "ok", "dry_run": app.state.settings.dry_run}

    return app


app = create_app()
