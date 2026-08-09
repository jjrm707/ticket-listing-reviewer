from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from ticket_reviewer.config import Settings


def test_safe_defaults_are_local_and_dry_run():
    settings = Settings(_env_file=None)

    assert settings.host == "127.0.0.1"
    assert settings.scan_interval_minutes == 60
    assert settings.budget_cap == Decimal("400.00")
    assert settings.alert_profit_threshold == Decimal("50.00")
    assert settings.profit_improvement_threshold == Decimal("20.00")
    assert settings.observation_freshness_minutes == 120
    assert settings.stubhub_seller_fee_rate == Decimal("0.15")
    assert settings.dry_run is True


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_settings_accept_only_deliberate_loopback_host_forms(host):
    assert Settings(_env_file=None, host=host).host == host


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "192.168.1.50",
        "localhost.example",
        "LOCALHOST",
        " localhost",
        "localhost ",
        "localhost:8765",
        "user@localhost",
        "127.0.0.1\npublic.example",
        "\u212alocalhost",
    ],
)
def test_settings_reject_noncanonical_or_nonloopback_hosts(host):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, host=host)


def test_env_example_parses_with_safe_defaults_and_blank_secrets(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(f"TR_{name.upper()}", raising=False)
    settings = Settings(_env_file=Path(__file__).parents[1] / ".env.example")

    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.scan_interval_minutes == 60
    assert settings.dry_run is True
    for name in (
        "ticketmaster_api_key",
        "seatgeek_client_id",
        "seatgeek_client_secret",
        "stubhub_client_id",
        "stubhub_client_secret",
        "ntfy_topic",
        "ntfy_access_token",
    ):
        secret = getattr(settings, name)
        assert secret is None or secret.get_secret_value() == ""
