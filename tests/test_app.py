from fastapi.testclient import TestClient

from ticket_reviewer.config import Settings
from ticket_reviewer.main import create_app


def test_health_route_does_not_expose_secrets():
    settings = Settings(_env_file=None, ticketmaster_api_key="secret")

    response = TestClient(create_app(settings)).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "dry_run": True}
    assert "secret" not in response.text
