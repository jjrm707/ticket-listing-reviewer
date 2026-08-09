from datetime import timedelta

from ticket_reviewer.data.schema import ConnectorRunRow

from .conftest import NOW


def test_health_shows_connector_failure_without_secret(client, failed_run):
    response = client.get("/health")

    assert response.status_code == 200
    assert "StubHub" in response.text
    assert "authentication failed" in response.text
    assert "client_secret" not in response.text
    assert "[REDACTED]" not in response.text


def test_health_re_redacts_markup_controls_headers_cookies_and_credentials(
    client, session_factory
):
    secret = "never-render-this-value"
    with session_factory() as session:
        session.add(
            ConnectorRunRow(
                source="ticketmaster",
                started_at=NOW - timedelta(hours=3),
                finished_at=NOW - timedelta(hours=3),
                success=False,
                observation_count=0,
                redacted_error=(
                    f"<b>quota failed</b>\x01 Authorization: Bearer {secret} "
                    f"Cookie: sid={secret} api_key={secret}"
                ),
            )
        )
        session.commit()

    response = client.get("/health")

    assert response.status_code == 200
    assert "quota failed" in response.text
    assert secret not in response.text
    assert "Authorization" not in response.text
    assert "Cookie" not in response.text
    assert "api_key" not in response.text
    assert "&lt;b&gt;" not in response.text
    assert "Stale" in response.text


def test_health_empty_state_is_setup_oriented(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.text.count("Not run yet") == 3
    assert "Failure" not in response.text
    assert "event-only signals cannot alone confirm a buyable pair" in response.text


def test_health_in_progress_run_does_not_invent_an_error(client, session_factory):
    with session_factory() as session:
        session.add(
            ConnectorRunRow(
                source="stubhub",
                started_at=NOW,
                finished_at=None,
                success=None,
                observation_count=0,
                redacted_error=None,
            )
        )
        session.commit()

    response = client.get("/health")

    assert response.status_code == 200
    assert "In progress" in response.text
    assert "unexpected connector error" not in response.text
