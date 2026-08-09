"""Conservative selection and aggregation of same-event asking-price evidence."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from .enums import ObservationKind
from .matching import normalize_label
from .models import Comparable, SourceObservation
from .pricing import money


_MAX_AGE = timedelta(hours=24)
_CLOCK_SKEW = timedelta(minutes=5)
_QUALITY = {
    ObservationKind.LISTING: Decimal("1.00"),
    ObservationKind.EVENT_AGGREGATE: Decimal("0.70"),
    ObservationKind.EVENT_FLOOR: Decimal("0.50"),
}


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _parking_like(value: str | None) -> bool:
    if not value:
        return False
    label = normalize_label(value)
    tokens = set(label.split())
    return "parking" in tokens or "parkade" in tokens or (
        "lot" in tokens and bool(tokens & {"pass", "parking"})
    ) or label.startswith("lot ")


def _known_label(value: str | None) -> str | None:
    if not value:
        return None
    normalized = normalize_label(value)
    return normalized or None


def comparable_weight(candidate: SourceObservation, other: SourceObservation) -> Decimal:
    """Return the exact, intentionally conservative seat-match weight."""
    candidate_section = _known_label(candidate.section)
    other_section = _known_label(other.section)
    if (
        candidate_section is not None
        and other_section is not None
        and candidate_section == other_section
    ):
        if (
            candidate.row
            and other.row
            and normalize_label(candidate.row) == normalize_label(other.row)
        ):
            return Decimal("1.00")
        return Decimal("0.80")
    if other.kind is ObservationKind.EVENT_AGGREGATE:
        return Decimal("0.35")
    if other.kind is ObservationKind.EVENT_FLOOR:
        return Decimal("0.20")
    return Decimal("0.50")


def _same_snapshot(candidate: SourceObservation, other: SourceObservation) -> bool:
    return (
        candidate.source is other.source
        and candidate.listing_id == other.listing_id
        and candidate.observed_at == other.observed_at
    )


def select_comparables(
    candidate: SourceObservation,
    observations: Sequence[SourceObservation],
    *,
    now: datetime | None = None,
) -> tuple[Comparable, ...]:
    """Select fresh, same-event USD asking-price signals for ``candidate``."""
    supplied = tuple(observations)
    if now is None:
        reference = max(
            (observation.observed_at for observation in supplied),
            default=candidate.observed_at,
        )
    else:
        _require_aware(now, "now")
        reference = now

    selected: list[Comparable] = []
    for observation in supplied:
        if observation.event_external_id != candidate.event_external_id:
            continue
        if observation.currency != "USD":
            continue
        price = observation.pair_price
        if price is None or not price.is_finite() or price <= 0:
            continue
        basis = money(price)
        if basis <= 0:
            continue
        age = reference - observation.observed_at
        if age > _MAX_AGE or age < -_CLOCK_SKEW:
            continue
        if _same_snapshot(candidate, observation):
            continue
        if _parking_like(observation.section) or _parking_like(observation.row):
            continue

        selected.append(
            Comparable(
                source=observation.source,
                event_external_id=observation.event_external_id,
                kind=observation.kind,
                currency="USD",
                price_basis=basis,
                proceeds_basis=basis,
                section=observation.section,
                observed_at=observation.observed_at,
                relevance=comparable_weight(candidate, observation),
                quality=_QUALITY[observation.kind],
                observation_id=observation.observation_id,
            )
        )

    return tuple(
        sorted(
            selected,
            key=lambda item: (
                item.observed_at,
                item.source.value,
                item.kind.value,
                item.price_basis,
                item.section or "",
            ),
        )
    )


def weighted_quantile(
    comparables: Sequence[Comparable], quantile: Decimal = Decimal("0.40")
) -> Decimal:
    """Return the first price whose positive evidence weight reaches ``quantile``."""
    if not isinstance(quantile, Decimal) or not quantile.is_finite():
        raise ValueError("quantile must be a finite Decimal")
    if not Decimal("0") <= quantile <= Decimal("1"):
        raise ValueError("quantile must be between 0 and 1")

    weighted: list[tuple[Decimal, Decimal, Comparable]] = []
    for item in comparables:
        price = item.price_basis
        relevance = item.relevance
        quality = item.quality
        if (
            not isinstance(price, Decimal)
            or not isinstance(relevance, Decimal)
            or not isinstance(quality, Decimal)
            or not price.is_finite()
            or not relevance.is_finite()
            or not quality.is_finite()
            or price <= 0
            or relevance < 0
            or quality < 0
        ):
            raise ValueError("comparables must contain finite positive prices and weights")
        weight = relevance * quality
        if weight > 0:
            weighted.append((price, weight, item))

    if not weighted:
        raise ValueError("at least one positive-weight comparable is required")

    weighted.sort(
        key=lambda value: (
            value[0],
            value[2].source.value,
            value[2].observed_at,
            value[2].kind.value,
        )
    )
    total_weight = sum((weight for _, weight, _ in weighted), Decimal("0"))
    target = total_weight * quantile
    cumulative = Decimal("0")
    for price, weight, _ in weighted:
        cumulative += weight
        if cumulative >= target:
            return money(price)
    return money(weighted[-1][0])
