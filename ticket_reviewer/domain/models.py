from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .enums import Confidence, ObservationKind, Source, Team


def _require_timezone_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_non_negative(value: Decimal | int | None, field_name: str) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{field_name} must be non-negative")


def _require_decimal(value: Decimal | None, field_name: str) -> None:
    if value is not None and not isinstance(value, Decimal):
        raise ValueError(f"{field_name} must be a Decimal")


def _require_rate(value: Decimal | None, field_name: str) -> None:
    if value is not None and not Decimal("0") <= value <= Decimal("1"):
        raise ValueError(f"{field_name} must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ExternalEvent:
    source: Source
    external_id: str
    team: Team
    opponent: str
    venue: str
    starts_at: datetime
    is_home: bool
    is_parking: bool
    url: str | None

    def __post_init__(self) -> None:
        _require_timezone_aware(self.starts_at, "starts_at")


@dataclass(frozen=True, slots=True)
class SourceObservation:
    source: Source
    event_external_id: str
    observed_at: datetime
    kind: ObservationKind
    currency: str
    pair_price: Decimal | None
    buyer_fees: Decimal | None
    estimated_tax: Decimal | None
    section: str | None
    row: str | None
    quantity_available: int | None
    can_buy_pair: bool | None
    listing_id: str | None
    listing_url: str | None
    listing_count: int | None = None
    popularity: Decimal | None = None

    def __post_init__(self) -> None:
        _require_timezone_aware(self.observed_at, "observed_at")
        object.__setattr__(self, "currency", self.currency.upper())
        _require_decimal(self.pair_price, "pair_price")
        _require_decimal(self.buyer_fees, "buyer_fees")
        _require_decimal(self.estimated_tax, "estimated_tax")
        _require_decimal(self.popularity, "popularity")
        _require_non_negative(self.pair_price, "pair_price")
        _require_non_negative(self.buyer_fees, "buyer_fees")
        _require_non_negative(self.estimated_tax, "estimated_tax")
        _require_non_negative(self.quantity_available, "quantity_available")
        _require_non_negative(self.listing_count, "listing_count")
        _require_non_negative(self.popularity, "popularity")


@dataclass(frozen=True, slots=True)
class Comparable:
    """A normalized same-event price signal used for later section scoring."""

    source: Source
    event_external_id: str
    kind: ObservationKind
    currency: str
    price_basis: Decimal | None
    proceeds_basis: Decimal | None
    section: str | None
    observed_at: datetime
    relevance: Decimal
    quality: Decimal

    def __post_init__(self) -> None:
        _require_timezone_aware(self.observed_at, "observed_at")
        object.__setattr__(self, "currency", self.currency.upper())
        _require_decimal(self.price_basis, "price_basis")
        _require_decimal(self.proceeds_basis, "proceeds_basis")
        _require_decimal(self.relevance, "relevance")
        _require_decimal(self.quality, "quality")
        _require_non_negative(self.price_basis, "price_basis")
        _require_non_negative(self.proceeds_basis, "proceeds_basis")
        _require_non_negative(self.relevance, "relevance")
        _require_non_negative(self.quality, "quality")


@dataclass(frozen=True, slots=True)
class ExitScenario:
    marketplace: Source
    projected_resale_gross: Decimal
    seller_fee_rate: Decimal
    projected_proceeds: Decimal
    comparable_count: int

    def __post_init__(self) -> None:
        _require_decimal(self.projected_resale_gross, "projected_resale_gross")
        _require_decimal(self.seller_fee_rate, "seller_fee_rate")
        _require_decimal(self.projected_proceeds, "projected_proceeds")
        _require_non_negative(self.projected_resale_gross, "projected_resale_gross")
        _require_rate(self.seller_fee_rate, "seller_fee_rate")
        _require_non_negative(self.projected_proceeds, "projected_proceeds")
        _require_non_negative(self.comparable_count, "comparable_count")


@dataclass(frozen=True, slots=True)
class OpportunityEstimate:
    acquisition_total: Decimal
    exit_source: Source | None
    projected_resale_gross: Decimal | None
    seller_fee_rate: Decimal | None
    projected_proceeds: Decimal | None
    estimated_net_profit: Decimal | None
    roi: Decimal | None
    confidence: Confidence
    risk_reasons: tuple[str, ...]
    actionable: bool
    scenarios: tuple[ExitScenario, ...]

    def __post_init__(self) -> None:
        _require_decimal(self.acquisition_total, "acquisition_total")
        _require_decimal(self.projected_resale_gross, "projected_resale_gross")
        _require_decimal(self.seller_fee_rate, "seller_fee_rate")
        _require_decimal(self.projected_proceeds, "projected_proceeds")
        _require_decimal(self.estimated_net_profit, "estimated_net_profit")
        _require_decimal(self.roi, "roi")
        _require_non_negative(self.acquisition_total, "acquisition_total")
        _require_non_negative(self.projected_resale_gross, "projected_resale_gross")
        _require_rate(self.seller_fee_rate, "seller_fee_rate")
        _require_non_negative(self.projected_proceeds, "projected_proceeds")
