from decimal import Decimal

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
