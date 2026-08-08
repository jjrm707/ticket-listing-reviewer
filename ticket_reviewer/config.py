"""Application configuration models."""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings for the local, dry-run application."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TR_",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8765
    scan_interval_minutes: int = 60
    budget_cap: Decimal = Decimal("400.00")
    alert_profit_threshold: Decimal = Decimal("50.00")
    profit_improvement_threshold: Decimal = Decimal("20.00")
    observation_freshness_minutes: int = 120
    ticketmaster_seller_fee_rate: Decimal = Decimal("0.15")
    seatgeek_seller_fee_rate: Decimal = Decimal("0.15")
    stubhub_seller_fee_rate: Decimal = Decimal("0.15")
    timezone: str = "America/Chicago"
    database_url: str = "sqlite:///./data/ticket_reviewer.db"
    screenshot_directory: Path = Path("data/screenshots")
    dry_run: bool = True

    ticketmaster_api_key: SecretStr | None = None
    seatgeek_client_id: SecretStr | None = None
    seatgeek_client_secret: SecretStr | None = None
    stubhub_api_key: SecretStr | None = None
    ntfy_topic: SecretStr | None = None
    ntfy_access_token: SecretStr | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """The non-secret settings available to runtime services."""

    budget_cap: Decimal
    alert_profit_threshold: Decimal
    profit_improvement_threshold: Decimal
    observation_freshness_minutes: int
    scan_interval_minutes: int
    ticketmaster_seller_fee_rate: Decimal
    seatgeek_seller_fee_rate: Decimal
    stubhub_seller_fee_rate: Decimal

    @classmethod
    def from_settings(cls, settings: Settings) -> "RuntimeSettings":
        """Extract the non-secret settings used by runtime services."""

        return cls(
            budget_cap=settings.budget_cap,
            alert_profit_threshold=settings.alert_profit_threshold,
            profit_improvement_threshold=settings.profit_improvement_threshold,
            observation_freshness_minutes=settings.observation_freshness_minutes,
            scan_interval_minutes=settings.scan_interval_minutes,
            ticketmaster_seller_fee_rate=settings.ticketmaster_seller_fee_rate,
            seatgeek_seller_fee_rate=settings.seatgeek_seller_fee_rate,
            stubhub_seller_fee_rate=settings.stubhub_seller_fee_rate,
        )
