import pytest

from decimal import Decimal

from ticket_reviewer.domain.pricing import (
    acquisition_total,
    money,
    net_profit,
    projected_proceeds,
    roi,
)


def test_pair_profit_uses_decimal_cents():
    cost = acquisition_total(Decimal("175.00"), Decimal("20.00"), Decimal("12.50"))
    proceeds = projected_proceeds(Decimal("300.00"), Decimal("0.15"))

    assert cost == Decimal("207.50")
    assert proceeds == Decimal("255.00")
    assert net_profit(proceeds, cost) == Decimal("47.50")
    assert roi(Decimal("47.50"), cost) == Decimal("0.2289")


def test_zero_cost_roi_is_none():
    assert roi(Decimal("10.00"), Decimal("0")) is None


def test_money_rounds_half_up_to_cents():
    assert money(Decimal("1.005")) == Decimal("1.01")


@pytest.mark.parametrize(
    ("operation", "values"),
    [
        (money, (1.0,)),
        (acquisition_total, (1.0, Decimal("0"), Decimal("0"))),
        (acquisition_total, (Decimal("0"), 1.0, Decimal("0"))),
        (acquisition_total, (Decimal("0"), Decimal("0"), 1.0)),
        (projected_proceeds, (1.0, Decimal("0.15"))),
        (projected_proceeds, (Decimal("300"), 0.15)),
        (net_profit, (1.0, Decimal("200"))),
        (net_profit, (Decimal("200"), 1.0)),
        (roi, (1.0, Decimal("200"))),
        (roi, (Decimal("50"), 1.0)),
    ],
)
def test_pricing_boundaries_reject_binary_float_inputs(operation, values):
    with pytest.raises(ValueError, match="Decimal"):
        operation(*values)
