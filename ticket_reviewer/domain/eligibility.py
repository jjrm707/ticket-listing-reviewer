"""Hard analysis-only eligibility checks for a confirmed ticket pair."""

from decimal import Decimal

from .enums import ObservationKind
from .models import SourceObservation


def _is_finite_nonnegative_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value >= 0


def is_actionable_pair(
    observation: SourceObservation, acquisition: Decimal, budget_cap: Decimal
) -> tuple[bool, tuple[str, ...]]:
    """Determine whether a listing is eligible for analysis-only recommendation.

    Rejections are returned in this fixed order: listing confirmation, pair
    purchase confirmation, quantity, price, currency, acquisition validity,
    cap validity, then cap exceedance.  This function never initiates or
    exposes a purchase operation.
    """
    reasons: list[str] = []

    if observation.kind is not ObservationKind.LISTING:
        reasons.append("pair availability is not confirmed")
    if observation.can_buy_pair is not True:
        reasons.append("pair cannot be purchased")
    if type(observation.quantity_available) is not int or observation.quantity_available < 2:
        reasons.append("at least two tickets are not confirmed available")
    if observation.pair_price is None:
        reasons.append("pair price is unavailable")
    if observation.currency != "USD":
        reasons.append("currency is not USD")

    acquisition_valid = _is_finite_nonnegative_decimal(acquisition)
    budget_cap_valid = _is_finite_nonnegative_decimal(budget_cap)
    if not acquisition_valid:
        reasons.append("acquisition total is invalid")
    if not budget_cap_valid:
        reasons.append("budget cap is invalid")
    if acquisition_valid and budget_cap_valid and acquisition > budget_cap:
        reasons.append("acquisition total exceeds budget cap")

    return (not reasons, tuple(reasons))
