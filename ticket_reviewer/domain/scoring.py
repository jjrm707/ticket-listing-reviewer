"""Conservative, analysis-only ticket opportunity estimation."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from .comparables import select_comparables, weighted_quantile
from .eligibility import is_actionable_pair
from .enums import Confidence, ObservationKind, Source
from .matching import normalize_label
from .models import Comparable, ExitScenario, OpportunityEstimate, SourceObservation
from .pricing import acquisition_total, money, net_profit, projected_proceeds, roi


_ONE = Decimal("1")
_SPARSE_MULTIPLIER = Decimal("0.90")
_FIVE_PERCENT_HAIRCUT = Decimal("0.95")


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _valid_fee(value: object) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and Decimal("0") <= value <= Decimal("1")
    )


def _scenario_sort_key(scenario: ExitScenario) -> str:
    return scenario.marketplace.value


def _build_scenarios(
    comparables: Sequence[Comparable], fee_profiles: Mapping[Source, Decimal]
) -> tuple[ExitScenario, ...]:
    grouped: dict[Source, list[Comparable]] = defaultdict(list)
    for comparable in comparables:
        if comparable.source is Source.MANUAL:
            continue
        grouped[comparable.source].append(comparable)

    scenarios: list[ExitScenario] = []
    for marketplace in sorted(grouped, key=lambda source: source.value):
        fee_rate = fee_profiles.get(marketplace)
        if not _valid_fee(fee_rate):
            continue
        gross = weighted_quantile(grouped[marketplace])
        scenarios.append(
            ExitScenario(
                marketplace=marketplace,
                projected_resale_gross=gross,
                seller_fee_rate=fee_rate,
                projected_proceeds=projected_proceeds(gross, fee_rate),
                comparable_count=len(grouped[marketplace]),
            )
        )
    return tuple(sorted(scenarios, key=_scenario_sort_key))


def estimate_exit_scenarios(
    candidate: SourceObservation,
    observations: Sequence[SourceObservation],
    fee_profiles: Mapping[Source, Decimal],
    *,
    now: datetime | None = None,
) -> tuple[ExitScenario, ...]:
    """Build a conservative 40th-percentile scenario for each usable marketplace."""
    comparables = select_comparables(candidate, observations, now=now)
    return _build_scenarios(comparables, fee_profiles)


def _best_scenario(scenarios: Sequence[ExitScenario]) -> ExitScenario | None:
    if not scenarios:
        return None
    return min(
        scenarios,
        key=lambda scenario: (-scenario.projected_proceeds, scenario.marketplace.value),
    )


def _append_unique(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _snapshot_trend(comparables: Sequence[Comparable]) -> tuple[bool, bool, bool]:
    by_time: dict[datetime, list[Decimal]] = defaultdict(list)
    for comparable in comparables:
        if comparable.price_basis is not None:
            by_time[comparable.observed_at].append(comparable.price_basis)
    times = sorted(by_time)[-3:]
    if len(times) < 3:
        return False, False, False
    medians = [median(by_time[observed_at]) for observed_at in times]
    declining_over_ten = medians[-1] < medians[0] * Decimal("0.90")
    nonpositive = medians[-1] <= medians[0]
    return True, declining_over_ten, nonpositive


def _popularity_values(
    candidate: SourceObservation,
    observations: Sequence[SourceObservation],
    now: datetime,
) -> list[Decimal]:
    values: list[Decimal] = []
    for observation in observations:
        age = now - observation.observed_at
        if observation.event_external_id != candidate.event_external_id:
            continue
        if age > timedelta(hours=24) or age < -timedelta(minutes=5):
            continue
        if observation.popularity is not None:
            values.append(observation.popularity)
    return values


def _latest_listing_count(
    candidate: SourceObservation,
    observations: Sequence[SourceObservation],
    now: datetime,
) -> int | None:
    usable = [
        observation
        for observation in observations
        if observation.event_external_id == candidate.event_external_id
        and observation.listing_count is not None
        and now - observation.observed_at <= timedelta(hours=24)
        and observation.observed_at - now <= timedelta(minutes=5)
    ]
    if not usable:
        return None
    latest = max(observation.observed_at for observation in usable)
    return max(
        observation.listing_count
        for observation in usable
        if observation.observed_at == latest and observation.listing_count is not None
    )


def _adjust_scenarios(
    scenarios: Sequence[ExitScenario], multiplier: Decimal
) -> tuple[ExitScenario, ...]:
    adjusted: list[ExitScenario] = []
    for scenario in scenarios:
        gross = money(scenario.projected_resale_gross * multiplier)
        adjusted.append(
            ExitScenario(
                marketplace=scenario.marketplace,
                projected_resale_gross=gross,
                seller_fee_rate=scenario.seller_fee_rate,
                projected_proceeds=projected_proceeds(gross, scenario.seller_fee_rate),
                comparable_count=scenario.comparable_count,
            )
        )
    return tuple(sorted(adjusted, key=_scenario_sort_key))


def estimate_opportunity(
    candidate: SourceObservation,
    observations: Sequence[SourceObservation],
    fee_profiles: Mapping[Source, Decimal],
    now: datetime,
    budget_cap: Decimal,
    *,
    kickoff_at: datetime | None = None,
) -> OpportunityEstimate:
    """Estimate a conservative opportunity without initiating a transaction."""
    _require_aware(now, "now")
    if kickoff_at is not None:
        _require_aware(kickoff_at, "kickoff_at")

    pair_price = candidate.pair_price or Decimal("0")
    buyer_fees = candidate.buyer_fees or Decimal("0")
    estimated_tax = candidate.estimated_tax or Decimal("0")
    acquisition = acquisition_total(pair_price, buyer_fees, estimated_tax)
    hard_eligible, hard_reasons = is_actionable_pair(candidate, acquisition, budget_cap)

    reasons = list(hard_reasons)
    confidence_drops = 0
    if candidate.buyer_fees is None:
        _append_unique(reasons, "buyer fees unavailable")
    if candidate.estimated_tax is None:
        _append_unique(reasons, "estimated tax unavailable")

    comparables = select_comparables(candidate, observations, now=now)
    listing_comparables = tuple(
        comparable
        for comparable in comparables
        if comparable.kind is ObservationKind.LISTING
    )
    sparse = len(listing_comparables) < 3
    if sparse:
        confidence_drops += 1
        _append_unique(reasons, "fewer than 3 seat-level comparables")

    same_section = bool(
        candidate.section
        and any(
            comparable.section
            and normalize_label(candidate.section) == normalize_label(comparable.section)
            for comparable in comparables
        )
    )
    row_known = bool(candidate.row and normalize_label(candidate.row))
    if not row_known or not same_section:
        confidence_drops += 1
        _append_unique(reasons, "seat quality unverified")

    if candidate.buyer_fees is None or candidate.estimated_tax is None:
        confidence_drops += 1

    if comparables and max(item.observed_at for item in comparables) < now - timedelta(hours=2):
        confidence_drops += 1
        _append_unique(reasons, "newest comparable is older than 2 hours")

    if candidate.source is Source.MANUAL:
        confidence_drops += 1
        _append_unique(reasons, "candidate source is manually corrected OCR")

    popularity = _popularity_values(candidate, observations, now)
    if not popularity:
        _append_unique(reasons, "opponent demand unverified")
    elif median(popularity) < Decimal("0.35"):
        _append_unique(reasons, "low opponent demand")

    if kickoff_at is None:
        _append_unique(reasons, "kickoff proximity unverified")

    multiplier = _ONE
    if sparse:
        multiplier *= _SPARSE_MULTIPLIER
        _append_unique(reasons, "sparse comparable haircut applied")

    trend_verified, declining_over_ten, nonpositive = _snapshot_trend(comparables)
    if not trend_verified:
        _append_unique(reasons, "price trend unverified")
    elif declining_over_ten:
        multiplier *= _FIVE_PERCENT_HAIRCUT
        _append_unique(reasons, "declining comparable prices")

    if kickoff_at is not None:
        time_to_kickoff = kickoff_at - now
        if timedelta(0) <= time_to_kickoff <= timedelta(days=7) and trend_verified and nonpositive:
            multiplier *= _FIVE_PERCENT_HAIRCUT
            _append_unique(reasons, "nonpositive trend within 7 days of kickoff")
        latest_count = _latest_listing_count(candidate, observations, now)
        if (
            timedelta(0) <= time_to_kickoff <= timedelta(days=3)
            and latest_count is not None
            and latest_count > 500
        ):
            multiplier *= _FIVE_PERCENT_HAIRCUT
            _append_unique(reasons, "high inventory within 3 days of kickoff")

    base_scenarios = _build_scenarios(comparables, fee_profiles)
    observed_sources = sorted({item.source for item in comparables}, key=lambda source: source.value)
    for source in observed_sources:
        if not _valid_fee(fee_profiles.get(source)):
            _append_unique(reasons, f"seller fee unavailable for {source.value}")
    scenarios = _adjust_scenarios(base_scenarios, multiplier)
    best = _best_scenario(scenarios)
    if best is None:
        _append_unique(reasons, "no valid exit scenario")

    confidence = (Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW)[
        min(confidence_drops, 2)
    ]
    if best is None:
        projected_gross = None
        seller_fee = None
        proceeds = None
        profit = None
        return_on_investment = None
        exit_source = None
    else:
        projected_gross = best.projected_resale_gross
        seller_fee = best.seller_fee_rate
        proceeds = best.projected_proceeds
        profit = net_profit(proceeds, acquisition)
        return_on_investment = roi(profit, acquisition)
        exit_source = best.marketplace

    return OpportunityEstimate(
        acquisition_total=acquisition,
        exit_source=exit_source,
        projected_resale_gross=projected_gross,
        seller_fee_rate=seller_fee,
        projected_proceeds=proceeds,
        estimated_net_profit=profit,
        roi=return_on_investment,
        confidence=confidence,
        risk_reasons=tuple(reasons),
        actionable=hard_eligible and best is not None,
        scenarios=scenarios,
    )
